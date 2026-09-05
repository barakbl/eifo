//! The menu, and the words in it.
//!
//! Built once and updated in place rather than rebuilt on every reading: an
//! `NSMenu` swapped out from under an open menu is a menu that closes itself
//! while somebody is reading it, twenty seconds into every minute.
//!
//! Two lists here are not fixed in length - the run's steps and the services on
//! offer - so those two are grown and trimmed a row at a time rather than
//! rebuilt. Their contents change every few seconds; their *shape* changes when
//! a run reaches another source, or when somebody adds a service, which is
//! rarely enough that the flicker is not a thing anybody sees.

use std::cell::RefCell;

use chrono::Utc;
use muda::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};

use crate::health::Status;
use crate::runs::{self, RunView, SourceOption};
use crate::worker::{Snapshot, UpdateView};

/// Prefix on the id of a per-source item, so a click can be turned back into
/// the source it was for. The id is the only thing a menu event carries.
pub const SOURCE_PREFIX: &str = "source:";

/// How many rows the progress list may take before it stops naming what is
/// still to come. A menu longer than the screen is not a readout.
const MAX_PROGRESS_ROWS: usize = 28;

/// Every item the app keeps a handle on, so their labels can change.
pub struct Items {
    pub status: MenuItem,
    pub detail: MenuItem,
    pub fetch_state: MenuItem,
    pub progress: Submenu,
    /// The rows inside it, grown and trimmed to fit the run.
    progress_rows: RefCell<Vec<MenuItem>>,
    pub run_sync: MenuItem,
    pub sync_one: Submenu,
    /// One row per service on offer, with the key its id was built from.
    source_items: RefCell<Vec<(String, MenuItem)>>,
    pub run_enrich: MenuItem,
    pub run_images: MenuItem,
    pub run_all: MenuItem,
    pub stop_fetch: MenuItem,
    pub check_now: MenuItem,
    pub server_state: MenuItem,
    pub start_server: MenuItem,
    pub stop_server: MenuItem,
    pub restart_server: MenuItem,
    pub start_on_open: CheckMenuItem,
    pub keep_up: CheckMenuItem,
    pub schedule: CheckMenuItem,
    pub next_run: MenuItem,
    pub open_app: MenuItem,
    pub open_manage: MenuItem,
    pub update: MenuItem,
    pub paste_token: MenuItem,
    pub forget_token: MenuItem,
    pub choose_folder: MenuItem,
    pub login_item: CheckMenuItem,
    pub about: MenuItem,
    pub quit: MenuItem,
}

pub fn build(login_enabled: bool) -> (Menu, Items) {
    let menu = Menu::new();

    // Disabled on purpose: the top two lines are a readout, not a control. An
    // enabled item that does nothing when clicked is a worse lie than a greyed
    // one that plainly is not for clicking.
    let status = MenuItem::new("Checking…", false, None);
    let detail = MenuItem::new("", false, None);

    // A readout, like the server line below: what fetch, if any, is running now.
    let fetch_state = MenuItem::new("Fetch: idle", false, None);
    let progress = Submenu::new("Progress", true);
    let run_sync = MenuItem::new("Sync every service now", true, None);
    let sync_one = Submenu::new("Sync one service", true);
    let run_enrich = MenuItem::new("Refresh ratings now", true, None);
    let run_images = MenuItem::new("Download artwork now", true, None);
    let run_all = MenuItem::new("Run everything now", true, None);
    let stop_fetch = MenuItem::new("Stop the current fetch", false, None);
    let check_now = MenuItem::new("Check now", true, None);
    let schedule = CheckMenuItem::new("Run nightly", true, true, None);
    let next_run = MenuItem::new("", false, None);

    let server_state = MenuItem::new("Web server", false, None);
    let start_server = MenuItem::new("Start", true, None);
    let stop_server = MenuItem::new("Stop", true, None);
    let restart_server = MenuItem::new("Restart", true, None);
    let start_on_open = CheckMenuItem::new("Start when Eifo opens", true, true, None);
    let keep_up = CheckMenuItem::new("Restart if it stops", true, true, None);

    let open_app = MenuItem::new("Open Eifo", true, None);
    let open_manage = MenuItem::new("Open Manage", true, None);
    let update = MenuItem::new("Check for updates", true, None);
    let paste_token = MenuItem::new("Paste API token from clipboard", true, None);
    let forget_token = MenuItem::new("Forget API token", true, None);
    let choose_folder = MenuItem::new("Choose Eifo folder…", true, None);
    let login_item = CheckMenuItem::new("Open at login", true, login_enabled, None);
    let about = MenuItem::new("About Eifo", true, None);
    let quit = MenuItem::new("Quit", true, None);

    let separator = PredefinedMenuItem::separator;
    let _ = menu.append_items(&[
        &status,
        &detail,
        &separator(),
        &fetch_state,
        &progress,
        &run_sync,
        &sync_one,
        &run_enrich,
        &run_images,
        &run_all,
        &stop_fetch,
        &separator(),
        &check_now,
        &schedule,
        &next_run,
        &separator(),
        &server_state,
        &start_server,
        &stop_server,
        &restart_server,
        &start_on_open,
        &keep_up,
        &separator(),
        &open_app,
        &open_manage,
        &separator(),
        &update,
        &separator(),
        &paste_token,
        &forget_token,
        &choose_folder,
        &login_item,
        &about,
        &separator(),
        &quit,
    ]);

    let items = Items {
        status,
        detail,
        fetch_state,
        progress,
        progress_rows: RefCell::new(Vec::new()),
        run_sync,
        sync_one,
        source_items: RefCell::new(Vec::new()),
        run_enrich,
        run_images,
        run_all,
        stop_fetch,
        check_now,
        server_state,
        start_server,
        stop_server,
        restart_server,
        start_on_open,
        keep_up,
        schedule,
        next_run,
        open_app,
        open_manage,
        update,
        paste_token,
        forget_token,
        choose_folder,
        login_item,
        about,
        quit,
    };
    (menu, items)
}

