//! What colour the dot should be, and why.
//!
//! Read from `GET /api/v1/meta`, which is the same view the Manage tab has and
//! already does the arithmetic: it decides staleness against the deployment's
//! own `stale_after_hours`, and it knows that a retired source is not a stale
//! one. Recomputing any of that here would be a second opinion that could
//! disagree with the product's own.

use std::time::Duration;

use serde::Deserialize;

/// How long to wait on the API before calling it unreachable. Generous, because
/// the answer counts a few dozen rows and a busy nightly run can hold SQLite's
/// write lock for a moment.
const TIMEOUT: Duration = Duration::from_secs(8);

/// The one status code that means "you, specifically, may not" rather than
/// "something is wrong".
const UNAUTHORIZED: u16 = 401;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    /// The server answers and every source it still collects is fresh.
    Ok,
    /// The server answers, but something is behind or a source's last sync failed.
    Attention,
    /// The server is not answering.
    Down,
    /// Nothing has been asked yet, or there is nowhere to ask.
    Unknown,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SourceFreshness {
    pub key: String,
    pub name: String,
    pub active: bool,
    pub stale: bool,
    pub last_sync_status: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Meta {
    pub title_count: i64,
    pub sources: Vec<SourceFreshness>,
}

/// One reading of the system.
#[derive(Debug, Clone)]
pub struct Health {
    pub status: Status,
    /// One line for the top of the menu, in the shape a person would say it.
    pub summary: String,
    /// Sources worth naming, so the menu can say which ones rather than how many.
    pub problems: Vec<String>,
}

impl Health {
    pub fn unknown(reason: impl Into<String>) -> Self {
        Self {
            status: Status::Unknown,
            summary: reason.into(),
            problems: Vec::new(),
        }
    }

    pub fn down(reason: impl Into<String>) -> Self {
        Self {
            status: Status::Down,
            summary: reason.into(),
            problems: Vec::new(),
        }
    }

    /// The catalog is private and this app is not holding the key.
    ///
    /// Amber rather than red, because nothing is broken: the server is up and
    /// the operator has some typing to do. Red would send somebody to look at
    /// a process that is serving perfectly well.
    pub fn locked(had_token: bool) -> Self {
        Self {
            status: Status::Attention,
            summary: if had_token {
                "the API token was refused - it may have been revoked".into()
            } else {
                "this catalog is members-only and needs an API token".into()
            },
            problems: Vec::new(),
        }
    }
}

/// Ask the API how it is.
pub fn poll(base_url: &str, token: Option<&str>) -> Health {
    let url = format!("{}/api/v1/meta", base_url.trim_end_matches('/'));
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(TIMEOUT))
        .build()
        .new_agent();

    let mut request = agent.get(&url);
    if let Some(token) = token {
        request = request.header("Authorization", format!("Bearer {token}"));
    }

    match request.call() {
        Ok(mut response) => match response.body_mut().read_json::<Meta>() {
            Ok(meta) => evaluate(&meta),
            // Answering with something this app cannot read is not the same as
            // not answering: the port is served by something, just not by Eifo.
            Err(err) => Health::down(format!("the API answered but not with Eifo's meta ({err})")),
        },
        // A refusal is an answer. The server is up, it understood the request
        // and it declined it - which is nothing like a dead port, and calling
        // it "not answering" sent somebody looking for a crashed process that
        // was serving perfectly well the whole time.
        Err(ureq::Error::StatusCode(UNAUTHORIZED)) => Health::locked(has_token(token)),
        Err(_) => Health::down("the web server is not answering"),
    }
}

/// Whether a token was sent, which is the difference between the two ways of
/// being refused and the only thing that changes what to do about it.
fn has_token(token: Option<&str>) -> bool {
    token.is_some_and(|value| !value.trim().is_empty())
}

