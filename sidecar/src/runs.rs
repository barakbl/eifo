//! What the fetcher is doing, has done, and has not got to yet.
//!
//! A full run takes hours - the last one here spent fifty-two minutes on one
//! source alone - and until this existed the menu had one word for all of it:
//! "running". Whoever was watching had no way to tell a sync working through
//! FreeTV from a sync wedged on it, no way to know whether Kan had already been
//! done, and no way to see that Reshet 13 had failed forty minutes ago. That is
//! a long time to sit in the dark next to a machine that knows.
//!
//! It knows because the fetcher writes it down. A `fetch_runs` row is opened
//! when a phase starts and closed when it ends (`eifo_fetcher.runs`), so the
//! run log is a live account of the run rather than a report filed afterwards.
//! This reads it the same way `pending_backfills` reads the backfill flags:
//! read-only, straight from SQLite, because the endpoint that would answer is
//! behind the admin session this app does not have.
//!
//! Three questions, and the third is the only hard one:
//!
//! * **What is running now** is the row whose status is still `running`.
//! * **What is done** is the rest of this run's rows, with what each found.
//! * **What is next** is not written down anywhere. The order sources are
//!   synced in is the order the plugins declare them, which lives in Python and
//!   changes only when somebody adds a plugin - so the last time each source
//!   ran is a faithful record of where it comes in the queue. Sorting the
//!   sources this run has not reached by their previous run's start time
//!   reproduces the order exactly, and a source that has never run has nothing
//!   to sort by and goes last.

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Local, NaiveDateTime, TimeZone, Utc};
use rusqlite::{Connection, OpenFlags};

use crate::config::Config;
use crate::health::thousands;

/// How long a pause may be before it is a different run rather than a gap
/// between two phases of this one.
///
/// The phases of a run follow each other within a second or two, but not
/// always: recomputing every aggregate after the IMDb pass, or a dedupe sweep,
/// happens between rows and takes as long as the catalog is big. Ten minutes
/// covers that and still separates the nightly run from anything anybody runs
/// by hand hours later.
const SAME_RUN_GAP_SECONDS: i64 = 10 * 60;

/// How many rows to read. A run of a large catalog is around twenty; this is
/// several runs' worth, and the walk stops at the first row of the current one.
const ROWS_READ: usize = 200;

/// The fetcher's own words for how a run ended.
const STATUS_RUNNING: &str = "running";
const STATUS_OK: &str = "ok";
const STATUS_CRASHED: &str = "crashed";
const STATUS_SUSPICIOUS: &str = "aborted_suspicious";

/// Source keys the fetcher records bulk passes under. They are not catalogs -
/// there is no `sources` row for them - but they are long jobs that can fail on
/// their own, so they get a row and therefore need a name.
const IMDB_KEY: &str = "imdb";
const SERET_INDEX_KEY: &str = "seret-index";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepState {
    Running,
    Ok,
    Failed,
    /// The volume guard tripped: far fewer items than last time, so the sync
    /// was treated as a broken parser rather than as a mass removal.
    Suspicious,
    /// Found still open by a later fetcher - the process that opened it died
    /// without saying how it went.
    Crashed,
}

impl StepState {
    fn read(value: &str) -> Self {
        match value {
            STATUS_RUNNING => StepState::Running,
            STATUS_OK => StepState::Ok,
            STATUS_CRASHED => StepState::Crashed,
            STATUS_SUSPICIOUS => StepState::Suspicious,
            _ => StepState::Failed,
        }
    }

    /// The mark down the left of the progress list.
    ///
    /// One column of glyphs rather than words, so the shape of the run is
    /// readable down the edge of the menu without reading any of it.
    pub fn mark(self) -> &'static str {
        match self {
            StepState::Running => "▶",
            StepState::Ok => "✓",
            StepState::Failed => "✕",
            StepState::Suspicious => "⚠",
            StepState::Crashed => "⚠",
        }
    }

    pub fn is_failure(self) -> bool {
        !matches!(self, StepState::Running | StepState::Ok)
    }
}

/// One `fetch_runs` row, as the menu says it.
#[derive(Debug, Clone)]
pub struct Step {
    /// The source key, for matching against the roster. `None` for a pass that
    /// is not about one source.
    pub key: Option<String>,
    /// What to call it: the source's own name, or the name of the bulk pass.
    pub name: String,
    pub phase: String,
    pub state: StepState,
    pub started_at: DateTime<Utc>,
    pub finished_at: Option<DateTime<Utc>>,
    stats: serde_json::Value,
}

impl Step {
    pub fn is_sync(&self) -> bool {
        self.phase == "sync"
    }

    /// What to call this step's phase in a sentence.
    ///
    /// The stored value is a column value - `images` - and the menu is a
    /// sentence. It matters for a run this app did not start: those are named
    /// by the row they opened, and a nightly fired by the LaunchAgent should
    /// read the same as one started from this menu.
    pub fn phase_label(&self) -> String {
        match self.phase.as_str() {
            "images" => "the artwork pass".into(),
            other => other.into(),
        }
    }

    /// How long it ran, or has been running.
    pub fn seconds(&self, now: DateTime<Utc>) -> i64 {
        let end = self.finished_at.unwrap_or(now);
        (end - self.started_at).num_seconds().max(0)
    }

    /// What it found, in the numbers that phase is read for.
    ///
    /// Different per phase because the phases do different work: a sync is read
    /// for how much of a catalog it saw, an enrich for how many ratings it
    /// wrote, an artwork pass for how many posters came down. One shared shape
    /// would be a row of zeroes with the useful number hidden among them.
    pub fn found(&self) -> String {
        let n = |field: &str| self.stats.get(field).and_then(|v| v.as_i64()).unwrap_or(0);
        let phrase = match (self.phase.as_str(), self.key.as_deref()) {
            ("sync", _) => tally(&[
                ("items", n("items_seen")),
                ("new", n("titles_created")),
                ("retired", n("retired")),
            ]),
            ("enrich", Some(IMDB_KEY)) => {
                tally(&[("matched", n("matched")), ("updated", n("updated"))])
            }
            ("enrich", Some(SERET_INDEX_KEY)) => tally(&[
                ("pages read", n("fetched")),
                ("still to read", n("remaining")),
            ]),
            ("enrich", _) => tally(&[
                ("titles", n("titles_seen")),
                ("ratings", n("ratings_written")),
            ]),
            ("images", _) => tally(&[("downloaded", n("downloaded")), ("failed", n("failed"))]),
            _ => String::new(),
        };
        // A count on its own is the wrong thing to print about a run that went
        // wrong. "12 items" beside a warning triangle reads as a small night;
        // what happened is that the volume guard threw the sync away, and the
        // 12 is the evidence for it rather than the result of it. A source that
        // failed on its first request has no count at all, and "0 items" beside
        // a cross is noise - the reason is on the run's row in the Runs tab.
        match self.state {
            StepState::Running | StepState::Ok => phrase,
            StepState::Crashed => "did not finish".into(),
            StepState::Suspicious if phrase.is_empty() => "too few items to apply".into(),
            StepState::Suspicious => format!("{phrase}, too few to apply"),
            StepState::Failed if phrase.is_empty() => "failed".into(),
            StepState::Failed => format!("failed after {phrase}"),
        }
    }

