//! When the nightly chain should run, now that this app owns the schedule.
//!
//! Deliberately small. The fetcher already knows how to run a night; the only
//! question here is when, and the only hard part of that is a machine that was
//! asleep at the appointed minute. Skipping the night because nobody was awake
//! for it is the failure mode this exists to avoid - it is how a catalog goes
//! quietly stale over a fortnight of closed lids.
//!
//! Running twice is safe and running never is not, which is why catch-up errs
//! towards firing: the fetcher's single-flight lock means a second run beside
//! the LaunchAgent's simply stands down.

use chrono::{DateTime, Datelike, Local, NaiveTime, TimeZone};

/// Parse `HH:MM`, the same shape the fetcher's own config uses.
pub fn parse_time(value: &str) -> Option<NaiveTime> {
    let (hour, minute) = value.split_once(':')?;
    NaiveTime::from_hms_opt(hour.trim().parse().ok()?, minute.trim().parse().ok()?, 0)
}

/// The local date as this app records it, `YYYY-MM-DD`.
pub fn day_of(now: DateTime<Local>) -> String {
    format!("{:04}-{:02}-{:02}", now.year(), now.month(), now.day())
}

/// Whether tonight's run is owed.
///
/// True when the hour has passed today and today has not been run yet - which
/// covers both the ordinary case, where the minute arrives while the app is
/// running, and the interesting one, where the machine was asleep at 03:00 and
/// is opened at nine.
pub fn is_due(now: DateTime<Local>, at: NaiveTime, last_run_on: Option<&str>) -> bool {
    if now.time() < at {
        return false;
    }
    last_run_on != Some(day_of(now).as_str())
}

/// When the next run is expected, for the menu to say so.
pub fn next_run(now: DateTime<Local>, at: NaiveTime) -> DateTime<Local> {
    let today = now.date_naive().and_time(at);
    let naive = if now.time() < at {
        today
    } else {
        today + chrono::Duration::days(1)
    };
    Local
        .from_local_datetime(&naive)
        .single()
        // A clock change can make a local time ambiguous or skip it entirely.
        // An hour either way in a menu label is not worth a failure mode.
        .unwrap_or_else(|| now + chrono::Duration::days(1))
}

/// "tonight at 03:00" / "today at 03:00", as a person would say it.
pub fn describe_next(now: DateTime<Local>, at: NaiveTime) -> String {
    let next = next_run(now, at);
    let when = format!("{:02}:{:02}", at.format("%H"), at.format("%M"));
    if next.date_naive() == now.date_naive() {
        format!("today at {when}")
    } else {
        format!("tomorrow at {when}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn at(hour: u32, minute: u32) -> DateTime<Local> {
        Local
            .with_ymd_and_hms(2026, 8, 31, hour, minute, 0)
            .single()
            .expect("a plain afternoon, unambiguous in any zone")
    }

    #[test]
    fn a_time_is_read_the_way_the_config_writes_it() {
        assert_eq!(parse_time("03:00"), NaiveTime::from_hms_opt(3, 0, 0));
        assert_eq!(parse_time("23:45"), NaiveTime::from_hms_opt(23, 45, 0));
        assert_eq!(parse_time("nonsense"), None);
        assert_eq!(parse_time("25:00"), None);
    }

    #[test]
    fn nothing_is_due_before_the_hour() {
        let three = parse_time("03:00").unwrap();
        assert!(!is_due(at(2, 59), three, None));
    }

    #[test]
    fn it_is_due_once_the_hour_has_passed() {
        let three = parse_time("03:00").unwrap();
        assert!(is_due(at(3, 0), three, None));
    }

    #[test]
    fn a_machine_asleep_at_the_hour_still_runs_on_waking() {
        // The whole point. Skipping the night because nobody was awake for it
        // is how a catalog goes stale over a fortnight of closed lids.
        let three = parse_time("03:00").unwrap();
        assert!(is_due(at(9, 30), three, Some("2026-08-30")));
    }

    #[test]
    fn it_is_not_due_twice_in_one_day() {
        let three = parse_time("03:00").unwrap();
        assert!(!is_due(at(9, 30), three, Some("2026-08-31")));
    }

    #[test]
    fn the_next_run_is_today_before_the_hour_and_tomorrow_after() {
        let three = parse_time("03:00").unwrap();
        assert_eq!(
            next_run(at(1, 0), three).date_naive(),
            at(1, 0).date_naive()
        );
        assert_eq!(
            next_run(at(5, 0), three).date_naive(),
            at(5, 0).date_naive() + chrono::Duration::days(1)
        );
    }

    #[test]
    fn the_menu_says_when_in_words() {
        let three = parse_time("03:00").unwrap();
        assert_eq!(describe_next(at(1, 0), three), "today at 03:00");
        assert_eq!(describe_next(at(5, 0), three), "tomorrow at 03:00");
    }
}
