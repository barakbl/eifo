# 05 — Fetcher Service (`tvil-fetcher`)

The fetcher is a CLI (also runnable as a daemon) that owns all writes to catalog tables.
Design goals: **pluggable sources**, **idempotent phases**, **graceful degradation** (one
broken source never blocks the others), and **honest lifecycle** for disappearing content.

## CLI surface

```
tvil-fetch sync [--source KEY]...     # pull catalogs, match, upsert availability
tvil-fetch enrich [--provider P]...   # ratings + metadata refresh, recompute aggregates
tvil-fetch images                     # download & resize missing artwork
tvil-fetch all                        # sync → enrich → images
tvil-fetch sources list               # table: key, name, active, last run, item count
tvil-fetch review [--list|--resolve ID --title-id T|--create-title|--skip]
tvil-fetch daemon                     # APScheduler loop per [schedule] config
tvil-fetch db upgrade                 # alembic upgrade head (delegates to tvil-core)
```

Exit codes: 0 all ok; 1 fatal; 2 completed with per-source failures (cron-friendly).

## Source plugin framework

One plugin = one module in `tvil_fetcher/sources/`, registered under the entry-point group
`tvil.sources` (so out-of-tree plugins install as separate pip packages with zero core
changes).

```python
# tvil_fetcher/sources/base.py
@dataclass(frozen=True)
class SourceInfo:
    key: str  # "cellcom_tv" — must match config + sources.key
    name: str  # "Cellcom TV"
    kind: SourceKind  # SUBSCRIPTION | FREE | RENT_BUY
    website_url: str


@dataclass(frozen=True)
class RawItem:
    source_key: str  # which source this availability belongs to
    kind: TitleKind  # MOVIE | SERIES
    name: str  # as listed by the source (he or en)
    name_alt: str | None  # second-language name if the source has it
    year: int | None
    tmdb_id: int | None  # provider plugins often have this — matcher fast path
    imdb_id: str | None
    deep_link_url: str | None
    offer_type: OfferType  # STREAM | RENT | BUY | FREE
    poster_url: str | None  # fallback artwork if TMDB has none
    extra: Mapping[str, Any]  # kept verbatim in match_reviews for debugging


class SourcePlugin(ABC):
    def sources(self) -> list[SourceInfo]: ...  # ≥1; tmdb-providers returns many
    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]: ...
```

- `sources()` returning a **list** is deliberate: the `tmdb-providers` harvester is one
  plugin that declares `netflix_il`, `disney_plus_il`, `yes_plus`, … and yields items
  tagged with the right `source_key`, so we don't re-crawl TMDB once per service.
- `FetchContext` provides: the shared rate-limited `httpx` client, structured logger,
  settings for that source (from `tvil.toml`), and a `record_error()` hook that feeds
  `fetch_runs.stats`.
- Plugins are **pure producers**: they never touch the DB. All persistence, matching, and
  sweeping lives in the core pipeline — that's what keeps plugins ~100 lines and testable
  from fixtures alone.

### Configuration (`config/tvil.toml`)

```toml
[sources.cellcom_tv]
enabled = true
rate_limit_rps = 0.5          # optional per-source override

[sources.free_tv]
enabled = false               # retired ⇒ next sync deactivates it (data kept)

[schedule]
sync = "03:00"                # daemon mode; cron users ignore this
enrich = "04:30"
```

## Sync pipeline (per enabled source)

1. **Fetch** — stream `RawItem`s; hard cap on consecutive errors per source (default 25)
   aborts that source only.
2. **Match** — resolve each item to a canonical title (below).
3. **Upsert** — `availability` upserted with `last_seen = run_start`; new titles created
   with whatever metadata the item carried (enrich fills the rest).
4. **Sweep** — after a **successful** fetch: availabilities of this source with
   `last_seen < run_start` get a miss-count bump; at `misses ≥ 2` (two consecutive
   successful syncs without the item) → `is_current = false`, `gone_since = now`.
   Failed/aborted syncs never sweep — a scraper outage must not mass-expire a catalog.
5. **Record** — write `fetch_runs` row. The **<20% guard**: if items_seen < 20% of the
   previous successful run, mark `aborted_suspicious`, skip sweep, keep data (assume layout
   change, not a real catalog purge).

Source-level lifecycle: a configured-off or missing plugin ⇒ `sources.active = false`,
`deactivated_at = now`. Nothing else changes — the API/UI derive the "source no longer
tracked" badge from `active = false` while all availability history remains queryable.

## Title matching (canonicalization)

Order of attempts, first hit wins:

1. **External ID** — `tmdb_id` or `imdb_id` on the RawItem matches an existing title.
2. **TMDB lookup** — search TMDB (`language=he-IL` then `en-US`) by name + year; accept a
   result only when year matches ±1 and normalized-name similarity ≥ 90 (rapidfuzz,
   after stripping punctuation/articles; Hebrew compared to `name_he`, Latin to `name_en`).
   A TMDB hit that resolves to an existing title's `tmdb_id` merges into it.
3. **Local fuzzy** — same similarity test against existing titles (for items TMDB doesn't
   know, e.g. local reality shows).
4. **Unmatched** — if the source is TMDB-less and steps 2–3 fail: create a new local title
   when the item has (name + year + kind) confidence, else park it in `match_reviews`.

Matching MUST be deterministic and logged (`stats.matched_by = {external_id: n, tmdb: n,
fuzzy: n, created: n, review: n}`), so a matching regression is visible in one
`fetch_runs` row diff.

## Image pipeline

- Prefer TMDB artwork (stable CDN, licensing-clean); fall back to `RawItem.poster_url`.
- Variants: poster `w200`, `w500`; backdrop `w1280`. JPEG quality 82, Pillow, EXIF stripped.
- Stored as `data/images/{posters|backdrops}/{title_id}/{variant}.jpg`; DB stores the
  relative path. Idempotent: existing files are skipped unless `--force`.
- Download failures are logged and retried next run — a missing poster never fails a sync
  (UI has a placeholder).

## Failure & retry policy

- Per-request: `tenacity` — 3 attempts, exponential backoff + jitter, honor `Retry-After`.
- Per-source: isolated try/except at the pipeline level; one source's exception → its
  `fetch_runs.status = failed`, others proceed; process exit code 2.
- The whole `sync` is resumable: re-running after a crash is safe (idempotent upserts,
  sweep keyed to run_start of successful runs only).
