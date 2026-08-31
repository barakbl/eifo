# Eifo's menu-bar companion

A dot in the macOS menu bar: **green** when the catalog is well, **orange** when a
source has gone stale or a run failed, **red** when the web server is not
answering, grey when it has not looked yet. Behind it, a menu that runs the
nightly chain, keeps the server up, and does nothing else.

It is a companion to an Eifo checkout, not a copy of one. Every action is the
same `eifo-fetch` command you would type, run against the folder you point it
at, and every reading comes from `GET /api/v1/meta` - the same view the Manage
tab has. Nothing here re-implements a decision the product already makes.

## Build and run

```bash
./build-app.sh            # -> target/Eifo.app
open target/Eifo.app
```

`cargo run` works too, for development. The first launch asks for your Eifo
checkout with the system folder chooser and refuses anything that has no
`packages/eifo-api` in it; the answer is remembered in
`~/Library/Application Support/Eifo/config.json`.

## What the dot means

| Colour | Meaning |
|---|---|
| Green | The server answers and every source still being collected is fresh |
| Orange | The server answers, but a source is stale or its last run failed |
| Red | The server is not answering |
| Grey | Nothing asked yet, or the folder needs attention |

Staleness is not computed here. `/meta` decides it against the deployment's own
`stale_after_hours`, and it already knows that a retired source is not a stale
one - working that out again locally would be a second opinion that could
disagree with the product's.

The dot is drawn as a small round LED - shaded brighter towards the centre, a
darker rim, and a soft highlight - so it reads as a status light rather than a
printed circle.

## The running fetch

The **Fetch** line says what is running now: a phase this app started, or one
started elsewhere - a nightly run from the LaunchAgent, or an `eifo-fetch` typed
in a terminal - identified by the pid in `data/.eifo-fetch.lock`. **Stop the
current fetch** sends its process `SIGTERM` (then `SIGKILL` if it ignores that),
so the fetcher can close the database and release its lock on the way out. A
phase this app started runs in its own process and is watched, not waited on, so
the menu and the Stop item stay responsive for the hours a full run can take.

## Scheduling

The app owns the nightly chain: at the configured hour (03:00 by default) it
runs `eifo-fetch all`. Two things it does that a plain cron entry cannot:

* **Catch-up.** A machine asleep at 03:00 runs on waking rather than skipping
  the night, which is how a catalog goes quietly stale over a fortnight of
  closed lids. At most one run a day, recorded in the config file.
* **Backfills.** A source switched on in the Manage tab is pulled within the
  minute instead of at the next nightly, by watching
  `sources.backfill_requested_at` directly.

**Keep the LaunchAgent.** `~/Library/LaunchAgents/com.eifo.fetch.plist` should
stay exactly as it is. It fires whether or not you are logged in and whether or
not this app is running, and the fetcher's single-flight lock means whichever
gets there first wins while the other stands down. Two schedulers are safe here;
no scheduler is not.

## The server

It answers on `http://localhost:3436` by default - both the menu's **Open Eifo**
and **Open Manage** links and the health poll use that origin, and the server is
started with a matching `--host localhost --port 3436` (`localhost`, not
`127.0.0.1`, so the links open the origin you would actually type). Override it
by setting `base_url` in `config.json`; the host and port the server binds are
parsed back out of whatever you put there.

> Why 3436? No RFC, no committee, no lovingly curated list of "great ports for
> your app". Just a rotary phone and the word `EIFO`: E and F on the 3 (DEF),
> I on the 4 (GHI), O on the 6 (MNO) — dial E-I-F-O, get 3-4-3-6. And if you
> ever figure out where the *name* comes from, you get to enjoy the joke a
> second time. 😉

Started as `.venv/bin/uvicorn` directly, not through `uv run`. `uv run` is a
wrapper that spawns the real server as a child, so stopping the wrapper can
leave the server orphaned and still holding the port - which is exactly the
state that makes "restart it when it is down" restart into a port collision.

Opening Eifo starts the web server if it is not already answering - **Start when
Eifo opens** in the menu, on by default. That is a separate choice from **Restart
if it stops**: you can want the companion to bring the server up on launch
without also wanting it resurrected every time you stop it by hand.

If the server stops answering it is restarted, backing off 2s, 10s, 30s, 120s
and then stopping and saying so. Whatever is wrong at the fourth attempt will
not be fixed by the fortieth, and a restart loop buries the cause under its own
noise.

A server this app started is stopped when the app quits - including when it is
killed. `Drop` alone was not enough: a process terminated by a signal runs no
destructors, so a `kill` used to leave the server running and holding the port,
and the next launch would find something answering, decide all was well, and
report on a process it did not own. There is a signal handler now.

A server it did *not* start is adopted rather than duplicated, and the menu says
so - otherwise **Stop** looks like it would work on it, and it would not.

## Never two fetchers

Before running anything, the app tries the fetcher's own advisory lock on
`data/.eifo-fetch.lock`. Two fetchers would not corrupt anything - SQLite would
see to that - but both would ask every source for the same catalog at the same
time, which is the behaviour a scraper should not exhibit.

The lock is tested by trying to take it, never by trusting the pid in the file:
a stale file with a live pid in it is possible, an unheld `flock` is not.

## Layout

| File | What it holds |
|---|---|
| `config.rs` | The checkout, the schedule, what is remembered |
| `health.rs` | `/meta` → a colour and a sentence |
| `icons.rs` | The dot, drawn rather than shipped |
| `procs.rs` | Starting, stopping, and the single-flight check |
| `schedule.rs` | When tonight's run is owed |
| `worker.rs` | The background half: everything slow |
| `menu.rs` | The menu and the words in it |
| `platform.rs` | About panel, folder chooser, Login Items |
| `main.rs` | The run loop, which does nothing slow |

The main thread owns the run loop, the status item and the menu, and renders
whatever the worker hands it. That split is why the menu still opens instantly
while a two-hour sync is running.

## Native, not imitation

`NSStatusItem` and `NSMenu` via `tray-icon`/`muda`, the standard About panel via
`orderFrontStandardAboutPanelWithOptions:`, `NSOpenPanel` for the folder, and
`SMAppService` for Login Items. `LSUIElement` in the bundle keeps it out of the
Dock and the app switcher, and the activation policy is set in code as well so
`cargo run` behaves like the bundle does.

The dot is deliberately **not** a template image. macOS recolours those to match
the menu bar, which is right for a glyph that means something by its shape and
exactly wrong here, where the colour is the message.

## Caveats

* **Ad-hoc signed.** `build-app.sh` signs the bundle so `SMAppService` accepts
  it. Without a signature the "Open at login" toggle fails rather than silently
  doing nothing - the error is shown in the menu.
* **Changing the folder needs a restart.** The menu writes the new path and says
  so; the worker reads it at startup.
