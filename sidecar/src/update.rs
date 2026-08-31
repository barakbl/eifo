//! Is there a newer Eifo, and which one is this.
//!
//! The version that matters is the tag the checkout sits on, not the one this
//! binary was compiled as: the companion's job is to keep the *checkout*
//! current, and after an update it is a checkout at a new tag that a still-
//! running old binary is reporting on until it relaunches. `git tag` at the
//! checkout's HEAD is that answer; `CARGO_PKG_VERSION` is the fallback for a
//! folder that was copied rather than cloned.

use std::path::Path;
use std::process::Command;
use std::time::Duration;

use serde::Deserialize;

/// GitHub's "latest full release" - drafts and pre-releases are excluded for us.
const RELEASES_API: &str = "https://api.github.com/repos/barakbl/eifo/releases/latest";
/// Short: this runs on the worker's poll loop, and a slow network should not
/// hold a menu click behind it for longer than a moment.
const TIMEOUT: Duration = Duration::from_secs(6);

pub type Version = (u64, u64, u64);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    /// As GitHub names it, e.g. `v0.3.0` - shown in the menu verbatim.
    pub tag: String,
    pub version: Version,
    /// The release page, for the notification to point at.
    pub url: String,
}

#[derive(Deserialize)]
struct ApiRelease {
    tag_name: String,
    html_url: String,
}

/// Parse `v1.2.3` or `1.2.3`, ignoring any `-rc1` / `+build` suffix.
pub fn parse_version(tag: &str) -> Option<Version> {
    let core = tag.trim().trim_start_matches('v');
    let core = core.split(['-', '+']).next()?;
    let mut parts = core.split('.');
    let major = parts.next()?.trim().parse().ok()?;
    let minor = parts.next()?.trim().parse().ok()?;
    let patch = parts.next().unwrap_or("0").trim().parse().ok()?;
    Some((major, minor, patch))
}

/// The version this checkout is on.
pub fn current_version(app_dir: &Path) -> Version {
    if let Ok(output) = Command::new("git")
        .arg("-C")
        .arg(app_dir)
        .args(["tag", "--points-at", "HEAD"])
        .output()
    {
        if output.status.success() {
            if let Some(version) = String::from_utf8_lossy(&output.stdout)
                .lines()
                .filter_map(parse_version)
                .max()
            {
                return version;
            }
        }
    }
    parse_version(env!("CARGO_PKG_VERSION")).unwrap_or((0, 0, 0))
}

/// The latest release on GitHub, or `None` for anything that is not an answer -
/// offline, rate-limited, a shape this does not understand. None of those are
/// worth telling the user about.
pub fn latest_release() -> Option<Release> {
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(TIMEOUT))
        .build()
        .new_agent();
    let mut response = agent
        .get(RELEASES_API)
        // GitHub answers 403 to a request with no User-Agent.
        .header("User-Agent", "eifo-tray")
        .header("Accept", "application/vnd.github+json")
        .call()
        .ok()?;
    let api: ApiRelease = response.body_mut().read_json().ok()?;
    let version = parse_version(&api.tag_name)?;
    Some(Release {
        tag: api.tag_name,
        version,
        url: api.html_url,
    })
}

/// The script that performs an update, baked into the binary so the copy that
/// runs is never the one being checked out from under it.
pub const SCRIPT: &str = include_str!("../update.sh");

/// Write [`SCRIPT`] somewhere runnable and hand back the path.
pub fn write_script() -> std::io::Result<std::path::PathBuf> {
    let path = std::env::temp_dir().join("eifo-update.sh");
    std::fs::write(&path, SCRIPT)?;
    Ok(path)
}

/// Where the update script's output is kept, for a failure to be explained.
pub fn log_path() -> std::path::PathBuf {
    std::env::temp_dir().join("eifo-update.log")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_parse_with_or_without_the_v_and_the_suffix() {
        assert_eq!(parse_version("v0.3.0"), Some((0, 3, 0)));
        assert_eq!(parse_version("1.2.3"), Some((1, 2, 3)));
        assert_eq!(parse_version("v2.0"), Some((2, 0, 0)));
        assert_eq!(parse_version("v0.3.0-rc1"), Some((0, 3, 0)));
        assert_eq!(parse_version("nightly"), None);
    }

    #[test]
    fn versions_order_the_way_releases_do() {
        assert!(parse_version("v0.10.0") > parse_version("v0.9.9"));
        assert!(parse_version("v1.0.0") > parse_version("v0.99.99"));
        assert_eq!(parse_version("v0.2.0"), parse_version("0.2.0"));
    }

    #[test]
    fn a_folder_that_is_not_a_git_checkout_falls_back_to_the_built_in_version() {
        let dir = std::env::temp_dir().join("eifo-not-a-repo");
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(
            current_version(&dir),
            parse_version(env!("CARGO_PKG_VERSION")).unwrap()
        );
    }

    #[test]
    fn the_script_is_the_one_in_the_tree() {
        assert!(SCRIPT.contains("build-app.sh"));
        assert!(SCRIPT.starts_with("#!"));
    }

    #[test]
    #[ignore = "hits the GitHub API"]
    fn latest_release_reads_a_real_release() {
        let release = latest_release().expect("GitHub should have answered");
        assert!(release.tag.starts_with('v'), "{}", release.tag);
        assert!(release.version >= (0, 2, 0));
        assert!(release.url.contains("github.com/barakbl/eifo"));
    }
}