/// The headline line, which is the one thing read at a glance.
pub fn headline(snapshot: &Snapshot) -> String {
    if let UpdateView::Installing { tag } = &snapshot.update {
        return format!("Updating to {tag}…");
    }
    if !snapshot.setup_problems.is_empty() {
        return snapshot.setup_problems[0].clone();
    }
    if snapshot.fetch_running {
        let what = match &snapshot.running_phase {
            Some(phase) => format!("Running {phase}"),
            None => "A fetch is running".into(),
        };
        // The fraction is the difference between a run that is going and a run
        // that is getting somewhere. Only while a sweep is on, and only when
        // there is more than one service in it: "0 of 1" said about a sync of
        // one service is arithmetic where a word would do.
        return match (snapshot.run.sync_position(), snapshot.run.current()) {
            (Some((done, total)), _) if total > 1 => {
                format!("{what} · {done} of {total} services")
            }
            // Not a sweep, so name the pass it is on instead: during the enrich
            // half of a full run, "IMDb ratings" is the thing worth knowing.
            // Unless the phase is already called that - "Running the artwork
            // pass · Artwork" is one fact said twice.
            (None, Some(step)) if !says_the_same(&what, &step.name) => {
                format!("{what} · {}", step.name)
            }
            _ => format!("{what}…"),
        };
    }
    match snapshot.status {
        Status::Ok => format!("All well · {}", snapshot.summary),
        Status::Attention => format!("Needs attention · {}", snapshot.summary),
        Status::Down => format!("Not running · {}", snapshot.summary),
        Status::Unknown => snapshot.summary.clone(),
    }
}

/// Whether a line already says what naming the step would say.
fn says_the_same(line: &str, name: &str) -> bool {
    line.to_lowercase().contains(&name.to_lowercase())
}

