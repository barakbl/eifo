# TVIL — Internal Specs

TVIL is a personal "what can I watch, where, and is it good?" service for the Israeli market.
It aggregates the catalogs of Israeli VOD/streaming services, enriches every title with
ratings (IMDb, Rotten Tomatoes, TMDB, Israeli sites like Seret), and serves it through a
clean web app where users can search, filter by the services they actually subscribe to,
and keep watched / want-to-watch lists.

## Reading order

| Doc | What it covers |
|---|---|
| [01-overview.md](01-overview.md) | Vision, goals, non-goals, product principles |
| [02-architecture.md](02-architecture.md) | Components, data flow, tech stack, repo layout |
| [03-sources.md](03-sources.md) | Catalog of Israeli sources + acquisition strategy per source |
| [04-data-model.md](04-data-model.md) | Database schema |
| [05-fetcher.md](05-fetcher.md) | Fetcher service: source plugin framework, sync lifecycle, images |
| [06-enrichment.md](06-enrichment.md) | Metadata/ratings enrichment + score aggregation |
| [07-api.md](07-api.md) | FastAPI REST service spec |
| [08-web-client.md](08-web-client.md) | HTML+JS client: UX, pages, design system, RTL |
| [09-auth-privacy.md](09-auth-privacy.md) | Google/X login, sessions, privacy model |
| [10-quality.md](10-quality.md) | Testing, linting, CI, code standards |
| [11-ops-install.md](11-ops-install.md) | Installation, configuration, deployment, backups |
| [12-stages.md](12-stages.md) | Staged delivery plan with acceptance criteria |

## One-paragraph summary of the system

Two Python services share one database. The **fetcher** (`tvil-fetcher`) runs on a schedule,
pulls each enabled source's catalog (free APIs where they exist — TMDB watch providers,
IMDb datasets — scraping where they don't), resolves every item to a canonical title,
downloads poster images, attaches ratings from global and Israeli providers, and computes an
aggregate score. The **API** (`tvil-api`, FastAPI) is a read/write REST layer over that
database: public catalog search + per-user lists behind Google/X OAuth login. The **client**
is a static HTML+JS app (no build step) served alongside the API; the same static app is
later wrapped with Capacitor to become the Android/iOS hybrid app.

## Conventions used in these docs

- **MUST / SHOULD / MAY** carry RFC-2119 meaning.
- "Title" always means the canonical movie-or-series entity; "item" means a source's
  listing of a title.
- Stage numbers (S0…S8) refer to [12-stages.md](12-stages.md).
