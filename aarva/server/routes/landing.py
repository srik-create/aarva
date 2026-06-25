"""Landing page at `/` — explains Aarva to first-time visitors.

For listeners who already know what Aarva is, /today is the natural
entry point. This page is for the first arrival: tagline, what it is,
why it exists, how to start. Heavy on type, light on chrome.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from aarva.server.app import app
from aarva.server.templates import templates


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    """Marketing landing. Static prose — no DB queries needed at this
    layer; the page introduces Aarva to first-time visitors and points
    at the browse routes."""
    return templates.TemplateResponse(request, "landing.html", {})
