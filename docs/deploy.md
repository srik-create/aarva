# Aarva web app — deployment runbook

This document covers deploying the FastAPI web app (`aarva/server/`)
to a public host. v0.1 deploys to **Render.com** at **aarva.app**.

The audio side and the static RSS+HTML are already in production
(R2 + GitHub Pages respectively). This runbook is just for the web
app — the listener-facing browse interface.

**Read first:**
- `docs/project_brief.md` — what Aarva is + current decisions log
- `docs/roadmap.md` — what's next + deferred items
- `AGENTS.md` — rules of engagement (esp. rule 7b on portability)

---

## What's deploying

A Python + FastAPI app, single container. Reads-only against a SQLite
DB on a persistent disk. The app makes NO outbound LLM / TTS / R2
calls — those happen in the pipeline (still on the operator's laptop
for v0.1). The web app just serves HTML to listeners.

URLs the web app serves:
- `/` — landing page
- `/today` — today's daily edition
- `/edition/<date>` — past edition
- `/categories`, `/category/<slug>` — browse by JTBD
- `/publications`, `/publication/<slug>` — browse by source
- `/article/<id>` — per-article detail
- `/crosscut/<edition_id>` — per-crosscut detail
- `/editions` — list of past editions
- `/health`, `/health/db` — liveness probes

---

## Render.com setup (one-time)

### 1. Account + repo connection

1. Sign up at https://render.com (free; no card required until you
   add a paid service).
2. Connect your GitHub account when prompted.
3. Authorise Render to read `srik-create/aarva`.

### 2. Provision the service

In the Render dashboard:

1. **New +** → **Blueprint**.
2. Pick the `srik-create/aarva` repo.
3. Pick branch `main`.
4. Render detects `render.yaml` and shows what it'll create:
   - One web service (`aarva-web`)
   - One persistent disk (`aarva-data`, 1 GB at `/data`)
5. Click **Apply**.

Render builds the Dockerfile, attaches the disk, starts the
container, and gives you a URL like
`https://aarva-web-xxxx.onrender.com`.

### 3. Verify the deploy

Once Render reports the service as live:

```bash
curl -sI https://aarva-web-xxxx.onrender.com/health
# expected: HTTP/2 200
curl    https://aarva-web-xxxx.onrender.com/health
# expected: {"status":"ok","service":"aarva"}
```

The site itself will show "First edition coming soon" because the DB
on the persistent disk is empty. That's expected — see DB sync below.

### 4. Sync the SQLite DB from your laptop to Render

The persistent disk starts empty. You need to copy
`~/Projects/Aarva/aarva/data/aarva.db` to `/data/aarva.db` on Render.

**Option A — Render shell (one-time, manual):**

1. In the Render dashboard, your service → **Shell** tab.
2. From your laptop:
   ```bash
   # Pack DB + WAL into a single uploadable archive
   cd ~/Projects/Aarva/aarva/data
   tar czf /tmp/aarva-db.tar.gz aarva.db*
   ```
3. Upload `/tmp/aarva-db.tar.gz` to a publicly-readable place — easiest is
   to put it temporarily in your R2 bucket:
   ```bash
   aws s3 cp /tmp/aarva-db.tar.gz \
     s3://aarva-audio/_tmp/aarva-db.tar.gz \
     --endpoint-url https://<your-account>.r2.cloudflarestorage.com
   ```
4. In Render's shell:
   ```bash
   cd /data
   curl -sL https://audio.aarva.app/_tmp/aarva-db.tar.gz -o /tmp/aarva-db.tar.gz
   tar xzf /tmp/aarva-db.tar.gz
   # /data/aarva.db now in place
   ```
5. Delete the tmp upload from R2 once verified:
   ```bash
   aws s3 rm s3://aarva-audio/_tmp/aarva-db.tar.gz \
     --endpoint-url https://<your-account>.r2.cloudflarestorage.com
   ```

The service is configured to auto-restart on file change; if it
doesn't pick up the new DB, click **Restart** in the dashboard.

**Option B — repeatable sync after every daily run** (deferred —
write `scripts/sync_db_to_render.sh` later if this becomes routine).

### 5. Custom domain (Cloudflare DNS for aarva.app)

The Render-given URL works, but we want `aarva.app`.

1. In Render dashboard, your service → **Settings** → **Custom Domains** → **Add Custom Domain**.
2. Enter `aarva.app` (or `www.aarva.app` if you prefer www-prefixed).
3. Render gives you DNS records to add (typically a `CNAME` or
   apex `A` record).
