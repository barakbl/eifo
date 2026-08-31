//! Starting things, stopping them, and never starting two of the same.
//!
//! Two rules here exist because the things being run are not reentrant:
//!
//! * **The server is one process, owned outright.** `.venv/bin/uvicorn` is
//!   exec'd directly rather than through `uv run`, which is a wrapper that
//!   spawns the real server as a child - stop the wrapper and the server can
//!   outlive it, still holding the port, which is exactly the state that makes
//!   "restart it when it is down" restart into a port collision.
//! * **The fetcher is asked for permission first.** It arbitrates itself with
//!   an advisory lock on `data/.eifo-fetch.lock`, so before running one this
//!   checks whether the lock is free. A second fetcher would not corrupt
//!   anything - SQLite would see to that - but both would ask every source for
//!   the same catalog at the same time, which is the behaviour a scraper should
//!   not exhibit.

use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};

use crate::config::Config;

/// The pid of the server this app started, for the signal handler to reach.
///
/// A global because a signal handler may touch almost nothing: no allocation,
/// no locks, no `&mut`. An atomic and `kill(2)` are both async-signal-safe, and
/// between them they are enough to keep a terminated app from leaving a server
/// behind holding the port.
static SERVER_PID: AtomicI32 = AtomicI32::new(0);

/// What the fetcher lock says about who, if anyone, is running.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetcherState {
    Idle,
    /// Somebody holds the lock. The pid is whatever they wrote into the file,
    /// which is for saying so in the menu, not for acting on.
    Running {
        pid: Option<u32>,
    },
}

/// Whether a fetcher holds the single-flight lock right now.
///
/// Asked by taking the lock non-blocking and letting it go again: the only
/// honest test of an advisory lock is trying to take it. The pid in the file is
/// read for the menu's benefit, but never trusted - a stale file with a live
/// pid in it is possible, an unheld `flock` is not.
pub fn fetcher_state(config: &Config) -> FetcherState {
    let path = config.lock_file();
    let Ok(file) = File::open(&path) else {
        // No lock file at all means no fetcher has ever run here.
        return FetcherState::Idle;
    };

    if flock_is_free(&file) {
        FetcherState::Idle
    } else {
        FetcherState::Running {
            pid: read_pid(&path),
        }
    }
}

/// Try to take a shared lock without blocking, then release it.
fn flock_is_free(file: &File) -> bool {
    use std::os::unix::io::AsRawFd;
    // SAFETY: a libc call on a fd this function owns for its whole duration.
    unsafe {
        let fd = file.as_raw_fd();
        // LOCK_EX | LOCK_NB - if anyone holds it we get EWOULDBLOCK.
        if libc_flock(fd, 2 | 4) == 0 {
            libc_flock(fd, 8); // LOCK_UN
            true
        } else {
            false
        }
    }
}

extern "C" {
    #[link_name = "flock"]
    fn libc_flock(fd: i32, operation: i32) -> i32;
}

fn read_pid(path: &Path) -> Option<u32> {
    let mut text = String::new();
    File::open(path).ok()?.read_to_string(&mut text).ok()?;
    text.split_whitespace().last()?.parse().ok()
}

/// The web server, as this app runs it.
pub struct Server {
    child: Option<Child>,
}

impl Server {
    pub fn new() -> Self {
        Self { child: None }
    }

    /// Whether the process this app started is still alive.
    ///
    /// Not the same question as "is the server answering" - a process can be
    /// alive and wedged - which is why the traffic light asks the API and this
    /// only reports on the child. Both are shown, because when they disagree
    /// that disagreement is the useful part.
    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            None => false,
            Some(child) => matches!(child.try_wait(), Ok(None)),
        }
    }

    pub fn pid(&self) -> Option<u32> {
        self.child.as_ref().map(|c| c.id())
    }

    /// Start it, unless this app already has one running.
    pub fn start(&mut self, config: &Config) -> Result<(), String> {
        if self.is_running() {
            return Ok(());
        }
        let uvicorn = config.uvicorn();
        if !uvicorn.exists() {
            return Err(format!("{} does not exist", uvicorn.display()));
        }
        let (host, port) = config.host_port();

        let child = Command::new(&uvicorn)
            .arg("eifo_api.main:app")
            .args(["--host", &host])
            .args(["--port", &port.to_string()])
            .current_dir(&config.app_dir)
            // No --reload: a watcher forks on every file change, which turns
            // one owned process back into a tree this app cannot stop cleanly.
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|err| format!("could not start the server: {err}"))?;

        SERVER_PID.store(child.id() as i32, Ordering::SeqCst);
        self.child = Some(child);
        Ok(())
    }

    /// Ask it to stop, and wait for it to actually be gone.
    ///
    /// SIGTERM first, because uvicorn shuts its workers down on it. SIGKILL
    /// only if it is still there, so a restart cannot race a process that has
    /// not yet let go of the port.
    pub fn stop(&mut self) {
        let Some(child) = self.child.as_mut() else {
            return;
        };
        let pid = child.id();
        // SAFETY: a signal to a pid this struct owns.
        unsafe { libc_kill(pid as i32, 15) };

        for _ in 0..50 {
            if let Ok(Some(_)) = child.try_wait() {
                self.child = None;
                SERVER_PID.store(0, Ordering::SeqCst);
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }

        let _ = child.kill();
        let _ = child.wait();
        self.child = None;
        SERVER_PID.store(0, Ordering::SeqCst);
    }
}

extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
    #[link_name = "signal"]
    fn libc_signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    #[link_name = "_exit"]
    fn libc_exit(code: i32) -> !;
}

/// Take the server down with us when the app is terminated.
///
/// `Drop` is not enough and never was: a process killed by a signal runs no
/// destructors, so a `kill` from the shell - or a logout, or a crash reporter -
/// left the server this app started running, still holding the port. The next
/// launch would then find something answering, decide all was well, and never
/// own the process it was reporting on. Found by killing the app and watching
/// the port stay open.
extern "C" fn on_terminate(_signal: i32) {
    // Only async-signal-safe calls here: an atomic load, kill(2), _exit(2).
    let pid = SERVER_PID.load(Ordering::SeqCst);
    if pid > 0 {
        unsafe { libc_kill(pid, 15) };
    }
    unsafe { libc_exit(0) }
}

/// Install the handlers. Called once, before anything is spawned.
pub fn catch_termination() {
    for signal in [
        1,  /* SIGHUP */
        2,  /* SIGINT */
        15, /* SIGTERM */
    ] {
        unsafe { libc_signal(signal, on_terminate) };
    }
}

impl Drop for Server {
    /// A server this app started should not outlive it holding the port.
    fn drop(&mut self) {
        self.stop();
    }
}

/// One phase of the fetcher, run to completion.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Sync,
    Enrich,
    All,
}

impl Phase {
    pub fn argument(self) -> &'static str {
        match self {
            Phase::Sync => "sync",
            Phase::Enrich => "enrich",
            Phase::All => "all",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Phase::Sync => "sync",
            Phase::Enrich => "enrich",
            Phase::All => "the full run",
        }
    }
}

/// A fetcher phase this app started and is watching.
///
/// Owned as a `Child` rather than run to completion in place: a phase takes
/// minutes to hours, and the worker thread has to stay free to poll the API and
/// to act on a Stop while it runs.
pub struct Fetch {
    child: Child,
    pub phase: Phase,
}

impl Fetch {
    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// `None` while it is still running; the outcome once it is over.
    pub fn poll(&mut self) -> Option<Result<(), String>> {
        match self.child.try_wait() {
            Ok(None) => None,
            Ok(Some(status)) => Some(phase_outcome(self.phase, status.code())),
            Err(err) => Some(Err(format!("lost track of {}: {err}", self.phase.label()))),
        }
    }

