# 04 — Data Model

SQLAlchemy 2.0 typed models in `tvil_core/models.py`; Alembic migrations in
`tvil-core/alembic/`. SQLite in WAL mode; schema stays Postgres-compatible (no SQLite-only
column types outside the FTS mirror table). All timestamps UTC. Soft deletes only — catalog
rows are never physically deleted by the application.

## Entity overview

```
titles ──< title_genres >── genres
titles ──< availability >── sources
titles ──< external_ratings
titles ──1 aggregate_scores
titles ──< user_items >── users
sources ──< fetch_runs
titles ──< match_reviews (unresolved matches)
```

## Tables

### `titles` — canonical movie / series

| column | type | notes |
|---|---|---|
| id | int PK | |
| type | enum `movie` \| `series` | |
| tmdb_id | int, unique, nullable | canonical external anchor |
| imdb_id | text, unique, nullable | `tt…`; enables IMDb dataset join + link |
| name_en | text, nullable | at least one of name_en/name_he required |
| name_he | text, nullable | |
| year | int, nullable | first release year |
| overview_en / overview_he | text, nullable | |
| poster_path / backdrop_path | text, nullable | relative path under `data/images/` |
| runtime_minutes | int, nullable | movies: runtime; series: avg episode |
| seasons | int, nullable | series only |
| status | text, nullable | e.g. `ended`, `returning` (series) |
| created_at / updated_at | datetime | |

Indexes: `(type, year)`, `tmdb_id`, `imdb_id`.

**FTS:** `titles_fts` — SQLite FTS5 virtual table (`name_en`, `name_he`, `overview_en`,
`overview_he`; tokenizer `unicode61 remove_diacritics 2`, which handles Hebrew), kept in
sync by triggers created in an Alembic migration. This powers text search in both languages.

### `genres` / `title_genres`

TMDB's genre taxonomy (`genres`: id, tmdb_id, name_en, name_he). `title_genres` is the
usual join table.

### `sources` — one row per tracked service

| column | type | notes |
|---|---|---|
| id | int PK | |
| key | text, unique | e.g. `cellcom_tv` — matches plugin key |
| name | text | display name |
| kind | enum `subscription` \| `free` \| `rent_buy` | drives UI grouping |
| website_url | text | |
| logo_path | text, nullable | local asset |
| active | bool, default true | false ⇒ "source no longer tracked" badge everywhere |
| deactivated_at | datetime, nullable | |
| created_at / updated_at | datetime | |

### `availability` — "title X is on source Y"

| column | type | notes |
|---|---|---|
| id | int PK | |
| title_id | FK titles | |
| source_id | FK sources | |
| deep_link_url | text, nullable | direct page on the service, when the plugin has one |
| offer_type | enum `stream` \| `rent` \| `buy` \| `free` | |
| is_current | bool | maintained by the sweep (see [05-fetcher.md](05-fetcher.md)) |
| first_seen / last_seen | datetime | last_seen = last successful sync that included it |
| gone_since | datetime, nullable | set when is_current flips to false |

Unique: `(title_id, source_id, offer_type)`. Index: `(source_id, is_current)`,
`(title_id, is_current)`.

### `external_ratings` — one row per (title, provider)

| column | type | notes |
|---|---|---|
| id | int PK | |
| title_id | FK titles | |
| provider | enum `imdb` \| `tmdb` \| `rt_critics` \| `rt_audience` \| `seret_critics` \| `seret_viewers` \| `edb` | extensible enum (text + CHECK) |
| score_raw | float | in the provider's native scale |
| score_normalized | int 0–100 | see normalization table in [06-enrichment.md](06-enrichment.md) |
| vote_count | int, nullable | |
| url | text | link to the provider's page for this title (always shown in UI) |
| fetched_at | datetime | |

Unique: `(title_id, provider)`.

### `aggregate_scores`

| column | type | notes |
|---|---|---|
| title_id | FK titles, PK | |
| score | int 0–100, nullable | null until ≥ 2 components exist |
| score_israeli | int 0–100, nullable | Israeli providers only (seret_*, edb) |
| components | JSON | `{provider: {normalized, weight}}` — full transparency |
| computed_at | datetime | |

### `users`

| column | type | notes |
|---|---|---|
| id | int PK | |
| auth_provider | enum `google` \| `x` | |
| auth_subject | text | provider's stable user id |
| email | text, nullable | from provider; not shown publicly, ever |
| display_name | text | |
| handle | text, unique, nullable | chosen by user; required to go public |
| avatar_url | text, nullable | |
| is_public | bool, default **false** | privacy default ([09-auth-privacy.md](09-auth-privacy.md)) |
| my_source_ids | JSON int[] | "services I have" filter preset |
| created_at / last_login_at | datetime | |

Unique: `(auth_provider, auth_subject)`.

### `user_items` — a user's relationship to a title

| column | type | notes |
|---|---|---|
| id | int PK | |
| user_id / title_id | FKs | unique together |
| status | enum `watched` \| `want_to_watch`, nullable | null = only rated/noted |
| rating | int 1–10, nullable | |
| note | text, nullable | private always, even on public profiles (S7 decision) |
| created_at / updated_at | datetime | |

### `fetch_runs` — observability

| column | type | notes |
|---|---|---|
| id | int PK | |
| source_key | text, nullable | null for enrich/images runs |
| phase | enum `sync` \| `enrich` \| `images` | |
| started_at / finished_at | datetime | |
| status | enum `ok` \| `failed` \| `aborted_suspicious` | last = the <20% guard |
| stats | JSON | items_seen, created, matched, unmatched, errors[] (capped) |

### `match_reviews` — unresolved title matches

Raw items the matcher couldn't confidently resolve: `(id, source_key, raw_payload JSON,
candidates JSON, created_at, resolved_at nullable)`. Worked through with the
`tvil-fetch review` CLI; low volume expected.

## Volume assumptions

~30–60k titles, ~100–200k availability rows, ~10 sources, ratings ≈ 4×titles. Comfortably
SQLite territory; every listed index exists to keep the API's hot queries (search + filter
by source + sort by score) index-served.
