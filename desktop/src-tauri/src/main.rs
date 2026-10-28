// Zoro desktop shell.
//
// A thin native window around the hosted Zoro UI. Adds the desktop-native
// behaviour the browser can't: a system-tray presence, launch-at-login,
// close-to-tray, global Alt+F5 Always-on-Top pinning, native OS cursor-tracked
// dragging, and "wake-summon".
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WebviewWindow, WindowEvent,
};

static IS_DRAGGING: AtomicBool = AtomicBool::new(false);
static IS_PINNED: AtomicBool = AtomicBool::new(false);
static LAST_PIN_TOGGLE_MS: AtomicU64 = AtomicU64::new(0);

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(target_os = "windows")]
mod win32 {
    use std::ffi::c_void;

    #[repr(C)]
    #[derive(Copy, Clone, Default)]
    pub struct Point {
        pub x: i32,
        pub y: i32,
    }

    pub const VK_LBUTTON: i32 = 0x01;
    pub const VK_ESCAPE: i32 = 0x1B;
    pub const VK_MENU: i32 = 0x12; // Alt key
    pub const VK_F4: i32 = 0x73;
    pub const VK_F5: i32 = 0x74;

    pub const HWND_TOP: isize = 0;
    pub const HWND_BOTTOM: isize = 1;
    pub const HWND_TOPMOST: isize = -1;
    pub const HWND_NOTOPMOST: isize = -2;

    pub const SWP_NOSIZE: u32 = 0x0001;
    pub const SWP_NOMOVE: u32 = 0x0002;
    pub const SWP_NOACTIVATE: u32 = 0x0010;
    pub const SWP_SHOWWINDOW: u32 = 0x0040;

    #[link(name = "user32")]
    extern "system" {
        pub fn GetCursorPos(lp_point: *mut Point) -> i32;
        pub fn GetAsyncKeyState(v_key: i32) -> i16;
        pub fn SetWindowPos(
            hwnd: *mut c_void,
            hwnd_insert_after: isize,
            x: i32,
            y: i32,
            cx: i32,
            cy: i32,
            u_flags: u32,
        ) -> i32;
        pub fn GetForegroundWindow() -> *mut c_void;
    }

    pub fn is_key_down(vk: i32) -> bool {
        unsafe { (GetAsyncKeyState(vk) as u16 & 0x8000) != 0 }
    }

    pub fn cursor_pos() -> Option<Point> {
        let mut pt = Point::default();
        if unsafe { GetCursorPos(&mut pt) } != 0 {
            Some(pt)
        } else {
            None
        }
    }

    pub fn apply_topmost(hwnd: *mut c_void, pinned: bool) {
        unsafe {
            if pinned {
                SetWindowPos(
                    hwnd,
                    HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE,
                );
            } else {
                SetWindowPos(
                    hwnd,
                    HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                );
                let fg = GetForegroundWindow();
                if !fg.is_null() && fg != hwnd {
                    // Drop behind active windows immediately so unpinning is visually instant
                    SetWindowPos(
                        hwnd,
                        HWND_BOTTOM,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                    );
                } else {
                    SetWindowPos(
                        hwnd,
                        HWND_TOP,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                    );
                }
            }
        }
    }
}

