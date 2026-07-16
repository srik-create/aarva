"""Share-button click logging.

  POST /api/v1/share-event   Body: {"content_type": "article"|"crosscut",
                              "content_id": <int>}. Fired by
                              aarva/server/static/share.js after a
                              successful Web Share or copy-link
                              action. Fire-and-forget from the
                              client's perspective — always returns
                              200-ish quickly; a bad payload gets a
                              quiet 400, never a 500.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from aarva.server.app import app
from aarva.services.share_analytics import VALID_CONTENT_TYPES, log_share_click


@app.post("/api/v1/share-event")
async def share_event(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "ignored"}, status_code=400)

    content_type = str((payload or {}).get("content_type") or "")
    if content_type not in VALID_CONTENT_TYPES:
        return JSONResponse({"status": "ignored"}, status_code=400)

    try:
        content_id = int((payload or {}).get("content_id"))
    except (TypeError, ValueError):
        return JSONResponse({"status": "ignored"}, status_code=400)

    log_share_click(request.app.state.listener_db, content_type, content_id)
    return JSONResponse({"status": "ok"})
