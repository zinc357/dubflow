// DubFlow GUI shell.
// Dev workflow: start engine first (see README), then `npm run tauri dev`.
// Packaged app: spawns the bundled dubflow-engine sidecar on startup and
// kills it on exit. The engine listens on 127.0.0.1:8741 (DUBFLOW_PORT).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// 持有引擎子进程，应用退出时 kill。
struct EngineChild(Mutex<Option<CommandChild>>);

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(EngineChild(Mutex::new(None)))
        .setup(|app| {
            let sidecar = app.shell().sidecar("dubflow-engine")?;
            let (mut rx, child) = sidecar.spawn()?;
            app.state::<EngineChild>()
                .0
                .lock()
                .unwrap()
                .replace(child);

            // 排空引擎 stdout/stderr，避免管道写满阻塞引擎
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let tauri_plugin_shell::process::CommandEvent::Error(err) = event {
                        eprintln!("engine stderr: {}", err);
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running DubFlow")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(child) =
                    app_handle.state::<EngineChild>().0.lock().unwrap().take()
                {
                    let _ = child.kill();
                }
            }
        });
}
