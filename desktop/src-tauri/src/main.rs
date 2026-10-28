// Zoro desktop shell.
//
// A thin native window around the hosted Zoro UI. Adds the desktop-native
// behaviour the browser can't: a system-tray presence, launch-at-login,
// close-to-tray, global Alt+F5 Always-on-Top pinning, native OS cursor-tracked
// dragging (both double-click + move and click + drag), and "wake-summon".
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::OpenOptions;
use std::io::Write;
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
static DRAG_START_MS: AtomicU64 = AtomicU64::new(0);

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn log_desktop(msg: &str) {
    let path = std::path::Path::new("C:\\Users\\senze\\zoro\\data\\desktop.log");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "[{}] {}", now_ms(), msg);
    }
}

#[cfg(target_os = "windows")]
mod win32 {
    use std::ffi::c_void;

    #[repr(C)]
    #[derive(Copy, Clone, Default, Debug)]
    pub struct Point {
        pub x: i32,
        pub y: i32,
    }

    #[repr(C)]
    #[derive(Copy, Clone, Default, Debug)]
    pub struct Rect {
        pub left: i32,
        pub top: i32,
        pub right: i32,
        pub bottom: i32,
    }

    #[repr(C)]
    pub struct Msg {
        pub hwnd: *mut c_void,
        pub message: u32,
        pub w_param: usize,
        pub l_param: isize,
        pub time: u32,
        pub pt: Point,
    }

    pub const VK_LBUTTON: i32 = 0x01;
    pub const VK_ESCAPE: i32 = 0x1B;
    pub const VK_MENU: i32 = 0x12; // Alt key
    pub const VK_LMENU: i32 = 0xA4;
    pub const VK_RMENU: i32 = 0xA5;
    pub const VK_F4: i32 = 0x73;
    pub const VK_F5: i32 = 0x74;

    pub const MOD_ALT: u32 = 0x0001;
    pub const MOD_NOREPEAT: u32 = 0x4000;
    pub const WM_HOTKEY: u32 = 0x0312;
    pub const PM_REMOVE: u32 = 0x0001;
    pub const GA_ROOT: u32 = 2;

    pub const HWND_TOP: isize = 0;
    pub const HWND_BOTTOM: isize = 1;
    pub const HWND_TOPMOST: isize = -1;
    pub const HWND_NOTOPMOST: isize = -2;

