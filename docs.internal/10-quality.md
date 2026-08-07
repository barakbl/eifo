# 10 — Quality: Testing, Linting, CI, Code Standards

The bar: **all code with tests, linted, clean, easy to follow.** Concretely:

## Tooling (configured once, at the workspace root `pyproject.toml`)

| Tool | Role | Policy |
|---|---|---|
| Ruff | lint + format | `ruff check` (rules: E,F,W,I,N,UP,B,SIM,RUF) and `ruff format`; zero warnings — CI fails on any |
| mypy | typing | strict on `tvil-core`; `disallow_untyped_defs` on the other packages |
| pytest | tests | `pytest -q` runs everything from the root |
| coverage | thresholds | ≥ 90% `tvil-core`, ≥ 85% fetcher/api; enforced in CI |
| pre-commit | local gate | ruff, ruff-format, mypy (changed files), trailing whitespace |
| pip-audit | dependency CVEs | weekly scheduled CI job |
| ESLint + Prettier | `web/` JS/CSS | dev-time only (`npx`); no runtime npm deps |

## Test strategy per package

### `tvil-core`
- Model/constraint tests against a temp SQLite (unique keys, enums, cascade behavior).
- Alembic: test that a fresh `upgrade head` schema == models metadata
  (`alembic-autogen-check` style — catches drift).
- FTS triggers: insert/update/delete a title → FTS search reflects it, Hebrew and English.

### `tvil-fetcher`
- **Every parser tests against recorded fixtures** (`tests/fixtures/<source>/*.html|json`
  captured by a `--record` helper). No test ever hits the network — `respx` mocks all
  HTTP; CI runs fully offline.
- Pipeline tests: fake plugin yielding controlled RawItems → assert titles created,
  availability upserted, sweep flips `is_current` only after 2 successful misses, failed
  sync doesn't sweep, <20% guard triggers `aborted_suspicious`.
- Matcher: table-driven cases (exact id, TMDB hit, fuzzy Hebrew, ambiguous → review).
- Aggregation: exact-math tests for weights, damping, ≥2-provider rule, `score_israeli`.
- Image pipeline: temp dir, tiny fixture images, idempotency.

### `tvil-api`
- `TestClient` + fresh seeded SQLite per test session; every endpoint gets: happy path,
  auth-required (401), validation error (422), not-found (404).
- Auth flow with mocked provider endpoints (respx): full login → cookie → `/me` →
  CSRF-rejected PUT without header → accepted with header.
- Privacy tests are mandatory: private profile 404s; public profile never leaks email or
  notes (assert on full response body keys).
- The titles-list query perf test (60k synthetic titles, p95 < 100 ms) — marked `slow`,
  runs in CI nightly, not per-PR.

### `web/`
- S4: pure-logic modules (i18n, store, api error mapping) tested with `node --test`.
- S6+: one Playwright smoke (`search → open title → toggle want-to-watch` against a seeded
  local stack) run in CI nightly.

## CI (GitHub Actions, `ci.yml`)

Per PR/push: `uv sync` → ruff check + format --check → mypy → pytest w/ coverage gates →
eslint/prettier check. Nightly: slow marks (perf, Playwright) + pip-audit.
A `sync --source` dry run against fixtures is part of the per-PR suite, so a plugin can't
merge without its fixtures.

## Code standards

- Small modules with one job; routers/plugins stay ~100–150 lines — if bigger, split.
- Public functions typed and docstringed (one summary line; args only when non-obvious).
- No naked `except:`; catch specific errors; every swallowed exception is logged with
  context.
- Logging: stdlib `logging`, structured `extra={}` fields, human format in dev; no print.
- Constants/config never inline — settings object or module-level named constant.
- Comments explain *why*, never narrate the code.
- Conventional Commits (`feat:`, `fix:`, `docs:`…) once the repo is git-initialized.
