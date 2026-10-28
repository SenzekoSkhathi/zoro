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
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WebviewWindow, WindowEvent,
};

#[tauri::command]
fn start_drag(window: WebviewWindow) -> Result<(), String> {
    window.start_dragging().map_err(|e| e.to_string())
}

#[tauri::command]
fn toggle_always_on_top(window: WebviewWindow) -> Result<bool, String> {
    let current = window.is_always_on_top().map_err(|e| e.to_string())?;
    let next = !current;
    window.set_always_on_top(next).map_err(|e| e.to_string())?;
    Ok(next)
}

#[tauri::command]
fn set_always_on_top(window: WebviewWindow, enabled: bool) -> Result<bool, String> {
    window.set_always_on_top(enabled).map_err(|e| e.to_string())?;
    Ok(enabled)
}

#[tauri::command]
fn get_always_on_top(window: WebviewWindow) -> Result<bool, String> {
    window.is_always_on_top().map_err(|e| e.to_string())
}

#[tauri::command]
fn set_window_size(window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    if let Ok(Some(monitor)) = window.primary_monitor() {
        let scale = monitor.scale_factor();
        window.set_size(tauri::Size::Physical(tauri::PhysicalSize {
            width: (width * scale) as u32,
            height: (height * scale) as u32,
        })).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn hide_window(window: WebviewWindow) -> Result<(), String> {
    window.hide().map_err(|e| e.to_string())
}

/// Reveal + focus the main window (used by the tray).
fn surface_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Reveal the main window without stealing focus from fullscreen apps/movies.
fn surface_main_passive(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
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
            // window passively (no focus steal) if carrying --wake, and kick
            // off the session.
            if argv_has_wake(argv) {
                surface_main_passive(app);
                trigger_wake(app);
            } else {
                surface_main(app);
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![
            start_drag,
            toggle_always_on_top,
            set_always_on_top,
            get_always_on_top,
            set_window_size,
            hide_window
        ])
        .setup(|app| {
            // Enable launch-at-login (idempotent).
            {
                use tauri_plugin_autostart::ManagerExt;
                let _ = app.autolaunch().enable();
            }

            // System tray with Show / Hide / Always On Top / Reset / Quit.
            let show_i = MenuItem::with_id(app, "show", "Show Zoro", true, None::<&str>)?;
            let hide_i = MenuItem::with_id(app, "hide", "Hide Zoro", true, None::<&str>)?;
            let top_i = MenuItem::with_id(app, "toggle_top", "Toggle Always on Top", true, None::<&str>)?;
            let reset_pos_i = MenuItem::with_id(app, "reset_pos", "Reset Position (Bottom-Right)", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit Zoro", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &hide_i, &top_i, &reset_pos_i, &sep, &quit_i])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Zoro - Voice Assistant")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => surface_main(app),
                    "hide" => {
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.hide();
                        }
                    }
                    "toggle_top" => {
                        if let Some(win) = app.get_webview_window("main") {
                            if let Ok(cur) = win.is_always_on_top() {
                                let _ = win.set_always_on_top(!cur);
                            }
                        }
                    }
                    "reset_pos" => {
                        if let Some(win) = app.get_webview_window("main") {
                            if let Ok(Some(monitor)) = win.primary_monitor() {
                                let screen_size = monitor.size();
                                let scale = monitor.scale_factor();
                                let win_w = (240.0 * scale) as i32;
                                let win_h = (240.0 * scale) as i32;
                                let pos_x = (screen_size.width as i32) - win_w - (24.0 * scale) as i32;
                                let pos_y = (screen_size.height as i32) - win_h - (60.0 * scale) as i32;
                                let _ = win.set_position(tauri::Position::Physical(tauri::PhysicalPosition { x: pos_x, y: pos_y }));
                                let _ = win.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                                    width: win_w as u32,
                                    height: win_h as u32,
                                }));
                            }
                        }
                    }
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

            // Anchor the floating window to the bottom-right of the primary display in compact Orb mode
            if let Some(win) = app.get_webview_window("main") {
                if let Ok(Some(monitor)) = win.primary_monitor() {
                    let screen_size = monitor.size();
                    let scale = monitor.scale_factor();
                    // Compact 240x240 initial box around the Orb — does not block screen buttons!
                    let win_w = (240.0 * scale) as i32;
                    let win_h = (240.0 * scale) as i32;
                    let pos_x = (screen_size.width as i32) - win_w - (24.0 * scale) as i32;
                    let pos_y = (screen_size.height as i32) - win_h - (60.0 * scale) as i32;
                    let _ = win.set_position(tauri::Position::Physical(tauri::PhysicalPosition { x: pos_x, y: pos_y }));
                    let _ = win.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                        width: win_w as u32,
                        height: win_h as u32,
                    }));
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
