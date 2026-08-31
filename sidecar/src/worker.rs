//! The background half: everything that takes longer than a frame.
//!
//! The menu bar must stay responsive while a sync runs for an hour, so nothing
//! slow happens on the main thread. This owns the state instead: it polls, it
//! spawns, it schedules, and it sends a finished picture to the main thread to
//! render. The main thread never asks it a question and waits for an answer -
//! it posts a command and carries on.

use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

use chrono::Local;

use crate::config::Config;
use crate::health::{self, Health, Status};
use crate::procs::{self, FetcherState, Phase, Server};
use crate::{platform, schedule, update};

/// How often to ask the API how it is.
const POLL_EVERY: Duration = Duration::from_secs(20);
/// How often to ask GitHub whether there is a newer release. Twice a day: an
/// update is not urgent, and a menu-bar app that hammers an API is a bad guest.
const UPDATE_CHECK_EVERY: Duration = Duration::from_secs(12 * 60 * 60);
/// How long to leave a restarted server before believing the next reading.
/// Uvicorn needs a moment to bind; without this the restart looks like a
/// failure and triggers another one.
const SETTLE: Duration = Duration::from_secs(6);
/// Waits between automatic restart attempts, then it stops trying and says so.
/// Backing off rather than hammering: whatever is wrong at attempt four is not
/// going to be fixed by attempt forty, and a restart loop hides the cause.
const BACKOFF: [u64; 4] = [2, 10, 30, 120];

/// What the main thread can ask for.
///
/// Not comparable: `Shutdown` carries the channel it will be acknowledged on,
/// and a one-shot sender is not a value to compare.
#[derive(Debug)]
pub enum Command {
    Refresh,
    /// Stop the server and acknowledge, so Quit can wait for it.
    Shutdown(std::sync::mpsc::Sender<()>),
    Run(Phase),
    /// Stop the fetch that is running now, whoever started it.
    StopFetch,
    StartServer,
    StopServer,
    RestartServer,
    SetKeepServerUp(bool),
    SetStartServerOnOpen(bool),
    SetScheduleEnabled(bool),
    /// Ask GitHub now, rather than waiting for the twice-a-day check.
    CheckUpdate,
    /// Move the checkout to the newer release and relaunch.
    RunUpdate,
}

/// What the menu's update line says and offers.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum UpdateView {
    /// Not checked yet this session.
    #[default]
    Unknown,
    UpToDate {
        version: String,
    },
    Checking,
    Available {
        tag: String,
    },
    Installing {
        tag: String,
    },
    /// The last attempt failed; the reason is on the detail line.
    Failed,
}

/// The finished picture the menu renders.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub status: Status,
    pub summary: String,
    pub problems: Vec<String>,
    /// True while any fetch is running, whoever started it.
    pub fetch_running: bool,
    /// The phase, when this app started the fetch and so knows it.
    pub running_phase: Option<String>,
    /// The fetcher's pid, for naming it on the Stop item.
    pub fetch_pid: Option<u32>,
    /// What the last run did, once it is over.
    pub last_result: Option<String>,
    pub server_owned: bool,
    pub server_pid: Option<u32>,
    pub keep_server_up: bool,
    pub start_server_on_open: bool,
    pub schedule_enabled: bool,
    pub next_run: String,
    pub restarts_given_up: bool,
    /// Problems with the configured directory itself, which outrank everything.
    pub setup_problems: Vec<String>,
    /// Whether a newer release exists, and how far along installing it is.
    pub update: UpdateView,
    /// Set once, when an update has been built and the app must relaunch to
    /// finish it. The main thread reads this and quits into the new bundle.
    pub relaunch: bool,
}

pub fn spawn(
    config: Config,
    commands: Receiver<Command>,
    updates: Sender<Snapshot>,
    wake: impl Fn() + Send + 'static,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut worker = Worker::new(config, updates, Box::new(wake));
        worker.run(commands);
    })
}

struct Worker {
    config: Config,
    server: Server,
    updates: Sender<Snapshot>,
    wake: Box<dyn Fn() + Send>,
    health: Health,
    fetch: Option<procs::Fetch>,
    last_result: Option<String>,
    restart_attempt: usize,
    restarts_given_up: bool,
    quiet_until: Option<Instant>,
    update: UpdateView,
    /// The release the Available/Installing states refer to.
    pending_update: Option<update::Release>,
    update_job: Option<procs::Job>,
    last_update_check: Option<Instant>,
    relaunch: bool,
}