    /// Ask it to stop, then make sure it has. SIGTERM first, so the fetcher can
    /// close the database and release its lock; SIGKILL only if it ignores it.
    pub fn stop(&mut self) {
        let pid = self.child.id() as i32;
        // SAFETY: a signal to a pid this struct owns.
        unsafe { libc_kill(pid, 15) };
        for _ in 0..50 {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Start a fetcher phase, refusing if one is already going.
///
/// Returns immediately with a handle to watch; it does not wait for the run.
pub fn start_phase(config: &Config, phase: Phase) -> Result<Fetch, String> {
    if let FetcherState::Running { pid } = fetcher_state(config) {
        return Err(match pid {
            Some(pid) => format!("a fetcher is already running (pid {pid})"),
            None => "a fetcher is already running".into(),
        });
    }

    let fetcher = config.fetcher();
    if !fetcher.exists() {
        return Err(format!("{} does not exist", fetcher.display()));
    }

    let child = Command::new(&fetcher)
        .arg(phase.argument())
        .current_dir(&config.app_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("could not run {}: {err}", phase.label()))?;

    Ok(Fetch { child, phase })
}

/// Stop a fetcher this app did not start, by the pid in its lock file.
///
/// Best effort: the pid is read from a file and could in principle be stale, so
/// this sends SIGTERM, gives it a moment, and follows with SIGKILL only if
/// something with that pid is still there. The advisory lock frees either way.
pub fn stop_external(pid: u32) {
    let pid = pid as i32;
    // SAFETY: kill(2) is async-signal-safe and harmless on a pid that is gone.
    unsafe { libc_kill(pid, 15) };
    for _ in 0..30 {
        std::thread::sleep(std::time::Duration::from_millis(100));
        if unsafe { libc_kill(pid, 0) } != 0 {
            return;
        }
    }
    unsafe { libc_kill(pid, 9) };
}

/// How the fetcher's exit code reads as an outcome.
fn phase_outcome(phase: Phase, code: Option<i32>) -> Result<(), String> {
    // 2 is the fetcher's "finished, but some source failed" - a real outcome
    // worth reporting differently from a crash, because the catalog did update.
    match code {
        Some(0) => Ok(()),
        Some(2) => Err(format!("{} finished with source failures", phase.label())),
        Some(code) => Err(format!("{} exited {code}", phase.label())),
        None => Err(format!("{} was stopped", phase.label())),
    }
}

/// How many sources an operator has switched on and is waiting for.
///
/// Read straight from the database, read-only, because there is no other way to
/// ask: the endpoint that knows is behind the admin session this app does not
/// have. One indexed count over a table with a dozen rows in it.
pub fn pending_backfills(config: &Config) -> usize {
    let path = config.database();
    if !path.exists() {
        return 0;
    }
    let flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI;
    let Ok(db) = rusqlite::Connection::open_with_flags(&path, flags) else {
        return 0;
    };
    db.query_row(
        "SELECT count(*) FROM sources WHERE backfill_requested_at IS NOT NULL",
        [],
        |row| row.get::<_, i64>(0),
    )
    .map(|n| n as usize)
    .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn config_in(dir: &Path) -> Config {
        std::fs::create_dir_all(dir.join("data")).unwrap();
        Config::new(dir.to_path_buf())
    }

    #[test]
    fn no_lock_file_means_nothing_is_running() {
        let dir = tempdir();
        assert_eq!(fetcher_state(&config_in(&dir)), FetcherState::Idle);
    }

    #[test]
    fn an_unheld_lock_file_means_nothing_is_running() {
        // A fetcher killed mid-run leaves the file behind; the OS releases the
        // lock. The file is not the claim - the lock is.
        let dir = tempdir();
        let config = config_in(&dir);
        let mut file = File::create(config.lock_file()).unwrap();
        writeln!(file, "pid 99999").unwrap();
        drop(file);
        assert_eq!(fetcher_state(&config), FetcherState::Idle);
    }

    #[test]
    fn a_held_lock_is_reported_with_whoever_wrote_the_pid() {
        let dir = tempdir();
        let config = config_in(&dir);
        let path = config.lock_file();
        let mut file = File::create(&path).unwrap();
        write!(file, "pid 4242").unwrap();
        file.flush().unwrap();

        use std::os::unix::io::AsRawFd;
        unsafe { libc_flock(file.as_raw_fd(), 2 | 4) };

        assert_eq!(
            fetcher_state(&config),
            FetcherState::Running { pid: Some(4242) }
        );
        unsafe { libc_flock(file.as_raw_fd(), 8) };
    }

    #[test]
    fn a_phase_refuses_to_start_beside_a_running_fetcher() {
        let dir = tempdir();
        let config = config_in(&dir);
        let path = config.lock_file();
        let file = File::create(&path).unwrap();
        use std::os::unix::io::AsRawFd;
        unsafe { libc_flock(file.as_raw_fd(), 2 | 4) };

        let error = start_phase(&config, Phase::Sync).err().unwrap();
        assert!(error.contains("already running"), "{error}");

        unsafe { libc_flock(file.as_raw_fd(), 8) };
    }

    #[test]
    fn a_stopped_phase_says_it_was_stopped_not_that_it_crashed() {
        assert_eq!(
            phase_outcome(Phase::Sync, None).unwrap_err(),
            "sync was stopped"
        );
        assert!(phase_outcome(Phase::Enrich, Some(0)).is_ok());
    }

    #[test]
    fn a_missing_database_has_nothing_pending() {
        let dir = tempdir();
        assert_eq!(pending_backfills(&config_in(&dir)), 0);
    }

    #[test]
    fn phases_map_to_the_cli_verbs() {
        assert_eq!(Phase::Sync.argument(), "sync");
        assert_eq!(Phase::Enrich.argument(), "enrich");
        assert_eq!(Phase::All.argument(), "all");
    }

    fn tempdir() -> std::path::PathBuf {
        let base = std::env::temp_dir().join(format!(
            "eifo-tray-test-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&base).unwrap();
        base
    }
}
