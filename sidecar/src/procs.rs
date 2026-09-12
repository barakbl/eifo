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

        // To a file rather than to /dev/null. A server this app started has no
        // console, so anything it prints - a port already in use, a traceback
        // from a failed import, uvicorn's own access log - used to be thrown
        // away at the one moment somebody wanted to read it.
        let (out, err) = appending_to(&config.console_log("eifo-api"));
        let child = Command::new(&uvicorn)
            .arg("eifo_api.main:app")
            .args(["--host", &host])
            .args(["--port", &port.to_string()])
            .current_dir(&config.app_dir)
            // No --reload: a watcher forks on every file change, which turns
            // one owned process back into a tree this app cannot stop cleanly.
            .stdout(out)
            .stderr(err)
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

/// Where a child's console output should go, appending to `path`.
///
/// Appending, not truncating: a server restarted three times in a minute
/// because it will not come up is exactly the case this is for, and each
/// attempt overwriting the last would leave only the quietest one.
///
/// Falls back to discarding it when the file cannot be opened. This app's job
/// is to keep the catalog running; refusing to start a server because a log
/// file could not be created would be the tail wagging the dog.
fn appending_to(path: &Path) -> (Stdio, Stdio) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match File::options().create(true).append(true).open(path) {
        Ok(file) => match file.try_clone() {
            Ok(second) => (Stdio::from(file), Stdio::from(second)),
            Err(_) => (Stdio::null(), Stdio::null()),
        },
        Err(_) => (Stdio::null(), Stdio::null()),
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
///
/// Each is a command somebody could type, and is run as exactly that. `One` is
/// `eifo-fetch sync --source KEY`: the fetcher has always been able to do a
/// single service, and there was no reason for the menu to be the one place
/// that could only do all of them - a service that has just come back after a
/// morning of failing should be answerable in a click rather than by a two-hour
/// sweep of the other thirteen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phase {
    Sync,
    /// One source, named so the menu can say which without a lookup.
    One {
        key: String,
        name: String,
    },
    Enrich,
    /// One rating provider, the same idea applied to the enrich half. The Apple
    /// price pass moves at a third of a request a second and covers the catalog
    /// over a fortnight of nights, so "is the new one behaving" is a question
    /// worth being able to ask without waiting for all six.
    OneEnricher {
        key: String,
        name: String,
    },
    Images,
    All,
}

impl Phase {
    /// The command line, after `eifo-fetch`.
    pub fn arguments(&self) -> Vec<String> {
        match self {
            Phase::Sync => vec!["sync".into()],
            Phase::One { key, .. } => vec!["sync".into(), "--source".into(), key.clone()],
            Phase::Enrich => vec!["enrich".into()],
            Phase::OneEnricher { key, .. } => {
                vec!["enrich".into(), "--only".into(), key.clone()]
            }
            Phase::Images => vec!["images".into()],
            Phase::All => vec!["all".into()],
        }
    }

    /// What to call it in a sentence: "Running the full run…", "sync stopped".
    pub fn label(&self) -> String {
        match self {
            Phase::Sync => "sync".into(),
            Phase::One { name, .. } => format!("sync of {name}"),
            Phase::Enrich => "enrich".into(),
            Phase::OneEnricher { name, .. } => format!("refresh of {name}"),
            Phase::Images => "the artwork pass".into(),
            Phase::All => "the full run".into(),
        }
    }

    /// Whether this phase sweeps every source, which is what decides if the
    /// menu can honestly count "4 of 14" against it.
    pub fn is_sweep(&self) -> bool {
        matches!(self, Phase::Sync | Phase::All)
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
            Ok(Some(status)) => Some(phase_outcome(&self.phase, status.code())),
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

/// One thing an enrich can be narrowed to, as the fetcher lists them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnricherOption {
    pub key: String,
    pub name: String,
}

/// What the enrich can be narrowed to, asked of the fetcher itself.
///
/// Read from the checkout rather than kept here. A list of providers maintained
/// in both Python and Rust is a list that disagrees with itself the first time
/// somebody adds one, and the menu would go on offering an enricher that no
/// longer exists - or, worse, quietly stop offering the new one nobody has
/// checked yet.
///
/// None when the fetcher is not there or would not answer, so the caller can
/// keep whatever list it already had rather than emptying the submenu over a
/// checkout that is mid-update.
pub fn enricher_options(config: &Config) -> Option<Vec<EnricherOption>> {
    let fetcher = config.fetcher();
    if !fetcher.exists() {
        return None;
    }

    let output = Command::new(&fetcher)
        .args(["enrich", "--list"])
        .current_dir(&config.app_dir)
        // The fetcher logs its startup to stderr; only stdout is the answer.
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }

    let listed = parse_enrichers(&String::from_utf8_lossy(&output.stdout));
    // An empty answer is not an answer. A build whose `--list` prints nothing
    // is a build this app should keep the old list for, not one to show an
    // empty submenu about.
    (!listed.is_empty()).then_some(listed)
}

/// `key<tab>name` a line, which is what `eifo-fetch enrich --list` prints.
///
/// Anything that is not that shape is dropped rather than guessed at: this
/// output is the contract between two programs, and a line that does not match
/// it means the contract moved, not that a provider is called something odd.
fn parse_enrichers(text: &str) -> Vec<EnricherOption> {
    text.lines()
        .filter_map(|line| {
            let (key, name) = line.split_once('\t')?;
            let (key, name) = (key.trim(), name.trim());
            (!key.is_empty() && !name.is_empty()).then(|| EnricherOption {
                key: key.to_string(),
                name: name.to_string(),
            })
        })
        .collect()
}

/// Start a fetcher phase, refusing if one is already going.
///
/// Returns immediately with a handle to watch; it does not wait for the run.
pub fn start_phase(config: &Config, phase: Phase, token: Option<&str>) -> Result<Fetch, String> {
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

    // A remote catalog cannot be filled anonymously, and the checkout's own
    // `.env` is not a fallback worth having here: any token in it was minted
    // against the database on this disk, so a server elsewhere has never heard
    // of it. Letting the run start would spend a catalog read on a 401 nobody
    // would connect to a missing token twenty minutes later.
    if config.is_remote() && token.is_none() {
        return Err(format!(
            "no API token, and {} is not this machine - paste one from the server \
             (Settings in the web app, or `eifo-fetch token create`)",
            config.base_url
        ));
    }

    // Same reasoning as the server's: a run this app started writes to nobody's
    // terminal. The fetcher keeps its own structured log beside this one; what
    // lands here is whatever escaped it, which is what a run that died on the
    // way in leaves behind.
    let (out, err) = appending_to(&config.console_log("eifo-fetch"));
    let mut command = Command::new(&fetcher);
    command
        .args(phase.arguments())
        .current_dir(&config.app_dir)
        .stdout(out)
        .stderr(err);

    // Which catalog this run fills, and what it proves itself with.
    //
    // Passed rather than left to the checkout's own `.env`, because the menu is
    // the thing that knows where this app is pointed - and the two can disagree.
    // A checkout whose `.env` still names a token minted against the database on
    // *this* disk would send it to a server that has never heard of it: a 401 a
    // long way from anything that explains it. The environment wins over `.env`
    // in the settings chain, so what the menu says is what the run does.
    command.env("EIFO_API_BASE_URL", config.base_url.trim_end_matches('/'));
    if let Some(token) = token {
        command.env("EIFO_API_TOKEN", token);
    }

    let child = command
        .spawn()
        .map_err(|err| format!("could not run {}: {err}", phase.label()))?;

    Ok(Fetch { child, phase })
}

/// A long job this app started and is watching - the self-update.
///
/// Its output goes to a file rather than a pipe: the build alone prints more
/// than a pipe buffer holds, and a full pipe would wedge the job halfway. On a
/// failure the last few lines of that file are what the menu shows.
pub struct Job {
    child: Child,
    log: std::path::PathBuf,
}

impl Job {
    /// `None` while it runs; the outcome once it is over. The error is the tail
    /// of the log, so "update failed" comes with a reason.
    pub fn poll(&mut self) -> Option<Result<(), String>> {
        match self.child.try_wait() {
            Ok(None) => None,
            Ok(Some(status)) if status.success() => Some(Ok(())),
            Ok(Some(_)) => Some(Err(self.tail())),
            Err(err) => Some(Err(err.to_string())),
        }
    }

