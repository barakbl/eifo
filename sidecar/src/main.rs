//! Eifo's menu-bar companion.
//!
//! A dot in the menu bar that is green when the catalog is well, orange when a
//! source has gone stale, and red when the web server is not answering; a menu
//! that runs the nightly chain and keeps the server up; and nothing else. It is
//! a companion to an Eifo checkout, not a copy of one - every action here is
//! the same `eifo-fetch` command a person would type, run against the folder
//! this app was pointed at.
//!
//! The main thread does nothing slow. It owns the run loop, the status item and
//! the menu, and it renders whatever the worker thread hands it; the worker
//! polls, spawns and waits. That split is why a menu still opens instantly
//! while a two-hour sync is running.

#![cfg_attr(not(test), windows_subsystem = "windows")]

mod about;
mod config;
mod health;
mod icons;
mod menu;
mod platform;
mod procs;
mod runs;
mod schedule;
mod update;
mod worker;

use std::sync::mpsc::{channel, Receiver, Sender};

use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::macos::{ActivationPolicy, EventLoopExtMacOS};
use tray_icon::menu::MenuEvent;
use tray_icon::{TrayIcon, TrayIconBuilder};

use config::Config;
use health::Status;
use procs::Phase;
use runs::SourceOption;
use worker::{Command, Snapshot, UpdateView};

/// Posted to the run loop when the worker has something new to show.
#[derive(Debug, Clone, Copy)]
struct Wake;

fn main() {
    // Before anything is spawned: a signal that arrives later must find a
    // handler that knows which process to take down with it.
    procs::catch_termination();

    let mut event_loop = EventLoopBuilder::<Wake>::with_user_event().build();
    // The programmatic half of LSUIElement: no Dock icon, no app-switcher
    // entry. Set here as well as in Info.plist so a `cargo run` during
    // development behaves like the bundle does.
    event_loop.set_activation_policy(ActivationPolicy::Accessory);
    let mtm = objc2::MainThreadMarker::new().expect("the event loop runs on the main thread");

    // Nowhere to point at means nothing to say, so this is asked before
    // anything else - and asked with the system folder chooser, because a
    // menu-bar app has nowhere to put a settings window.
    let config = match Config::load() {
        Some(config) => config,
        None => match first_run(mtm) {
            Some(config) => config,
            None => return,
        },
    };

    let (to_worker, from_main) = channel::<Command>();
    let (to_main, from_worker) = channel::<Snapshot>();

    let proxy: EventLoopProxy<Wake> = event_loop.create_proxy();
    let _worker = worker::spawn(config.clone(), from_main, to_main, move || {
        // Waking the run loop is the only thing the worker does to the main
        // thread; everything it wants shown travels on the channel.
        let _ = proxy.send_event(Wake);
    });

    let (tray_menu, items) = menu::build(platform::login_item::is_enabled());
    let mut tray = Some(
        TrayIconBuilder::new()
            .with_menu(Box::new(tray_menu))
            .with_icon(icons::icon_for(Status::Unknown))
            .with_tooltip("Eifo")
            .build()
            .expect("the status item could not be created"),
    );

    let mut shown = Status::Unknown;
    let mut config = config;
    // The last thing the worker showed, so a menu click knows what state it is
    // acting on - "Check for updates" and "Update to v0.3.0" are one item.
    let mut update = UpdateView::Unknown;
    // The services currently on offer, so a click on one of them can be turned
    // back into the source it names. A menu event carries an id and nothing
    // else, and the id is built from the key.
    let mut sources: Vec<SourceOption> = Vec::new();

    // Stop the server, drop the status item, and leave the run loop - the one
    // exit path, shared by Quit and by a finished update that must relaunch.
    let quit = |to_worker: &Sender<Command>,
                tray: &mut Option<TrayIcon>,
                control_flow: &mut ControlFlow| {
        let (ack, acked) = channel::<()>();
        let _ = to_worker.send(Command::Shutdown(ack));
        let _ = acked.recv_timeout(std::time::Duration::from_secs(8));
        tray.take();
        *control_flow = ControlFlow::Exit;
    };

    event_loop.run(move |_event, _target, control_flow| {
        *control_flow = ControlFlow::Wait;

        if let Some(snapshot) = drain_updates(&from_worker, &items, tray.as_mut(), &mut shown) {
            update = snapshot.update.clone();
            sources = snapshot.run.sources.clone();
            if snapshot.relaunch {
                // The worker has built the new bundle and spawned the process
                // that will open it once we are gone. All that is left is to go.
                quit(&to_worker, &mut tray, control_flow);
                return;
            }
        }

        while let Ok(event) = MenuEvent::receiver().try_recv() {
            let id = &event.id;
            if id == items.quit.id() {
                // Waited on rather than fired and forgotten: the event loop
                // never returns from `run` on macOS, so exiting immediately can
                // beat the worker to stopping the server - and an orphaned
                // server still holding the port is what makes the next launch
                // report on a process it does not own.
                quit(&to_worker, &mut tray, control_flow);
            } else if id == items.update.id() {
                // One item, two jobs: install the update if there is one to
                // install, otherwise go and look.
                let command = match update {
                    UpdateView::Available { .. } => Command::RunUpdate,
                    _ => Command::CheckUpdate,
                };
                let _ = to_worker.send(command);
            } else if id == items.run_sync.id() {
                let _ = to_worker.send(Command::Run(Phase::Sync));
            } else if id == items.run_enrich.id() {
                let _ = to_worker.send(Command::Run(Phase::Enrich));
            } else if id == items.run_images.id() {
                let _ = to_worker.send(Command::Run(Phase::Images));
            } else if id == items.run_all.id() {
                let _ = to_worker.send(Command::Run(Phase::All));
            } else if id == items.stop_fetch.id() {
                let _ = to_worker.send(Command::StopFetch);
            } else if id == items.check_now.id() {
                let _ = to_worker.send(Command::Refresh);
            } else if id == items.start_server.id() {
                let _ = to_worker.send(Command::StartServer);
            } else if id == items.stop_server.id() {
                let _ = to_worker.send(Command::StopServer);
            } else if id == items.restart_server.id() {
                let _ = to_worker.send(Command::RestartServer);
            } else if id == items.keep_up.id() {
                let _ = to_worker.send(Command::SetKeepServerUp(items.keep_up.is_checked()));
            } else if id == items.start_on_open.id() {
                let _ = to_worker.send(Command::SetStartServerOnOpen(
                    items.start_on_open.is_checked(),
                ));
            } else if id == items.schedule.id() {
                let _ = to_worker.send(Command::SetScheduleEnabled(items.schedule.is_checked()));
            } else if id == items.open_app.id() {
                platform::open_url(&config.base_url);
            } else if id == items.open_manage.id() {
                platform::open_url(&format!(
                    "{}/#/manage",
                    config.base_url.trim_end_matches('/')
                ));
            } else if id == items.about.id() {
                platform::show_about(mtm);
            } else if id == items.login_item.id() {
                let wanted = items.login_item.is_checked();
                if let Err(err) = platform::login_item::set(wanted) {
                    // Put the tick back: a checkbox that stays ticked after the
                    // system refused is a lie about the state of the machine.
                    items.login_item.set_checked(!wanted);
                    items
                        .detail
                        .set_text(format!("Could not change login item: {err}"));
                }
            } else if id == items.choose_folder.id() {
                if let Some(dir) = platform::choose_directory(mtm, "Choose your Eifo folder") {
                    if Config::looks_like_eifo(&dir) {
                        config.app_dir = dir;
                        let _ = config.save();
                        items
                            .detail
                            .set_text("Folder changed - restart Eifo to use it");
                    } else {
                        items.detail.set_text("That folder is not an Eifo checkout");
                    }
                }
            } else if let Some(key) = menu::source_key(&id.0) {
                // One service from the Sync one submenu. Matched against the
                // list the last snapshot carried rather than trusted from the
                // id, so a stale item left by a source that has since been
                // switched off cannot start a run for it.
                if let Some(source) = sources.iter().find(|source| source.key == key) {
                    let _ = to_worker.send(Command::Run(Phase::One {
                        key: source.key.clone(),
                        name: source.name.clone(),
                    }));
                }
            }
        }
    });
}