impl Worker {
    fn new(config: Config, updates: Sender<Snapshot>, wake: Box<dyn Fn() + Send>) -> Self {
        Self {
            config,
            server: Server::new(),
            updates,
            wake,
            health: Health::unknown("starting up"),
            fetch: None,
            last_result: None,
            restart_attempt: 0,
            restarts_given_up: false,
            quiet_until: None,
            update: UpdateView::Unknown,
            pending_update: None,
            update_job: None,
            last_update_check: None,
            relaunch: false,
        }
    }

    fn run(&mut self, commands: Receiver<Command>) {
        // Adopt whatever is already serving before starting anything: a server
        // the user started by hand is still a server, and starting a second one
        // would only collide on the port.
        self.poll();
        // Opening Eifo brings the server up by default: a companion whose whole
        // job is "is the catalog answering" is not much use sitting next to a
        // server it could have started.
        if self.health.status == Status::Down && self.config.start_server_on_open {
            self.start_server();
        }
        self.publish();

        loop {
            // A running fetch is watched more closely than the API is polled:
            // the point of the Stop item is that it takes effect promptly, and
            // a finished run should show its result without a twenty-second wait.
            let wait = if self.fetch.is_some() || self.update_job.is_some() {
                Duration::from_secs(3)
            } else {
                POLL_EVERY
            };
            match commands.recv_timeout(wait) {
                // Quit from the menu waits on this: on macOS the event loop
                // never returns from `run`, so without an acknowledgement the
                // process can exit before the server has actually gone.
                Ok(Command::Shutdown(ack)) => {
                    self.server.stop();
                    let _ = ack.send(());
                    return;
                }
                Ok(command) => {
                    self.handle(command);
                    self.publish();
                }
                Err(RecvTimeoutError::Timeout) => {
                    self.tick();
                    self.publish();
                }
                Err(RecvTimeoutError::Disconnected) => {
                    self.server.stop();
                    return;
                }
            }
        }
    }

    fn handle(&mut self, command: Command) {
        match command {
            Command::Refresh => self.poll(),
            Command::Run(phase) => self.start_phase(phase),
            Command::StopFetch => self.stop_fetch(),
            Command::StartServer => {
                self.restarts_given_up = false;
                self.restart_attempt = 0;
                self.start_server();
            }
            Command::StopServer => {
                // A deliberate stop must not be undone two seconds later by the
                // thing that watches for the server being down.
                self.config.keep_server_up = false;
                let _ = self.config.save();
                self.server.stop();
                self.health = Health::down("the web server is stopped");
            }
            Command::RestartServer => {
                self.server.stop();
                self.restarts_given_up = false;
                self.restart_attempt = 0;
                self.start_server();
            }
            Command::SetKeepServerUp(on) => {
                self.config.keep_server_up = on;
                self.restarts_given_up = false;
                self.restart_attempt = 0;
                let _ = self.config.save();
            }
            Command::SetStartServerOnOpen(on) => {
                self.config.start_server_on_open = on;
                let _ = self.config.save();
            }
            Command::SetScheduleEnabled(on) => {
                self.config.schedule_enabled = on;
                let _ = self.config.save();
            }
            Command::CheckUpdate => self.check_update(),
            Command::RunUpdate => self.run_update(),
            Command::Shutdown(_) => {}
        }
    }

    /// One turn of the loop: is it time to run, and is the server still there.
    fn tick(&mut self) {
        self.reap_fetch();
        self.reap_update();
        self.maybe_check_update();

        // While an update runs, the checkout is being rewritten and the server
        // is deliberately down. Nothing else should start a fetch or resurrect
        // the server on top of that.
        if self.update_job.is_some() {
            return;
        }

        if self.fetch.is_none() && self.config.schedule_enabled {
            if let Some(at) = schedule::parse_time(&self.config.nightly) {
                let now = Local::now();
                if schedule::is_due(now, at, self.config.last_nightly_on.as_deref()) {
                    // Recorded before the run rather than after: a run that
                    // fails is still tonight's run, and retrying it every
                    // twenty seconds until midnight would be worse than
                    // waiting for tomorrow.
                    self.config.last_nightly_on = Some(schedule::day_of(now));
                    let _ = self.config.save();
                    self.start_phase(Phase::All);
                    return;
                }
            }
        }

        // A source somebody has just switched on in the Manage tab. The daemon
        // polls for these every thirty seconds; without something doing it, the
        // ask waits until the next nightly.
        if self.fetch.is_none() && procs::pending_backfills(&self.config) > 0 {
            if let FetcherState::Idle = procs::fetcher_state(&self.config) {
                self.start_phase(Phase::Sync);
                return;
            }
        }

        self.poll();
        self.supervise();
    }