    /// The line this step gets in the progress list.
    pub fn line(&self, now: DateTime<Utc>) -> String {
        let mark = self.state.mark();
        if self.state == StepState::Running {
            return format!(
                "{mark}  {} - {} so far",
                self.name,
                compact_duration(self.seconds(now))
            );
        }
        // A row nobody ever closed has no length worth printing: the time it
        // stopped is exactly what was not written down.
        if self.finished_at.is_none() {
            return format!("{mark}  {} - {}", self.name, self.found());
        }
        let found = self.found();
        let took = compact_duration(self.seconds(now));
        if found.is_empty() {
            format!("{mark}  {} - {took}", self.name)
        } else {
            format!("{mark}  {} - {found} ({took})", self.name)
        }
    }
}

/// A source the menu can offer to sync on its own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceOption {
    pub key: String,
    pub name: String,
    /// Whether the fetcher would actually collect it. A source that is switched
    /// off is not offered: `eifo-fetch sync --source` refuses one, and a menu
    /// item that quietly does nothing is worse than one that is not there.
    pub on: bool,
    /// When its catalog was last confirmed, for saying how old it is.
    pub last_ok: Option<DateTime<Utc>>,
}

impl SourceOption {
    /// "Netflix - synced 2h ago", the label on its item in the Sync one submenu.
    pub fn label(&self, now: DateTime<Utc>) -> String {
        match self.last_ok {
            Some(at) => format!("{} - synced {}", self.name, ago(at, now)),
            None => format!("{} - never synced", self.name),
        }
    }
}

/// The run log, as much of it as the menu has any use for.
#[derive(Debug, Clone, Default)]
pub struct RunView {
    /// This run's rows, oldest first. Empty when nothing has ever run.
    pub steps: Vec<Step>,
    /// Sources this run has not reached yet, in the order it will take them.
    pub waiting: Vec<String>,
    /// Every source, for the menu that offers them one at a time.
    pub sources: Vec<SourceOption>,
}

impl RunView {
    pub fn current(&self) -> Option<&Step> {
        self.steps.iter().find(|s| s.state == StepState::Running)
    }

    /// When this run began - the first row of it.
    pub fn started_at(&self) -> Option<DateTime<Utc>> {
        self.steps.first().map(|s| s.started_at)
    }

    /// When the last thing in it ended, if everything has.
    pub fn finished_at(&self) -> Option<DateTime<Utc>> {
        if self.current().is_some() {
            return None;
        }
        self.steps.iter().filter_map(|s| s.finished_at).max()
    }

    /// Services swept so far and how many there are, while a sweep is on.
    ///
    /// `None` at every other moment, and deliberately: once a run is over there
    /// is nothing left to come, whatever the roster says about a service that
    /// never ran, and during the enrich phase "14 of 14 services" is a true
    /// sentence about a thing that is no longer happening. A fraction is only
    /// worth showing while it is still moving.
    pub fn sync_position(&self) -> Option<(usize, usize)> {
        let current = self.current()?;
        if !current.is_sync() {
            return None;
        }
        let done = self.steps.iter().filter(|s| s.is_sync()).count() - 1;
        Some((done, done + 1 + self.waiting.len()))
    }

    pub fn failures(&self) -> Vec<&Step> {
        self.steps.iter().filter(|s| s.state.is_failure()).collect()
    }

    /// Treat a row still marked running as the abandoned row it is.
    ///
    /// Called when no fetcher holds the lock: a phase records itself as running
    /// when it starts and closes the row when it ends, so a row still open with
    /// nothing running belongs to a process that died - an OOM, a power cut, a
    /// Stop from this very menu. The fetcher says the same thing itself, but
    /// only when it next starts (`close_abandoned_runs`), and "Now: FreeTV,
    /// three hours in" is a poor thing for the menu to insist on until then.
    pub fn forget_abandoned(&mut self) {
        for step in &mut self.steps {
            if step.state == StepState::Running {
                step.state = StepState::Crashed;
            }
        }
    }

    /// What the run has amounted to: "12 services synced, 1 failed".
    ///
    /// Counted rather than listed, because this is the line above the list -
    /// which names every one of them.
    pub fn did(&self) -> String {
        let synced = self.steps.iter().filter(|s| s.is_sync()).count();
        let failed = self.failures().len();
        match (synced, failed) {
            (0, 0) => "nothing yet".into(),
            (0, f) => format!("{f} failed"),
            (n, 0) => format!("{n} service{} synced", plural(n)),
            (n, f) => format!("{n} service{} synced, {f} failed", plural(n)),
        }
    }

    /// One sentence about a run that has ended, for the menu and the banner.
    /// `None` while it is still going, which is not an outcome yet.
    pub fn outcome(&self) -> Option<String> {
        let finished = self.finished_at()?;
        Some(format!("finished at {} · {}", clock(finished), self.did()))
    }
}

#[cfg(test)]
impl Step {
    /// A step built by hand, for the tests of everything that renders one.
    pub fn sample(
        name: &str,
        phase: &str,
        state: StepState,
        started: &str,
        finished: Option<&str>,
    ) -> Self {
        Step {
            key: Some(name.to_lowercase().replace(' ', "_")),
            name: name.into(),
            phase: phase.into(),
            state,
            started_at: timestamp(started).expect("a test timestamp"),
            finished_at: finished.and_then(timestamp),
            stats: serde_json::json!({"items_seen": 100}),
        }
    }
}

/// Read the run log. Returns an empty view rather than failing: the menu can
/// say less, and a companion that shows nothing because a query failed would be
/// less use than one that shows what it has.
pub fn read(config: &Config) -> RunView {
    let path = config.database();
    if !path.exists() {
        return RunView::default();
    }
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI;
    let Ok(db) = Connection::open_with_flags(&path, flags) else {
        return RunView::default();
    };

    let names = source_names(&db);
    let last_sync = last_sync_by_source(&db);
    let sources = sources(&db, &last_sync);
    let steps = current_run(rows(&db), &names);
    let waiting = still_to_come(&steps, &sources, &last_sync);

    RunView {
        steps,
        waiting,
        sources,
    }
}