4. In Cloudflare dashboard, your domain → **DNS** → **Records**.
   - For root `aarva.app`: Render usually wants a CNAME-flattening
     setup. Cloudflare supports this natively — add a CNAME for `@`
     pointing at `<render-url>.onrender.com` and Cloudflare will
     auto-flatten to A records.
   - **Proxy status: DNS only** (grey cloud, not orange). Render
     wants to terminate TLS itself; Cloudflare's proxy can interfere
     with the cert-renewal process.
5. Wait 5-30 min for DNS propagation and Render's cert issuance.
6. Once Render shows the domain as **Active**, visit https://aarva.app
   in a browser.

After this lands, update `render.yaml`'s `AARVA_SERVER_PUBLIC_URL` to
`https://aarva.app` if it isn't already, and re-deploy.

---

## Ongoing operation

### Updating the live site

Any commit to `main` triggers an auto-deploy (`autoDeploy: true` in
`render.yaml`). Render builds the Dockerfile from the new commit,
swaps containers with zero downtime.

To deploy a non-`main` branch manually: dashboard → **Manual Deploy** →
pick branch.

### Updating the SQLite DB

The DB on Render's persistent disk is the source of truth for what
the web app serves. Two ways to update it:

1. **Manual sync after each daily run** — repeat step 4 above.
2. **Future automation** — a `scripts/sync_db_to_render.sh` that pushes
   the DB to R2 + triggers Render to download it on next request.
   Deferred to Phase 2/3.

### Monitoring

- Health: https://aarva.app/health
- DB connectivity: https://aarva.app/health/db
- Render logs: dashboard → **Logs** tab
- Render metrics: dashboard → **Metrics** tab (CPU, memory, request
  count, response latency)

### Rollback

Dashboard → **Deploys** → pick a previous successful deploy →
**Redeploy**. Zero-downtime swap.

---

## Switching providers

The whole point of building this with the `Dockerfile` as the source
of truth (per AGENTS.md rule 7b) is that switching providers is a
config-file change, not a code rewrite. To move off Render:

### Fly.io

1. Install `flyctl`. Run `fly launch` in the repo root.
2. Pick a region. Fly autodetects the Dockerfile.
3. Add `fly.toml` (Fly creates it; commit it).
4. Add a persistent volume: `fly volumes create aarva_data --size 1`.
5. Reference it in `fly.toml`'s `[mounts]` section pointing at `/data`.
6. Set env vars via `fly secrets set AARVA_DB_PATH=/data/aarva.db ...`
7. `fly deploy`.
8. Update Cloudflare DNS to point aarva.app at the Fly app.

### Railway

1. New project from GitHub repo. Railway reads the Dockerfile.
2. Add a persistent volume (Railway's "Volumes" feature) mounted at `/data`.
3. Set env vars in dashboard.
4. Deploy.

### Bare VPS (DigitalOcean Droplet, Hetzner, etc.)

1. Install Docker + docker compose on the VPS.
2. `docker build -t aarva-web .`
3. `docker run -d -p 8000:8000 -v /data:/data --env-file .env aarva-web`
4. Front with Caddy or nginx for TLS termination.
5. Point DNS.

**In all cases**: the Dockerfile is unchanged, the env vars are the
same, the persistent disk semantics are the same. Only the
provider-specific config file (`render.yaml` / `fly.toml` / etc.)
differs.

---

## Cost reference (Render, June 2026)

| Item | Cost |
|---|---|
| Starter web service | $7 / month |
| 1 GB persistent disk | $0.25 / month |
| Bandwidth | included up to a generous limit |
| TLS certs | free (Render handles via Let's Encrypt) |
| **Total** | **~$7.25 / month** |

Bumping to Standard tier ($25/mo) is necessary only if traffic grows
past the Starter limits or the SQLite DB grows past what 1 GB can
hold (years away at v0.1 volume).

Cloudflare DNS + R2 + custom domain on R2: free at Aarva's current
scale (covered in `docs/project_brief.md`).

---

## What to do when something goes wrong

| Symptom | First place to look |
|---|---|
| `/health` returns 200 but pages 500 | Render logs — likely a DB issue (file missing, schema drift) |
| `/health/db` returns 500 with "no such table" | DB on persistent disk is stale or wrong — re-sync via step 4 |
| Pages load but audio doesn't play | Check `audio.aarva.app/<path>` directly — R2 issue, not Render |
| Deploy fails at "Building" | Dockerfile syntax / requirements.txt issue — see build log |
| Deploy fails at "Live" | App crash on startup — check `/health` returns 200, look at logs |
| Domain shows "Not secure" | TLS not yet issued — wait 10-30 min after adding the domain |

When in doubt, the Render dashboard's **Events** tab shows every
state transition for the service.