    fn poll(&mut self) {
        if !self.config.problems().is_empty() {
            self.health = Health::unknown("the Eifo folder needs attention");
            return;
        }
        // A server still binding its port has not failed; believing the reading
        // during that window is how a restart turns into a restart loop.
        if let Some(until) = self.quiet_until {
            if Instant::now() < until {
                return;
            }
            self.quiet_until = None;
        }
        self.health = health::poll(&self.config.base_url);
    }

    /// Put the server back up if it has gone, unless told not to.
    fn supervise(&mut self) {
        if self.health.status != Status::Down
            || !self.config.keep_server_up
            || self.restarts_given_up
        {
            if self.health.status != Status::Down {
                self.restart_attempt = 0;
            }
            return;
        }

        if self.restart_attempt >= BACKOFF.len() {
            // Whatever is wrong is not a transient, and a restart loop would
            // bury the reason under its own noise.
            self.restarts_given_up = true;
            self.last_result = Some("gave up restarting the server".into());
            return;
        }

        std::thread::sleep(Duration::from_secs(BACKOFF[self.restart_attempt]));
        self.restart_attempt += 1;
        self.start_server();
    }

    fn start_server(&mut self) {
        match self.server.start(&self.config) {
            Ok(()) => {
                self.quiet_until = Some(Instant::now() + SETTLE);
                self.health = Health::unknown("starting the web server");
            }
            Err(err) => {
                self.last_result = Some(err.clone());
                self.health = Health::down(err);
            }
        }
    }

    /// Start a fetcher phase. It runs in its own process; the loop watches it.
    fn start_phase(&mut self, phase: Phase) {
        if self.fetch.is_some() || self.update_job.is_some() {
            return;
        }
        self.last_result = None;
        match procs::start_phase(&self.config, phase) {
            Ok(fetch) => self.fetch = Some(fetch),
            Err(err) => self.last_result = Some(err),
        }
    }

    /// If the running fetch has finished, record what it did and refresh.
    fn reap_fetch(&mut self) {
        let Some(fetch) = self.fetch.as_mut() else {
            return;
        };
        let Some(outcome) = fetch.poll() else {
            return;
        };
        let phase = fetch.phase;
        self.fetch = None;
        self.last_result = Some(match outcome {
            Ok(()) => format!("{} finished", capitalise(phase.label())),
            Err(err) => err,
        });
        // The catalog just changed, so the reading from before it is stale.
        self.poll();
    }

    /// Stop whatever fetch is running: a clean signal to our own child, or a
    /// kill by pid for one another process started.
    fn stop_fetch(&mut self) {
        if let Some(mut fetch) = self.fetch.take() {
            let phase = fetch.phase;
            fetch.stop();
            self.last_result = Some(format!("{} stopped", capitalise(phase.label())));
            self.poll();
            return;
        }
        if let FetcherState::Running { pid: Some(pid) } = procs::fetcher_state(&self.config) {
            procs::stop_external(pid);
            self.last_result = Some("stopped the running fetch".into());
            self.poll();
        }
    }

    /// Ask GitHub about the newest release once the interval is up - or once on
    /// the first tick after launch, so a stale checkout is flagged promptly.
    ///
    /// Runs even when the folder has problems: knowing a fix is out is worth
    /// more then, not less. Only a job already in flight defers it.
    fn maybe_check_update(&mut self) {
        if self.update_job.is_some() || self.fetch.is_some() {
            return;
        }
        let due = match self.last_update_check {
            None => true,
            Some(at) => at.elapsed() >= UPDATE_CHECK_EVERY,
        };
        if due {
            self.check_update();
        }
    }

    /// One check: the tag the checkout is on against GitHub's latest release.
    fn check_update(&mut self) {
        self.last_update_check = Some(Instant::now());
        self.update = UpdateView::Checking;
        self.publish();

        let current = update::current_version(&self.config.app_dir);
        match update::latest_release() {
            Some(release) if release.version > current => {
                let tag = release.tag.clone();
                // Notify once per version, not once per check.
                if self.config.update_notified_version.as_deref() != Some(&tag) {
                    platform::notify(
                        &format!("Eifo {tag} is available"),
                        "Open the menu-bar menu and choose Update.",
                    );
                    self.config.update_notified_version = Some(tag.clone());
                    let _ = self.config.save();
                }
                self.pending_update = Some(release);
                self.update = UpdateView::Available { tag };
            }
            Some(_) => {
                self.pending_update = None;
                self.update = UpdateView::UpToDate {
                    version: version_string(current),
                };
            }
            // A check that could not reach GitHub is not news; keep whatever the
            // menu last said rather than claiming anything.
            None => {
                self.update = match &self.pending_update {
                    Some(release) => UpdateView::Available {
                        tag: release.tag.clone(),
                    },
                    None => UpdateView::Unknown,
                };
            }
        }
    }