/// One `fetch_runs` row, before it is decided which run it belongs to.
struct Row {
    key: Option<String>,
    phase: String,
    state: StepState,
    started_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
    stats: serde_json::Value,
}

impl Row {
    /// The moment the next row would have started from, had it been part of the
    /// same run.
    ///
    /// Its end, except for a crashed row - whose `finished_at` was written by
    /// whichever fetcher came along afterwards and found it open, and so is a
    /// timestamp from the *next* run rather than this one. Using it would glue
    /// yesterday's abandoned row onto today's first source.
    fn ends_at(&self) -> DateTime<Utc> {
        match self.state {
            StepState::Crashed => self.started_at,
            _ => self.finished_at.unwrap_or(self.started_at),
        }
    }
}

fn rows(db: &Connection) -> Vec<Row> {
    let sql = "SELECT source_key, phase, status, started_at, finished_at, stats \
               FROM fetch_runs ORDER BY started_at DESC, id DESC LIMIT ?1";
    let Ok(mut statement) = db.prepare(sql) else {
        return Vec::new();
    };
    let read = statement.query_map([ROWS_READ], |row| {
        Ok(Row {
            key: row.get::<_, Option<String>>(0)?,
            phase: row.get::<_, String>(1)?,
            state: StepState::read(&row.get::<_, String>(2)?),
            started_at: timestamp(&row.get::<_, String>(3)?).unwrap_or_else(Utc::now),
            finished_at: row
                .get::<_, Option<String>>(4)?
                .and_then(|value| timestamp(&value)),
            stats: row
                .get::<_, Option<String>>(5)?
                .and_then(|raw| serde_json::from_str(&raw).ok())
                .unwrap_or(serde_json::Value::Null),
        })
    });
    match read {
        Ok(mapped) => mapped.filter_map(Result::ok).collect(),
        Err(_) => Vec::new(),
    }
}

/// Take the rows belonging to the newest run, oldest first.
///
/// Walked backwards from the newest row while each row leads into the next.
/// Two things end the walk: a pause longer than a run's phases ever leave
/// between them, and a source appearing twice - one sweep syncs each source
/// once, so a repeat is the previous sweep showing through.
fn current_run(rows: Vec<Row>, names: &HashMap<String, String>) -> Vec<Step> {
    let mut taken: Vec<Row> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for row in rows {
        if let Some(previous) = taken.last() {
            if (previous.started_at - row.ends_at()).num_seconds() > SAME_RUN_GAP_SECONDS {
                break;
            }
        }
        if row.phase == "sync" {
            if let Some(key) = &row.key {
                if !seen.insert(key.clone()) {
                    break;
                }
            }
        }
        taken.push(row);
    }

    taken.reverse();
    taken
        .into_iter()
        .map(|row| Step {
            name: step_name(&row.phase, row.key.as_deref(), names),
            key: row.key,
            phase: row.phase,
            state: row.state,
            started_at: row.started_at,
            finished_at: row.finished_at,
            stats: row.stats,
        })
        .collect()
}

/// What to call a row in the menu.
///
/// A source by its own name, so the menu reads the way the Manage tab does. The
/// passes that are not sources are named for what they do rather than for the
/// key they are filed under: "imdb" is a row in a table, "IMDb ratings" is what
/// is happening.
fn step_name(phase: &str, key: Option<&str>, names: &HashMap<String, String>) -> String {
    match (phase, key) {
        ("enrich", Some(IMDB_KEY)) => "IMDb ratings".into(),
        ("enrich", Some(SERET_INDEX_KEY)) => "Seret index".into(),
        ("enrich", None) => "Ratings and metadata".into(),
        ("images", _) => "Artwork".into(),
        (_, Some(key)) => names.get(key).cloned().unwrap_or_else(|| key.to_string()),
        (phase, None) => phase.to_string(),
    }
}

/// The sources this run still has to reach, in the order it will take them.
fn still_to_come(
    steps: &[Step],
    sources: &[SourceOption],
    last_sync: &HashMap<String, LastSync>,
) -> Vec<String> {
    let done: HashSet<&str> = steps
        .iter()
        .filter(|s| s.is_sync())
        .filter_map(|s| s.key.as_deref())
        .collect();

    let mut waiting: Vec<&SourceOption> = sources
        .iter()
        .filter(|source| source.on && !done.contains(source.key.as_str()))
        .collect();

    // By where each was in the last run: the fetcher takes its sources in the
    // order the plugins declare them, which is the same order every night, so
    // the previous run is a faithful record of the queue. One that has never
    // run has nothing to sort by, and goes last under its own name.
    waiting.sort_by(|a, b| {
        let position = |source: &SourceOption| {
            last_sync
                .get(&source.key)
                .map(|last| last.started_at)
                .unwrap_or(DateTime::<Utc>::MAX_UTC)
        };
        position(a)
            .cmp(&position(b))
            .then_with(|| a.name.cmp(&b.name))
    });

    waiting.into_iter().map(|s| s.name.clone()).collect()
}

/// When a source last ran, and when it last ran successfully.
struct LastSync {
    started_at: DateTime<Utc>,
    ok_at: Option<DateTime<Utc>>,
}

fn last_sync_by_source(db: &Connection) -> HashMap<String, LastSync> {
    let sql = "SELECT source_key, MAX(started_at), \
               MAX(CASE WHEN status = 'ok' THEN finished_at END) \
               FROM fetch_runs WHERE phase = 'sync' AND source_key IS NOT NULL \
               GROUP BY source_key";
    let Ok(mut statement) = db.prepare(sql) else {
        return HashMap::new();
    };
    let read = statement.query_map([], |row| {
        let key: String = row.get(0)?;
        let started: String = row.get(1)?;
        let ok: Option<String> = row.get(2)?;
        Ok((key, started, ok))
    });
    let Ok(mapped) = read else {
        return HashMap::new();
    };
    mapped
        .filter_map(Result::ok)
        .filter_map(|(key, started, ok)| {
            Some((
                key,
                LastSync {
                    started_at: timestamp(&started)?,
                    ok_at: ok.as_deref().and_then(timestamp),
                },
            ))
        })
        .collect()
}

fn source_names(db: &Connection) -> HashMap<String, String> {
    let Ok(mut statement) = db.prepare("SELECT key, name FROM sources") else {
        return HashMap::new();
    };
    let read = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    });
    match read {
        Ok(mapped) => mapped.filter_map(Result::ok).collect(),
        Err(_) => HashMap::new(),
    }
}

