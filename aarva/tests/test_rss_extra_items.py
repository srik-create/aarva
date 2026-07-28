"""Tests for the ad-hoc RSS extra-items feature.

See docs/session_plan_rss_extra_items.md. Covers: schema migration,
the query helper, the render helper (including the _audio_full_url
absolute-URL bug fix this feature exposed), the CLI's manual-mode
logic, and the admin endpoint's auth/validation/composition — all
against disposable on-disk SQLite DBs, no mocking of the DB layer.
"""
from __future__ import annotations

import asyncio
import os
import types
from datetime import date

import pytest

from aarva.db import Database
from aarva.listener_db import ListenerDatabase
from aarva.output.rss_feed import _audio_full_url, _extra_item_xml
from aarva.services.queries import load_rss_extra_items


# ─── Schema ────────────────────────────────────────────────────────────────

class TestSchema:
    def test_table_created_fresh(self, tmp_path):
        db = Database(str(tmp_path / "fresh.db"))
        with db.connect() as conn:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(rss_extra_items)"
            ).fetchall()}
        assert "guid" in cols
        assert "audio_url" in cols

    def test_init_schema_is_idempotent(self, tmp_path):
        path = str(tmp_path / "twice.db")
        Database(path)
        Database(path)  # second init on the same file must not raise


# ─── Query helper ──────────────────────────────────────────────────────────

def _insert_extra(db: Database, **overrides) -> dict:
    row = {
        "guid": "aarva-extra-test-2026-07-28",
        "episode_date": "2026-07-28",
        "title": "Test Episode",
        "description_html": "<p>hi</p>",
        "audio_url": "https://audio.aarva.app/x.mp3",
        "byte_length": 12345,
        "duration_seconds": 600,
        "author": "Aarva",
        "subtitle": "A subtitle",
        "itunes_episode_type": "full",
    }
    row.update(overrides)
    with db.connect() as conn:
        conn.execute("""
            INSERT INTO rss_extra_items
                (guid, episode_date, title, description_html, audio_url,
                 byte_length, duration_seconds, author, subtitle,
                 itunes_episode_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, tuple(row[k] for k in (
            "guid", "episode_date", "title", "description_html", "audio_url",
            "byte_length", "duration_seconds", "author", "subtitle",
            "itunes_episode_type",
        )))
        conn.commit()
    return row


class TestQueryHelper:
    def test_empty(self, tmp_path):
        db = Database(str(tmp_path / "empty.db"))
        assert load_rss_extra_items(db) == []

    def test_orders_by_date_desc_then_added_at_desc(self, tmp_path):
        db = Database(str(tmp_path / "ordered.db"))
        _insert_extra(db, guid="g-old", episode_date="2026-07-20")
        _insert_extra(db, guid="g-new", episode_date="2026-07-28")
        rows = load_rss_extra_items(db)
        assert [r["guid"] for r in rows] == ["g-new", "g-old"]


# ─── Render helpers ─────────────────────────────────────────────────────────

class TestAudioFullUrl:
    """The bug this feature exposed: the spec assumed _audio_full_url
    was already a no-op for absolute URLs (needed for rss_add's fully-
    manual mode, where the operator supplies a complete external URL).
    It wasn't — it always prepended the base, corrupting the URL."""

    def test_relative_path_gets_base_prepended(self):
        assert _audio_full_url("output/audio/x.mp3", "https://audio.aarva.app") == \
            "https://audio.aarva.app/output/audio/x.mp3"

    def test_absolute_https_url_passed_through_unchanged(self):
        url = "https://audio.aarva.app/some/other/path.mp3"
        assert _audio_full_url(url, "https://totally-different-base.example") == url

    def test_absolute_http_url_passed_through_unchanged(self):
        url = "http://example.com/file.mp3"
        assert _audio_full_url(url, "https://audio.aarva.app") == url


class TestExtraItemXml:
    def test_renders_expected_fields(self):
        row = {
            "guid": "aarva-extra-abc-2026-07-28",
            "episode_date": "2026-07-28",
            "title": "Crosscut: A Topic",
            "description_html": "<p>desc</p>",
            "audio_url": "https://audio.aarva.app/x.mp3",
            "byte_length": 4242,
            "duration_seconds": 125,
            "author": "Aarva",
            "subtitle": "Crosscut · A Topic",
            "itunes_episode_type": "full",
        }
        xml = _extra_item_xml(row, "https://public.example", None)
        assert "<title>Crosscut: A Topic</title>" in xml
        assert 'length="4242"' in xml
        assert "<itunes:duration>02:05</itunes:duration>" in xml
        assert 'guid isPermaLink="false">aarva-extra-abc-2026-07-28<' in xml
        assert "<itunes:episodeType>full</itunes:episodeType>" in xml

    def test_missing_optional_fields_dont_crash(self):
        row = {
            "guid": "g",
            "episode_date": "2026-07-28",
            "title": "T",
            "description_html": None,
            "audio_url": "https://x.example/a.mp3",
            "byte_length": None,
            "duration_seconds": None,
            "author": None,
            "subtitle": None,
            "itunes_episode_type": None,
        }
        xml = _extra_item_xml(row, "https://public.example", None)
        assert 'length="0"' in xml
        assert "<itunes:author>Aarva</itunes:author>" in xml
        assert "<itunes:episodeType>full</itunes:episodeType>" in xml


class TestGenerateFeedIncludesExtras:
    def test_extra_item_appears_in_feed_xml(self, tmp_path, monkeypatch):
        import dataclasses
        from aarva.config import load_pipeline_config
        from aarva.output.rss_feed import generate_feed

        db_path = tmp_path / "main.db"
        monkeypatch.setenv("AARVA_DB_PATH", str(db_path))
        db = Database(str(db_path))
        _insert_extra(db, guid="aarva-extra-feed-check-2026-07-28")

        cfg = load_pipeline_config()
        cfg = dataclasses.replace(cfg, rss_feed_path=tmp_path / "feed.xml")
        stats = generate_feed(cfg, db)

        xml = stats.feed_path.read_text()
        assert "aarva-extra-feed-check-2026-07-28" in xml

    def test_website_has_no_references_to_rss_extra_items(self):
        # Non-goal per the spec: zero website impact. Grep-verifiable.
        import subprocess
        server_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "server",
        )
        result = subprocess.run(
            ["grep", "-rl", "--include=*.py", "rss_extra_items", server_dir],
            capture_output=True, text=True,
        )
        hits = [l for l in result.stdout.splitlines() if l]
        # admin.py's new endpoint is expected — it's an operator API,
        # not a listener-facing website surface.
        assert hits == [os.path.join(server_dir, "routes", "admin.py")]


