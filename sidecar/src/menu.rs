//! The menu, and the words in it.
//!
//! Built once and updated in place rather than rebuilt on every reading: an
//! `NSMenu` swapped out from under an open menu is a menu that closes itself
//! while somebody is reading it, twenty seconds into every minute.

use muda::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};

use crate::health::Status;
use crate::worker::Snapshot;

/// Every item the app keeps a handle on, so their labels can change.
pub struct Items {
    pub status: MenuItem,
    pub detail: MenuItem,
    pub run_sync: MenuItem,
    pub run_enrich: MenuItem,
    pub run_all: MenuItem,
    pub check_now: MenuItem,
    pub server_state: MenuItem,
    pub start_server: MenuItem,
    pub stop_server: MenuItem,
    pub restart_server: MenuItem,
    pub keep_up: CheckMenuItem,
    pub schedule: CheckMenuItem,
    pub next_run: MenuItem,
    pub open_app: MenuItem,
    pub open_manage: MenuItem,
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

    let run_sync = MenuItem::new("Run sync now", true, None);
    let run_enrich = MenuItem::new("Run enrich now", true, None);
    let run_all = MenuItem::new("Run everything now", true, None);
    let check_now = MenuItem::new("Check now", true, None);
    let schedule = CheckMenuItem::new("Run nightly", true, true, None);
    let next_run = MenuItem::new("", false, None);

    let server_state = MenuItem::new("Web server", false, None);
    let start_server = MenuItem::new("Start", true, None);
    let stop_server = MenuItem::new("Stop", true, None);
    let restart_server = MenuItem::new("Restart", true, None);
    let keep_up = CheckMenuItem::new("Restart if it stops", true, true, None);

    let open_app = MenuItem::new("Open Eifo", true, None);
    let open_manage = MenuItem::new("Open Manage", true, None);
    let choose_folder = MenuItem::new("Choose Eifo folder…", true, None);
    let login_item = CheckMenuItem::new("Open at login", true, login_enabled, None);
    let about = MenuItem::new("About Eifo", true, None);
    let quit = MenuItem::new("Quit", true, None);

    let separator = PredefinedMenuItem::separator;
    let _ = menu.append_items(&[
        &status,
        &detail,
        &separator(),
        &run_sync,
        &run_enrich,
        &run_all,
        &separator(),
        &check_now,
        &schedule,
        &next_run,
        &separator(),
        &server_state,
        &start_server,
        &stop_server,
        &restart_server,
        &keep_up,
        &separator(),
        &open_app,
        &open_manage,
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
        run_sync,
        run_enrich,
        run_all,
        check_now,
        server_state,
        start_server,
        stop_server,
        restart_server,
        keep_up,
        schedule,
        next_run,
        open_app,
        open_manage,
        choose_folder,
        login_item,
        about,
        quit,
    };
    (menu, items)
}

/// The headline line, which is the one thing read at a glance.
pub fn headline(snapshot: &Snapshot) -> String {
    if !snapshot.setup_problems.is_empty() {
        return snapshot.setup_problems[0].clone();
    }
    if let Some(phase) = &snapshot.running_phase {
        return format!("Running {phase}…");
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

    let busy = snapshot.running_phase.is_some();
    items.run_sync.set_enabled(!busy);
    items.run_enrich.set_enabled(!busy);
    items.run_all.set_enabled(!busy);
    items.run_sync.set_text(if busy {
        "Run sync now (a run is in progress)"
    } else {
        "Run sync now"
    });

    items.schedule.set_checked(snapshot.schedule_enabled);
    items
        .next_run
        .set_text(format!("Next run: {}", snapshot.next_run));

    items.server_state.set_text(server_line(snapshot));
    items.start_server.set_enabled(!snapshot.server_owned);
    items.stop_server.set_enabled(snapshot.server_owned);
    items.keep_up.set_checked(snapshot.keep_server_up);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Snapshot {
        Snapshot {
            status: Status::Ok,
            summary: "38,836 titles, every source fresh".into(),
            problems: Vec::new(),
            running_phase: None,
            last_result: None,
            server_owned: true,
            server_pid: Some(1234),
            keep_server_up: true,
            schedule_enabled: true,
            next_run: "tomorrow at 03:00".into(),
            restarts_given_up: false,
            setup_problems: Vec::new(),
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
        s.running_phase = Some("sync".into());
        assert_eq!(headline(&s), "Running sync…");
    }

    #[test]
    fn a_broken_folder_outranks_everything() {
        let mut s = snapshot();
        s.setup_problems = vec!["/nope is not an Eifo checkout".into()];
        assert!(headline(&s).contains("not an Eifo checkout"));
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