#[tauri::command]
fn start_drag(window: WebviewWindow) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        if IS_DRAGGING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            // Already in a drag session -> second click drops the orb
            IS_DRAGGING.store(false, Ordering::SeqCst);
            return Ok(());
        }

        let start_cursor = match win32::cursor_pos() {
            Some(pt) => pt,
            None => {
                IS_DRAGGING.store(false, Ordering::SeqCst);
                return window.start_dragging().map_err(|e| e.to_string());
            }
        };
        let start_win = match window.outer_position() {
            Ok(pos) => pos,
            Err(e) => {
                IS_DRAGGING.store(false, Ordering::SeqCst);
                return Err(e.to_string());
            }
        };

        std::thread::spawn(move || {
            // Brief 65ms window to detect whether the user is holding the mouse button
            // down (hold-to-drag) OR released the second click of a double-click
            // (float-and-move until next click).
            for _ in 0..13 {
                if !IS_DRAGGING.load(Ordering::SeqCst) {
                    break;
                }
                if let Some(cur) = win32::cursor_pos() {
                    let dx = cur.x - start_cursor.x;
                    let dy = cur.y - start_cursor.y;
                    if dx != 0 || dy != 0 {
                        let _ = window.set_position(tauri::Position::Physical(
                            tauri::PhysicalPosition {
                                x: start_win.x + dx,
                                y: start_win.y + dy,
                            },
                        ));
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }

            let hold_mode = win32::is_key_down(win32::VK_LBUTTON);
            let mut saw_release = !hold_mode;

            while IS_DRAGGING.load(Ordering::SeqCst) {
                if win32::is_key_down(win32::VK_ESCAPE) {
                    break;
                }

                let lbutton = win32::is_key_down(win32::VK_LBUTTON);
                if hold_mode {
                    // Hold-and-drag: finish as soon as left mouse button is released
                    if !lbutton {
                        break;
                    }
                } else {
                    // Double-click-and-move: follow cursor until the user clicks to drop
                    if !lbutton {
                        saw_release = true;
                    } else if saw_release {
                        // Wait for release of the drop click so it doesn't trigger a call
                        while win32::is_key_down(win32::VK_LBUTTON) {
                            std::thread::sleep(std::time::Duration::from_millis(8));
                        }
                        break;
                    }
                }

                if let Some(cur) = win32::cursor_pos() {
                    let dx = cur.x - start_cursor.x;
                    let dy = cur.y - start_cursor.y;
                    let _ = window.set_position(tauri::Position::Physical(
                        tauri::PhysicalPosition {
                            x: start_win.x + dx,
                            y: start_win.y + dy,
                        },
                    ));
                }

                std::thread::sleep(std::time::Duration::from_millis(6));
            }

            IS_DRAGGING.store(false, Ordering::SeqCst);
            let _ = window.eval("window.__zoroEndDrag && window.__zoroEndDrag();");
        });

        Ok(())
    }

    #[cfg(not(target_os = "windows"))]
    {
        window.start_dragging().map_err(|e| e.to_string())
    }
}

fn apply_pin_state(win: &WebviewWindow, next: bool) -> Result<bool, String> {
    IS_PINNED.store(next, Ordering::SeqCst);
    if next {
        let _ = win.show();
        let _ = win.unminimize();
    }
    let _ = win.set_always_on_top(next);
    #[cfg(target_os = "windows")]
    if let Ok(hwnd) = win.hwnd() {
        win32::apply_topmost(hwnd.0 as *mut std::ffi::c_void, next);
    }
    let _ = win.eval(&format!(
        "window.__zoroSetPinned && window.__zoroSetPinned({});",
        next
    ));
    Ok(next)
}

#[tauri::command]
fn toggle_always_on_top(window: WebviewWindow) -> Result<bool, String> {
    let now = now_ms();
    let prev = LAST_PIN_TOGGLE_MS.load(Ordering::SeqCst);
    if now.saturating_sub(prev) < 350 {
        return Ok(IS_PINNED.load(Ordering::SeqCst));
    }
    LAST_PIN_TOGGLE_MS.store(now, Ordering::SeqCst);
    let next = !IS_PINNED.load(Ordering::SeqCst);
    apply_pin_state(&window, next)
}

#[tauri::command]
fn set_always_on_top(window: WebviewWindow, enabled: bool) -> Result<bool, String> {
    apply_pin_state(&window, enabled)
}

#[tauri::command]
fn get_always_on_top(_window: WebviewWindow) -> Result<bool, String> {
    Ok(IS_PINNED.load(Ordering::SeqCst))
}