/// Every source, with whether the fetcher would collect it.
///
/// "Switched on" is `enabled` when an operator has answered in the Manage tab
/// and the plugin's own default when nobody has - the same rule the fetcher
/// applies (`eifo_fetcher.registry.enabled_sources`). A retired source - one no
/// plugin declares any more - is off whatever the columns say.
fn sources(db: &Connection, last_sync: &HashMap<String, LastSync>) -> Vec<SourceOption> {
    let sql = "SELECT key, name, active, enabled, default_enabled FROM sources ORDER BY name";
    let Ok(mut statement) = db.prepare(sql) else {
        return Vec::new();
    };
    let read = statement.query_map([], |row| {
        let key: String = row.get(0)?;
        let name: String = row.get(1)?;
        let active: bool = row.get::<_, i64>(2)? != 0;
        let enabled: Option<i64> = row.get(3)?;
        let default_enabled: bool = row.get::<_, i64>(4)? != 0;
        let on = active && enabled.map(|value| value != 0).unwrap_or(default_enabled);
        Ok(SourceOption {
            last_ok: None,
            key,
            name,
            on,
        })
    });
    let Ok(mapped) = read else {
        return Vec::new();
    };
    mapped
        .filter_map(Result::ok)
        .map(|mut source| {
            source.last_ok = last_sync.get(&source.key).and_then(|last| last.ok_at);
            source
        })
        .collect()
}

/// `2026-09-04 16:11:35.588866` as an instant.
///
/// Stored naive and always UTC (`eifo_core.types.UtcDateTime`), so the zone is
/// attached here rather than guessed at. The fractional part is optional
/// because SQLite writes whatever it was handed.
fn timestamp(value: &str) -> Option<DateTime<Utc>> {
    let value = value.trim();
    // The API answers in RFC 3339, with an offset; SQLite holds a naive local
    // string. Both are read here so the rest of this file does not care which
    // side a row came from.
    if let Ok(fixed) = DateTime::parse_from_rfc3339(value) {
        return Some(fixed.with_timezone(&Utc));
    }
    NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f"))
        .ok()
        .map(|naive| Utc.from_utc_datetime(&naive))
}

/// Named counts as a phrase, leaving out the zeroes.
///
/// The same choice the fetcher's own progress lines make: on most runs most of
/// these are zero, and they crowd out the number somebody is reading for.
fn tally(counts: &[(&str, i64)]) -> String {
    counts
        .iter()
        .filter(|(_, value)| *value != 0)
        .map(|(name, value)| format!("{} {name}", thousands(*value)))
        .collect::<Vec<_>>()
        .join(", ")
}

/// "45s", "12m", "2h 25m", "3d" - a duration at the length a menu can read.
pub fn compact_duration(seconds: i64) -> String {
    match seconds {
        s if s < 60 => format!("{s}s"),
        s if s < 3600 => format!("{}m", s / 60),
        s if s < 86_400 => {
            let (hours, minutes) = (s / 3600, (s % 3600) / 60);
            if minutes == 0 {
                format!("{hours}h")
            } else {
                format!("{hours}h {minutes}m")
            }
        }
        s => format!("{}d", s / 86_400),
    }
}

/// "2h ago", "just now" - how long since, in the same units.
pub fn ago(at: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let seconds = (now - at).num_seconds();
    if seconds < 60 {
        return "just now".into();
    }
    format!("{} ago", compact_duration(seconds))
}

/// The local wall clock, which is the one the person reading the menu is on.
pub fn clock(at: DateTime<Utc>) -> String {
    at.with_timezone(&Local).format("%H:%M").to_string()
}