# ─── CLI (manual mode + management, no network) ────────────────────────────

class TestCliManualMode:
    def test_crosscut_kind_prefixes_title(self, tmp_path):
        from aarva.rss_add import CROSSCUT_PREFIX
        assert CROSSCUT_PREFIX == "Crosscut: "

    def test_slugify(self):
        from aarva.rss_add import _slugify
        assert _slugify("Some Topic!") == "some-topic"
        assert _slugify("") == "episode"

    def test_cmd_manual_writes_row_and_prefixes_crosscut(self, tmp_path):
        from aarva.rss_add import cmd_manual
        db = Database(str(tmp_path / "cli.db"))
        args = types.SimpleNamespace(
            title="Some Topic", kind="crosscut", episode_date="2026-07-28",
            byte_length=999, audio_url="https://x.example/a.mp3",
            description=None, duration=100, guid=None, author=None,
            subtitle=None,
        )
        cmd_manual(args, db)
        rows = load_rss_extra_items(db)
        assert len(rows) == 1
        assert rows[0]["title"] == "Crosscut: Some Topic"
        assert rows[0]["guid"] == "aarva-extra-crosscut-some-topic-2026-07-28"

    def test_cmd_manual_never_double_prefixes(self, tmp_path):
        from aarva.rss_add import cmd_manual
        db = Database(str(tmp_path / "cli2.db"))
        args = types.SimpleNamespace(
            title="Crosscut: Already Prefixed", kind="crosscut",
            episode_date="2026-07-28", byte_length=1, audio_url="https://x.example/a.mp3",
            description=None, duration=None, guid=None, author=None,
            subtitle=None,
        )
        cmd_manual(args, db)
        rows = load_rss_extra_items(db)
        assert rows[0]["title"] == "Crosscut: Already Prefixed"

    def test_idempotent_by_guid(self, tmp_path):
        from aarva.rss_add import cmd_manual
        db = Database(str(tmp_path / "cli3.db"))
        args = types.SimpleNamespace(
            title="Ep", kind="episode", episode_date="2026-07-28",
            byte_length=1, audio_url="https://x.example/a.mp3",
            description=None, duration=None, guid="fixed-guid", author=None,
            subtitle=None,
        )
        cmd_manual(args, db)
        cmd_manual(args, db)
        rows = load_rss_extra_items(db)
        assert len(rows) == 1

    def test_remove_deletes_row(self, tmp_path):
        from aarva.rss_add import cmd_manual, cmd_remove
        db = Database(str(tmp_path / "cli4.db"))
        args = types.SimpleNamespace(
            title="Ep", kind="episode", episode_date="2026-07-28",
            byte_length=1, audio_url="https://x.example/a.mp3",
            description=None, duration=None, guid="to-remove", author=None,
            subtitle=None,
        )
        cmd_manual(args, db)
        assert len(load_rss_extra_items(db)) == 1
        cmd_remove(types.SimpleNamespace(remove="to-remove"), db)
        assert len(load_rss_extra_items(db)) == 0

    def test_remove_nonexistent_guid_is_a_noop(self, tmp_path, capsys):
        from aarva.rss_add import cmd_remove
        db = Database(str(tmp_path / "cli5.db"))
        cmd_remove(types.SimpleNamespace(remove="never-existed"), db)
        assert "nothing to remove" in capsys.readouterr().out


