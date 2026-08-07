# 12 — Staged Delivery Plan

Each stage ships something usable, ends green (all tests + lint pass), and is a sensible
stopping point. Don't start a stage before the previous one's acceptance criteria pass.

## S0 — Foundation
Repo scaffolding: uv workspace, three packages, ruff/mypy/pytest/pre-commit/CI wired,
`tvil-core` settings + DB bootstrap + initial Alembic migration (titles, sources,
availability, genres, fetch_runs), docker-compose skeleton, `git init`.
**Accept:** `uv sync && pytest && ruff check` green in CI; `tvil-fetch db upgrade` creates
the schema; a trivial `/api/v1/meta` endpoint responds.

## S1 — Fetcher core + first sources
Plugin framework, matcher, sweep + <20% guard, fetch_runs, image pipeline. Plugins:
`tmdb-providers` (covering netflix_il, disney_plus_il, prime_video_il, apple_tv_plus,
yes_plus, sting_tv, hot, cellcom_tv, partner_tv per JustWatch-IL coverage) + **one**
scraped broadcaster source (`mako`) to prove the scrape path.
**Accept:** `tvil-fetch all` fills a DB with real titles + posters; rerun is idempotent;
disabling a source in config flips it inactive with data retained; pipeline/matcher/sweep
tests green from fixtures.

## S2 — Enrichment & scores
Enricher framework; TMDB metadata fill; IMDb datasets bulk join; RT scraper; Seret scraper;
normalization + weighted aggregate + `score_israeli`; refresh policy.
**Accept:** >90% of titles that have an `imdb_id` show an IMDb score; sampled titles match
provider sites; aggregate math covered by exact tests; provider links stored for every
rating.

## S3 — Read-only API
Catalog endpoints (titles list/detail, sources, genres, meta), FTS search (he+en),
filters/sort/pagination, ETag caching, problem-details errors, static images serving.
**Accept:** endpoint test suite green; Hebrew and English searches return the same title;
`sources=`+`available=` filters correct incl. gone/inactive semantics.

## S4 — Web client v1 (public)
Home grid + filters + search, title detail with ratings/availability/badges, i18n he/en,
RTL, dark/light, empty/error states, attribution footer. No login yet.
**Accept:** the four badge/availability states render correctly; UI usable on a phone;
Lighthouse Perf ≥ 90 / A11y ≥ 95; JS logic modules have node:test coverage.

## S5 — Accounts & lists
OAuth (Google + X), sessions + CSRF, `/me` endpoints, my-services preset ("filter by what
I have"), watched/want-to-watch/rating UI, settings page, account deletion.
**Accept:** full login E2E (mocked providers) green; privacy tests green (no email leak,
`no-store` on user routes); optimistic UI rolls back on error; account deletion verified.

## S6 — Hardening & ops
Rate limiting, backups + rehearsed restore, daemon housekeeping (sessions, backups),
failure notifications, perf test in nightly CI, Playwright smoke, pip-audit, Caddy docs.
**Accept:** restore rehearsal documented and done; nightly CI green a full week; a killed
mid-sync fetcher leaves a consistent DB (crash test).

## S7 — Social layer
Per-item private notes; public profiles: handle selection, explicit opt-in with
what-becomes-visible copy, `#/u/{handle}` page; notes remain private everywhere.
**Accept:** profile privacy matrix (private/public × own/other) fully tested; toggling back
to private 404s the profile immediately.

## S8 — Hybrid mobile app
Capacitor project wrapping `web/` unchanged; CORS config for capacitor origins; cookie
handling verified on iOS/Android WebViews (fallback: bearer-token mode behind a config
flag if third-party-cookie rules bite); OAuth via system browser (Custom Tabs /
ASWebAuthenticationSession) with app-scheme redirect; icons/splash; store-readiness
checklist.
**Accept:** Android APK + iOS build run the full flow (search → detail → login → list) on
real devices.

## Post-v1 backlog (explicitly deferred)
Episode-level tracking for series; price tracking for rent/buy offers; more Israeli
ratings providers (EDB on by default?); CSV/Letterboxd import; notifications ("a
want-to-watch title arrived on your service" — the data model already supports it:
availability first_seen × user lists); recommendations (still no LLM commitment).