fn plural(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn a_duration_is_said_at_the_length_it_is() {
        assert_eq!(compact_duration(0), "0s");
        assert_eq!(compact_duration(45), "45s");
        assert_eq!(compact_duration(60), "1m");
        assert_eq!(compact_duration(52 * 60), "52m");
        assert_eq!(compact_duration(2 * 3600 + 25 * 60), "2h 25m");
        assert_eq!(compact_duration(3 * 3600), "3h");
        assert_eq!(compact_duration(3 * 86_400), "3d");
    }

    #[test]
    fn a_recent_moment_is_just_now_rather_than_a_number() {
        let now = at("2026-09-04 16:00:00");
        assert_eq!(ago(at("2026-09-04 15:59:30"), now), "just now");
        assert_eq!(ago(at("2026-09-04 14:00:00"), now), "2h ago");
    }

    #[test]
    fn the_stored_timestamp_is_read_as_utc() {
        // Naive in the file, UTC by contract: getting this wrong would put
        // every elapsed time out by the offset of wherever the machine is.
        let parsed = timestamp("2026-09-04 16:11:35.588866").expect("the stored shape parses");
        assert_eq!(parsed.to_rfc3339(), "2026-09-04T16:11:35.588866+00:00");
        assert!(timestamp("2026-09-04 16:11:35").is_some(), "no fraction");
        assert!(timestamp("not a time").is_none());
    }

    #[test]
    fn zero_counts_are_left_out_of_the_phrase() {
        assert_eq!(
            tally(&[("items", 3204), ("new", 0), ("retired", 2)]),
            "3,204 items, 2 retired"
        );
        assert_eq!(tally(&[("items", 0)]), "");
    }

    #[test]
    fn the_current_run_stops_at_a_gap() {
        let view = view_of(
            &[
                // Last night, hours ago and complete.
                run(
                    "sync",
                    Some("netflix_il"),
                    "ok",
                    "2026-09-03 00:05:00",
                    Some("2026-09-03 00:40:00"),
                ),
                // Tonight.
                run(
                    "sync",
                    Some("netflix_il"),
                    "ok",
                    "2026-09-04 13:47:00",
                    Some("2026-09-04 13:48:00"),
                ),
                run("sync", Some("kan"), "running", "2026-09-04 13:48:00", None),
            ],
            &[source("netflix_il", "Netflix"), source("kan", "Kan Box")],
        );
        assert_eq!(view.steps.len(), 2, "last night is a different run");
        assert_eq!(
            view.current().map(|s| s.name.clone()),
            Some("Kan Box".into())
        );
    }

    #[test]
    fn a_run_is_the_phases_that_follow_one_another() {
        let view = view_of(
            &[
                run(
                    "sync",
                    Some("kan"),
                    "ok",
                    "2026-09-04 13:47:00",
                    Some("2026-09-04 13:50:00"),
                ),
                run(
                    "enrich",
                    None,
                    "ok",
                    "2026-09-04 13:50:01",
                    Some("2026-09-04 13:55:00"),
                ),
                run(
                    "enrich",
                    Some("imdb"),
                    "ok",
                    "2026-09-04 13:55:01",
                    Some("2026-09-04 13:56:00"),
                ),
                run("images", None, "running", "2026-09-04 13:56:10", None),
            ],
            &[source("kan", "Kan Box")],
        );
        assert_eq!(view.steps.len(), 4);
        assert_eq!(
            view.steps
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            ["Kan Box", "Ratings and metadata", "IMDb ratings", "Artwork"],
            "the passes are named for what they do, not for their key"
        );
    }

    #[test]
    fn a_crashed_row_does_not_glue_two_runs_together() {
        // Its finished_at is written by whoever found it open, which is a
        // timestamp from the *next* run. Believing it would make yesterday's
        // abandoned row the first step of today's.
        let view = view_of(
            &[
                run(
                    "sync",
                    Some("netflix_il"),
                    "crashed",
                    "2026-09-03 01:02:00",
                    Some("2026-09-04 13:47:18"),
                ),
                run(
                    "sync",
                    Some("kan"),
                    "ok",
                    "2026-09-04 13:47:19",
                    Some("2026-09-04 13:48:00"),
                ),
            ],
            &[source("netflix_il", "Netflix"), source("kan", "Kan Box")],
        );
        assert_eq!(view.steps.len(), 1);
        assert_eq!(view.steps[0].name, "Kan Box");
    }

    #[test]
    fn what_is_left_comes_in_the_order_the_last_run_took_it() {
        // The order lives in the plugin list, which this cannot read - but the
        // fetcher takes them in the same order every night, so the last run is
        // a faithful record of the queue.
        let view = view_of(
            &[
                // Yesterday, in the fetcher's order.
                run(
                    "sync",
                    Some("netflix_il"),
                    "ok",
                    "2026-09-03 00:01:00",
                    Some("2026-09-03 00:02:00"),
                ),
                run(
                    "sync",
                    Some("freetv"),
                    "ok",
                    "2026-09-03 00:02:00",
                    Some("2026-09-03 00:03:00"),
                ),
                run(
                    "sync",
                    Some("kan"),
                    "ok",
                    "2026-09-03 00:03:00",
                    Some("2026-09-03 00:04:00"),
                ),
                run(
                    "sync",
                    Some("mako"),
                    "ok",
                    "2026-09-03 00:04:00",
                    Some("2026-09-03 00:05:00"),
                ),
                // Tonight, part-way through.
                run(
                    "sync",
                    Some("netflix_il"),
                    "ok",
                    "2026-09-04 13:47:00",
                    Some("2026-09-04 13:48:00"),
                ),
                run(
                    "sync",
                    Some("freetv"),
                    "running",
                    "2026-09-04 13:48:00",
                    None,
                ),
            ],
            &[
                source("netflix_il", "Netflix"),
                source("freetv", "FreeTV"),
                source("kan", "Kan Box"),
                source("mako", "Mako VOD"),
            ],
        );
        assert_eq!(view.waiting, vec!["Kan Box", "Mako VOD"]);
        assert_eq!(
            view.sync_position(),
            Some((1, 4)),
            "one done, four in the run"
        );
    }

    #[test]
    fn a_source_that_has_never_run_is_last_in_the_queue() {
        let view = view_of(
            &[
                run(
                    "sync",
                    Some("mako"),
                    "ok",
                    "2026-09-03 00:04:00",
                    Some("2026-09-03 00:05:00"),
                ),
                run("sync", Some("kan"), "running", "2026-09-04 13:47:00", None),
            ],
            &[
                source("kan", "Kan Box"),
                source("mako", "Mako VOD"),
                source("aaa_new", "A New Service"),
            ],
        );
        // Alphabetically first, but nothing says where it belongs in the queue,
        // so it goes behind the one whose place in it is known.
        assert_eq!(view.waiting, vec!["Mako VOD", "A New Service"]);
    }

    #[test]
    fn a_source_that_is_switched_off_is_neither_queued_nor_offered() {
        let mut off = source("hot", "HOT");
        off.4 = 0; // enabled = false
        let view = view_of(
            &[run(
                "sync",
                Some("kan"),
                "running",
                "2026-09-04 13:47:00",
                None,
            )],
            &[source("kan", "Kan Box"), off],
        );
        assert!(view.waiting.is_empty(), "{:?}", view.waiting);
        assert_eq!(
            view.sources.iter().filter(|s| s.on).count(),
            1,
            "only the source the fetcher would collect"
        );
    }

    #[test]
    fn a_retired_source_is_off_whatever_its_switch_says() {
        // Retired means no plugin declares it any more: syncing it is not a
        // thing that can happen, however the Manage tab last left the toggle.
        let mut retired = source("hot", "HOT");
        retired.2 = 0; // active = false
        retired.4 = 1; // enabled = true
        let view = view_of(&[], &[retired]);
        assert!(!view.sources[0].on);
    }

    #[test]
    fn a_finished_run_says_what_it_did_and_when() {
        let view = view_of(
            &[
                run(
                    "sync",
                    Some("kan"),
                    "ok",
                    "2026-09-04 13:47:00",
                    Some("2026-09-04 13:48:00"),
                ),
                run(
                    "sync",
                    Some("mako"),
                    "failed",
                    "2026-09-04 13:48:00",
                    Some("2026-09-04 13:49:00"),
                ),
            ],
            &[source("kan", "Kan Box"), source("mako", "Mako VOD")],
        );
        let outcome = view.outcome().expect("a run that has ended has an outcome");
        assert_eq!(
            outcome,
            format!(
                "finished at {} · 2 services synced, 1 failed",
                clock(at("2026-09-04 13:49:00"))
            )
        );
        assert_eq!(view.failures().len(), 1);
    }

    #[test]
    fn a_sync_the_volume_guard_threw_away_does_not_read_as_a_small_night() {
        // 12 items beside a warning triangle looks like a thin catalog. What
        // happened is that the sync was discarded, and the 12 is why.
        let view = view_of(
            &[run_with(
                "sync",
                Some("kan"),
                "aborted_suspicious",
                "2026-09-04 13:47:00",
                Some("2026-09-04 13:48:00"),
                r#"{"items_seen": 12}"#,
            )],
            &[source("kan", "Kan Box")],
        );
        assert_eq!(
            view.steps[0].line(at("2026-09-04 14:00:00")),
            "⚠  Kan Box - 12 items, too few to apply (1m)"
        );
    }

    #[test]
    fn a_failure_part_way_through_says_how_far_it_got() {
        let view = view_of(
            &[run_with(
                "sync",
                Some("kan"),
                "failed",
                "2026-09-04 13:47:00",
                Some("2026-09-04 13:48:00"),
                r#"{"items_seen": 900}"#,
            )],
            &[source("kan", "Kan Box")],
        );
        assert_eq!(
            view.steps[0].line(at("2026-09-04 14:00:00")),
            "✕  Kan Box - failed after 900 items (1m)"
        );
    }

    #[test]
    fn a_row_nobody_closed_stops_being_reported_as_running() {
        // Killed mid-source: the row stays open until the next fetcher tidies
        // it, and until then the menu would insist a sync was still going.
        let mut view = view_of(
            &[run(
                "sync",
                Some("kan"),
                "running",
                "2026-09-04 13:47:00",
                None,
            )],
            &[source("kan", "Kan Box")],
        );
        view.forget_abandoned();
        assert!(view.current().is_none());
        assert_eq!(
            view.steps[0].line(at("2026-09-04 17:00:00")),
            "⚠  Kan Box - did not finish"
        );
        assert_eq!(view.failures().len(), 1);
    }

    #[test]
    fn a_run_still_going_has_no_outcome_yet() {
        let view = view_of(
            &[run(
                "sync",
                Some("kan"),
                "running",
                "2026-09-04 13:47:00",
                None,
            )],
            &[source("kan", "Kan Box")],
        );
        assert!(view.outcome().is_none());
        assert!(view.finished_at().is_none());
    }

    #[test]
    fn a_step_line_names_what_it_found() {
        let view = view_of(
            &[run_with(
                "sync",
                Some("kan"),
                "ok",
                "2026-09-04 13:47:00",
                Some("2026-09-04 13:48:20"),
                r#"{"items_seen": 3204, "titles_created": 51, "retired": 0}"#,
            )],
            &[source("kan", "Kan Box")],
        );
        let line = view.steps[0].line(at("2026-09-04 14:00:00"));
        assert_eq!(line, "✓  Kan Box - 3,204 items, 51 new (1m)");
    }

    #[test]
    fn a_running_step_says_how_long_it_has_been_going() {
        let view = view_of(
            &[run(
                "sync",
                Some("kan"),
                "running",
                "2026-09-04 13:47:00",
                None,
            )],
            &[source("kan", "Kan Box")],
        );
        let line = view.steps[0].line(at("2026-09-04 13:59:00"));
        assert_eq!(line, "▶  Kan Box - 12m so far");
    }

    #[test]
    fn a_source_that_failed_with_nothing_to_count_still_says_so() {
        let view = view_of(
            &[run_with(
                "sync",
                Some("kan"),
                "failed",
                "2026-09-04 13:47:00",
                Some("2026-09-04 13:47:01"),
                r#"{"items_seen": 0, "errors": ["boom"]}"#,
            )],
            &[source("kan", "Kan Box")],
        );
        assert_eq!(
            view.steps[0].line(at("2026-09-04 14:00:00")),
            "✕  Kan Box - failed (1s)"
        );
    }

    #[test]
    fn the_offer_for_a_source_says_how_old_its_catalog_is() {
        let view = view_of(
            &[run(
                "sync",
                Some("kan"),
                "ok",
                "2026-09-04 12:00:00",
                Some("2026-09-04 12:05:00"),
            )],
            &[source("kan", "Kan Box"), source("mako", "Mako VOD")],
        );
        let now = at("2026-09-04 14:05:00");
        let labels: Vec<String> = view.sources.iter().map(|s| s.label(now)).collect();
        assert_eq!(
            labels,
            ["Kan Box - synced 2h ago", "Mako VOD - never synced"]
        );
    }

    #[test]
    fn an_empty_database_is_an_empty_view_rather_than_a_failure() {
        let dir = tempdir("empty");
        std::fs::create_dir_all(dir.join("data")).unwrap();
        let view = read(&Config::new(dir));
        assert!(view.steps.is_empty() && view.sources.is_empty());
    }

    // -- fixtures ---------------------------------------------------------

    /// (key, name, active, default_enabled, enabled-or-null-as--1)
    type SourceRow = (&'static str, &'static str, i64, i64, i64);

    fn source(key: &'static str, name: &'static str) -> SourceRow {
        (key, name, 1, 1, -1)
    }

    /// (phase, source_key, status, started_at, finished_at, stats)
    type RunRow = (
        &'static str,
        Option<&'static str>,
        &'static str,
        &'static str,
        Option<&'static str>,
        &'static str,
    );

    fn run(
        phase: &'static str,
        key: Option<&'static str>,
        status: &'static str,
        started: &'static str,
        finished: Option<&'static str>,
    ) -> RunRow {
        (phase, key, status, started, finished, "{}")
    }

    fn run_with(
        phase: &'static str,
        key: Option<&'static str>,
        status: &'static str,
        started: &'static str,
        finished: Option<&'static str>,
        stats: &'static str,
    ) -> RunRow {
        (phase, key, status, started, finished, stats)
    }

    fn at(value: &str) -> DateTime<Utc> {
        timestamp(value).expect("a test timestamp")
    }

    /// A database with these rows in it, read back the way the app reads it.
    ///
    /// Against real SQLite rather than a hand-built struct: the walk back
    /// through the run log is the part worth testing, and it is the ordering
    /// and the nulls that make it interesting.
    fn view_of(runs: &[RunRow], sources: &[SourceRow]) -> RunView {
        let dir = tempdir(&format!("{:?}", std::thread::current().id()));
        let data = dir.join("data");
        std::fs::create_dir_all(&data).unwrap();
        let path = data.join("eifo.db");
        let _ = std::fs::remove_file(&path);

        let db = Connection::open(&path).unwrap();
        db.execute_batch(
            "CREATE TABLE fetch_runs (
                 id INTEGER PRIMARY KEY, source_key TEXT, phase TEXT NOT NULL,
                 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, stats TEXT);
             CREATE TABLE sources (
                 key TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL,
                 enabled INTEGER, default_enabled INTEGER NOT NULL);",
        )
        .unwrap();
        for (phase, key, status, started, finished, stats) in runs {
            db.execute(
                "INSERT INTO fetch_runs (source_key, phase, started_at, finished_at, status, stats) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![key, phase, started, finished, status, stats],
            )
            .unwrap();
        }
        for (key, name, active, default_enabled, enabled) in sources {
            let enabled = if *enabled < 0 { None } else { Some(*enabled) };
            db.execute(
                "INSERT INTO sources (key, name, active, enabled, default_enabled) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![key, name, active, enabled, default_enabled],
            )
            .unwrap();
        }
        drop(db);

        read(&Config::new(dir))
    }

    fn tempdir(tag: &str) -> PathBuf {
        let base = std::env::temp_dir().join(format!(
            "eifo-tray-runs-{}-{}",
            std::process::id(),
            tag.replace(|c: char| !c.is_alphanumeric(), "")
        ));
        std::fs::create_dir_all(&base).unwrap();
        base
    }
}

