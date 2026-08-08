# TVIL

**What is streaming in Israel, and is it any good?**

TVIL aggregates the catalogs of Israeli VOD services (yes+, Sting TV, HOT/NEXT, Cellcom TV,
Partner TV, the broadcaster VODs, and the international services' Israeli catalogs),
enriches every title with ratings from IMDb, Rotten Tomatoes, TMDB and Israeli sites such as
Seret, and serves it through a web app where you can search, filter by the services you
actually subscribe to, and track what you watched and what you want to watch.

The full specifications live in [`docs.internal/`](docs.internal/README.md).

## Status

Stages **S0 — S5** are complete: the workspace and schema, the fetcher with its source
plugins, enrichment and scores, the read-only catalog API, the web client, and accounts —
sign-in with Google or X, watched / want-to-watch lists, 1–10 ratings, private notes, and
the "my services" filter preset. Hardening (S6) is next — see
[the staged plan](docs.internal/12-stages.md).

Accounts are optional: a deployment with no OAuth credentials serves the catalog to
everyone and simply offers no sign-in.

## Layout

| Path | What it is |
|---|---|
| `packages/tvil-core` | Settings, SQLAlchemy schema, Alembic migrations — the only contract between the services |
| `packages/tvil-fetcher` | `tvil-fetch` CLI: catalog sync, enrichment, artwork |
| `packages/tvil-api` | FastAPI REST service |
| `web/` | Static HTML+JS client (no build step) |
| `docs.internal/` | Specifications |

## Quick start

```bash
uv sync
cp config/tvil.example.toml config/tvil.toml
cp .env.example .env                   # fill in TVIL_TMDB_API_KEY when you reach S1
uv run tvil-fetch db upgrade           # create the schema
uv run uvicorn tvil_api.main:app --reload
```

Then open <http://localhost:8000/api/v1/meta> or the interactive docs at
<http://localhost:8000/docs>.

With Docker instead:

```bash
cp config/tvil.example.toml config/tvil.toml && cp .env.example .env
docker compose up -d
```

## Development

```bash
uv run pytest                # all tests
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pre-commit install    # run the same gates before each commit
```

Coverage gates match CI: 90% for `tvil-core`, 85% for the other packages.

Migrations are generated from the models and must never drift from them
(`test_migrations.py` enforces this):

```bash
uv run alembic -c packages/tvil-core/alembic.ini revision --autogenerate -m "what changed"
```

## Configuration

Defaults ← `config/tvil.toml` (committable, no secrets) ← `TVIL_*` environment variables
(secrets). See [`docs.internal/11-ops-install.md`](docs.internal/11-ops-install.md).

## Data attribution

Streaming availability data by JustWatch · Metadata and artwork by TMDB · Ratings by IMDb
(non-commercial datasets), Rotten Tomatoes and Seret. The API serves these credits from
`GET /api/v1/meta` and the client is required to display them.