/// The second line: what to do about it, or what just happened.
pub fn detail(snapshot: &Snapshot) -> String {
    if snapshot.setup_problems.len() > 1 {
        return snapshot.setup_problems[1].clone();
    }
    if snapshot.restarts_given_up {
        return "Gave up restarting - start it by hand".into();
    }
    // Above the running fetch, because a catalog this app cannot read is the
    // thing to fix first and the only one with an instruction attached.
    if snapshot.status == Status::Attention && !snapshot.has_token && snapshot.problems.is_empty() {
        return "Create one in Settings, copy it, then Paste API token".into();
    }
    // While something is running, what it is on now outranks every other thing
    // this line could say: it is the answer to the question somebody opened the
    // menu with, and the rest will still be true in an hour.
    if let Some(step) = snapshot.run.current() {
        let mut line = format!(
            "Now: {}, {} in",
            step.name,
            runs::compact_duration(step.seconds(Utc::now()))
        );
        let failed = snapshot.run.failures().len();
        if failed > 0 {
            line.push_str(&format!(" · {failed} failed so far"));
        }
        return line;
    }
    if let Some(result) = &snapshot.last_result {
        return result.clone();
    }
    match snapshot.problems.len() {
        0 => String::new(),
        1 => snapshot.problems[0].clone(),
        // Naming two and counting the rest: a menu is not a list view, and
        // "3 sources" without saying which is a number nobody can act on.
        _ => format!(
            "{}, {} and {} more",
            snapshot.problems[0],
            snapshot.problems[1],
            snapshot.problems.len() - 2
        ),
    }
}

/// The fetch readout: what is running now, and whether Stop can reach it.
pub fn fetch_line(snapshot: &Snapshot) -> String {
    if !snapshot.fetch_running {
        // Idle is a fact about now; what the last run did is the thing somebody
        // opening the menu at nine in the morning actually wants.
        return match snapshot.run.outcome() {
            Some(outcome) => format!("Fetch: idle · last run {outcome}"),
            None => "Fetch: idle".into(),
        };
    }
    let what = match &snapshot.running_phase {
        Some(phase) => format!("running {phase}"),
        None => "running (started elsewhere)".into(),
    };
    let since = match snapshot.run.started_at() {
        Some(at) => format!(
            ", {} in",
            runs::compact_duration((Utc::now() - at).num_seconds().max(0))
        ),
        None => String::new(),
    };
    match snapshot.fetch_pid {
        Some(pid) => format!("Fetch: {what}{since} (pid {pid})"),
        None => format!("Fetch: {what}{since}"),
    }
}

/// The label on the Stop item, which names what it will stop.
pub fn stop_fetch_label(snapshot: &Snapshot) -> String {
    if !snapshot.fetch_running {
        return "Stop the current fetch".into();
    }
    // No article of its own: a phase names itself the way a sentence would
    // have it - "sync", "the full run", "sync of Kan Box" - and prefixing
    // "the" to all of them produced "Stop the the full run".
    match (&snapshot.running_phase, snapshot.fetch_pid) {
        (Some(phase), Some(pid)) => format!("Stop {phase} (pid {pid})"),
        (Some(phase), None) => format!("Stop {phase}"),
        (None, Some(pid)) => format!("Stop the running fetch (pid {pid})"),
        (None, None) => "Stop the running fetch".into(),
    }
}

/// The progress list: what this run has done, is doing, and has not reached.
///
/// One line per step in the order they happened, so the run reads top to bottom
/// the way it ran. The rows still to come are named rather than counted -
/// "5 more" answers nothing, and "Kan, then Mako" is the answer to "is it going
/// to get to the one I care about".
pub fn progress_lines(run: &RunView) -> Vec<String> {
    let now = Utc::now();
    let Some(started) = run.started_at() else {
        return vec!["Nothing has run yet".into()];
    };

    let mut lines = vec![match run.finished_at() {
        Some(finished) => format!(
            "Ran {} to {} · {}",
            runs::clock(started),
            runs::clock(finished),
            run.did()
        ),
        None => format!(
            "Started {} · {} so far",
            runs::clock(started),
            runs::compact_duration((now - started).num_seconds().max(0))
        ),
    }];

    for step in &run.steps {
        lines.push(step.line(now));
    }

    // Only while something is actually running. A queue shown against a run
    // that has ended is a promise nobody made: a `sync --source` touches one
    // service and finishes, and listing the other thirteen underneath it as
    // though they were coming would be this app inventing a run.
    if run.current().is_none() {
        return lines;
    }
    // The dot is deliberately not the tick's column: something not started yet
    // has not earned a mark, and a row of ticks that included the future would
    // be the one thing this list must not say.
    let room = MAX_PROGRESS_ROWS.saturating_sub(lines.len());
    for name in run.waiting.iter().take(room) {
        lines.push(format!("·  {name}"));
    }
    if run.waiting.len() > room {
        lines.push(format!("·  and {} more", run.waiting.len() - room));
    }
    lines
}

