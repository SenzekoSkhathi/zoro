fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
            "start_drag",
            "toggle_always_on_top",
            "set_always_on_top",
            "get_always_on_top",
            "set_window_size",
            "hide_window",
        ])),
    )
    .expect("failed to run tauri-build");
}
