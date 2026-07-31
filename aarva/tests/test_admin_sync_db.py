"""Tests for /admin/sync-db's event-loop-blocking fix.

Verified 2026-07-31: syncing the real ~148MB main DB triggered a
Render instance restart mid-request (the endpoint's synchronous R2
fetch/decompress/write/validate work starved /health past Render's
5s health-check timeout). Fixed by extracting that work into
_fetch_decompress_validate_and_replace and running it via
run_in_threadpool (same pattern already used in
aarva/server/routes/create.py for propose_candidates/find_near_miss).

These tests cover the full auth/validation/error-code surface through
the real route function (called directly, no live server needed,
matching the existing aarva/tests/test_rss_extra_items.py pattern) —
confirming the extraction didn't change any documented status code —
plus the success path end-to-end against disposable on-disk DBs.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import types

import pytest

from aarva.db import Database
from aarva.listener_db import ListenerDatabase


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeRequest:
    """Minimal stand-in for fastapi.Request. `_json` is either a dict
    (returned by the async .json()) or an exception instance/class to
    raise from .json(), so callers can simulate a malformed body."""

    def __init__(self, headers, app_state, json_body=None, json_raises=None):
        self.headers = _FakeHeaders({k.lower(): v for k, v in headers.items()})
        self.app = types.SimpleNamespace(state=app_state)
        self._json_body = json_body
        self._json_raises = json_raises

    async def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        return self._json_body


class _FakeR2Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class _FakeR2Client:
    """Stands in for the boto3 client _build_r2_client would return.
    `content_length_override` lets a test claim a bigger size than the
    real payload, to exercise the early-exit 413 check without
    actually needing a huge in-memory payload."""

    def __init__(self, compressed: bytes, raise_error: Exception | None = None,
                 content_length_override: int | None = None):
        self._compressed = compressed
        self._raise_error = raise_error
        self._content_length_override = content_length_override

    def get_object(self, Bucket, Key):
        if self._raise_error is not None:
            raise self._raise_error
        length = (
            self._content_length_override
            if self._content_length_override is not None
            else len(self._compressed)
        )
        return {"ContentLength": length, "Body": _FakeR2Body(self._compressed)}


def _call(coro):
    return asyncio.run(coro)


def _make_db_bytes(article_count: int, with_articles_table: bool = True,
                    padding_rows: int = 0) -> bytes:
    """Build a real, valid sqlite3 DB file's raw bytes with N rows in
    an `articles` table (or no such table at all, for the schema-
    mismatch case). `padding_rows` adds extra filler rows so the
    gzip-compressed result clears the endpoint's 1024-byte floor even
    when article_count is 0 — a handful of real rows compress to well
    under that on their own."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "src.db"
        conn = sqlite3.connect(str(p))
        if with_articles_table:
            conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)")
            for i in range(article_count):
                conn.execute("INSERT INTO articles (title) VALUES (?)", (f"Article {i}",))
            if padding_rows:
                conn.execute("CREATE TABLE _padding (id INTEGER PRIMARY KEY, filler TEXT)")
                for i in range(padding_rows):
                    conn.execute(
                        "INSERT INTO _padding (filler) VALUES (?)",
                        (f"padding row {i} with extra text to bulk up the file size",),
                    )
        else:
            conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE _padding (id INTEGER PRIMARY KEY, filler TEXT)")
            for i in range(padding_rows):
                conn.execute(
                    "INSERT INTO _padding (filler) VALUES (?)",
                    (f"padding row {i} with extra text to bulk up the file size",),
                )
        conn.commit()
        conn.close()
        return p.read_bytes()


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """AARVA_DB_PATH points at a hand-built raw sqlite file (what the
    sync endpoint atomically replaces via os.replace) — deliberately
    NOT the same file as app_state.db, which is a normal, fully-
    schema'd Database instance used only for the _find_lost_episodes
    pre-check. Mirrors production: the endpoint manipulates the DB
    file directly by path, independent of the already-open db/
    listener_db connections used for the lost-episode diagnostic."""
    monkeypatch.setenv("AARVA_RENDER_SYNC_TOKEN", "test-token")
    db_path = tmp_path / "live_aarva.db"
    monkeypatch.setenv("AARVA_DB_PATH", str(db_path))

    # A pre-existing "live" DB the sync should replace.
    live_bytes = _make_db_bytes(article_count=1)
    db_path.write_bytes(live_bytes)

    db = Database(str(tmp_path / "main.db"))
    listener_db = ListenerDatabase(str(tmp_path / "listener.db"))
    pipeline_cfg = types.SimpleNamespace(raw={"tts": {"r2": {}}})  # unused — R2 client is monkeypatched
    app_state = types.SimpleNamespace(db=db, listener_db=listener_db, pipeline_cfg=pipeline_cfg)
    return types.SimpleNamespace(app_state=app_state, db_path=db_path)