// ---------------------------------------------------------------- remote
//
// The same run log, read over the wire, for a catalog that is not on this
// disk. Only where the rows come from changes: `current_run` and
// `still_to_come` below are pure, so the picture the menu renders is assembled
// by exactly the code the local path uses and tested by exactly its tests.
//
// Two calls rather than one, on two cadences. The runs change every few seconds
// while a fetch is going and are asked for on every poll; the list of services
// changes when somebody adds a plugin, and is asked for rarely - see
// `SOURCES_EVERY` in the worker. Polling both at the fetch cadence would be
// forty requests a minute at somebody else's server for one line of a menu.

/// How long to wait on the API. The same generosity `health` allows, for the
/// same reason: a nightly run can hold the write lock for a moment.
const REMOTE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(8);

/// Runs per request. The admin API caps a page at 100, which is several nights
/// across every source - and `current_run` stops at the first ten-minute gap
/// long before it runs out of them.
const REMOTE_ROWS: usize = 100;

#[derive(serde::Deserialize)]
struct RunPage {
    items: Vec<RemoteRun>,
}

#[derive(serde::Deserialize)]
struct RemoteRun {
    source_key: Option<String>,
    phase: String,
    status: String,
    started_at: String,
    finished_at: Option<String>,
    #[serde(default)]
    stats: serde_json::Value,
}

