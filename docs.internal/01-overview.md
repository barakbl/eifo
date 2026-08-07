# 01 — Product Overview

## Vision

One place that answers, for an Israeli viewer: **"What is available to watch right now, on
the services I have, and is it worth my time?"**

The Israeli market is fragmented: yes+ / Sting TV, HOT / NEXT, Cellcom TV, Partner TV, the
international services (Netflix, Disney+, Prime Video, Apple TV+), and the free broadcaster
VODs (Kan, Mako/Keshet 12, Reshet 13, Now 14). No single site shows availability across all
of them together with trustworthy ratings — including *Israeli* ratings, which matter a lot
for local content.

## Goals

1. **Coverage** — track the catalogs of all significant Israeli VOD sources; adding or
   removing a source is configuration + one plugin file, never a rewrite.
2. **Trust** — every title carries IMDb, Rotten Tomatoes, TMDB, and Israeli (Seret, …)
   scores, each linked to its origin page, plus a transparent aggregate score.
3. **Personal** — users log in with Google or X, mark what they watched / want to watch,
   rate titles, and (later) add private notes; profiles are private by default.
4. **Honest data** — when a source disappears or drops a title, the title stays in the DB
   with a clear "no longer available on X" badge, never silently deleted.
5. **Simple to run** — one machine, one SQLite file, one `docker compose up` (or three CLI
   commands); no LLMs, no vector DBs, no message queues.

## Non-goals

- **No playback / hosting of video.** TVIL stores metadata and deep links only. Users are
  sent to the source's own site/app to watch.
- **No LLM features, no vector search, no recommendations engine** (for now). Search is
  plain full-text; discovery is filters + sort by score.
- **No piracy sources.** Only legitimate services are cataloged (see legal notes in
  [03-sources.md](03-sources.md)).
- **No real-time freshness.** Daily sync is the contract; "up and running" means "as of the
  last successful sync, timestamped".
- **No horizontal scale.** Single-node design; SQLite is the default and is sufficient for
  the expected data volume (tens of thousands of titles, thousands of users).

## Users & core flows

| Persona | Flow |
|---|---|
| Anonymous visitor | Search "פאודה" / "Fauda" → see it's on yes+ and Netflix, IMDb 8.4, Seret 8.9 → click through to watch |
| Logged-in user | Set "my services = Netflix + Cellcom TV" → browse only what they can actually watch, sorted by aggregate score → add to want-to-watch |
| Returning user | Open "My list" → mark an item watched, rate it 8/10, add a note (later stage) |
| Curious friend (later) | Open a user's *public* profile → see their ratings and lists |

## Product principles

- **Hebrew-first, bilingual.** Titles have Hebrew and English names; UI supports RTL and
  an He/En toggle. Search matches both languages.
- **Ratings are attributed.** A score is never shown without its provider name and a link
  to the provider's page for that title.
- **State the staleness.** Every availability fact shows when it was last verified.
- **Boring technology.** Python + FastAPI + SQLAlchemy + SQLite + static HTML/JS. Every
  dependency must earn its place.