    fn tail(&self) -> String {
        let text = std::fs::read_to_string(&self.log).unwrap_or_default();
        let last: Vec<&str> = text.lines().rev().take(3).collect();
        if last.is_empty() {
            "no output".into()
        } else {
            last.into_iter().rev().collect::<Vec<_>>().join(" · ")
        }
    }
}

/// Start the update script for a tag, output going to a log file.
pub fn start_update(app_dir: &Path, tag: &str) -> Result<Job, String> {
    let script = crate::update::write_script().map_err(|err| err.to_string())?;
    let log = crate::update::log_path();
    let out = File::create(&log).map_err(|err| err.to_string())?;
    let err = out.try_clone().map_err(|err| err.to_string())?;

    let child = Command::new("sh")
        .arg(&script)
        .arg(app_dir)
        .arg(tag)
        .current_dir(app_dir)
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err))
        .spawn()
        .map_err(|err| format!("could not start the update: {err}"))?;

    Ok(Job { child, log })
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
fn phase_outcome(phase: &Phase, code: Option<i32>) -> Result<(), String> {
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

    /// A fetcher stub that records the environment it was handed.
    ///
    /// The real binary would go and read somebody's catalog; what is under test
    /// is only which server this run was pointed at, which is settled before it
    /// does anything at all.
    fn stub_fetcher(config: &Config, writing_to: &Path) {
        let bin = config.fetcher();
        std::fs::create_dir_all(bin.parent().unwrap()).unwrap();
        std::fs::write(
            &bin,
            format!(
                "#!/bin/sh\necho \"$EIFO_API_BASE_URL|$EIFO_API_TOKEN\" > \"{}\"\n",
                writing_to.display()
            ),
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    #[test]
    fn a_run_is_pointed_at_the_server_the_menu_is_watching() {
        // The whole of "fetch here, fill there": the fetcher reads the services
        // from this machine and posts what it found to whichever catalog this
        // app is configured for - not to whatever the checkout's .env happens
        // to name, which is a different setting that nothing keeps in step.
        let dir = tempdir().join("pointed");
        let _ = std::fs::remove_dir_all(&dir);
        let mut config = config_in(&dir);
        config.base_url = "https://eifo.example.com/".into();
        let seen = dir.join("seen.txt");
        stub_fetcher(&config, &seen);

        let mut fetch = start_phase(&config, Phase::Sync, Some("eifo_pat_from_keychain")).unwrap();
        for _ in 0..50 {
            if fetch.poll().is_some() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }

        let recorded = std::fs::read_to_string(&seen).unwrap_or_default();
        // The trailing slash is trimmed: the fetcher joins paths onto this and
        // a doubled slash is a 404 nobody would connect to a stray character.
        assert_eq!(
            recorded.trim(),
            "https://eifo.example.com|eifo_pat_from_keychain"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_remote_catalog_will_not_be_filled_without_a_token() {
        // The checkout's .env is not a fallback worth having: any token in it
        // was minted against the database on this disk, so a server elsewhere
        // has never heard of it. Better to say so than to spend a catalog read
        // on a 401.
        let dir = tempdir().join("untokened");
        let _ = std::fs::remove_dir_all(&dir);
        let mut config = config_in(&dir);
        config.base_url = "https://eifo.example.com".into();
        stub_fetcher(&config, &dir.join("unused.txt"));

        let error = start_phase(&config, Phase::Sync, None).err().unwrap();

        assert!(error.contains("no API token"), "{error}");
        assert!(error.contains("eifo.example.com"), "{error}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_local_catalog_still_runs_without_one() {
        // It can mint its own against the database it can see, so demanding a
        // token here would break the ordinary single-box install.
        let dir = tempdir().join("local-no-token");
        let _ = std::fs::remove_dir_all(&dir);
        let config = config_in(&dir);
        let seen = dir.join("seen.txt");
        stub_fetcher(&config, &seen);

        assert!(start_phase(&config, Phase::Sync, None).is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_phase_refuses_to_start_beside_a_running_fetcher() {
        let dir = tempdir();
        let config = config_in(&dir);
        let path = config.lock_file();
        let file = File::create(&path).unwrap();
        use std::os::unix::io::AsRawFd;
        unsafe { libc_flock(file.as_raw_fd(), 2 | 4) };

        let error = start_phase(&config, Phase::Sync, None).err().unwrap();
        assert!(error.contains("already running"), "{error}");

        unsafe { libc_flock(file.as_raw_fd(), 8) };
    }

    #[test]
    fn a_stopped_phase_says_it_was_stopped_not_that_it_crashed() {
        assert_eq!(
            phase_outcome(&Phase::Sync, None).unwrap_err(),
            "sync was stopped"
        );
        assert!(phase_outcome(&Phase::Enrich, Some(0)).is_ok());
    }

    #[test]
    fn a_missing_database_has_nothing_pending() {
        let dir = tempdir();
        assert_eq!(pending_backfills(&config_in(&dir)), 0);
    }

    #[test]
    fn phases_map_to_the_cli_verbs() {
        assert_eq!(Phase::Sync.arguments(), ["sync"]);
        assert_eq!(Phase::Enrich.arguments(), ["enrich"]);
        assert_eq!(Phase::Images.arguments(), ["images"]);
        assert_eq!(Phase::All.arguments(), ["all"]);
    }

    #[test]
    fn one_source_is_the_flag_the_fetcher_already_takes() {
        // Not a mode of its own: `sync --source KEY` is what somebody would
        // type, so it is what gets run.
        let one = Phase::One {
            key: "kan".into(),
            name: "Kan Box".into(),
        };
        assert_eq!(one.arguments(), ["sync", "--source", "kan"]);
        assert_eq!(one.label(), "sync of Kan Box");
        assert!(!one.is_sweep(), "one source is not a sweep of every source");
        assert!(Phase::All.is_sweep());
    }

    #[test]
    fn one_enricher_is_the_flag_the_fetcher_already_takes() {
        // Same reasoning as one source: `enrich --only KEY` is what somebody
        // would type. The Apple price pass covers the catalog over a fortnight
        // of nights, so being able to ask about it alone is worth a menu item.
        let one = Phase::OneEnricher {
            key: "apple_prices".into(),
            name: "Apple TV prices".into(),
        };
        assert_eq!(one.arguments(), ["enrich", "--only", "apple_prices"]);
        assert_eq!(one.label(), "refresh of Apple TV prices");
        assert!(
            !one.is_sweep(),
            "one provider is not a sweep of every source"
        );
    }

    #[test]
    fn the_enricher_listing_is_read_as_key_and_name() {
        let listed = parse_enrichers("tmdb\tTMDB metadata\napple_prices\tApple TV prices\n");

        assert_eq!(
            listed,
            vec![
                EnricherOption {
                    key: "tmdb".into(),
                    name: "TMDB metadata".into()
                },
                EnricherOption {
                    key: "apple_prices".into(),
                    name: "Apple TV prices".into()
                },
            ]
        );
    }

    #[test]
    fn a_line_that_is_not_that_shape_is_dropped_rather_than_guessed_at() {
        // This output is a contract between two programs. A line that does not
        // match it means the contract moved, not that a provider is called
        // something odd - and half-reading it would put a menu item there that
        // starts a run the fetcher then refuses.
        let listed = parse_enrichers("a warning that escaped\nrt\tRotten Tomatoes\n\n\t\n");

        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].key, "rt");
    }

    #[test]
    fn a_checkout_with_no_fetcher_is_asked_nothing() {
        // Rather than spawning something that is not there and reading a
        // failure back out of the error.
        let dir = tempdir();

        assert_eq!(enricher_options(&config_in(&dir)), None);
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

#[cfg(test)]
mod live {
    use super::*;

    /// The real `eifo-fetch --list`, against the checkout this app is pointed
    /// at. Ignored by default because it needs a built venv; the parser tests
    /// above pin the shape, and this pins that the shape is still what the
    /// fetcher actually prints.
    ///
    ///   EIFO_TEST_APP_DIR=/path/to/eifo cargo test live -- --ignored --nocapture
    #[test]
    #[ignore = "needs a checkout with a built .venv"]
    fn the_real_fetcher_lists_its_enrichers() {
        let Ok(dir) = std::env::var("EIFO_TEST_APP_DIR") else {
            panic!("set EIFO_TEST_APP_DIR");
        };
        let config = Config::new(std::path::PathBuf::from(dir));

        let listed = enricher_options(&config).expect("a listing");

        assert!(
            listed.iter().any(|option| option.key == "tmdb"),
            "no tmdb in {listed:?}"
        );
        for option in &listed {
            println!("  {} - {}", option.key, option.name);
        }
    }
}
