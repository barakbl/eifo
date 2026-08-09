<div align="center">

# TVIL

### Which service is showing the thing I want to watch?

That's the whole idea. You have a film or a series in mind, or you just want something
good tonight — TVIL tells you where in Israel you can actually watch it, and whether it's
worth your evening.

[![CI](https://github.com/barakbl/tvil/actions/workflows/ci.yml/badge.svg)](https://github.com/barakbl/tvil/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-690%20passing-brightgreen)](https://github.com/barakbl/tvil/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

</div>

---

> [!IMPORTANT]
> **This was built for personal use.** It is a hobby project that scratches one person's
> itch, published in case it scratches yours. It is not a product, it carries no warranty,
> and nobody is on call for it.
>
> **This project is not affiliated with, endorsed by, or connected to any of the services
> it lists or any of the data providers it reads.** Every name and trademark belongs to its
> owner.
>
> **Anyone running it is responsible for their own compliance.** Your use of the data it
> retrieves is governed by the terms of the services it comes from — TMDB, JustWatch, IMDb,
> Rotten Tomatoes, Seret and each streaming service. Read them. See
> [Data, attribution and fair use](#data-attribution-and-fair-use).

---

## What it does

Israeli streaming is split across a dozen services, and no two of them agree on what they
carry. TVIL collects their catalogs into one place, enriches every title with the ratings
people actually trust, and puts a search box in front of it.

- **One catalog, every service.** yes+, Sting TV, HOT, Cellcom TV, Partner TV, the
  broadcaster VODs, and the Israeli catalogs of Netflix, Disney+, Prime Video and Apple TV+.
- **Filter by what you already pay for.** Tick your services once; the catalog narrows to
  what you can watch tonight without buying anything new. "Everything" stays one tap away.
- **Ratings worth trusting, side by side.** IMDb, Rotten Tomatoes (critics and audience),
  TMDB and the Israeli site Seret — each with a link back to where it came from, plus a
  weighted aggregate that **shows its working** rather than asking you to take a number on
  faith. Israeli titles get a separate Israeli score, because local critics and global
  audiences rarely rate the same film the same way.
- **Bilingual search that actually works.** Hebrew and English over one FTS5 index, with
  prefix matching as you type. "פאודה" and "Fauda" find the same series.
- **It remembers what left.** A title that vanished from a service is badged, not deleted —
  and a service TVIL no longer tracks is badged differently, because "it's gone" and "we
  can't vouch for this any more" are different facts.
- **Personal lists.** Sign in with Google or X to keep watched / want-to-watch lists, rate
  1–10, and write private notes. Notes are private always, including on a public profile.
- **Private by default.** New accounts are invisible. Going public is an explicit choice
  with copy that spells out exactly what becomes visible — and your email, sign-in identity,
  notes and chosen services never do.
- **Runs on a Raspberry Pi.** One SQLite file, one container, no cluster, no queue, no
  external observability stack.

Accounts are optional. Leave the OAuth credentials unset and TVIL serves the catalog to
everyone with no sign-in at all.

## Coverage

Which services TVIL can read, and how. **✅** has its own catalog surface and per-title
data; **◐** is tracked but with a caveat (coverage or freshness); **❌** has a real catalog
but no surface an honest client can reach without a paying subscriber's login — so TVIL
leaves it alone rather than spoof a browser or ride someone's subscription
([why](#data-attribution-and-fair-use)).

| Service | Supported | Plugin | Notes |
|---|---|---|---|
| **FreeTV** | ✅ Yes | `freetv` | Public RedGalaxy JSON API — no key. The largest source. `platform=BROWSER` is an undocumented convention, so a portal change could break it. |
| **Disney+** (IL) | ✅ Yes | `disney_plus` | Reads Disney's public per-region sitemaps — no key. (Absent from JustWatch's IL data, so it needs its own plugin.) |
| **Netflix** (IL) | ✅ Yes | `tmdb-providers` | Availability from JustWatch via the [TMDB API](#install) — **free key required**. The "watch" link goes to TMDB's per-title page, not the service's player; refreshed daily. |
| **Prime Video** (IL) | ✅ Yes | `tmdb-providers` | As Netflix — JustWatch via TMDB, free key. |
| **Apple TV** (IL) | ✅ Yes | `tmdb-providers` | As Netflix — JustWatch via TMDB, free key. |
| **HBO Max** (IL) | ✅ Yes | `tmdb-providers` | As Netflix — JustWatch via TMDB, free key. |
| **MUBI** (IL) | ✅ Yes | `tmdb-providers` | As Netflix — JustWatch via TMDB, free key. Films only. |
| **Crunchyroll** (IL) | ✅ Yes | `tmdb-providers` | As Netflix — JustWatch via TMDB, free key. |
| **Mako VOD** (Keshet 12) | ◐ Partial | `mako` | Free broadcaster VOD. **Scrapes** the site's embedded catalog data (no key) — may break if Mako changes its page. Series/programmes only, no films. |
| **Cellcom TV** | ❌ No | — | Real VOD library, but only inside the subscriber app — no honest public surface. |
| **HOT / NEXT** | ❌ No | — | Catalog API is reachable, but returns nothing without a paying subscriber's credential. |
| **yes+** | ❌ No | — | Host resets honest clients at the TLS handshake, and the catalog needs subscriber auth. |
| **Sting+** | ❌ No | — | Sibling of yes+ — same wall. |
| **Partner TV** | ❌ No | — | App host is closed to honest clients; the web player is login-gated. |
| **Kan 11 · Reshet 13** | ❌ No | — | Free public-broadcaster VODs, but both serve a bot-check (403) to non-browser clients, and TVIL won't spoof a browser to get past it. |

Ratings come from IMDb (datasets), TMDB, Rotten Tomatoes and Seret — the last two by scraping,
so they can lag or break when those sites change. Adding a service is a ~100-line plugin:
see [Hack on it](#hack-on-it).

## Install

You need a free [TMDB API key](https://www.themoviedb.org/settings/api). Everything else is
optional.

### With Docker (recommended)

```bash
git clone https://github.com/barakbl/tvil.git && cd tvil
cp config/tvil.example.toml config/tvil.toml
cp .env.example .env          # fill in TVIL_TMDB_API_KEY
docker compose up -d
```

Then fill the catalog — the first run takes a while, and is the only slow part:

```bash
docker compose exec api tvil-fetch all
```

Open <http://localhost:8000>.

### With uv, no container

```bash
git clone https://github.com/barakbl/tvil.git && cd tvil
uv sync
cp config/tvil.example.toml config/tvil.toml
cp .env.example .env          # fill in TVIL_TMDB_API_KEY

uv run tvil-fetch db upgrade  # create the schema
uv run tvil-fetch all         # fill the catalog
uv run uvicorn tvil_api.main:app --reload
```

Same address. The client is static files served by the same process — there is no build
step and nothing to compile.

### Keeping it fresh

Catalogs move daily. The Docker setup includes a `fetcher` service running the bundled
daemon, so it keeps itself current with nothing further from you. Running without a
container, the same thing:

```bash
uv run tvil-fetch daemon      # sync 03:00, enrich 04:30, artwork 05:30 UTC
```

Or skip the long-running process and drive the phases from cron:

```bash
uv run tvil-fetch sync        # catalogs
uv run tvil-fetch enrich      # ratings and metadata
uv run tvil-fetch images      # posters and backdrops
```

Times come from `[schedule]` in `config/tvil.toml`. `tvil-fetch all` runs all three once.

### Turning on sign-in

Only needed if you want lists and ratings.

1. Create an OAuth client — [Google](https://console.cloud.google.com/apis/credentials)
   (Web application) and/or [X](https://developer.x.com/).
2. Register the callback: `https://your-host/api/v1/auth/callback/google` (and `/x`).
   `http://localhost:8000/...` works for local development.
3. Put the credentials plus a session secret in `.env`:

```bash
TVIL_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
TVIL_GOOGLE_CLIENT_ID=…
TVIL_GOOGLE_CLIENT_SECRET=…
```

Restart. A sign-in button appears for each provider you configured, and for no others.

Every setting, with its default, is in `config/tvil.example.toml` and `.env.example`.

## How it fits together

```
  streaming services ──► tvil-fetcher ──► SQLite ◄── tvil-api ──► web client
   ratings providers        plugins                                (no build)
```

| Path | What it is |
|---|---|
| `packages/tvil-core` | Settings, SQLAlchemy schema, Alembic migrations — the only contract between the services |
| `packages/tvil-fetcher` | `tvil-fetch` CLI: catalog sync, enrichment, artwork |
| `packages/tvil-api` | FastAPI REST service; also serves the client and the images |
| `web/` | Static HTML + JS client, vanilla ES modules |

Three deliberate choices, in case you were about to ask:

- **SQLite, not Postgres.** One writer, a few thousand readers a day, and a backup that is
  a file copy. Moving is explicitly not a goal.
- **No frontend framework.** No build step means the client is auditable, fast, and
  wrappable by Capacitor without a toolchain.
- **A missing catalog is never a mass deletion.** A title has to be absent from two
  consecutive *successful* syncs before it is marked gone, and a sync returning far fewer
  items than last time aborts as suspicious instead of wiping a service clean.

## Hack on it

**Pull requests are welcome, and new sources are the most useful thing you can add.**
Israel has more VOD services than this repo tracks, and every one of them is a plugin of
about a hundred lines.

### Adding a source

A plugin is a **pure producer**: it declares which services it covers and yields listings.
It never touches the database — matching, the availability sweep, artwork and the API all
happen downstream, which is what keeps a plugin small enough to test entirely from a
recorded fixture.

```python
class MyServicePlugin(SourcePlugin):
    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key="my_service",
                name="My Service",
                kind=SourceKind.SUBSCRIPTION,
                website_url="https://my.service.co.il",
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        for listing in ctx.http.get_json(CATALOG_URL)["items"]:
            yield RawItem(
                source_key="my_service",
                kind=TitleKind.MOVIE,
                name=listing["title"],
                year=listing.get("year"),
                deep_link_url=listing.get("url"),
            )
```

Start from `sources/mako.py` (a scraper) or `sources/tmdb_providers.py` (an API client
covering several services at once). Register it in `registry.py` — **or don't**: plugins
are also discovered through the `tvil.sources` entry-point group, so a source can live in
its own repository and install as an ordinary pip package with no change to this codebase.

The pipeline handles everything after the yield: matching a listing to a canonical title,
parking the ambiguous ones for review, expiring what disappeared, and downloading artwork.

### Adding a ratings provider

Same shape, in `enrichers/`: add a `RatingProvider` enum member, give it a weight under
`[scores.weights]` in the config, and write the enricher. Normalisation to 0–100 and the
weighted aggregate are already handled.

### Other good first issues

Episode-level tracking for series, price tracking for rent/buy offers, CSV or Letterboxd
import, notifications when a want-to-watch title lands on a service you have — the data
model already supports the last one.

### House rules

Everything is tested, linted and typed; CI enforces all three, and the coverage gates
(90% for `tvil-core`, 85% elsewhere) are not negotiable.

```bash
uv run pytest                                        # the Python suite
node --test "web/tests/*.test.js"                    # the client's logic
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pre-commit install                            # run all of it before each commit
```

**No test may touch the network.** Every parser is tested against a recorded fixture, so
the suite runs offline and a provider changing its HTML shows up as a failing test rather
than as silently missing data.

Small modules with one job, public functions typed and docstringed, comments that explain
*why* rather than narrate the code, and Conventional Commits.

## Data, attribution and fair use

TVIL displays data it does not own, and the licence in this repository covers the **code
only**. If you run your own instance, the obligations are yours:

- **TMDB** — metadata and artwork. Requires your own API key and carries an attribution
  requirement. This product uses the TMDB API but is not endorsed or certified by TMDB.
- **JustWatch** — streaming availability, reached via TMDB's provider data, with an
  attribution requirement.
- **IMDb** — ratings come from the official datasets, which are licensed for
  **personal and non-commercial use only**. No scraping.
- **Rotten Tomatoes**, **Seret** and the streaming services — scraped politely: one
  identifying user agent, `robots.txt` honoured, rate-limited, and no catalog is ever
  redistributed as a bulk download.

The API serves these credits from `GET /api/v1/meta` and the client displays them. **Do
not remove them.** If you intend to run this commercially, don't — the data licences above
do not permit it.

## Licence

[MIT](LICENSE) for the code. The data is not mine to license — see the section above.