def _patch_r2_client(monkeypatch, client):
    from aarva.server.routes import admin
    monkeypatch.setattr(admin, "_build_r2_client", lambda pipeline_cfg: (client, "fake-bucket"))


class TestAdminSyncDbAuth:
    def test_no_token_configured_is_503(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        monkeypatch.delenv("AARVA_RENDER_SYNC_TOKEN", raising=False)
        req = _FakeRequest({}, sync_env.app_state)
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 503

    def test_missing_token_is_401(self, sync_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        req = _FakeRequest({}, sync_env.app_state)
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 401

    def test_wrong_token_is_401(self, sync_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        req = _FakeRequest({"Authorization": "Bearer wrong"}, sync_env.app_state)
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 401


class TestAdminSyncDbPayloadValidation:
    def test_invalid_json_body_is_400(self, sync_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_raises=json.JSONDecodeError("bad", "doc", 0),
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 400

    def test_missing_r2_key_is_400(self, sync_env):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state, json_body={},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 400


class TestAdminSyncDbR2AndValidation:
    def test_r2_fetch_failure_is_502(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        _patch_r2_client(monkeypatch, _FakeR2Client(b"", raise_error=RuntimeError("boom")))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 502

    def test_oversized_object_is_413(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db, _MAX_PAYLOAD_BYTES

        _patch_r2_client(
            monkeypatch,
            _FakeR2Client(b"x" * 2000, content_length_override=_MAX_PAYLOAD_BYTES + 1),
        )
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 413

    def test_too_small_payload_is_403(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        _patch_r2_client(monkeypatch, _FakeR2Client(b"tiny"))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 403

    def test_gunzip_failure_is_403(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        _patch_r2_client(monkeypatch, _FakeR2Client(b"not gzip data" * 100))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 403
        assert "gunzip failed" in exc.value.detail

    def test_non_sqlite_payload_is_403(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        # os.urandom padding — highly repetitive text compresses well
        # below the endpoint's 1024-byte floor on its own.
        import os
        compressed = gzip.compress(b"this is not a sqlite database" + os.urandom(2000))
        _patch_r2_client(monkeypatch, _FakeR2Client(compressed))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 403
        assert "not a SQLite DB" in exc.value.detail

    def test_missing_articles_table_is_403(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        compressed = gzip.compress(_make_db_bytes(0, with_articles_table=False, padding_rows=100))
        _patch_r2_client(monkeypatch, _FakeR2Client(compressed))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 403
        assert "missing expected schema" in exc.value.detail

    def test_zero_articles_is_403(self, sync_env, monkeypatch):
        from fastapi import HTTPException
        from aarva.server.routes.admin import admin_sync_db

        compressed = gzip.compress(_make_db_bytes(0, padding_rows=100))
        _patch_r2_client(monkeypatch, _FakeR2Client(compressed))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        with pytest.raises(HTTPException) as exc:
            _call(admin_sync_db(req))
        assert exc.value.status_code == 403
        assert "0 articles" in exc.value.detail

        # Refusing to swap must leave the live DB untouched.
        with sqlite3.connect(str(sync_env.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


class TestAdminSyncDbSuccess:
    def test_successful_sync_replaces_db_and_reports_counts(self, sync_env, monkeypatch):
        from aarva.server.routes.admin import admin_sync_db

        compressed = gzip.compress(_make_db_bytes(7, padding_rows=100))
        _patch_r2_client(monkeypatch, _FakeR2Client(compressed))
        req = _FakeRequest(
            {"Authorization": "Bearer test-token"}, sync_env.app_state,
            json_body={"r2_key": "x.gz"},
        )
        resp = _call(admin_sync_db(req))
        body = json.loads(resp.body)
        assert body["status"] == "ok"
        assert body["articles"] == 7
        assert body["bytes"] == len(compressed)
        assert body["lost_episodes_found"] == []

        # The live DB at AARVA_DB_PATH was actually atomically replaced.
        with sqlite3.connect(str(sync_env.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 7

        # No leftover staging file next to the live DB.
        leftovers = list(sync_env.db_path.parent.glob("aarva-db-staging-*"))
        assert leftovers == []
