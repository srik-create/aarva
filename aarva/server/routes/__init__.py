"""Route modules for the Aarva web app.

Each module imports `app` from aarva.server.app and registers its
handlers via `@app.get(...)` etc. Keeping routes split across files
so each concern stays bounded as the surface area grows.

Importing this package triggers all route registrations as a
side-effect — aarva/server/app.py imports this at module-load time
so every route is wired up before uvicorn starts serving.
"""
from aarva.server.routes import (   # noqa: F401
    landing, home, editions, articles, crosscuts, categories, publications,
    admin, create,
)
