"""Service layer.

Sits between web routes (FastAPI) and pipeline stages / DB. Pure
Python functions taking a Database (and any other clients) plus
input args; returns plain dicts / dataclasses suitable for JSON
serialisation. No printing, no sys.exit, no logging of user-facing
errors — raise from aarva.exceptions instead.

The web app composes these into HTTP routes. The CLI keeps using
the stages directly (no behavioural change).
"""