/// The label on the progress submenu itself, so the shape of the run is visible
/// without opening it.
pub fn progress_label(run: &RunView) -> String {
    if run.steps.is_empty() {
        return "Progress".into();
    }
    match (run.current(), run.sync_position()) {
        (None, _) => format!("Progress · last run, {}", run.did()),
        (_, Some((done, total))) if total > 1 => {
            format!("Progress · {done} of {total} services")
        }
        (Some(step), _) => format!("Progress · {}", step.name),
    }
}

/// What the server line says about a process this app may or may not own.
pub fn server_line(snapshot: &Snapshot) -> String {
    match (snapshot.server_owned, snapshot.status) {
        (true, Status::Down) => "Web server: started, not answering".into(),
        (true, _) => match snapshot.server_pid {
            Some(pid) => format!("Web server: running (pid {pid})"),
            None => "Web server: running".into(),
        },
        // Answering but not ours: somebody started it by hand, or it is the
        // LaunchAgent's. Saying so is the difference between "up" and "up, and
        // Stop will not touch it".
        (false, Status::Down) => "Web server: not running".into(),
        (false, _) => "Web server: running (not started here)".into(),
    }
}

/// The update line: what it says, and whether clicking it does anything.
pub fn update_line(update: &UpdateView) -> (String, bool) {
    match update {
        UpdateView::Unknown | UpdateView::Failed => ("Check for updates".into(), true),
        UpdateView::UpToDate { version } => (format!("Up to date · {version}"), true),
        UpdateView::Checking => ("Checking for updates…".into(), false),
        UpdateView::Available { tag } => (format!("Update to {tag}…"), true),
        UpdateView::Installing { tag } => (format!("Updating to {tag}…"), false),
    }
}

/// What hovering the dot says: the headline, without the menu around it.
pub fn tooltip(snapshot: &Snapshot) -> String {
    format!("Eifo — {}", headline(snapshot))
}

/// The id a per-source item carries, and the key read back out of one.
pub fn source_id(key: &str) -> String {
    format!("{SOURCE_PREFIX}{key}")
}

pub fn source_key(id: &str) -> Option<&str> {
    id.strip_prefix(SOURCE_PREFIX)
}

pub fn apply(items: &Items, snapshot: &Snapshot) {
    items.status.set_text(headline(snapshot));
    let detail_text = detail(snapshot);
    items.detail.set_text(&detail_text);
    // An empty second line would be a blank row in the menu; hide it instead.
    items.detail.set_enabled(false);

    items.fetch_state.set_text(fetch_line(snapshot));
    items.progress.set_text(progress_label(&snapshot.run));
    fill(
        &items.progress,
        &items.progress_rows,
        &progress_lines(&snapshot.run),
    );

    let busy = snapshot.fetch_running || matches!(snapshot.update, UpdateView::Installing { .. });
    for item in [
        &items.run_sync,
        &items.run_enrich,
        &items.run_images,
        &items.run_all,
    ] {
        item.set_enabled(!busy);
    }
    items.run_sync.set_text(if busy {
        "Sync every service now (a run is in progress)"
    } else {
        "Sync every service now"
    });
    let offered = snapshot.run.sources.iter().filter(|s| s.on).count();
    items.sync_one.set_enabled(!busy && offered > 0);
    fill_sources(items, &snapshot.run.sources);

    items.stop_fetch.set_enabled(busy);
    items.stop_fetch.set_text(stop_fetch_label(snapshot));

    items.schedule.set_checked(snapshot.schedule_enabled);
    items
        .next_run
        .set_text(format!("Next run: {}", snapshot.next_run));

    items.server_state.set_text(server_line(snapshot));
    items.start_server.set_enabled(!snapshot.server_owned);
    items.stop_server.set_enabled(snapshot.server_owned);
    items
        .start_on_open
        .set_checked(snapshot.start_server_on_open);
    items.keep_up.set_checked(snapshot.keep_server_up);

    items.paste_token.set_text(if snapshot.has_token {
        "Replace API token from clipboard"
    } else {
        "Paste API token from clipboard"
    });
    // Nothing to forget, so nothing to press. A greyed item says "there is no
    // token here" better than an enabled one that does nothing.
    items.forget_token.set_enabled(snapshot.has_token);

    let (update_text, update_enabled) = update_line(&snapshot.update);
    items.update.set_text(update_text);
    items.update.set_enabled(update_enabled);
}

