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
/// Where the API is expected to answer.
pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8000";

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
    /// The last date this app ran the nightly chain, `YYYY-MM-DD` local, so a
    /// machine that was asleep at the hour still gets its run on waking rather
    /// than silently skipping the night.
    #[serde(default)]
    pub last_nightly_on: Option<String>,
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
            last_nightly_on: None,
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
        serde_json::from_str(&raw).ok()
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
            .unwrap_or("127.0.0.1:8000");
        let mut parts = rest.splitn(2, ':');
        let host = parts.next().unwrap_or("127.0.0.1").to_string();
        let port = parts.next().and_then(|p| p.parse().ok()).unwrap_or(8000);
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
        config.base_url = "http://127.0.0.1:8000".into();
        assert_eq!(config.host_port(), ("127.0.0.1".into(), 8000));
    }

    #[test]
    fn a_url_without_a_port_gets_the_default() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://localhost".into();
        assert_eq!(config.host_port(), ("localhost".into(), 8000));
    }

    #[test]
    fn a_trailing_slash_is_not_part_of_the_port() {
        let mut config = Config::new(PathBuf::from("/tmp"));
        config.base_url = "http://localhost:9000/".into();
        assert_eq!(config.host_port(), ("localhost".into(), 9000));
    }
}