# ─── Admin endpoint (direct call, no HTTP server) ───────────────────────────

class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeRequest:
    def __init__(self, query_params, headers, app_state):
        self.query_params = query_params
        self.headers = _FakeHeaders({k.lower(): v for k, v in headers.items()})
        self.app = types.SimpleNamespace(state=app_state)


@pytest.fixture
def episode_metadata_env(tmp_path, monkeypatch):
    """A main_db + listener_db pair with one real listener-created
    crosscut (edition_id=1000011, matching the spec's own example) and
    one crosscut with no audio yet (1000012, for the 400 path)."""
    from aarva.config import load_pipeline_config

    monkeypatch.setenv("AARVA_RENDER_SYNC_TOKEN", "test-token")
    db = Database(str(tmp_path / "main.db"))
    listener_db = ListenerDatabase(str(tmp_path / "listener.db"))

    with listener_db.connect() as conn:
        conn.execute("""
            INSERT INTO editions (id, edition_date, edition_type, topic_label,
                                   intro_text, outro_text, user_id)
            VALUES (1000011, '2026-07-28', 'crosscut', 'us war in iran',
                    'Intro.', 'Outro.', 42)
        """)
        conn.execute("""
            INSERT INTO edition_pieces (edition_id, article_id, slot, position,
                                         audio_url, duration_seconds,
                                         article_title, article_publication)
            VALUES (1000011, 1, 'crosscut_a', 0,
                    'output/audio/crosscut/1000011.mp3', 1620,
                    'Title A', 'Pub A')
        """)
        # bridge_between (rendered between intro and outro) comes from
        # the SECOND piece's bridge_text — see load_listener_episodes.
        conn.execute("""
            INSERT INTO edition_pieces (edition_id, article_id, slot, position,
                                         bridge_text, article_title, article_publication)
            VALUES (1000011, 2, 'crosscut_b', 1, 'Bridge.', 'Title B', 'Pub B')
        """)
        conn.execute("""
            INSERT INTO editions (id, edition_date, edition_type, topic_label, user_id)
            VALUES (1000012, '2026-07-28', 'crosscut', 'no audio yet', 42)
        """)
        conn.execute("""
            INSERT INTO edition_pieces (edition_id, article_id, slot, position,
                                         article_title, article_publication)
            VALUES (1000012, 3, 'crosscut_a', 0, 'Title C', 'Pub C')
        """)
        conn.execute("""
            INSERT INTO edition_pieces (edition_id, article_id, slot, position,
                                         article_title, article_publication)
            VALUES (1000012, 4, 'crosscut_b', 1, 'Title D', 'Pub D')
        """)
        conn.commit()

    app_state = types.SimpleNamespace(
        db=db, listener_db=listener_db, pipeline_cfg=load_pipeline_config(),
    )
    return app_state


def _call(coro):
    return asyncio.run(coro)


class TestAdminEpisodeMetadata:
    def test_missing_token_is_401(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest({"edition_id": "1000011"}, {}, episode_metadata_env)
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 401

    def test_wrong_token_is_401(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest(
            {"edition_id": "1000011"},
            {"Authorization": "Bearer wrong"},
            episode_metadata_env,
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 401

    def test_missing_edition_id_is_400(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest({}, {"Authorization": "Bearer test-token"}, episode_metadata_env)
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 400

    def test_non_integer_edition_id_is_400(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest(
            {"edition_id": "abc"}, {"Authorization": "Bearer test-token"},
            episode_metadata_env,
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 400

    def test_nonexistent_edition_is_404(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest(
            {"edition_id": "999999"}, {"Authorization": "Bearer test-token"},
            episode_metadata_env,
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 404

    def test_no_audio_yet_is_400(self, episode_metadata_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest(
            {"edition_id": "1000012"}, {"Authorization": "Bearer test-token"},
            episode_metadata_env,
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_episode_metadata(req))
        assert exc.value.status_code == 400

    def test_real_crosscut_composes_expected_payload(self, episode_metadata_env):
        from aarva.server.routes.admin import admin_episode_metadata

        req = _FakeRequest(
            {"edition_id": "1000011"}, {"Authorization": "Bearer test-token"},
            episode_metadata_env,
        )
        resp = _call(admin_episode_metadata(req))
        import json
        body = json.loads(resp.body)
        assert body["kind"] == "crosscut"
        assert body["guid"] == "aarva-crosscut-1000011"
        assert body["title"] == "Crosscut: us war in iran"
        assert "Intro." in body["description_html"]
        assert "<em>Bridge.</em>" in body["description_html"]
        assert "Outro." in body["description_html"]
        assert body["duration_seconds"] == 1620
        assert body["subtitle"] == "Crosscut · us war in iran"
        assert body["itunes_episode_type"] == "full"