/// Render every snapshot waiting, and repaint the dot only when it changed.
/// Returns the one it rendered, so the caller can act on `relaunch`.
fn drain_updates(
    from_worker: &Receiver<Snapshot>,
    items: &menu::Items,
    tray: Option<&mut TrayIcon>,
    shown: &mut Status,
) -> Option<Snapshot> {
    let mut latest = None;
    while let Ok(snapshot) = from_worker.try_recv() {
        latest = Some(snapshot);
    }
    let snapshot = latest?;

    menu::apply(items, &snapshot);
    if let Some(tray) = tray {
        let _ = tray.set_tooltip(Some(menu::tooltip(&snapshot)));
        // Repainted only on a change: setting the same image twenty times a
        // minute makes the status item flicker on some displays.
        if snapshot.status != *shown {
            let _ = tray.set_icon(Some(icons::icon_for(snapshot.status)));
            *shown = snapshot.status;
        }
    }
    Some(snapshot)
}

/// First launch: ask which folder, and refuse anything that is not one.
fn first_run(mtm: objc2::MainThreadMarker) -> Option<Config> {
    loop {
        let dir = platform::choose_directory(mtm, "Choose your Eifo folder")?;
        if Config::looks_like_eifo(&dir) {
            let config = Config::new(dir);
            let _ = config.save();
            return Some(config);
        }
        // Told while the chooser is still the thing on screen, rather than as
        // four broken menu items ten minutes later.
        if !platform::confirm_retry(mtm, &dir) {
            return None;
        }
    }
}

/// Sender kept alive for the life of the process; dropping it stops the worker.
#[allow(dead_code)]
struct KeepAlive(Sender<Command>);