#[tauri::command]
fn set_window_size(window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    if let Ok(Some(monitor)) = window.primary_monitor() {
        let scale = monitor.scale_factor();
        let screen_size = monitor.size();
        let new_w = (width * scale) as u32;
        let new_h = (height * scale) as u32;

        if let (Ok(old_size), Ok(old_pos)) = (window.outer_size(), window.outer_position()) {
            if old_size.width.abs_diff(new_w) <= 2 && old_size.height.abs_diff(new_h) <= 2 {
                return Ok(());
            }
            // Anchor the bottom-right corner (where the Orb sits) so expanding for
            // display cards grows upward/leftward instead of pushing the Orb off-screen!
            let anchor_right = old_pos.x + (old_size.width as i32);
            let anchor_bottom = old_pos.y + (old_size.height as i32);
            let max_x = (screen_size.width as i32 - new_w as i32).max(0);
            let max_y = (screen_size.height as i32 - new_h as i32).max(0);
            let new_x = (anchor_right - new_w as i32).clamp(0, max_x);
            let new_y = (anchor_bottom - new_h as i32).clamp(0, max_y);

            let _ = window.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                width: new_w,
                height: new_h,
            }));
            let _ = window.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
                x: new_x,
                y: new_y,
            }));
            return Ok(());
        }

        window
            .set_size(tauri::Size::Physical(tauri::PhysicalSize {
                width: new_w,
                height: new_h,
            }))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn hide_window(window: WebviewWindow) -> Result<(), String> {
    window.hide().map_err(|e| e.to_string())
}

fn ensure_window_on_screen(win: &WebviewWindow) {
    if let Ok(Some(monitor)) = win.primary_monitor() {
        let screen_size = monitor.size();
        let scale = monitor.scale_factor();
        if let (Ok(pos), Ok(size)) = (win.outer_position(), win.outer_size()) {
            let sw = screen_size.width as i32;
            let sh = screen_size.height as i32;
            let w = size.width as i32;
            let h = size.height as i32;
            if pos.x < -40 || pos.y < -40 || pos.x + w > sw + 40 || pos.y + h > sh + 40 {
                let win_w = (240.0 * scale) as i32;
                let win_h = (240.0 * scale) as i32;
                let pos_x = sw - win_w - (24.0 * scale) as i32;
                let pos_y = sh - win_h - (60.0 * scale) as i32;
                let _ = win.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                    width: win_w as u32,
                    height: win_h as u32,
                }));
                let _ = win.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
                    x: pos_x,
                    y: pos_y,
                }));
            }
        }
    }
}

/// Reveal + focus the main window (used by the tray and Desktop shortcut).
fn surface_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        ensure_window_on_screen(&win);
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
        #[cfg(target_os = "windows")]
        if let Ok(hwnd) = win.hwnd() {
            let ptr = hwnd.0 as *mut std::ffi::c_void;
            if IS_PINNED.load(Ordering::SeqCst) {
                win32::apply_topmost(ptr, true);
            } else {
                unsafe {
                    win32::SetWindowPos(
                        ptr,
                        win32::HWND_TOP,
                        0,
                        0,
                        0,
                        0,
                        win32::SWP_NOMOVE | win32::SWP_NOSIZE | win32::SWP_SHOWWINDOW,
                    );
                }
            }
        }
    }
}

/// Reveal the main window without stealing focus from fullscreen apps/movies.
fn surface_main_passive(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        ensure_window_on_screen(&win);
        let _ = win.show();
        let _ = win.unminimize();
    }
}

/// Toggle Always-on-Top (Pin) state and sync with the webview UI.
fn toggle_pin_state(app: &tauri::AppHandle) {
    let now = now_ms();
    let prev = LAST_PIN_TOGGLE_MS.load(Ordering::SeqCst);
    if now.saturating_sub(prev) < 350 {
        return;
    }
    LAST_PIN_TOGGLE_MS.store(now, Ordering::SeqCst);
    if let Some(win) = app.get_webview_window("main") {
        let next = !IS_PINNED.load(Ordering::SeqCst);
        let _ = apply_pin_state(&win, next);
    }
}

#[cfg(target_os = "windows")]
fn spawn_global_hotkey_listener(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut prev_alt_f5 = false;
        loop {
            let alt_down = win32::is_key_down(win32::VK_MENU);
            let f5_down = win32::is_key_down(win32::VK_F5);
            let alt_f5 = alt_down && f5_down;

            if alt_f5 && !prev_alt_f5 {
                toggle_pin_state(&app);
            }
            prev_alt_f5 = alt_f5;

            std::thread::sleep(std::time::Duration::from_millis(25));
        }
    });
}

