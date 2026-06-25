"""Aarva web app — FastAPI server.

Public, read-mostly site that lets listeners browse Aarva's daily
editions and (Phase 2+) search the catalog. Designed to be hostable
on any standard Python-friendly platform — see the Dockerfile at the
repo root for the canonical build, and docs/deploy.md for provider
notes (Render, Fly.io, etc.).

All configuration comes from environment variables (per AGENTS.md
rule 7b — portability by default); see aarva/server/config.py for
the full list. No imports from provider SDKs anywhere in this
package.
"""
