"""Article-level reads. Wraps the search CLI logic into a pure
function the web app can call.

Web app uses this for: article detail pages, the search endpoint,
and "browse the pool by JTBD / publication" filters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from aarva.db import Database
from aarva.exceptions import NotFoundError


@dataclass(frozen=True)
class ArticleSummary:
    id: int
    title: str
    byline: Optional[str]
    publication: Optional[str]
    canonical_url: Optional[str]
    published_date: Optional[str]
    word_count: int
    lens: Optional[str]
    jtbd_primary: Optional[str]
    jtbd_secondary: Optional[str]
    ranking_score: float
    status: str


def _row_to_summary(row: Any) -> ArticleSummary:
    pd = row["published_date"]
    return ArticleSummary(
        id=int(row["id"]),
        title=row["title"] or "",
        byline=row["byline"],
        publication=row["publication"],
        canonical_url=row["canonical_url"],
        published_date=str(pd)[:10] if pd else None,
        word_count=int(row["word_count"] or 0),
        lens=row["lens"],
        jtbd_primary=row["jtbd_primary"],
        jtbd_secondary=row["jtbd_secondary"],
        ranking_score=float(row["ranking_score"] or 0.0),
        status=row["status"],
    )


def get_article(db: Database, article_id: int) -> ArticleSummary:
    with db.connect() as conn:
        row = conn.execute("""
            SELECT a.id, a.title, a.byline, a.canonical_url,
                   a.published_date, a.word_count, a.status,
                   p.name AS publication,
                   s.lens, s.jtbd_primary, s.jtbd_secondary,
                   COALESCE(s.ranking_score, 0.0) AS ranking_score
              FROM articles a
              JOIN publications p ON p.id = a.publication_id
              LEFT JOIN article_scores s ON s.article_id = a.id
             WHERE a.id = ?
        """, (article_id,)).fetchone()
    if not row:
        raise NotFoundError(f"Article {article_id} not found.")
    return _row_to_summary(row)


def search_articles(
    db: Database,
    *,
    query: Optional[str] = None,
    semantic: bool = False,
    pub: Optional[str] = None,
    lens: Optional[str] = None,
    jtbd: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    full_text: bool = False,
    limit: int = 20,
) -> list[ArticleSummary]:
    """Search the article pool. Wraps aarva.search's filter logic
    behind a pure function. Semantic mode is supported (caller must
    pass an embedded query in semantic mode — for now this function
    only does the lexical/filter path; semantic ranking belongs in
    the search module which already has it).

    For the web API, the typical call shape is:
      search_articles(db, query="bukele", pub="atlantic", limit=10)
    """
    where: list[str] = []
    params: list[Any] = []

    if pub:
        where.append("LOWER(p.name) LIKE ?")
        params.append(f"%{pub.lower()}%")
    if lens:
        where.append("s.lens = ?")
        params.append(lens)
    if jtbd:
        where.append("(s.jtbd_primary = ? OR s.jtbd_secondary = ?)")
        params += [jtbd, jtbd]
    if status:
        where.append("a.status = ?")
        params.append(status)
    if since:
        where.append("COALESCE(a.published_date, a.ingested_date) >= ?")
        params.append(since)
    if query:
        like = f"%{query.lower()}%"
        if full_text:
            where.append(
                "(LOWER(a.title) LIKE ? OR LOWER(a.excerpt) LIKE ? "
                " OR LOWER(a.full_text) LIKE ?)"
            )
            params += [like, like, like]
        else:
            where.append("(LOWER(a.title) LIKE ? OR LOWER(a.excerpt) LIKE ?)")
            params += [like, like]

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT a.id, a.title, a.byline, a.canonical_url,
               a.published_date, a.word_count, a.status,
               p.name AS publication,
               s.lens, s.jtbd_primary, s.jtbd_secondary,
               COALESCE(s.ranking_score, 0.0) AS ranking_score
          FROM articles a
          JOIN publications p ON p.id = a.publication_id
          LEFT JOIN article_scores s ON s.article_id = a.id
        {where_sql}
         ORDER BY ranking_score DESC, a.id DESC
         LIMIT ?
    """
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_summary(r) for r in rows]