/// Turn one `/meta` reading into a colour and a sentence.
pub fn evaluate(meta: &Meta) -> Health {
    let stale: Vec<&SourceFreshness> = meta.sources.iter().filter(|s| s.stale).collect();
    // A source whose last run ended badly is worth flagging before it goes
    // stale: staleness is a delayed symptom of exactly this, and waiting two
    // days to mention it wastes the two days.
    let failed: Vec<&SourceFreshness> = meta
        .sources
        .iter()
        .filter(|s| {
            s.active
                && matches!(
                    s.last_sync_status.as_deref(),
                    Some("failed") | Some("crashed") | Some("aborted_suspicious")
                )
        })
        .collect();

    let mut problems: Vec<String> = Vec::new();
    for source in &stale {
        problems.push(format!("{} is not fresh", source.name));
    }
    for source in &failed {
        if !stale.iter().any(|s| s.key == source.key) {
            let how = source.last_sync_status.as_deref().unwrap_or("failed");
            problems.push(format!("{} last run {}", source.name, how));
        }
    }

    let status = if problems.is_empty() {
        Status::Ok
    } else {
        Status::Attention
    };
    let summary = match (stale.len(), failed.len()) {
        (0, 0) => format!("{} titles, every source fresh", thousands(meta.title_count)),
        (n, 0) => format!("{n} source{} not fresh", plural(n)),
        (0, n) => format!("{n} source{} failed last run", plural(n)),
        (s, f) => format!("{s} not fresh, {f} failed"),
    };

    Health {
        status,
        summary,
        problems,
    }
}

fn plural(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}

/// 38836 as "38,836". A count in a menu is read, not computed with.
pub fn thousands(value: i64) -> String {
    let digits = value.abs().to_string();
    let mut out = String::new();
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    if value < 0 {
        format!("-{out}")
    } else {
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(key: &str, active: bool, stale: bool, last: Option<&str>) -> SourceFreshness {
        SourceFreshness {
            key: key.into(),
            name: key.to_uppercase(),
            active,
            stale,
            last_sync_status: last.map(Into::into),
        }
    }

    #[test]
    fn everything_fresh_is_green() {
        let meta = Meta {
            title_count: 38836,
            sources: vec![source("mako", true, false, Some("ok"))],
        };
        let health = evaluate(&meta);
        assert_eq!(health.status, Status::Ok);
        assert!(health.summary.contains("38,836"));
    }

    #[test]
    fn a_stale_source_is_amber_and_named() {
        let meta = Meta {
            title_count: 10,
            sources: vec![
                source("mako", true, true, Some("ok")),
                source("kan", true, false, Some("ok")),
            ],
        };
        let health = evaluate(&meta);
        assert_eq!(health.status, Status::Attention);
        assert_eq!(health.problems, vec!["MAKO is not fresh"]);
    }

    #[test]
    fn a_failed_run_is_flagged_before_it_becomes_staleness() {
        // Staleness is a delayed symptom of this; waiting for it wastes two days.
        let meta = Meta {
            title_count: 10,
            sources: vec![source("kan", true, false, Some("failed"))],
        };
        let health = evaluate(&meta);
        assert_eq!(health.status, Status::Attention);
        assert_eq!(health.problems, vec!["KAN last run failed"]);
    }

    #[test]
    fn a_source_is_not_named_twice_for_one_problem() {
        let meta = Meta {
            title_count: 10,
            sources: vec![source("kan", true, true, Some("failed"))],
        };
        assert_eq!(evaluate(&meta).problems.len(), 1);
    }

    #[test]
    fn a_retired_source_is_neither_stale_nor_failing() {
        // "Off" and "behind" are different claims, and the server already knows
        // the difference - stale is false for anything not being collected.
        let meta = Meta {
            title_count: 10,
            sources: vec![source("hot", false, false, Some("failed"))],
        };
        assert_eq!(evaluate(&meta).status, Status::Ok);
    }

    #[test]
    fn a_run_in_flight_is_not_a_failure() {
        let meta = Meta {
            title_count: 10,
            sources: vec![source("kan", true, false, Some("running"))],
        };
        assert_eq!(evaluate(&meta).status, Status::Ok);
    }

    #[test]
    fn a_refusal_is_not_a_dead_server() {
        // The whole of the bug. A members-only catalog answers 401, which ureq
        // reports as an error - and reading every error as "not answering"
        // turned the dot red about a server that was serving perfectly well.
        let locked = Health::locked(false);

        assert_eq!(locked.status, Status::Attention, "amber: nothing is broken");
        assert!(locked.summary.contains("token"));
    }

    #[test]
    fn a_refused_token_and_a_missing_one_are_different_problems() {
        // Only one of them is fixed by pasting, so only one of them should say
        // so. Being told to paste a token you have already pasted is the kind
        // of advice that makes somebody distrust the rest of the menu.
        assert_ne!(Health::locked(true).summary, Health::locked(false).summary);
        assert!(Health::locked(true).summary.contains("refused"));
    }

    #[test]
    fn counts_are_grouped_for_reading() {
        assert_eq!(thousands(38836), "38,836");
        assert_eq!(thousands(999), "999");
        assert_eq!(thousands(1_000_000), "1,000,000");
        assert_eq!(thousands(0), "0");
    }
}