/// Make a submenu hold exactly these lines, adding and removing rows to fit.
///
/// Rows are reused rather than replaced, so the common case - the same run, a
/// minute later - changes text and nothing else. Only a run that has reached
/// another source touches the shape of the menu.
fn fill(submenu: &Submenu, rows: &RefCell<Vec<MenuItem>>, lines: &[String]) {
    let mut rows = rows.borrow_mut();
    while rows.len() < lines.len() {
        // Disabled: every one of these is a readout. Clicking a source in a
        // list of what has happened should not start anything.
        let item = MenuItem::new("", false, None);
        let _ = submenu.append(&item);
        rows.push(item);
    }
    while rows.len() > lines.len() {
        if let Some(item) = rows.pop() {
            let _ = submenu.remove(&item);
        }
    }
    for (row, line) in rows.iter().zip(lines) {
        row.set_text(line);
    }
}

/// One item per service the fetcher would actually collect.
///
/// A service that is switched off is left out: `eifo-fetch sync --source` on
/// one of those syncs nothing and says so in a log nobody is reading, and a
/// menu item that quietly does nothing is worse than an absent one. Rebuilt
/// only when the set of services changes, because each item's id is built from
/// its key and a click is matched by id.
fn fill_sources(items: &Items, sources: &[SourceOption]) {
    let now = Utc::now();
    let offered: Vec<&SourceOption> = sources.iter().filter(|s| s.on).collect();
    let mut held = items.source_items.borrow_mut();

    let same = held.len() == offered.len()
        && held
            .iter()
            .zip(&offered)
            .all(|((key, _), source)| key == &source.key);
    if !same {
        for (_, item) in held.drain(..) {
            let _ = items.sync_one.remove(&item);
        }
        for source in &offered {
            let item = MenuItem::with_id(source_id(&source.key), source.label(now), true, None);
            let _ = items.sync_one.append(&item);
            held.push((source.key.clone(), item));
        }
        return;
    }
    for ((_, item), source) in held.iter().zip(&offered) {
        item.set_text(source.label(now));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runs::{RunView, Step, StepState};

    /// A run part-way through a sweep: one service done, one going, two to come.
    fn mid_run() -> RunView {
        let started = Utc::now() - chrono::Duration::minutes(20);
        let on_it = Utc::now() - chrono::Duration::minutes(12);
        let stamp = |at: chrono::DateTime<Utc>| at.format("%Y-%m-%d %H:%M:%S").to_string();
        RunView {
            steps: vec![
                Step::sample(
                    "Netflix",
                    "sync",
                    StepState::Ok,
                    &stamp(started),
                    Some(&stamp(on_it)),
                ),
                Step::sample("FreeTV", "sync", StepState::Running, &stamp(on_it), None),
            ],
            waiting: vec!["Kan Box".into(), "Mako VOD".into()],
            sources: Vec::new(),
        }
    }

    fn snapshot() -> Snapshot {
        Snapshot {
            status: Status::Ok,
            summary: "38,836 titles, every source fresh".into(),
            problems: Vec::new(),
            fetch_running: false,
            running_phase: None,
            fetch_pid: None,
            last_result: None,
            run: RunView::default(),
            server_owned: true,
            server_pid: Some(1234),
            keep_server_up: true,
            start_server_on_open: true,
            schedule_enabled: true,
            next_run: "tomorrow at 03:00".into(),
            restarts_given_up: false,
            setup_problems: Vec::new(),
            has_token: false,
            update: UpdateView::Unknown,
            relaunch: false,
        }
    }

    #[test]
    fn a_healthy_system_says_so_first() {
        assert!(headline(&snapshot()).starts_with("All well"));
    }

    #[test]
    fn a_run_in_progress_outranks_the_status() {
        // What it is doing now matters more than what it found last time.
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = Some("sync".into());
        assert_eq!(headline(&s), "Running sync…");
    }

    #[test]
    fn a_fetch_started_elsewhere_still_shows_and_can_be_stopped() {
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = None;
        s.fetch_pid = Some(5150);
        assert_eq!(headline(&s), "A fetch is running…");
        assert_eq!(
            fetch_line(&s),
            "Fetch: running (started elsewhere) (pid 5150)"
        );
        assert_eq!(stop_fetch_label(&s), "Stop the running fetch (pid 5150)");
    }

    #[test]
    fn an_idle_fetcher_reads_as_idle() {
        assert_eq!(fetch_line(&snapshot()), "Fetch: idle");
    }

    #[test]
    fn the_update_line_offers_the_new_version_and_disables_while_it_installs() {
        let (text, enabled) = update_line(&UpdateView::Available {
            tag: "v0.3.0".into(),
        });
        assert_eq!(text, "Update to v0.3.0…");
        assert!(enabled, "an available update must be clickable");

        let (text, enabled) = update_line(&UpdateView::Installing {
            tag: "v0.3.0".into(),
        });
        assert_eq!(text, "Updating to v0.3.0…");
        assert!(!enabled, "no second click while it installs");

        assert_eq!(update_line(&UpdateView::Unknown).0, "Check for updates");
    }

    #[test]
    fn a_broken_folder_outranks_everything() {
        let mut s = snapshot();
        s.setup_problems = vec!["/nope is not an Eifo checkout".into()];
        assert!(headline(&s).contains("not an Eifo checkout"));
    }

    #[test]
    fn an_update_in_progress_is_the_headline() {
        // It outranks even a broken folder: the update is what will fix it.
        let mut s = snapshot();
        s.setup_problems = vec!["something is wrong".into()];
        s.update = UpdateView::Installing {
            tag: "v0.3.0".into(),
        };
        assert_eq!(headline(&s), "Updating to v0.3.0…");
    }

    #[test]
    fn two_problems_are_named_and_the_rest_counted() {
        let mut s = snapshot();
        s.problems = vec!["A is not fresh".into(), "B is not fresh".into(), "C".into()];
        assert_eq!(detail(&s), "A is not fresh, B is not fresh and 1 more");
    }

    #[test]
    fn one_problem_is_just_named() {
        let mut s = snapshot();
        s.problems = vec!["Mako is not fresh".into()];
        assert_eq!(detail(&s), "Mako is not fresh");
    }

    #[test]
    fn a_server_somebody_else_started_says_so() {
        // Otherwise Stop looks like it will work on it, and it will not.
        let mut s = snapshot();
        s.server_owned = false;
        assert!(server_line(&s).contains("not started here"));
    }

    #[test]
    fn a_server_that_started_but_does_not_answer_is_not_reported_as_running() {
        let mut s = snapshot();
        s.status = Status::Down;
        assert_eq!(server_line(&s), "Web server: started, not answering");
    }

    #[test]
    fn giving_up_says_what_to_do_next() {
        let mut s = snapshot();
        s.restarts_given_up = true;
        assert!(detail(&s).contains("by hand"));
    }

    #[test]
    fn a_locked_catalog_is_not_reported_as_a_dead_server() {
        // The bug this fixes: members-only answered 401, ureq called that an
        // error, and the dot went red saying the server was not answering -
        // about a server that was answering promptly and correctly.
        let mut s = snapshot();
        s.status = Status::Attention;
        s.summary = "this catalog is members-only and needs an API token".into();

        assert!(headline(&s).starts_with("Needs attention"));
        assert!(headline(&s).contains("members-only"));
        assert_eq!(
            detail(&s),
            "Create one in Settings, copy it, then Paste API token"
        );
    }

    #[test]
    fn a_token_that_was_refused_says_so_rather_than_asking_for_one() {
        // Two ways of being refused, and only one of them is fixed by pasting.
        let mut s = snapshot();
        s.status = Status::Attention;
        s.has_token = true;
        s.summary = "the API token was refused - it may have been revoked".into();

        assert!(headline(&s).contains("refused"));
        assert_ne!(
            detail(&s),
            "Create one in Settings, copy it, then Paste API token"
        );
    }

    #[test]
    fn a_stale_source_still_reads_as_a_stale_source() {
        // The locked-catalog line must not swallow the ordinary amber state,
        // which is also Attention and has its own thing to say.
        let mut s = snapshot();
        s.status = Status::Attention;
        s.problems = vec!["Mako is not fresh".into()];

        assert_eq!(detail(&s), "Mako is not fresh");
    }

    #[test]
    fn a_source_item_carries_its_key_and_gives_it_back() {
        assert_eq!(source_key(&source_id("kan")), Some("kan"));
        assert_eq!(source_key("stop_fetch"), None);
    }

    #[test]
    fn a_phase_that_names_itself_is_not_given_a_second_article() {
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = Some("the full run".into());
        s.fetch_pid = Some(4242);
        assert_eq!(stop_fetch_label(&s), "Stop the full run (pid 4242)");
        s.running_phase = Some("sync".into());
        assert_eq!(stop_fetch_label(&s), "Stop sync (pid 4242)");
    }

    #[test]
    fn a_sweep_says_how_far_through_it_is() {
        // The whole point: "running" for two hours says nothing, and the
        // fraction is the difference between going and getting somewhere.
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = Some("sync".into());
        s.run = mid_run();
        assert_eq!(headline(&s), "Running sync · 1 of 4 services");
        assert_eq!(detail(&s), "Now: FreeTV, 12m in");
        assert_eq!(progress_label(&s.run), "Progress · 1 of 4 services");
    }

    #[test]
    fn the_progress_list_shows_what_is_done_what_is_going_and_what_is_next() {
        let lines = progress_lines(&mid_run());
        assert!(lines[0].starts_with("Started "), "{:?}", lines[0]);
        assert!(
            lines[1].starts_with("✓  Netflix - 100 items"),
            "{:?}",
            lines[1]
        );
        assert_eq!(lines[2], "▶  FreeTV - 12m so far");
        assert_eq!(&lines[3..], ["·  Kan Box", "·  Mako VOD"]);
    }

    #[test]
    fn a_run_that_has_ended_promises_nothing_further() {
        // A `sync --source` touches one service and stops. Listing the other
        // thirteen underneath it would be a queue this app invented.
        let mut run = mid_run();
        run.steps[1].state = StepState::Ok;
        run.steps[1].finished_at = Some(Utc::now());
        let lines = progress_lines(&run);
        assert_eq!(lines.len(), 3, "a header and the two steps: {lines:?}");
        assert!(lines[0].starts_with("Ran "), "{:?}", lines[0]);
        assert!(progress_label(&run).starts_with("Progress · last run"));
    }

    #[test]
    fn a_phase_that_is_not_a_sweep_is_named_rather_than_counted() {
        // During the enrich half of a full run there is no queue of services
        // left, and "14 of 14" would be a true sentence about something that
        // has stopped happening.
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = Some("the full run".into());
        let mut run = mid_run();
        run.steps[1].state = StepState::Ok;
        run.steps[1].finished_at = Some(Utc::now());
        run.steps.push(Step::sample(
            "IMDb ratings",
            "enrich",
            StepState::Running,
            &Utc::now().format("%Y-%m-%d %H:%M:%S").to_string(),
            None,
        ));
        s.run = run;
        assert_eq!(headline(&s), "Running the full run · IMDb ratings");
    }

    #[test]
    fn an_idle_menu_still_says_what_the_last_run_did() {
        // Opening the menu at nine in the morning, the question is not "is
        // anything running" - it plainly is not - but "did the night go well".
        let mut s = snapshot();
        let mut run = mid_run();
        run.steps[1].state = StepState::Failed;
        run.steps[1].finished_at = Some(Utc::now());
        s.run = run;
        assert!(
            fetch_line(&s).contains("last run finished at"),
            "{}",
            fetch_line(&s)
        );
        assert!(fetch_line(&s).contains("1 failed"), "{}", fetch_line(&s));
    }

    #[test]
    fn a_phase_is_not_named_twice_in_one_line() {
        let mut s = snapshot();
        s.fetch_running = true;
        s.running_phase = Some("the artwork pass".into());
        let mut run = mid_run();
        run.steps[1].state = StepState::Ok;
        run.steps[1].finished_at = Some(Utc::now());
        run.steps.push(Step::sample(
            "Artwork",
            "images",
            StepState::Running,
            &Utc::now().format("%Y-%m-%d %H:%M:%S").to_string(),
            None,
        ));
        s.run = run;
        assert_eq!(headline(&s), "Running the artwork pass…");
    }

    #[test]
    fn nothing_run_yet_says_so_rather_than_showing_an_empty_list() {
        assert_eq!(progress_lines(&RunView::default()), ["Nothing has run yet"]);
        assert_eq!(progress_label(&RunView::default()), "Progress");
    }
}
