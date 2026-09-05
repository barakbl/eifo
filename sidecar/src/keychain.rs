//! Where the API token lives.
//!
//! The Keychain, not `config.json`. Everything else this app remembers - which
//! folder, what hour to run at - is a preference, and a preference in a JSON
//! file under Application Support is exactly right. A token is a credential
//! that reads a private catalog, and a credential in a plaintext file is
//! readable by every process running as this user, backed up in the clear, and
//! visible to anybody who opens the folder to check what the app remembers.
//!
//! Reached through the Security framework rather than by running
//! `/usr/bin/security`. The CLI would need the token as a command-line
//! argument, and arguments are visible in `ps` to anybody on the machine - a
//! second copy of the secret, in the one place that is world-readable, for the
//! sake of avoiding a dependency.
//!
//! Nothing here logs the token, and nothing returns it anywhere it could be
//! rendered. The menu is told whether there is one, never what it is.

use security_framework::passwords::{
    delete_generic_password, get_generic_password, set_generic_password,
};

/// How the entry appears in Keychain Access, so somebody looking for it - or
/// wanting to revoke it by hand - can find it under a name that means
/// something.
const SERVICE: &str = "Eifo";
const ACCOUNT: &str = "api-token";

/// What an Eifo token looks like. Checked before storing, so a mis-paste is
/// refused at the moment it can still be explained rather than becoming a
/// permanent 401 nobody can account for.
pub const TOKEN_PREFIX: &str = "eifo_pat_";

/// The stored token, or None when there is none.
///
/// A Keychain that will not answer is the same as an empty one here: the caller
/// polls without a token, the API says 401, and the menu says it needs one -
/// which is a true and actionable sentence whatever went wrong underneath.
pub fn token() -> Option<String> {
    let raw = get_generic_password(SERVICE, ACCOUNT).ok()?;
    let value = String::from_utf8(raw).ok()?;
    let value = value.trim().to_string();
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

/// Store a token, replacing whatever was there.
///
/// Returns what to tell the person when it will not do. The value itself never
/// appears in the message: an error is a thing that gets copied into a bug
/// report, and this one would carry the credential with it.
pub fn store(value: &str) -> Result<(), String> {
    let value = value.trim();
    if value.is_empty() {
        return Err("There is nothing on the clipboard to use as a token.".into());
    }
    if !value.starts_with(TOKEN_PREFIX) {
        return Err(format!(
            "That does not look like an Eifo token - they begin with {TOKEN_PREFIX}. \
             Copy one from Settings in the web app."
        ));
    }

    set_generic_password(SERVICE, ACCOUNT, value.as_bytes())
        .map_err(|err| format!("The Keychain would not store it: {err}"))
}

/// Remove the stored token. Absent is the desired state, so removing one that
/// was never there is a success rather than an error.
pub fn forget() {
    let _ = delete_generic_password(SERVICE, ACCOUNT);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn something_that_is_not_a_token_is_refused_before_it_is_stored() {
        // A mis-paste caught here is a sentence; stored, it is a permanent 401
        // with nothing to connect it to the moment somebody hit the wrong key.
        let refused = store("https://example.com").unwrap_err();

        assert!(refused.contains(TOKEN_PREFIX), "{refused}");
    }

    #[test]
    fn an_empty_clipboard_says_so_rather_than_storing_nothing() {
        assert!(store("   ").unwrap_err().contains("nothing"));
    }

    #[test]
    fn the_refusal_never_repeats_what_was_pasted() {
        // Errors get pasted into bug reports. This one must not carry a
        // credential along with it, even a mistyped one.
        let secret = "eifo_pat_but_wrong_in_some_other_way";
        let refused = store("not-a-token").unwrap_err();

        assert!(!refused.contains(secret));
        assert!(!refused.contains("not-a-token"));
    }
}

#[cfg(test)]
mod live {
    use super::*;

    #[test]
    #[ignore = "writes to the developer's login Keychain"]
    fn a_token_survives_a_round_trip() {
        let value = "eifo_pat_roundtrip_check";
        store(value).expect("stored");
        assert_eq!(token().as_deref(), Some(value));
        forget();
        assert_eq!(token(), None, "forget leaves nothing behind");
        println!("  keychain round trip: stored, read back, forgotten");
    }
}