    /// Start the update script for the pending release.
    ///
    /// The server is stopped first: the script re-syncs the environment, moves
    /// the source files and runs migrations, and none of that should happen
    /// under a live server holding the database open. The relaunch brings a
    /// fresh one up on the new code.
    fn run_update(&mut self) {
        let Some(release) = self.pending_update.clone() else {
            return;
        };
        if self.update_job.is_some() || self.fetch.is_some() {
            return;
        }
        self.last_result = None;
        self.server.stop();
        self.health = Health::unknown("updating Eifo");
        match procs::start_update(&self.config.app_dir, &release.tag) {
            Ok(job) => {
                self.update_job = Some(job);
                self.update = UpdateView::Installing { tag: release.tag };
            }
            Err(err) => {
                self.last_result = Some(format!("update failed: {err}"));
                self.update = UpdateView::Failed;
            }
        }
    }

    /// If the update script has finished, relaunch on success or say why not.
    fn reap_update(&mut self) {
        let Some(job) = self.update_job.as_mut() else {
            return;
        };
        let Some(outcome) = job.poll() else {
            return;
        };
        self.update_job = None;
        match outcome {
            Ok(()) => self.finish_update(),
            Err(err) => {
                self.last_result = Some(format!("update failed: {err}"));
                self.update = UpdateView::Failed;
                // It may be half-applied; the next check re-reads the checkout.
                self.pending_update = None;
            }
        }
    }

    /// The update is built. Stop our server, arrange for the new bundle to open
    /// once we have exited, and ask the main thread to quit into it.
    fn finish_update(&mut self) {
        self.server.stop();
        let app = self.config.app_dir.join("sidecar/target/Eifo.app");
        spawn_relauncher(&app);
        self.relaunch = true;
    }

    fn publish(&mut self) {
        let setup_problems = self.config.problems();
        let status = if !setup_problems.is_empty() {
            Status::Unknown
        } else {
            self.health.status
        };
        let next_run = match schedule::parse_time(&self.config.nightly) {
            Some(at) if self.config.schedule_enabled => schedule::describe_next(Local::now(), at),
            Some(_) => "off".into(),
            None => format!("invalid time {:?}", self.config.nightly),
        };

        // Ours if we started it; otherwise whatever the lock file says, so a
        // nightly run fired by the LaunchAgent or an `eifo-fetch` typed in a
        // terminal still shows up here.
        let (fetch_running, running_phase, fetch_pid) = match &self.fetch {
            Some(fetch) => (
                true,
                Some(fetch.phase.label().to_string()),
                Some(fetch.pid()),
            ),
            None => match procs::fetcher_state(&self.config) {
                FetcherState::Running { pid } => (true, None, pid),
                FetcherState::Idle => (false, None, None),
            },
        };

        let snapshot = Snapshot {
            status,
            summary: self.health.summary.clone(),
            problems: self.health.problems.clone(),
            fetch_running,
            running_phase,
            fetch_pid,
            last_result: self.last_result.clone(),
            server_owned: self.server.is_running(),
            server_pid: self.server.pid(),
            keep_server_up: self.config.keep_server_up,
            start_server_on_open: self.config.start_server_on_open,
            schedule_enabled: self.config.schedule_enabled,
            next_run,
            restarts_given_up: self.restarts_given_up,
            setup_problems,
            update: self.update.clone(),
            relaunch: self.relaunch,
        };
        let _ = self.updates.send(snapshot);
        (self.wake)();
    }
}

/// `(0, 3, 0)` as `v0.3.0`, the way the menu and the tags say it.
fn version_string(version: update::Version) -> String {
    format!("v{}.{}.{}", version.0, version.1, version.2)
}

/// Spawn a detached shell that waits for this process to exit, then opens the
/// freshly built bundle. Detached on purpose: it has to outlive us.
fn spawn_relauncher(app: &std::path::Path) {
    let pid = std::process::id();
    let path = app.display().to_string().replace('\'', r"'\''");
    let _ = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done; \
             sleep 1; open '{path}'"
        ))
        .spawn();
}

fn capitalise(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_read_as_sentences() {
        assert_eq!(capitalise("sync"), "Sync");
        assert_eq!(capitalise("the full run"), "The full run");
        assert_eq!(capitalise(""), "");
    }

    #[test]
    fn the_backoff_climbs_and_then_stops() {
        // A restart loop hides the cause; four tries over about three minutes
        // is enough for a transient and short of hammering.
        assert!(BACKOFF.windows(2).all(|pair| pair[0] < pair[1]));
        assert_eq!(BACKOFF.len(), 4);
    }
}
