//! The menu, and the words in it.
//!
//! Built once and updated in place rather than rebuilt on every reading: an
//! `NSMenu` swapped out from under an open menu is a menu that closes itself
//! while somebody is reading it, twenty seconds into every minute.

use muda::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};

use crate::health::Status;
use crate::worker::{Snapshot, UpdateView};

/// Every item the app keeps a handle on, so their labels can change.
pub struct Items {
    pub status: MenuItem,
    pub detail: MenuItem,
    pub fetch_state: MenuItem,
    pub run_sync: MenuItem,
    pub run_enrich: MenuItem,
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
    let run_sync = MenuItem::new("Run sync now", true, None);
    let run_enrich = MenuItem::new("Run enrich now", true, None);
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
        &run_sync,
        &run_enrich,
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
        run_sync,
        run_enrich,
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
        return match &snapshot.running_phase {
            Some(phase) => format!("Running {phase}…"),
            None => "A fetch is running".into(),
        };
    }
    match snapshot.status {
        Status::Ok => format!("All well · {}", snapshot.summary),
        Status::Attention => format!("Needs attention · {}", snapshot.summary),
        Status::Down => format!("Not running · {}", snapshot.summary),
        Status::Unknown => snapshot.summary.clone(),
    }
}

/// The second line: what to do about it, or what just happened.
pub fn detail(snapshot: &Snapshot) -> String {
    if snapshot.setup_problems.len() > 1 {
        return snapshot.setup_problems[1].clone();
    }
    if snapshot.restarts_given_up {
        return "Gave up restarting - start it by hand".into();
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
        return "Fetch: idle".into();
    }
    let what = match &snapshot.running_phase {
        Some(phase) => format!("running {phase}"),
        None => "running (started elsewhere)".into(),
    };
    match snapshot.fetch_pid {
        Some(pid) => format!("Fetch: {what} (pid {pid})"),
        None => format!("Fetch: {what}"),
    }
}

/// The label on the Stop item, which names what it will stop.
pub fn stop_fetch_label(snapshot: &Snapshot) -> String {
    match (&snapshot.running_phase, snapshot.fetch_pid) {
        _ if !snapshot.fetch_running => "Stop the current fetch".into(),
        (Some(phase), Some(pid)) => format!("Stop the {phase} (pid {pid})"),
        (Some(phase), None) => format!("Stop the {phase}"),
        (None, Some(pid)) => format!("Stop the running fetch (pid {pid})"),
        (None, None) => "Stop the running fetch".into(),
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

pub fn apply(items: &Items, snapshot: &Snapshot) {
    items.status.set_text(headline(snapshot));
    let detail_text = detail(snapshot);
    items.detail.set_text(&detail_text);
    // An empty second line would be a blank row in the menu; hide it instead.
    items.detail.set_enabled(false);

    items.fetch_state.set_text(fetch_line(snapshot));

    let busy = snapshot.fetch_running || matches!(snapshot.update, UpdateView::Installing { .. });
    items.run_sync.set_enabled(!busy);
    items.run_enrich.set_enabled(!busy);
    items.run_all.set_enabled(!busy);
    items.run_sync.set_text(if busy {
        "Run sync now (a run is in progress)"
    } else {
        "Run sync now"
    });
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

    let (update_text, update_enabled) = update_line(&snapshot.update);
    items.update.set_text(update_text);
    items.update.set_enabled(update_enabled);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Snapshot {
        Snapshot {
            status: Status::Ok,
            summary: "38,836 titles, every source fresh".into(),
            problems: Vec::new(),
            fetch_running: false,
            running_phase: None,
            fetch_pid: None,
            last_result: None,
            server_owned: true,
            server_pid: Some(1234),
            keep_server_up: true,
            start_server_on_open: true,
            schedule_enabled: true,
            next_run: "tomorrow at 03:00".into(),
            restarts_given_up: false,
            setup_problems: Vec::new(),
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
        assert_eq!(headline(&s), "A fetch is running");
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
}
