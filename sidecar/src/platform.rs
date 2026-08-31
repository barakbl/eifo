//! The AppKit corners: the About panel, the folder chooser, Login Items.
//!
//! Each of these has a system-provided version that looks and behaves the way
//! macOS users expect, and a version anybody could draw that does not. This
//! reaches for the system one every time - the standard About panel rather than
//! a window with labels in it, `NSOpenPanel` rather than a text field to paste
//! a path into, `SMAppService` rather than a login-item plist written by hand.

use objc2::rc::Retained;
use objc2::runtime::AnyObject;
use objc2::MainThreadMarker;
use objc2_app_kit::{NSApplication, NSOpenPanel, NSWorkspace};
use objc2_foundation::{NSDictionary, NSString, NSURL};

pub const REPO_URL: &str = "https://github.com/barakbl/eifo";

/// Post a macOS notification.
///
/// Through `osascript` rather than `UNUserNotificationCenter`: the modern API
/// wants an authorization prompt and a provisioning profile, and this is a menu-
/// bar helper that shows one banner a fortnight. Best-effort - a notification
/// that does not appear is not worth a failure path.
pub fn notify(title: &str, body: &str) {
    let script = format!(
        "display notification {} with title {}",
        applescript_string(body),
        applescript_string(title),
    );
    let _ = std::process::Command::new("osascript")
        .args(["-e", &script])
        .spawn();
}

/// An AppleScript double-quoted string literal, backslash-escaped.
fn applescript_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        if ch == '"' || ch == '\\' {
            out.push('\\');
        }
        out.push(ch);
    }
    out.push('"');
    out
}

/// Open a URL in the user's browser.
pub fn open_url(url: &str) {
    let string = NSString::from_str(url);
    // NSURL returns null for anything it cannot parse, which is checked here.
    if let Some(url) = NSURL::URLWithString(&string) {
        NSWorkspace::sharedWorkspace().openURL(&url);
    }
}

/// The standard About panel, filled in with this app's own details.
///
/// `orderFrontStandardAboutPanelWithOptions:` rather than a window of our own:
/// it is the panel every other Mac app shows, down to the placement and the
/// close behaviour, and the credits field takes an attributed string so the
/// link to the repository is a link rather than a URL to select and copy.
pub fn show_about(mtm: MainThreadMarker) {
    let app = NSApplication::sharedApplication(mtm);

    let credits = crate::about::credits();
    let keys = [
        NSString::from_str("ApplicationName"),
        NSString::from_str("ApplicationVersion"),
        NSString::from_str("Version"),
        NSString::from_str("Credits"),
    ];
    let name = NSString::from_str(crate::about::NAME);
    let version = NSString::from_str(env!("CARGO_PKG_VERSION"));
    let build = NSString::from_str("");

    let values: [&AnyObject; 4] = [
        // SAFETY: each is a live Objective-C object for the call's duration.
        unsafe { &*(Retained::as_ptr(&name) as *const AnyObject) },
        unsafe { &*(Retained::as_ptr(&version) as *const AnyObject) },
        unsafe { &*(Retained::as_ptr(&build) as *const AnyObject) },
        unsafe { &*(Retained::as_ptr(&credits) as *const AnyObject) },
    ];
    let key_refs: Vec<&NSString> = keys.iter().map(|k| &**k).collect();
    let options = NSDictionary::from_slices(&key_refs, &values);

    // Bring the panel to the front: a menu-bar app is not the active
    // application, so without this the panel opens behind whatever is.
    activate(mtm);
    // SAFETY: the options dictionary holds only the keys AppKit documents for
    // this panel, each mapped to a live object.
    unsafe { app.orderFrontStandardAboutPanelWithOptions(&options) };
}

/// Make this app frontmost, so a panel it opens is actually visible.
pub fn activate(mtm: MainThreadMarker) {
    let app = NSApplication::sharedApplication(mtm);
    #[allow(deprecated)]
    app.activateIgnoringOtherApps(true);
}

/// Ask for the Eifo checkout with the system folder chooser.
///
/// Returns None when the user cancels, which is an answer and not a failure.
pub fn choose_directory(mtm: MainThreadMarker, prompt: &str) -> Option<std::path::PathBuf> {
    activate(mtm);
    let panel = NSOpenPanel::openPanel(mtm);
    panel.setCanChooseDirectories(true);
    panel.setCanChooseFiles(false);
    panel.setAllowsMultipleSelection(false);
    panel.setMessage(Some(&NSString::from_str(prompt)));
    panel.setPrompt(Some(&NSString::from_str("Use this folder")));

    // 1 is NSModalResponseOK.
    if panel.runModal() != 1 {
        return None;
    }
    let url = panel.URL()?;
    let path = url.path()?;
    Some(std::path::PathBuf::from(path.to_string()))
}

/// Whether macOS is set to start this app at login.
pub mod login_item {
    use objc2_service_management::{SMAppService, SMAppServiceStatus};

    pub fn is_enabled() -> bool {
        let service = unsafe { SMAppService::mainAppService() };
        unsafe { service.status() == SMAppServiceStatus::Enabled }
    }

    /// Register or unregister, returning what went wrong if anything did.
    ///
    /// This only works from a bundled, signed-or-at-least-bundled app: the
    /// service is identified by the bundle, so a bare binary has nothing to
    /// register. The error is surfaced rather than swallowed, because a toggle
    /// that silently does nothing is worse than one that says it cannot.
    pub fn set(enabled: bool) -> Result<(), String> {
        let service = unsafe { SMAppService::mainAppService() };
        let result = if enabled {
            unsafe { service.registerAndReturnError() }
        } else {
            unsafe { service.unregisterAndReturnError() }
        };
        result.map_err(|err| err.localizedDescription().to_string())
    }
}

/// Say that a chosen folder is not an Eifo checkout, and ask whether to retry.
///
/// An alert rather than a silent re-open of the chooser: a picker that reappears
/// with no explanation reads as a bug, and the person choosing has no way to
/// know what was wrong with what they picked.
pub fn confirm_retry(mtm: MainThreadMarker, chosen: &std::path::Path) -> bool {
    use objc2_app_kit::NSAlert;

    activate(mtm);
    let alert = NSAlert::new(mtm);
    alert.setMessageText(&NSString::from_str("That folder is not an Eifo checkout"));
    alert.setInformativeText(&NSString::from_str(&format!(
        "{} has no packages/eifo-api or packages/eifo-fetcher in it.\n\nChoose the folder the project was cloned into.",
        chosen.display()
    )));
    alert.addButtonWithTitle(&NSString::from_str("Choose again"));
    alert.addButtonWithTitle(&NSString::from_str("Quit"));
    // 1000 is NSAlertFirstButtonReturn.
    alert.runModal() == 1000
}
