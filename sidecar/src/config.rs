//! Where Eifo lives, and what this app remembers between launches.
//!
//! One file under Application Support, which is where a macOS app is supposed
//! to keep this and where a user can find it to fix it by hand. The directory
//! is asked for once, with a native folder chooser, because a menu-bar app with
//! nowhere to point at has nothing to say - and guessing at `~/dev/eifo` and
//! being wrong is worse than asking.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Where the nightly run starts, local time, when this app owns the schedule.
pub const DEFAULT_NIGHTLY: &str = "03:00";
/// Where the API is expected to answer. `localhost` rather than `127.0.0.1` so
/// the links in the menu open the same origin the app is usually reached on, and
/// port 3436 because that is `EIFO` on an old phone keypad (3-4-3-6).
pub const DEFAULT_BASE_URL: &str = "http://localhost:3436";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// The Eifo checkout: the directory holding `packages/`, `data/` and `.venv/`.
    pub app_dir: PathBuf,
    /// Where the API answers, used both for health polling and for deriving the
    /// host and port the server is started on.
    #[serde(default = "default_base_url")]
    pub base_url: String,
    /// Local time the nightly chain starts, `HH:MM`.
    #[serde(default = "default_nightly")]
    pub nightly: String,
    /// Whether to run the nightly chain at all. Off leaves it to launchd.
    #[serde(default = "yes")]
    pub schedule_enabled: bool,
    /// Whether to start the server back up when it stops answering.
    #[serde(default = "yes")]
    pub keep_server_up: bool,
    /// Whether opening Eifo should start the web server if it is not answering.
    /// Separate from `keep_server_up`: a person can want the companion to bring
    /// the server up on launch without also wanting it resurrected every time
    /// they stop it by hand.
    #[serde(default = "yes")]
    pub start_server_on_open: bool,
    /// The last date this app ran the nightly chain, `YYYY-MM-DD` local, so a
    /// machine that was asleep at the hour still gets its run on waking rather
    /// than silently skipping the night.
    #[serde(default)]
    pub last_nightly_on: Option<String>,
    /// The release this app last raised a notification about, so a check that
    /// runs twice a day does not put the same banner up twice.
    #[serde(default)]
    pub update_notified_version: Option<String>,
}

fn default_base_url() -> String {
    DEFAULT_BASE_URL.to_string()
}
fn default_nightly() -> String {
    DEFAULT_NIGHTLY.to_string()
}
fn yes() -> bool {
    true
}

impl Config {
    pub fn new(app_dir: PathBuf) -> Self {
        Self {
            app_dir,
            base_url: default_base_url(),
            nightly: default_nightly(),
            schedule_enabled: true,
            keep_server_up: true,
            start_server_on_open: true,
            last_nightly_on: None,
            update_notified_version: None,
        }
    }

    /// `~/Library/Application Support/Eifo/config.json`.
    pub fn path() -> PathBuf {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
        Path::new(&home)
            .join("Library/Application Support/Eifo")
            .join("config.json")
    }

    pub fn load() -> Option<Self> {
        let raw = fs::read_to_string(Self::path()).ok()?;
        let mut config: Self = serde_json::from_str(&raw).ok()?;
        if config.migrate() {
            let _ = config.save();
        }
        Some(config)
    }

    /// Bring a config written by an older build up to date. Returns whether
    /// anything changed, so the caller knows to write it back.
    fn migrate(&mut self) -> bool {
        // An install from before the port move keeps the old default in its
        // saved config, which would send the menu's links and the health poll
        // to a server that is no longer there. Nudge it forward rather than
        // making anyone hand-edit JSON; a base_url that was set on purpose is
        // left alone.
        if self.base_url == "http://127.0.0.1:8000" {
            self.base_url = default_base_url();
            return true;
        }
        false
    }

