# 07 — REST API (`tvil-api`)

FastAPI app; read-only over catalog data, read/write over user data. Serves the static web
client and images too, so a single process is a complete deployment. OpenAPI docs at
`/docs` (disabled in production builds? No — keep them; the API is public-read anyway).

## Conventions

- Base path `/api/v1`; JSON only; Pydantic v2 response models for every endpoint.
- Pagination: `?page=1&page_size=24` (max 100); responses wrap as
  `{items: [...], page, page_size, total}`.
- Errors: RFC 9457 problem-details shape `{type, title, status, detail}`.
- Auth: session cookie (HttpOnly, Secure, SameSite=Lax) established by OAuth
  ([09-auth-privacy.md](09-auth-privacy.md)). Endpoints marked 🔒 require it (401
  otherwise). State-changing requests require the `X-CSRF-Token` header (double-submit).
- Caching: catalog GETs send `Cache-Control: public, max-age=300` + ETag; `/images/*`
  are immutable (`max-age=31536000`) since paths change when content changes.

## Catalog endpoints (public)

### `GET /api/v1/titles`
Query params:

| param | meaning |
|---|---|
| `q` | text search (FTS5 over he+en names/overviews; prefix matching for search-as-you-type) |
| `sources` | CSV of source keys — titles currently available on **any** of them |
| `available` | `current` (default: `is_current` on an **active** source) \| `any` \| `gone` |
| `type` | `movie` \| `series` |
| `genres` | CSV of genre ids |
| `year_min` / `year_max` | |
| `score_min` | aggregate ≥ N |
| `sort` | `score` (default) \| `score_israeli` \| `year` \| `name` \| `recently_added` |
| `page`, `page_size` | |

Returns `TitleCard[]`: id, type, names, year, poster, aggregate scores, genre names, and
`availability: [{source_key, source_name, source_active, is_current, offer_type,
deep_link_url, last_seen, gone_since}]` — everything the grid needs in one call, no N+1.

### `GET /api/v1/titles/{id}`
`TitleDetail` = TitleCard + overviews, runtime/seasons/status, backdrop, full
`ratings: [{provider, provider_name, score_raw, score_display, score_normalized,
vote_count, url}]`, `aggregate: {score, score_israeli, components}`, and full availability
history (including `is_current = false` rows — the UI's "was on / no longer on" story).

### `GET /api/v1/sources`
All sources: key, name, kind, logo, website, `active`, `deactivated_at`, current title
count, `last_synced_at` (from fetch_runs). Inactive sources are included — the client
renders their badge state.

### `GET /api/v1/genres`, `GET /api/v1/meta`
Genre list; `meta` = data freshness (last successful sync/enrich per source) + attribution
strings (JustWatch, IMDb, TMDB) the client must display.

## Auth endpoints

```
GET  /api/v1/auth/login/{provider}     provider ∈ {google, x}; 302 to provider
GET  /api/v1/auth/callback/{provider}  completes OAuth, sets session cookie, 302 to app
POST /api/v1/auth/logout            🔒 clears session
GET  /api/v1/me                     🔒 current user (also the CSRF token bootstrap)
```

## User endpoints (all 🔒)

```
PATCH /api/v1/me                       display_name, handle, is_public, my_source_ids
GET   /api/v1/me/items?status=&page=   my list, joined with TitleCard
PUT   /api/v1/me/items/{title_id}      {status?, rating?, note?} — upsert, partial
DELETE /api/v1/me/items/{title_id}     remove from lists entirely
DELETE /api/v1/me                      account deletion: user row + user_items, immediate
```

`PUT me/items` validates rating ∈ 1..10, note ≤ 2000 chars, title exists.

## Public profiles (S7)

```
GET /api/v1/users/{handle}             404 unless is_public; display_name, avatar, counts
GET /api/v1/users/{handle}/items       watched/want lists + ratings; notes NEVER included
```

## Static

- `GET /images/{...}` → `data/images/` (FileResponse / StaticFiles).
- `GET /` and unmatched non-API paths → `web/` static app (SPA fallback to index.html).

## Implementation notes

- Routers: `catalog.py`, `auth.py`, `me.py`, `users.py`, `static.py`; DB session and
  current-user come from `deps.py` dependencies — handlers stay ~15 lines.
- The titles list query is the only performance-sensitive path: one FTS/filtered CTE for
  matching ids + one page-sized hydration query with `selectinload` for availability.
  Target: p95 < 100 ms on SQLite at 60k titles (verified by a perf test in CI, S4).
- Rate limiting (slowapi): 60/min per IP on search, 10/min on auth endpoints. Fine to
  defer to S6 hardening.
- CORS: same-origin in web deployment ⇒ disabled by default; enabled for the Capacitor
  origins (`capacitor://localhost`, `http://localhost`) via config flag at S8.