#[derive(serde::Deserialize)]
struct RemoteSource {
    key: String,
    name: String,
    active: bool,
    /// The operator's switch, the config file and the plugin's own default,
    /// already folded together by the side that can see all three.
    effective_enabled: bool,
    /// When this source was last known good - the API's own answer, computed
    /// over the whole history rather than the page of runs read above.
    last_sync_at: Option<String>,
}

/// Every service the far catalog tracks, or None if it could not be asked.
pub fn fetch_sources(config: &Config, token: Option<&str>) -> Option<Vec<SourceOption>> {
    let rows: Vec<RemoteSource> = get_json(config, token, "/api/v1/admin/sources")?;
    Some(
        rows.into_iter()
            .map(|row| SourceOption {
                // Both halves, as the local query has it: a retired source is
                // not offered however its switch reads.
                on: row.active && row.effective_enabled,
                last_ok: row.last_sync_at.as_deref().and_then(timestamp),
                key: row.key,
                name: row.name,
            })
            .collect(),
    )
}

/// The far catalog's run log, given the services it was last known to have.
///
/// Returns None when the API could not be reached or would not answer, so the
/// caller can keep the picture it already had. An empty view would say "nothing
/// has run yet" about a server that has run nightly for a month, which is the
/// one thing this must never do.
pub fn fetch_run_view(
    config: &Config,
    token: Option<&str>,
    sources: &[SourceOption],
) -> Option<RunView> {
    let page: RunPage = get_json(
        config,
        token,
        &format!("/api/v1/admin/runs?page_size={REMOTE_ROWS}"),
    )?;

    // Newest first, which is the order the local query asks for and the order
    // `current_run` walks backwards from.
    let rows: Vec<Row> = page
        .items
        .into_iter()
        .filter_map(|run| {
            Some(Row {
                key: run.source_key,
                phase: run.phase,
                state: StepState::read(&run.status),
                started_at: timestamp(&run.started_at)?,
                finished_at: run.finished_at.as_deref().and_then(timestamp),
                stats: run.stats,
            })
        })
        .collect();

    let names: HashMap<String, String> = sources
        .iter()
        .map(|source| (source.key.clone(), source.name.clone()))
        .collect();
    // Only `started_at` is wanted here - what orders the queue of services
    // still to come. Each source's last *good* sync came from the API above,
    // which computes it over every run rather than the page read here.
    let last_sync = last_sync_from(&rows);

    let steps = current_run(rows, &names);
    let waiting = still_to_come(&steps, sources, &last_sync);
    Some(RunView {
        steps,
        waiting,
        sources: sources.to_vec(),
    })
}

/// When each source was last taken, from the runs already in hand.
fn last_sync_from(rows: &[Row]) -> HashMap<String, LastSync> {
    let mut found: HashMap<String, LastSync> = HashMap::new();
    for row in rows.iter().filter(|row| row.phase == "sync") {
        let Some(key) = row.key.clone() else { continue };
        let entry = found.entry(key).or_insert(LastSync {
            started_at: row.started_at,
            ok_at: None,
        });
        if row.started_at > entry.started_at {
            entry.started_at = row.started_at;
        }
    }
    found
}

/// One authenticated GET, parsed, or None with the reason left unsaid.
///
/// Unsaid because every caller's answer to a failure is the same - keep what
/// was already on screen - and a menu that reported "could not read the run
/// log" on a dropped packet would be noisier than the thing it is reporting on.
/// The dot beside it already says whether the server is answering.
fn get_json<T: serde::de::DeserializeOwned>(
    config: &Config,
    token: Option<&str>,
    path: &str,
) -> Option<T> {
    let url = format!("{}{path}", config.base_url.trim_end_matches('/'));
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(REMOTE_TIMEOUT))
        .build()
        .new_agent();

    let mut request = agent.get(&url);
    if let Some(token) = token {
        request = request.header("Authorization", format!("Bearer {token}"));
    }
    request.call().ok()?.body_mut().read_json::<T>().ok()
}

#[cfg(test)]
mod remote_tests {
    use super::*;

