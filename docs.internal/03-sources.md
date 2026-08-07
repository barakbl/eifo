# 03 — Source Catalog & Acquisition Strategy

A **source** is any service whose catalog we track. Sources are defined by plugins
([05-fetcher.md](05-fetcher.md)) and toggled in `config/tvil.toml`. This doc is the launch
catalog and the strategy for acquiring each catalog. Availability of specific services MUST
be re-verified at implementation time — the Israeli market shifts (e.g., yes satellite shut
down in favor of yes+ in early 2026); that churn is exactly why the plugin design exists.

## Acquisition strategies (in order of preference)

1. **`tmdb-providers`** — TMDB's watch-providers data (powered by JustWatch, free API,
   region `IL`). One harvester covers every service JustWatch tracks in Israel. Daily
   granularity, no deep links (link to the service's search/title page instead), and the UI
   MUST display "streaming availability data by JustWatch" attribution.
2. **`public-api`** — an official or openly documented JSON endpoint on the service's own
   site (many broadcaster VOD sites are React/Next apps backed by JSON APIs; prefer these
   over HTML parsing when discovered via DevTools).
3. **`scrape`** — polite HTML scraping of the service's public catalog pages. Last resort.

## Launch catalog

### Paid TV / streaming operators (strategy: `tmdb-providers`, fallback `scrape`)

| key | Name | Type | Notes |
|---|---|---|---|
| `yes_plus` | yes+ | subscription | yes's OTT service (replaced satellite, 2026) |
| `sting_tv` | Sting TV | subscription | yes's budget OTT brand |
| `hot` | HOT / NEXT | subscription | cable + NEXT OTT brand |
| `cellcom_tv` | Cellcom TV | subscription | |
| `partner_tv` | Partner TV | subscription | |
| `free_tv` | Free TV | subscription | Keshet/REG streaming venture — verify status at build time |

### International services with Israeli catalogs (strategy: `tmdb-providers`)

| key | Name | Notes |
|---|---|---|
| `netflix_il` | Netflix (Israel catalog) | |
| `disney_plus_il` | Disney+ | |
| `prime_video_il` | Prime Video | |
| `apple_tv_plus` | Apple TV+ | |

These map 1:1 to JustWatch provider IDs for region `IL`; the `tmdb-providers` harvester
declares all of them (one plugin, many source keys).

### Free broadcaster VODs (strategy: `public-api` / `scrape`)

JustWatch coverage of local free VODs is partial at best, so these get dedicated plugins:

| key | Name | Site | Notes |
|---|---|---|---|
| `kan` | Kan 11 / Kan Box | kan.org.il | public broadcaster, free VOD |
| `mako` | Mako VOD (Keshet 12) | mako.co.il | free VOD, registration-gated playback (we only need metadata) |
| `reshet13` | Reshet 13 | 13tv.co.il | free VOD |
| `now14` | Now 14 | now14.co.il | free VOD |

Each of these plugins ships with recorded HTML/JSON fixtures so its parser is tested
offline ([10-quality.md](10-quality.md)).

## Ratings providers (not "sources" — see [06-enrichment.md](06-enrichment.md))

| Provider | Method | Cost |
|---|---|---|
| IMDb | official non-commercial daily TSV datasets (`datasets.imdbws.com`) — **no scraping** | free |
| TMDB | official free API (also our canonical metadata + posters) | free key |
| Rotten Tomatoes | scrape (no public API) | — |
| Seret (seret.co.il) | scrape — the leading Israeli ratings site (critic + viewer scores) | — |
| EDB (edb.co.il) | scrape, optional secondary Israeli provider | — |

## Scraping policy (applies to every `scrape`/`public-api` plugin)

- **Metadata only.** Titles, descriptions, images, deep links, ratings. Never video, never
  anything behind a paywall or login.
- **Politeness.** Respect `robots.txt`; ≤ 1 request/second/host (configurable per source);
  exponential backoff on 429/5xx; identifying User-Agent with a contact URL; conditional
  requests (ETag/Last-Modified) where supported.
- **Resilience.** A parser that finds a page layout it doesn't recognize MUST fail that
  item loudly (logged, counted in `fetch_runs`) rather than storing garbage. A source whose
  sync yields < 20% of its previous item count is treated as a failed sync (layout change
  suspected), not as a mass content removal.
- **Legal.** IMDb datasets are under IMDb's non-commercial license; JustWatch-derived data
  requires attribution; per-site ToS should be reviewed before enabling a scraper. TVIL is
  a personal, non-commercial project and must stay compatible with that.

## Adding / removing a source

- **Add:** write one plugin module in `tvil_fetcher/sources/`, register it (entry point),
  add `[sources.<key>] enabled = true` to `tvil.toml`, add fixtures + tests. No schema or
  API changes.
- **Remove/retire:** set `enabled = false` (or the plugin disappears). The next sync marks
  the source `active = false` in the DB. **All titles and their availability history are
  kept**; the API flags them and the UI shows a "source no longer tracked" badge
  ([08-web-client.md](08-web-client.md)). Re-enabling resumes cleanly.
