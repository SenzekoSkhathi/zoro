fn main() {
    // Declare the app's custom commands so tauri-build generates
    // `allow-<command>` ACL permissions for them. Tauri v2 rejects every
    // custom command invoked from a *remote* origin (the hosted Zoro UI)
    // unless a capability explicitly grants it — without this manifest there
    // is nothing to grant, so e.g. `set_window_size` silently failed and the
    // window never expanded for widgets.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "start_drag",
                "toggle_always_on_top",
                "set_always_on_top",
                "get_always_on_top",
                "set_window_size",
                "hide_window",
                "set_overlay_mode",
                "set_hit_regions",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
