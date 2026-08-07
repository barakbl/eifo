# 02 — Architecture

## Components

```
┌────────────────────────────  server (single node)  ───────────────────────────┐
│                                                                               │
│  tvil-fetcher (Python CLI/daemon)          tvil-api (FastAPI + Uvicorn)       │
│  ├─ source plugins (APIs + scrapers)       ├─ /api/v1/* REST endpoints        │
│  ├─ title matcher (canonicalization)       ├─ OAuth login (Google / X)        │
│  ├─ enrichers (IMDb, RT, TMDB, Seret)      ├─ serves /images/* (static)       │
│  ├─ score aggregator                       └─ serves web client  (static)     │
│  └─ image pipeline                                                            │
│           │            ▲                              │                       │
│           ▼            │ reads schema from            ▼                       │
│  ┌─────────────────────┴──────────────┐    ┌────────────────────┐             │
│  │ tvil-core: SQLAlchemy models,      │    │ data/              │             │
│  │ settings, Alembic migrations       │    │ ├─ tvil.db (SQLite)│             │
│  └────────────────────────────────────┘    │ └─ images/         │             │
│                                            └────────────────────┘             │
└───────────────────────────────────────────────────────────────────────────────┘
                                                       ▲
                     web client (static HTML+JS) ──────┘  browser / Capacitor shell
```

Three packages, one shared database, one image directory. The fetcher and the API never
talk to each other directly — the database is the only contract between them
(`tvil-core` owns the schema so it is defined exactly once).

## Why this shape

- **Fetcher ≠ API process.** Scraping is slow, bursty, and failure-prone; the API must stay
  fast and stateless. Separate processes, separate lifecycles (cron vs. long-running).
- **SQLite by default.** One file, WAL mode, FTS5 for text search, trivially backed up.
  All DB access goes through SQLAlchemy 2.0, so moving to Postgres is a connection-string
  change plus an Alembic run — but it is explicitly *not* required.
- **Static client, no build step.** Plain ES modules + modern CSS keep the frontend
  auditable and make the later Capacitor wrap (S8) trivial: the same `web/` directory ships
  in the mobile shell unchanged.

## Tech stack (pinned decisions)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python ≥ 3.12 | both services |
| Package/env manager | `uv` (workspace mode) | one lockfile at repo root |
| Web framework | FastAPI + Uvicorn | Pydantic v2 response models |
| ORM / migrations | SQLAlchemy 2.0 (typed) + Alembic | migrations live in `tvil-core` |
| DB | SQLite (WAL, FTS5) | Postgres-compatible schema, but not a goal |
| HTTP client | `httpx` + `tenacity` (retries) | shared client with rate limiting |
| HTML parsing | `selectolax` | fast, no C-heavy bs4/lxml stack |
| Images | Pillow | resize to fixed variants |
| Scheduling | system cron (default) or `tvil-fetch daemon` (APScheduler) | |
| OAuth | Authlib | Google OIDC + X OAuth 2.0 (PKCE) |
| Fuzzy matching | `rapidfuzz` | title canonicalization |
| Config | pydantic-settings: `config/tvil.toml` + `TVIL_*` env overrides | |
| Lint/format | Ruff (lint + format), mypy (strict on `tvil-core`) | |
| Tests | pytest, respx (HTTP mocking), coverage | see [10-quality.md](10-quality.md) |
| Frontend | Vanilla ES modules, CSS custom properties, no framework, no bundler | |
| Mobile (S8) | Capacitor wrapping `web/` | |

## Repository layout

```
tvil/
├── docs.internal/              # these specs
├── pyproject.toml              # uv workspace root + shared tool config (ruff, mypy, pytest)
├── uv.lock
├── config/
│   └── tvil.toml               # sources on/off, weights, API keys via env
├── packages/
│   ├── tvil-core/              # src/tvil_core/{models.py, db.py, settings.py}, alembic/
│   ├── tvil-fetcher/           # src/tvil_fetcher/{cli.py, sources/, enrichers/, images.py, match.py}
│   └── tvil-api/               # src/tvil_api/{app.py, routers/, auth/, deps.py}
├── web/                        # index.html, css/, js/, assets/
├── data/                       # runtime (gitignored): tvil.db, images/
├── tests/                      # cross-package integration tests; unit tests live in each package
├── docker-compose.yml
└── .github/workflows/ci.yml
```

## Data flow (one sync cycle)

1. `tvil-fetch sync` iterates enabled source plugins → each yields raw catalog items.
2. **Matcher** resolves each item to a canonical `title` (external-ID match → TMDB lookup →
   fuzzy match → review queue). New titles are created; `availability` rows are upserted
   with `last_seen = now`.
3. **Sweep** marks availabilities not seen in N consecutive *successful* syncs of that
   source as `is_current = false` (title is never deleted).
4. `tvil-fetch enrich` fills/refreshes external ratings (IMDb dataset join, RT scrape,
   TMDB, Seret scrape) and recomputes `aggregate_scores`.
5. `tvil-fetch images` downloads missing posters/backdrops, resizes, stores under
   `data/images/`.
6. The API reads all of the above; it never writes catalog data (only user data).

Each phase is idempotent and independently re-runnable; a failed enrich never corrupts a
completed sync.

## Cross-cutting rules

- All timestamps stored in UTC (ISO 8601); the client localizes.
- All outbound HTTP goes through one shared client wrapper: per-host rate limit,
  retry-with-backoff, custom `User-Agent: tvil-fetcher/<version> (+contact URL)`.
- Every fetcher run writes a `fetch_runs` row (source, counts, duration, status) —
  this is the observability story; no external monitoring stack.
- Secrets (API keys, OAuth client secrets) come only from environment variables; the TOML
  file holds no secrets and is committable.