    /// One page of runs, exactly as `GET /api/v1/admin/runs` answers it.
    const RUNS: &str = r#"{
      "items": [
        {"id": 3, "source_key": null, "phase": "enrich", "status": "running",
         "started_at": "2026-09-07T14:40:00Z", "finished_at": null,
         "stats": {}, "has_log": false},
        {"id": 2, "source_key": "mako", "phase": "sync", "status": "ok",
         "started_at": "2026-09-07T14:35:00Z", "finished_at": "2026-09-07T14:38:00Z",
         "stats": {"items_seen": 120, "titles_created": 4}, "has_log": true},
        {"id": 1, "source_key": "kan", "phase": "sync", "status": "ok",
         "started_at": "2026-09-07T14:30:00Z", "finished_at": "2026-09-07T14:34:00Z",
         "stats": {"items_seen": 90}, "has_log": true}
      ],
      "page": 1, "page_size": 100, "total": 3
    }"#;

    const SOURCES: &str = r#"[
      {"key": "kan", "name": "Kan Box", "kind": "free",
       "website_url": "https://kan.org.il", "active": true, "enabled": null,
       "effective_enabled": true, "last_sync_at": "2026-09-07T14:34:00Z",
       "last_sync_status": "ok", "stale": false},
      {"key": "mako", "name": "Mako VOD", "kind": "free",
       "website_url": "https://mako.co.il", "active": true, "enabled": null,
       "effective_enabled": true, "last_sync_at": "2026-09-07T14:38:00Z",
       "last_sync_status": "ok", "stale": false},
      {"key": "gone", "name": "Retired", "kind": "free",
       "website_url": "https://gone.example", "active": false, "enabled": null,
       "effective_enabled": true, "last_sync_at": null,
       "last_sync_status": null, "stale": false},
      {"key": "off", "name": "Switched Off", "kind": "free",
       "website_url": "https://off.example", "active": true, "enabled": false,
       "effective_enabled": false, "last_sync_at": null,
       "last_sync_status": null, "stale": false}
    ]"#;

    fn sources() -> Vec<SourceOption> {
        let rows: Vec<RemoteSource> = serde_json::from_str(SOURCES).expect("sources parse");
        rows.into_iter()
            .map(|row| SourceOption {
                on: row.active && row.effective_enabled,
                last_ok: row.last_sync_at.as_deref().and_then(timestamp),
                key: row.key,
                name: row.name,
            })
            .collect()
    }

    fn view() -> RunView {
        let page: RunPage = serde_json::from_str(RUNS).expect("runs parse");
        let sources = sources();
        let rows: Vec<Row> = page
            .items
            .into_iter()
            .filter_map(|run| {
                Some(Row {
                    key: run.source_key,
                    phase: run.phase,
                    state: StepState::read(&run.status),
                    started_at: timestamp(&run.started_at)?,
                    finished_at: run.finished_at.as_deref().and_then(timestamp),
                    stats: run.stats,
                })
            })
            .collect();
        let names = sources
            .iter()
            .map(|s| (s.key.clone(), s.name.clone()))
            .collect();
        let last_sync = last_sync_from(&rows);
        let steps = current_run(rows, &names);
        let waiting = still_to_come(&steps, &sources, &last_sync);
        RunView {
            steps,
            waiting,
            sources,
        }
    }

    #[test]
    fn an_api_timestamp_reads_as_readily_as_a_sqlite_one() {
        // The two sides write them differently and the rest of this file must
        // not have to care which it is holding.
        let api = timestamp("2026-09-07T14:34:00Z").expect("rfc 3339");
        let sqlite = timestamp("2026-09-07 14:34:00").expect("sqlite");

        assert_eq!(api, sqlite);
        assert!(timestamp("2026-09-07T14:34:00.372000+00:00").is_some());
    }

    #[test]
    fn a_page_of_runs_becomes_the_run_the_menu_renders() {
        let view = view();

        // Oldest first, the way the run happened and the way it is read.
        let names: Vec<&str> = view.steps.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["Kan Box", "Mako VOD", "Ratings and metadata"]);
    }

    #[test]
    fn a_source_key_is_resolved_to_the_name_a_person_knows_it_by() {
        // The runs carry keys; the names live on the sources call. A menu row
        // reading "mako" rather than "Mako VOD" is the whole reason both are
        // fetched.
        assert!(view().steps.iter().any(|s| s.name == "Mako VOD"));
    }

    #[test]
    fn the_run_still_going_is_the_one_reported_as_current() {
        let view = view();

        let current = view.current().expect("something is running");
        assert_eq!(current.name, "Ratings and metadata");
        // No fraction: the step running is the enrich, and "2 of 4 services"
        // said about it would be a queue that has nothing to do with it.
        assert_eq!(view.sync_position(), None);
    }

    #[test]
    fn what_a_step_found_survives_the_wire() {
        // `stats` is free-form JSON on both sides, and it is what every count
        // in the menu is read out of.
        let view = view();

        let mako = view
            .steps
            .iter()
            .find(|s| s.name == "Mako VOD")
            .expect("mako ran");
        assert!(mako.found().contains("120"), "{}", mako.found());
    }

    #[test]
    fn a_retired_or_switched_off_service_is_not_offered() {
        // Both halves of the local query's answer: `active` and the switch. A
        // menu item that quietly does nothing is worse than one that is absent.
        let sources = sources();
        let offered: Vec<&str> = sources
            .iter()
            .filter(|s| s.on)
            .map(|s| s.key.as_str())
            .collect();

        assert_eq!(offered, ["kan", "mako"]);
    }

    #[test]
    fn when_a_service_was_last_known_good_comes_from_the_api() {
        // Computed there over every run, rather than here over the page just
        // read - which would date a source no older than the last hundred rows.
        let sources = sources();
        let kan = sources.iter().find(|s| s.key == "kan").expect("kan");

        assert_eq!(kan.last_ok, timestamp("2026-09-07T14:34:00Z"));
    }

    #[test]
    fn a_service_this_run_has_not_reached_is_still_to_come() {
        // Nothing here has synced "off" or "gone", and neither is offered, so
        // the queue is empty - the two that are offered have both been taken.
        assert!(view().waiting.is_empty());
    }

    #[test]
    fn a_reply_that_is_not_what_was_asked_for_is_declined_rather_than_guessed() {
        assert!(serde_json::from_str::<RunPage>("{\"items\": \"nope\"}").is_err());
        assert!(serde_json::from_str::<Vec<RemoteSource>>("{}").is_err());
    }
}

#[cfg(test)]
mod live {
    use super::*;

    /// The real thing, against a real catalog. Ignored by default because it
    /// needs a server and a credential; the fixtures above pin the shape, and
    /// this pins that the shape is still what the API actually sends.
    ///
    ///   EIFO_TEST_BASE_URL=https://… EIFO_TEST_TOKEN=eifo_pat_… \
    ///     cargo test live -- --ignored --nocapture
    #[test]
    #[ignore = "needs a reachable catalog and an administrator's token"]
    fn a_real_catalog_answers_in_the_shape_this_reads() {
        let Ok(base_url) = std::env::var("EIFO_TEST_BASE_URL") else {
            panic!("set EIFO_TEST_BASE_URL");
        };
        let token = std::env::var("EIFO_TEST_TOKEN").ok();

        let mut config = Config::new(std::path::PathBuf::from("/tmp"));
        config.base_url = base_url;

        let sources = fetch_sources(&config, token.as_deref()).expect("sources");
        assert!(!sources.is_empty(), "a catalog with no sources at all");

        let view = fetch_run_view(&config, token.as_deref(), &sources).expect("runs");
        assert!(!view.steps.is_empty(), "a catalog that has never run");

        println!(
            "  {} services, {} offered; last run: {}",
            sources.len(),
            sources.iter().filter(|s| s.on).count(),
            view.outcome().unwrap_or_else(|| "none".into())
        );
        for line in crate::menu::progress_lines(&view) {
            println!("  {line}");
        }
    }
}
