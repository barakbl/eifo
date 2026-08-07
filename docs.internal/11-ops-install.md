# 11 — Ops: Install, Configure, Run, Back Up

Target: a single Linux box (or a Mac for dev). Two supported install paths; both MUST stay
this short — "easy to install" is an acceptance criterion, not a hope.

## Path A — bare metal / dev

```bash
git clone <repo> && cd tvil
uv sync                              # one lockfile, all packages
cp config/tvil.example.toml config/tvil.toml
cp .env.example .env                 # fill: TVIL_TMDB_API_KEY, OAuth ids/secrets
uv run tvil-fetch db upgrade         # create/upgrade schema
uv run tvil-fetch all                # first full sync+enrich+images (long on first run)
uv run uvicorn tvil_api.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — done. Scheduling on bare metal: two crontab lines
(`tvil-fetch sync` nightly, `tvil-fetch enrich` after) or `tvil-fetch daemon`.

## Path B — Docker Compose (recommended for a server)

`docker-compose.yml` defines two services from one image (multi-stage build, `uv` inside):

- `api` — uvicorn, port 8000, mounts `./data` and `./config`.
- `fetcher` — runs `tvil-fetch daemon` (schedule from `tvil.toml`), same mounts.

```bash
cp config/tvil.example.toml config/tvil.toml && cp .env.example .env  # fill secrets
docker compose up -d
docker compose exec fetcher tvil-fetch all   # initial fill
```

TLS: any reverse proxy (Caddy is the documented default — two-line Caddyfile with
automatic certs) in front of :8000. OAuth redirect URIs must use the public HTTPS origin.

## Configuration reference

Layering: defaults (code) ← `config/tvil.toml` (committable, no secrets) ← `TVIL_*` env
vars (secrets + machine-specific). Key settings:

| Setting | Env | Default |
|---|---|---|
| DB path/URL | `TVIL_DB_URL` | `sqlite:///data/tvil.db` |
| Images dir | `TVIL_IMAGES_DIR` | `data/images` |
| TMDB API key | `TVIL_TMDB_API_KEY` | — (required) |
| OAuth creds | `TVIL_GOOGLE_*`, `TVIL_X_*` | — (required for login) |
| Public origin | `TVIL_PUBLIC_ORIGIN` | `http://localhost:8000` |
| Session secret | `TVIL_SECRET_KEY` | — (required; 32+ random bytes) |
| Sources on/off, weights, schedule | (toml) | see [03](03-sources.md)/[06](06-enrichment.md) |

Startup MUST fail fast with a clear message listing any missing required setting.

## Operations

- **Health:** `GET /api/v1/meta` doubles as the health/freshness endpoint (per-source last
  successful sync). A source stale > 48h is surfaced there; optional ntfy/email ping from
  the daemon on repeated failures (config-off by default).
- **Logs:** stdout (journald / `docker compose logs`). Fetch history queryable:
  `tvil-fetch sources list` shows last run status per source.
- **Backup:** the entire state is `data/` + `config/` + `.env`. Nightly
  `sqlite3 data/tvil.db ".backup data/backups/tvil-$(date +%F).db"` (safe under WAL,
  wired into the daemon's housekeeping; keep 14). Images are re-downloadable — back up
  best-effort.
- **Restore:** copy the backup file over `data/tvil.db`, restart. Rehearsed once per
  release (S6 acceptance).
- **Upgrade:** `git pull && uv sync && uv run tvil-fetch db upgrade && restart` (or
  `docker compose pull/build && up -d`; the api container runs `db upgrade` on start).
- **Reset a source:** `tvil-fetch sync --source X --force` refetches ignoring caches.

## Resource envelope

SQLite + static frontend + one uvicorn worker ≈ fits in 512 MB RAM / 1 vCPU / a few GB
disk (images dominate). No Redis, no Postgres, no queue — by design
([01-overview.md](01-overview.md) non-goals).
