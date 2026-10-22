// Zoro desktop shell.
//
// A thin native window around the hosted Zoro UI (loaded from Vercel, so UI
// updates arrive automatically). Adds the desktop-native behaviour the
// browser can't: a system-tray presence, launch-at-login, close-to-tray,
// and "wake-summon" — when the local wake service hears "Hey Zoro" it
// re-launches this exe, and the single-instance handler surfaces the window
// instead of opening a second copy.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};

/// Reveal + focus the main window (used by the tray, and by the wake-summon
/// single-instance callback).
fn surface_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Inject the wake trigger into the (remote) page. The hosted UI can't reach
/// the local ws:// wake socket from an HTTPS origin (mixed content), so the
/// native side bridges it: the page exposes window.__zoroWake(), and we call
/// it here when the wake service relaunches us with --wake / --proactive.
fn trigger_wake(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval("window.__zoroWake && window.__zoroWake();");
    }
}

fn argv_has_wake<I: IntoIterator<Item = String>>(argv: I) -> bool {
    argv.into_iter().any(|a| a == "--wake" || a == "--proactive")
}

fn main() {
    tauri::Builder::default()
        // A second launch (e.g. the wake service running the exe on
        // "Hey Zoro") routes here instead of starting a new process.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // The wake service relaunches this exe on "Hey Zoro"; surface the
            // window, and if it carried --wake, kick off the session too.
            surface_main(app);
            if argv_has_wake(argv) {
                trigger_wake(app);
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .setup(|app| {
            // Enable launch-at-login (idempotent).
            {
                use tauri_plugin_autostart::ManagerExt;
                let _ = app.autolaunch().enable();
            }

            // System tray with Show / Quit.
            let show_i = MenuItem::with_id(app, "show", "Show Zoro", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit Zoro", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &quit_i])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Zoro")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => surface_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        surface_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // Anchor the floating window to the bottom-right of the primary display
            if let Some(win) = app.get_webview_window("main") {
                if let Ok(Some(monitor)) = win.primary_monitor() {
                    let screen_size = monitor.size();
                    let scale = monitor.scale_factor();
                    let win_w = (500.0 * scale) as i32;
                    let win_h = (860.0 * scale) as i32;
                    let pos_x = (screen_size.width as i32) - win_w - (20.0 * scale) as i32;
                    let pos_y = (screen_size.height as i32) - win_h - (60.0 * scale) as i32;
                    let _ = win.set_position(tauri::Position::Physical(tauri::PhysicalPosition { x: pos_x, y: pos_y }));
                }

                if let Ok(url_str) = std::env::var("ZORO_OVERLAY_URL") {
                    if let Ok(target) = url_str.parse() {
                        let _ = win.navigate(target);
                    }
                }
            }

            // Cold-start wake: if the wake service launched us from scratch
            // with --wake, the remote page needs a moment to load and define
            // window.__zoroWake before we call it.
            if argv_has_wake(std::env::args()) {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(4));
                    trigger_wake(&handle);
                });
            }

            Ok(())
        })
        // Closing the window hides it to the tray instead of quitting, so
        // Zoro stays resident and wake-summon can re-surface her instantly.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Zoro desktop shell");
}