/// Inject the wake trigger into the (remote) page.
fn trigger_wake(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval("window.__zoroWake && window.__zoroWake();");
    }
}

fn argv_has_wake<I: IntoIterator<Item = String>>(argv: I) -> bool {
    argv.into_iter().any(|a| a == "--wake" || a == "--proactive")
}

fn argv_has_ensure<I: IntoIterator<Item = String>>(argv: I) -> bool {
    argv.into_iter().any(|a| a == "--ensure")
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if argv_has_wake(argv.clone()) {
                surface_main_passive(app);
                trigger_wake(app);
            } else if argv_has_ensure(argv) {
                surface_main_passive(app);
            } else {
                // User launched Zoro shortcut directly -> show window in front AND wake Zoro
                surface_main(app);
                trigger_wake(app);
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
            {
                use tauri_plugin_autostart::ManagerExt;
                let _ = app.autolaunch().enable();
            }

            let show_i = MenuItem::with_id(app, "show", "Show Zoro", true, None::<&str>)?;
            let hide_i =
                MenuItem::with_id(app, "hide", "Hide Zoro (Alt+F4)", true, None::<&str>)?;
            let top_i = MenuItem::with_id(
                app,
                "toggle_top",
                "Toggle Pin on Top (Alt+F5)",
                true,
                None::<&str>,
            )?;
            let reset_pos_i = MenuItem::with_id(
                app,
                "reset_pos",
                "Reset Position (Bottom-Right)",
                true,
                None::<&str>,
            )?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit Zoro", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[&show_i, &hide_i, &top_i, &reset_pos_i, &sep, &quit_i],
            )?;

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
                        toggle_pin_state(app);
                    }
                    "reset_pos" => {
                        if let Some(win) = app.get_webview_window("main") {
                            if let Ok(Some(monitor)) = win.primary_monitor() {
                                let screen_size = monitor.size();
                                let scale = monitor.scale_factor();
                                let win_w = (240.0 * scale) as i32;
                                let win_h = (240.0 * scale) as i32;
                                let pos_x = (screen_size.width as i32)
                                    - win_w
                                    - (24.0 * scale) as i32;
                                let pos_y = (screen_size.height as i32)
                                    - win_h
                                    - (60.0 * scale) as i32;
                                let _ = win.set_position(tauri::Position::Physical(
                                    tauri::PhysicalPosition { x: pos_x, y: pos_y },
                                ));
                                let _ = win.set_size(tauri::Size::Physical(
                                    tauri::PhysicalSize {
                                        width: win_w as u32,
                                        height: win_h as u32,
                                    },
                                ));
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

            if let Some(win) = app.get_webview_window("main") {
                if let Ok(Some(monitor)) = win.primary_monitor() {
                    let screen_size = monitor.size();
                    let scale = monitor.scale_factor();
                    let win_w = (240.0 * scale) as i32;
                    let win_h = (240.0 * scale) as i32;
                    let pos_x =
                        (screen_size.width as i32) - win_w - (24.0 * scale) as i32;
                    let pos_y =
                        (screen_size.height as i32) - win_h - (60.0 * scale) as i32;
                    let _ = win.set_position(tauri::Position::Physical(
                        tauri::PhysicalPosition { x: pos_x, y: pos_y },
                    ));
                    let _ = win.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                        width: win_w as u32,
                        height: win_h as u32,
                    }));
                    let _ = win.show();
                    let _ = win.unminimize();
                }

                if let Ok(url_str) = std::env::var("ZORO_OVERLAY_URL") {
                    if let Ok(target) = url_str.parse() {
                        let _ = win.navigate(target);
                    }
                }
            }

            if argv_has_wake(std::env::args()) {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(4));
                    trigger_wake(&handle);
                });
            }

            #[cfg(target_os = "windows")]
            spawn_global_hotkey_listener(app.handle().clone());

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Zoro desktop shell");
}
