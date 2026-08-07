# 06 — Enrichment & Score Aggregation

Enrichers attach metadata and ratings to canonical titles. Like sources, they are plugins
(`tvil_fetcher/enrichers/`, entry-point group `tvil.enrichers`) so providers can be added
or dropped without core changes.

```python
class Enricher(ABC):
    provider: str  # "imdb", "rt", "seret", ...

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None: ...

    # EnrichResult: ratings: list[Rating], metadata_patch: dict (optional)
```

Refresh policy: a title is (re-)enriched when it's new, when its rating is older than
`refresh_days` (default 14; 3 for titles currently on ≥1 active source — hot titles stay
fresh), or on `--force`.

## Providers

### TMDB (`tmdb`) — free API, also the canonical metadata source
- Fills: names (he/en via `language=` param), overviews, year, genres, runtime, seasons,
  status, artwork paths, `imdb_id` (via `/external_ids`).
- Rating: `vote_average` (0–10) + `vote_count`.
- Link: `themoviedb.org/{movie|tv}/{id}`.

### IMDb (`imdb`) — official non-commercial datasets, **no scraping**
- Nightly job downloads `title.ratings.tsv.gz` (~8 MB) from `datasets.imdbws.com`, loads it
  into a local staging table, and joins on `titles.imdb_id` — one bulk pass updates every
  title at once, so the per-title `enrich()` path is a no-op lookup.
- Rating: averageRating (0–10) + numVotes.
- Link: `imdb.com/title/{tt}/`.
- License: IMDb non-commercial terms — fine for this project; noted in the UI footer.

### Rotten Tomatoes (`rt`) — no public API
Verified against the live site (August 2026):
- Scores come from the embedded `media-scorecard-json` payload: `criticsScore.score` →
  `rt_critics` and `audienceScore.score` → `rt_audience`, both 0–100 percentages sent as
  strings. `ratingCount` is null on audience blocks, so `reviewCount` is the real figure.
- `hideAudienceScore` is respected: if RT declines to show a number, neither do we.
- **`/search` is robots-disallowed**, so search-based resolution is not available to a
  compliant crawler. Instead the English title is turned into RT's slug form and requested
  directly; RT redirects near-miss slugs to the right film, and a genuine absence answers
  404, which is a clean "not found". A title with no Latin name has no slug and is skipped.
- Many Israeli titles are simply not on RT — expected, and not an error.

### Seret (`seret`) — primary Israeli provider — **implemented but off by default**
Verified against the live site (August 2026):
- Scores come from **schema.org JSON-LD**, not CSS selectors: `aggregateRating.ratingValue`
  is the viewer score (0–10, with `ratingCount`) → `seret_viewers`, and an
  `additionalProperty` entry holding the site's composite editorial score →
  `seret_critics`.
- Films and series are **different endpoints with different id parameters**
  (`s_movies.asp?MID=` vs `s_series.asp?SID=`) and declare different JSON-LD types
  (`Movie` / `TVSeries`). Handling only one silently loses the other.
- Pages are **windows-1255**, not UTF-8. A wrong decode turns every Hebrew name to
  mojibake and breaks matching.
- `sameAs` carries an IMDb link, which settles identity far more reliably than comparing
  names — used first whenever both sides have one.

**Why it is disabled by default:** Seret publishes no working title search. The
`SearchAction` its own homepage advertises, the real form POST, and the autocomplete
endpoint all return a generic current-releases listing rather than results, so a title
cannot be resolved to a page id. Everything downstream of an id is implemented and tested;
resolution needs sitemap-based indexing, deferred to a follow-up. Switch it on with
`[enrich] enabled = ["seret"]` once that exists.

### EDB (`edb`) — scrape; optional secondary Israeli provider (config-off by default)
Same pattern as Seret. Exists mainly to prove the "Israeli providers are pluggable" claim.

## Normalization → 0–100

| provider | native | normalized |
|---|---|---|
| imdb, tmdb, seret_viewers, seret_critics, edb | 0–10 | ×10, rounded |
| rt_critics, rt_audience | 0–100 % | as-is |

`score_raw` keeps the native value for display ("8.4/10", "92%"); `score_normalized` is
what aggregation uses.

Two rules that are easy to get subtly wrong:

- **Halves round up, always.** Python's built-in `round` rounds halves to *even*, which
  would normalise 7.25 → 72 but 7.35 → 74. A user-visible score must not depend on the
  parity of the preceding digit, so normalisation and the weighted mean both use an
  explicit half-up rounding helper.
- **A score outside its provider's scale is rejected, not stored.** Reading a percentage
  as a 0–10 value (or vice versa) is the likeliest parser bug, and storing it would quietly
  skew the aggregate. The rating is dropped and the error recorded in `fetch_runs.stats`.

## Aggregate score

Weighted mean of available normalized scores; weights in `tvil.toml`:

```toml
[scores.weights]
imdb = 3.0
rt_critics = 2.0
rt_audience = 1.0
tmdb = 1.0
seret_critics = 2.0
seret_viewers = 1.5
edb = 0.5
```

Rules:
- `score` requires ≥ 2 providers, else null (UI shows the single provider score only —
  an "aggregate" of one number is misleading).
- Vote-count damping: a provider rating with < 50 votes gets its weight halved (protects
  against a 10/10-from-3-votes skew).
- `score_israeli` = same formula over Israeli providers only (`seret_*`, `edb`) — this is
  the dedicated Israeli-ratings aggregate, shown alongside the global one for local
  content. It deliberately needs only **one** provider: the whole point of a separate
  Israeli score is to surface local opinion, and requiring two would hide it for most
  titles, since Seret is the only Israeli provider enabled by default.
- `aggregate_scores.components` stores every input (provider, normalized, weight applied),
  so the UI can show "how was this computed" and tests can assert exact math.
- Recomputed at the end of every `enrich` run for titles whose ratings changed.

## Metadata precedence

When providers disagree on metadata (year, names): TMDB wins; source-provided values fill
gaps only. `metadata_patch` from enrichers never overwrites a non-null TMDB-sourced field.

## Adding a ratings provider (checklist)

1. New module in `enrichers/`, implement `Enricher`, add entry point.
2. Add provider key to the ratings enum migration + normalization table above.
3. Add weight to `[scores.weights]` (and default in settings).
4. Fixtures + parser tests; not-found memo; rate limit entry.
5. UI: nothing — providers render generically from `external_ratings` (name, score, link).