    pub fn save(&self) -> std::io::Result<()> {
        let path = Self::path();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, serde_json::to_string_pretty(self).unwrap_or_default())
    }

    /// The uvicorn binary this checkout provides.
    ///
    /// The venv's own executable rather than `uv run`: `uv run` is a wrapper
    /// process that spawns the real server as a child, so stopping it can leave
    /// the server orphaned and holding the port - which is precisely the state
    /// "restart it when it is down" must not create.
    pub fn uvicorn(&self) -> PathBuf {
        self.app_dir.join(".venv/bin/uvicorn")
    }

    /// The fetcher CLI this checkout provides.
    pub fn fetcher(&self) -> PathBuf {
        self.app_dir.join(".venv/bin/eifo-fetch")
    }

    /// The database, which is also where the fetcher's single-flight lock sits.
    pub fn database(&self) -> PathBuf {
        self.app_dir.join("data/eifo.db")
    }

    pub fn lock_file(&self) -> PathBuf {
        self.app_dir.join("data/.eifo-fetch.lock")
    }

    /// Host and port parsed out of `base_url`, for the server's own arguments.
    pub fn host_port(&self) -> (String, u16) {
        let rest = self
            .base_url
            .trim_end_matches('/')
            .rsplit("//")
            .next()
            .unwrap_or("localhost:3436");
        let mut parts = rest.splitn(2, ':');
        let host = parts.next().unwrap_or("localhost").to_string();
        let port = parts.next().and_then(|p| p.parse().ok()).unwrap_or(3436);
        (host, port)
    }

    /// Whether this directory looks like an Eifo checkout at all.
    ///
    /// Checked before the directory is accepted, so a wrong choice is reported
    /// while the chooser is still on screen rather than as four broken menu
    /// items ten minutes later.
    pub fn looks_like_eifo(dir: &Path) -> bool {
        dir.join("packages/eifo-api").is_dir() && dir.join("packages/eifo-fetcher").is_dir()
    }

    /// What is missing, if anything, for this config to be workable.
    pub fn problems(&self) -> Vec<String> {
        let mut found = Vec::new();
        if !Self::looks_like_eifo(&self.app_dir) {
            found.push(format!(
                "{} is not an Eifo checkout",
                self.app_dir.display()
            ));
        }
        if !self.uvicorn().exists() {
            found.push("no .venv/bin/uvicorn - run `uv sync` in the checkout".into());
        }
        if !self.fetcher().exists() {
            found.push("no .venv/bin/eifo-fetch - run `uv sync` in the checkout".into());
        }
        if !self.database().exists() {
            found.push("no data/eifo.db - run `eifo-fetch db upgrade`".into());
        }
        found
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_and_port_come_out_of_the_base_url() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://localhost:3436".into();
        assert_eq!(config.host_port(), ("localhost".into(), 3436));
    }

    #[test]
    fn a_url_without_a_port_gets_the_default() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://localhost".into();
        assert_eq!(config.host_port(), ("localhost".into(), 3436));
    }

    #[test]
    fn the_pre_move_default_url_is_carried_forward() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://127.0.0.1:8000".into();
        assert!(config.migrate(), "the old default should be rewritten");
        assert_eq!(config.base_url, "http://localhost:3436");
    }

    #[test]
    fn a_deliberately_set_url_is_left_alone() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://192.168.1.9:9000".into();
        assert!(!config.migrate());
        assert_eq!(config.base_url, "http://192.168.1.9:9000");
    }

    #[test]
    fn an_old_config_without_the_flag_still_starts_the_server_on_open() {
        // The field was added later; a config written before it must not read
        // back as "do not start the server".
        let config: Config =
            serde_json::from_str(r#"{"app_dir":"/tmp"}"#).expect("minimal config parses");
        assert!(config.start_server_on_open);
    }

    #[test]
    fn a_trailing_slash_is_not_part_of_the_port() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://localhost:9000/".into();
        assert_eq!(config.host_port(), ("localhost".into(), 9000));
    }
}
