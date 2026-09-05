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

When nothing is running, that line says how the last run went instead - the
question at nine in the morning is not "is anything running" but "did the night
go well".

### Progress

A full run takes hours; one source alone can take fifty minutes. "Running" is
not an answer for that long, so the **Progress** submenu is the run itself:

```
Started 13:47 · 2h 25m so far
✓  Netflix - 3,204 items, 51 new (1h 3m)
✓  Prime Video - 2,110 items, 4 retired (21m)
✕  Reshet 13 - failed (1s)
▶  FreeTV - 12m so far
·  Israel Film Archive (Jerusalem)
·  Kan Box (Kan 11)
·  Mako VOD (Keshet 12)
```

Done, doing, and to come, in the order it happened, with what each source
actually found. The headline says the same thing in one line - `Running sync ·
4 of 14 services` - and the second line names what it is on right now.

None of it is computed twice. The fetcher opens a `fetch_runs` row when a phase
starts and closes it when it ends (`eifo_fetcher.runs`), so the run log is a
live account of a run rather than a report filed afterwards. This reads it
straight from SQLite, read-only, the same way the backfill check does and for
the same reason: the endpoint that would answer is behind the admin session this
app does not have.

**What is still to come is the one thing nobody wrote down.** Sources are synced
in the order the plugins declare them, which lives in Python. But that order is
the same every night, so the last time each source ran says exactly where it
comes in the queue: sorting the ones this run has not reached by their previous
run's start time reproduces the order, and a source that has never run has
nothing to sort by and goes last. The queue is shown only while something is
actually running - a `sync --source` touches one service and stops, and listing
the other thirteen underneath it would be a run this app invented.

A row still marked `running` with no fetcher holding the lock is one whose
process died. The fetcher says so itself, but only when it next starts; the menu
says it immediately, rather than insisting for three hours that FreeTV is still
going.

When a run ends - whoever started it - a notification says how it went. The
whole point of a two-hour sweep in the background is that somebody is doing
something else.

## One service at a time

**Sync one service** lists every service the fetcher would actually collect,
each with how old its catalog is:

```
Kan Box (Kan 11) - synced 3h ago
Mako VOD (Keshet 12) - synced 2d ago
Reshet 13 - never synced
```

Clicking one runs `eifo-fetch sync --source KEY`, which is the command that was
always there - the menu was just the one place that could only do all of them.
A service that has come back after a morning of failing is a click, not a
two-hour sweep of the other thirteen.

Only services that are switched on are listed. `sync --source` on one that is
off syncs nothing and says so in a log nobody is reading, and a menu item that
quietly does nothing is worse than one that is not there. "Switched on" is the
operator's answer from the Manage tab where there is one and the plugin's own
default where there is not - the same rule the fetcher applies.

**Download artwork now** is the third verb, `eifo-fetch images`. The menu could
already sync and enrich; the pass that fetches missing posters was the one thing
it could not reach.

## A members-only catalog

`EIFO_MEMBERS_ONLY` closes the catalog to anybody who is not signed in, and
`/api/v1/meta` - the one thing this app reads - is part of what closes. Without
a token it gets a 401 and can say only that the catalog is private.

The dot goes **amber**, not red. A refusal is an answer: the server is up, it
understood the question and declined it, which is nothing like a dead port.
Reading every error as "not answering" is what the first version of this did,
and it sent somebody looking for a crashed process that had been serving
perfectly well the whole time.

To give it the key: create a token in the web app's **Settings**, copy it, and
choose **Paste API token from clipboard**. The clipboard rather than a text
field, because the token is shown once, beside a sentence telling you to copy it
now - asking for it to be pasted into a second box is asking somebody to do the
thing they have just done.

**It goes in the Keychain, not in `config.json`.** Everything else this app
remembers is a preference and a JSON file is right for those. A token is a
credential that reads a private catalog, and a credential in a plaintext file is
readable by every process running as you, backed up in the clear, and sitting in
a folder people open to see what an app remembers. It is reached through the
Security framework rather than by running `/usr/bin/security`, because the CLI
takes the password as a command-line argument and arguments are visible in `ps`.

The menu says which state it is in, because the alternative did not:

```
API token: none                          <- greyed "Forget" now has a reason
Paste API token from clipboard
Forget API token                         (greyed)
```

```
API token: in your Keychain · revoke it in Settings
Replace API token from clipboard
Forget API token
```

Look for **Eifo / api-token** in Keychain Access. **Forget** puts down this
app's copy and nothing more - the token goes on working for anything else
holding one until it is revoked in Settings, which is why the item does not say
"Delete". A menu item should not promise something it cannot do.

Two refusals, and they need different sentences. No token says to paste one;
a token that was refused says it may have been revoked - because being told to
paste a token you have already pasted is the kind of advice that makes somebody
distrust the rest of the menu.

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

## Updates

Twice a day - and once a few seconds after launch - the app asks GitHub for the
latest release. "Latest" is compared against the tag the checkout is sitting on
(`git tag --points-at HEAD`), not the version this binary was built as: the
companion's job is to keep the *checkout* current, and right after an update it
is a new checkout that a still-running old binary reports on until it relaunches.

When there is a newer release, a notification says so once (not once per check),
and the menu's **Check for updates** line becomes **Update to v0.3.0…**. Clicking
it runs [`update.sh`](update.sh) against the checkout - `git fetch` the tag,
`git checkout`, `uv sync`, `eifo-fetch db upgrade`, rebuild `Eifo.app` beside the
running one and swap it in - then the app quits and a small detached shell waits
for it to exit and reopens the fresh bundle. Every line of that is a command a
person updating by hand would run.

The script is baked into the binary with `include_str!`, so the copy that runs
is never the one being checked out from under it. Its output goes to
`$TMPDIR/eifo-update.log`; on a failure the last few lines are what the menu
shows.

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
| `keychain.rs` | The API token, kept where a credential belongs |
| `runs.rs` | The run log → what is done, going, and still to come |
| `icons.rs` | The dot, drawn rather than shipped |
| `procs.rs` | Starting, stopping, and the single-flight check |
| `schedule.rs` | When tonight's run is owed |
| `update.rs` | Is there a newer release, and which one is this |
| `worker.rs` | The background half: everything slow |
| `menu.rs` | The menu and the words in it |
| `platform.rs` | About panel, folder chooser, Login Items, notifications |
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
* **The queue is a prediction, not a promise.** It is read off the order the
  last run took, so the first run after a plugin is added has one source in the
  wrong place until that run finishes. The rows already done are never a
  prediction - those are what happened.