    pub const SWP_NOSIZE: u32 = 0x0001;
    pub const SWP_NOMOVE: u32 = 0x0002;
    pub const SWP_NOZORDER: u32 = 0x0004;
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
        pub fn GetWindowRect(hwnd: *mut c_void, lp_rect: *mut Rect) -> i32;
        pub fn WindowFromPoint(point: Point) -> *mut c_void;
        pub fn GetAncestor(hwnd: *mut c_void, ga_flags: u32) -> *mut c_void;
        pub fn RegisterHotKey(
            hwnd: *mut c_void,
            id: i32,
            fs_modifiers: u32,
            vk: u32,
        ) -> i32;
        pub fn PeekMessageW(
            lp_msg: *mut Msg,
            hwnd: *mut c_void,
            w_msg_filter_min: u32,
            w_msg_filter_max: u32,
            w_remove_msg: u32,
        ) -> i32;
    }

    pub fn is_key_down(vk: i32) -> bool {
        unsafe { (GetAsyncKeyState(vk) as u16 & 0x8000) != 0 }
    }

    pub fn is_alt_down() -> bool {
        is_key_down(VK_MENU) || is_key_down(VK_LMENU) || is_key_down(VK_RMENU)
    }

    pub fn cursor_pos() -> Option<Point> {
        let mut pt = Point::default();
        if unsafe { GetCursorPos(&mut pt) } != 0 {
            Some(pt)
        } else {
            None
        }
    }

    pub fn window_rect(hwnd: *mut c_void) -> Option<Rect> {
        let mut r = Rect::default();
        if unsafe { GetWindowRect(hwnd, &mut r) } != 0 {
            Some(r)
        } else {
            None
        }
    }

    pub fn is_cursor_on_our_orb(hwnd: *mut c_void, pt: Point, scale: f64) -> bool {
        if hwnd.is_null() {
            return false;
        }
        unsafe {
            let hit = WindowFromPoint(pt);
            if hit.is_null() {
                return false;
            }
            let root = GetAncestor(hit, GA_ROOT);
            if root != hwnd && hit != hwnd {
                return false;
            }
            if let Some(r) = window_rect(hwnd) {
                let orb_zone_height = (240.0 * scale) as i32;
                // Only treat the bottom 240px (where the Orb sits) as draggable so
                // any upper cards/widgets remain clickable without dragging the window.
                pt.x >= r.left
                    && pt.x <= r.right
                    && pt.y >= (r.bottom - orb_zone_height)
                    && pt.y <= r.bottom
            } else {
                false
            }
        }
    }

    pub fn move_window_fast(hwnd: *mut c_void, x: i32, y: i32) {
        if hwnd.is_null() {
            return;
        }
        unsafe {
            SetWindowPos(
                hwnd,
                0,
                x,
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
    }

    pub fn apply_topmost(hwnd: *mut c_void, pinned: bool) {
        if hwnd.is_null() {
            return;
        }
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
                let fg_root = if !fg.is_null() {
                    GetAncestor(fg, GA_ROOT)
                } else {
                    std::ptr::null_mut()
                };
                if !fg.is_null() && fg != hwnd && fg_root != hwnd {
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

#[cfg(target_os = "windows")]
fn run_drag_session(window: WebviewWindow, start_cursor: win32::Point, is_double_click: bool) {
    let now = now_ms();
    if IS_DRAGGING
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        // Ignore duplicate start_drag triggers within 260ms of starting a drag
        let started = DRAG_START_MS.load(Ordering::SeqCst);
        if now.saturating_sub(started) >= 260 {
            IS_DRAGGING.store(false, Ordering::SeqCst);
        }
        return;
    }
    DRAG_START_MS.store(now, Ordering::SeqCst);

    let hwnd_usize = window
        .hwnd()
        .map(|h| h.0 as usize)
        .unwrap_or(0);
    let start_win = match window.outer_position() {
        Ok(pos) => pos,
        Err(_) => {
            IS_DRAGGING.store(false, Ordering::SeqCst);
            return;
        }
    };

    log_desktop(&format!(
        "drag started (double_click={}) at cursor=({}, {}) win=({}, {})",
        is_double_click, start_cursor.x, start_cursor.y, start_win.x, start_win.y
    ));

    let _ = window.eval("window.__zoroStartDrag && window.__zoroStartDrag();");

    std::thread::spawn(move || {
        let hwnd = hwnd_usize as *mut std::ffi::c_void;
        let t0 = now_ms();
        let mut max_dist_while_held: f64 = 0.0;

        // Phase 1: While the initial mouse button press is still held down,
        // move the window with the cursor and measure how far the user dragged.
        while IS_DRAGGING.load(Ordering::SeqCst) && win32::is_key_down(win32::VK_LBUTTON) {
            if win32::is_key_down(win32::VK_ESCAPE) {
                IS_DRAGGING.store(false, Ordering::SeqCst);
                break;
            }
            if let Some(cur) = win32::cursor_pos() {
                let dx = cur.x - start_cursor.x;
                let dy = cur.y - start_cursor.y;
                let dist = ((dx * dx + dy * dy) as f64).sqrt();
                if dist > max_dist_while_held {
                    max_dist_while_held = dist;
                }
                if hwnd != std::ptr::null_mut() {
                    win32::move_window_fast(hwnd, start_win.x + dx, start_win.y + dy);
                } else {
                    let _ = window.set_position(tauri::Position::Physical(
                        tauri::PhysicalPosition {
                            x: start_win.x + dx,
                            y: start_win.y + dy,
                        },
                    ));
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(6));
        }

        let held_ms = now_ms().saturating_sub(t0);

        // Phase 2: If this was a tap-tap double-click (the user released the 2nd click
        // quickly without having dragged > 12px yet), keep floating the Orb with the
        // mouse cursor until the user clicks once (or presses Esc) to place it!
        if IS_DRAGGING.load(Ordering::SeqCst)
            && is_double_click
            && max_dist_while_held <= 12.0
            && held_ms <= 280
        {
            while IS_DRAGGING.load(Ordering::SeqCst) {
                if win32::is_key_down(win32::VK_ESCAPE) {
                    break;
                }
                if win32::is_key_down(win32::VK_LBUTTON) {
                    // User clicked to drop the Orb -> wait for release so it doesn't wake Zoro
                    while win32::is_key_down(win32::VK_LBUTTON) {
                        std::thread::sleep(std::time::Duration::from_millis(8));
                    }
                    break;
                }
                if let Some(cur) = win32::cursor_pos() {
                    let dx = cur.x - start_cursor.x;
                    let dy = cur.y - start_cursor.y;
                    if hwnd != std::ptr::null_mut() {
                        win32::move_window_fast(hwnd, start_win.x + dx, start_win.y + dy);
                    } else {
                        let _ = window.set_position(tauri::Position::Physical(
                            tauri::PhysicalPosition {
                                x: start_win.x + dx,
                                y: start_win.y + dy,
                            },
                        ));
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(6));
            }
        }

        IS_DRAGGING.store(false, Ordering::SeqCst);
        let _ = window.eval("window.__zoroEndDrag && window.__zoroEndDrag();");
        log_desktop("drag finished");
    });
}

#[tauri::command]
fn start_drag(window: WebviewWindow) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        if let Some(pt) = win32::cursor_pos() {
            run_drag_session(window, pt, true);
            return Ok(());
        }
        window.start_dragging().map_err(|e| e.to_string())
    }

    #[cfg(not(target_os = "windows"))]
    {
        window.start_dragging().map_err(|e| e.to_string())
    }
}

fn apply_pin_state(win: &WebviewWindow, next: bool) -> Result<bool, String> {
    IS_PINNED.store(next, Ordering::SeqCst);
    log_desktop(&format!("apply_pin_state -> {}", next));

    let win_clone = win.clone();
    let _ = win.run_on_main_thread(move || {
        if next {
            let _ = win_clone.show();
            let _ = win_clone.unminimize();
        }
        let _ = win_clone.set_always_on_top(next);
        #[cfg(target_os = "windows")]
        if let Ok(hwnd) = win_clone.hwnd() {
            win32::apply_topmost(hwnd.0 as *mut std::ffi::c_void, next);
        }
    });

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
fn spawn_native_input_engine(app: tauri::AppHandle) {
    const HOTKEY_PIN_ID: i32 = 1;

    std::thread::spawn(move || {
        // 1. Register kernel-level global hotkey for Alt+F5 so it works even when
        //    other apps or elevated windows are in the foreground.
        let hk_ok = unsafe {
            win32::RegisterHotKey(
                std::ptr::null_mut(),
                HOTKEY_PIN_ID,
                win32::MOD_ALT | win32::MOD_NOREPEAT,
                win32::VK_F5 as u32,
            )
        };
        log_desktop(&format!("RegisterHotKey(Alt+F5) result={}", hk_ok));

        let mut prev_alt_f5 = false;
        let mut prev_lbutton = false;
        let mut press_started_on_orb = false;
        let mut press_start_pt = win32::Point::default();
        let mut last_orb_press_ms: u64 = 0;

        let (hwnd_usize, scale) = if let Some(win) = app.get_webview_window("main") {
            let h = win.hwnd().map(|v| v.0 as usize).unwrap_or(0);
            let s = win
                .primary_monitor()
                .ok()
                .flatten()
                .map(|m| m.scale_factor())
                .unwrap_or(1.0);
            (h, s)
        } else {
            (0, 1.0)
        };
        let hwnd = hwnd_usize as *mut std::ffi::c_void;

        loop {
            // A. Check kernel WM_HOTKEY messages
            unsafe {
                let mut msg: win32::Msg = std::mem::zeroed();
                while win32::PeekMessageW(
                    &mut msg,
                    std::ptr::null_mut(),
                    0,
                    0,
                    win32::PM_REMOVE,
                ) != 0
                {
                    if msg.message == win32::WM_HOTKEY
                        && msg.w_param == HOTKEY_PIN_ID as usize
                    {
                        toggle_pin_state(&app);
                    }
                }
            }

            // B. Check GetAsyncKeyState for Alt+F5 (fallback if another process bound WM_HOTKEY)
            let alt_f5 = win32::is_alt_down() && win32::is_key_down(win32::VK_F5);
            if alt_f5 && !prev_alt_f5 {
                toggle_pin_state(&app);
            }
            prev_alt_f5 = alt_f5;

            // C. Native OS-level Orb Mouse Dragging (both Click+Drag and Double-Click+Move)
            if !IS_DRAGGING.load(Ordering::SeqCst) {
                let lbutton = win32::is_key_down(win32::VK_LBUTTON);
                if lbutton && !prev_lbutton {
                    // Rising edge of left mouse button
                    if let Some(pt) = win32::cursor_pos() {
                        if win32::is_cursor_on_our_orb(hwnd, pt, scale) {
                            let now = now_ms();
                            let delta = now.saturating_sub(last_orb_press_ms);
                            last_orb_press_ms = now;
                            press_started_on_orb = true;
                            press_start_pt = pt;

                            if delta > 0 && delta < 430 {
                                // Double-click on the Orb detected natively -> start drag!
                                press_started_on_orb = false;
                                if let Some(win) = app.get_webview_window("main") {
                                    run_drag_session(win, pt, true);
                                }
                            }
                        } else {
                            press_started_on_orb = false;
                        }
                    }
                } else if lbutton && prev_lbutton && press_started_on_orb {
                    // Mouse button held down after pressing on the Orb -> check for >5px drag
                    if let Some(pt) = win32::cursor_pos() {
                        let dx = pt.x - press_start_pt.x;
                        let dy = pt.y - press_start_pt.y;
                        if (dx * dx + dy * dy) > 25 {
                            press_started_on_orb = false;
                            last_orb_press_ms = 0;
                            if let Some(win) = app.get_webview_window("main") {
                                run_drag_session(win, press_start_pt, false);
                            }
                        }
                    }
                } else if !lbutton {
                    press_started_on_orb = false;
                }
                prev_lbutton = lbutton;
            } else {
                prev_lbutton = win32::is_key_down(win32::VK_LBUTTON);
                press_started_on_orb = false;
            }

            std::thread::sleep(std::time::Duration::from_millis(10));
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
            log_desktop("Zoro desktop setup starting");
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
            spawn_native_input_engine(app.handle().clone());

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
