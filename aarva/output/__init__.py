"""Aarva output renderers.

The publish stage (Stage 10) reads completed editions from the DB and emits:
  - A responsive HTML page per edition (web_renderer.py)
  - A unified podcast RSS feed across all editions (rss_feed.py)

Output goes to `aarva/output/web/` and `aarva/output/feed.xml`. The user
hosts these files at a public URL (GitHub Pages, Cloudflare R2, etc.); the
feed URL is what listeners subscribe to in their podcast app.
"""
