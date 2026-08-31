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
use crate::schedule;

/// How often to ask the API how it is.
const POLL_EVERY: Duration = Duration::from_secs(20);
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
    StartServer,
    StopServer,
    RestartServer,
    SetKeepServerUp(bool),
    SetScheduleEnabled(bool),
}

/// The finished picture the menu renders.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub status: Status,
    pub summary: String,
    pub problems: Vec<String>,
    /// Set while a fetcher phase is running, naming which.
    pub running_phase: Option<String>,
    /// What the last run did, once it is over.
    pub last_result: Option<String>,
    pub server_owned: bool,
    pub server_pid: Option<u32>,
    pub keep_server_up: bool,
    pub schedule_enabled: bool,
    pub next_run: String,
    pub restarts_given_up: bool,
    /// Problems with the configured directory itself, which outrank everything.
    pub setup_problems: Vec<String>,
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
    running_phase: Option<Phase>,
    last_result: Option<String>,
    restart_attempt: usize,
    restarts_given_up: bool,
    quiet_until: Option<Instant>,
}

impl Worker {
    fn new(config: Config, updates: Sender<Snapshot>, wake: Box<dyn Fn() + Send>) -> Self {
        Self {
            config,
            server: Server::new(),
            updates,
            wake,
            health: Health::unknown("starting up"),
            running_phase: None,
            last_result: None,
            restart_attempt: 0,
            restarts_given_up: false,
            quiet_until: None,
        }
    }

    fn run(&mut self, commands: Receiver<Command>) {
        // Adopt whatever is already serving before starting anything: a server
        // the user started by hand is still a server, and starting a second one
        // would only collide on the port.
        self.poll();
        if self.health.status == Status::Down && self.config.keep_server_up {
            self.start_server();
        }
        self.publish();

        loop {
            match commands.recv_timeout(POLL_EVERY) {
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
            Command::Run(phase) => self.run_phase(phase),
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
            Command::SetScheduleEnabled(on) => {
                self.config.schedule_enabled = on;
                let _ = self.config.save();
            }
            Command::Shutdown(_) => {}
        }
    }

    /// One turn of the loop: is it time to run, and is the server still there.
    fn tick(&mut self) {
        if self.running_phase.is_none() && self.config.schedule_enabled {
            if let Some(at) = schedule::parse_time(&self.config.nightly) {
                let now = Local::now();
                if schedule::is_due(now, at, self.config.last_nightly_on.as_deref()) {
                    // Recorded before the run rather than after: a run that
                    // fails is still tonight's run, and retrying it every
                    // twenty seconds until midnight would be worse than
                    // waiting for tomorrow.
                    self.config.last_nightly_on = Some(schedule::day_of(now));
                    let _ = self.config.save();
                    self.run_phase(Phase::All);
                    return;
                }
            }
        }

        // A source somebody has just switched on in the Manage tab. The daemon
        // polls for these every thirty seconds; without something doing it, the
        // ask waits until the next nightly.
        if self.running_phase.is_none() && procs::pending_backfills(&self.config) > 0 {
            if let FetcherState::Idle = procs::fetcher_state(&self.config) {
                self.run_phase(Phase::Sync);
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

    /// Run a fetcher phase, keeping the menu honest while it happens.
    fn run_phase(&mut self, phase: Phase) {
        self.running_phase = Some(phase);
        self.last_result = None;
        self.publish();

        let result = procs::run_phase(&self.config, phase);

        self.running_phase = None;
        self.last_result = Some(match result {
            Ok(()) => format!("{} finished", capitalise(phase.label())),
            Err(err) => err,
        });
        // The catalog just changed, so the reading from before it is stale.
        self.poll();
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

        let snapshot = Snapshot {
            status,
            summary: self.health.summary.clone(),
            problems: self.health.problems.clone(),
            running_phase: self.running_phase.map(|p| p.label().to_string()),
            last_result: self.last_result.clone(),
            server_owned: self.server.is_running(),
            server_pid: self.server.pid(),
            keep_server_up: self.config.keep_server_up,
            schedule_enabled: self.config.schedule_enabled,
            next_run,
            restarts_given_up: self.restarts_given_up,
            setup_problems,
        };
        let _ = self.updates.send(snapshot);
        (self.wake)();
    }
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
