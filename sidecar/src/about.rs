//! What the About panel says.
//!
//! Kept apart from the AppKit call that shows it so the words can be read, and
//! tested, without a run loop.

use objc2::rc::Retained;
use objc2_foundation::{NSAttributedString, NSString};

pub const NAME: &str = "Eifo";

/// The one-line description under the name.
pub const TAGLINE: &str = "Which service is showing the thing I want to watch?";

/// The body of the About panel.
///
/// Says what this app is - the companion, not the product - because somebody
/// reading it has a menu-bar icon in front of them and may not know which of
/// the two they are looking at.
pub fn credits_text() -> String {
    format!(
        "{TAGLINE}\n\n\
         This is the menu-bar companion: it watches the catalog, runs the nightly \
         sync and enrichment, and keeps the web server up.\n\n\
         {}\n\n\
         Not affiliated with, endorsed by, or connected to any of the services it \
         lists or any of the data providers it reads.",
        crate::platform::REPO_URL
    )
}

/// The same text as an attributed string, which is what the panel takes.
pub fn credits() -> Retained<NSAttributedString> {
    let text = NSString::from_str(&credits_text());
    NSAttributedString::from_nsstring(&text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_links_to_the_repository() {
        assert!(credits_text().contains("github.com/barakbl/eifo"));
    }

    #[test]
    fn it_says_which_of_the_two_things_this_is() {
        // Somebody reading this has a menu-bar icon in front of them and may
        // not know whether it is the product or its companion.
        assert!(credits_text().contains("menu-bar companion"));
    }

    #[test]
    fn it_carries_the_projects_own_disclaimer() {
        assert!(credits_text().contains("Not affiliated"));
    }
}
