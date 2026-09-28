#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use sysinfo::{ProcessRefreshKind, System, UpdateKind};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition};

struct PythonProcess(Mutex<Option<Child>>);
struct BackendPort(Mutex<u16>);

fn logs_dir() -> PathBuf {
    let dir = PathBuf::from("logs");
    let _ = fs::create_dir_all(&dir);
    dir
}

fn log(msg: &str) {
    let path = logs_dir().join("debug.txt");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{msg}");
    }
    println!("[launcher] {msg}");
}

fn resource_dir(app: &AppHandle) -> PathBuf {
    #[cfg(not(debug_assertions))]
    {
        app.path().resource_dir().expect("Failed to get resource dir")
    }
    #[cfg(debug_assertions)]
    {
        let _ = app;
        std::env::current_dir().expect("Failed to get current dir")
    }
}

fn normalize_path(path: &PathBuf) -> PathBuf {
    let s = path.to_string_lossy();
    let stripped = s.trim_start_matches("\\\\?\\");
    PathBuf::from(stripped)
}

fn is_position_visible(x: i64, y: i64, app: &AppHandle) -> bool {
    if let Ok(monitors) = app.available_monitors() {
        for monitor in monitors {
            let pos  = monitor.position();
            let size = monitor.size();
            let mx = pos.x as i64;
            let my = pos.y as i64;
            let mw = size.width as i64;
            let mh = size.height as i64;
            if x >= mx && x < mx + mw && y >= my && y < my + mh {
                return true;
            }
        }
    }
    false
}

#[tauri::command]
fn frontend_log(message: String) {
    let path = logs_dir().join("frontend.txt");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{message}");
    }
}

// Kills leftover macro backends: python processes running backend.py/.pyc. Matching on the script keeps
// unrelated Python work (notebooks, other tools) alive, which the old "every python.exe" sweep did not
fn kill_backend_processes() -> u32 {
    let own_pid = std::process::id();
    let mut sys = System::new();
    sys.refresh_processes_specifics(ProcessRefreshKind::new().with_cmd(UpdateKind::Always));

    let mut killed = 0;
    for (pid, process) in sys.processes() {
        if pid.as_u32() == own_pid || !process.name().to_lowercase().starts_with("python") {
            continue;
        }
        // The launcher always passes --pid, which keeps some other project's backend.py out of this
        let cmd = process.cmd();
        let is_backend = cmd.iter().any(|arg| arg == "--pid")
            && cmd.iter().any(|arg| {
                let arg = arg.to_lowercase();
                arg.ends_with("backend.pyc") || arg.ends_with("backend.py")
            });
        if is_backend && process.kill() {
            log(&format!("Killed backend {} (PID {})", process.name(), pid.as_u32()));
            killed += 1;
        }
    }

    if killed > 0 {
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    killed
}

#[cfg_attr(debug_assertions, allow(dead_code))]
fn spawn_backend_process(app: &AppHandle, launcher_pid: u32) -> Result<Child, String> {
    let res_dir = normalize_path(&resource_dir(app));
    let python_exe = res_dir.join("Python314").join("pythonw.exe");
    let script = res_dir.join("backend.pyc");

    log(&format!("Python exe: {:?} (exists: {})", python_exe, python_exe.exists()));
    log(&format!("Script:     {:?} (exists: {})", script, script.exists()));

    if !python_exe.exists() {
        return Err(format!("Python executable not found at {python_exe:?}"));
    }
    if !script.exists() {
        return Err(format!("Backend script not found at {script:?}"));
    }

    let prod_logs = res_dir.join("logs");
    let _ = fs::create_dir_all(&prod_logs);
    let stdout_file = fs::File::create(prod_logs.join("backend_stdout.txt")).map_err(|e| e.to_string())?;
    let stderr_file = fs::File::create(prod_logs.join("backend_stderr.txt")).map_err(|e| e.to_string())?;

    Command::new(&python_exe)
        .arg(&script)
        .arg("--pid")
        .arg(launcher_pid.to_string())
        .current_dir(&res_dir)
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|e| format!("Failed to spawn backend: {e}"))
}

// Dev builds prefer the project's .venv so the backend never runs under a Python missing its packages
#[cfg(debug_assertions)]
fn dev_python() -> PathBuf {
    let venv = if cfg!(target_os = "windows") { "../.venv/Scripts/python.exe" } else { "../.venv/bin/python" };
    let venv = PathBuf::from(venv);
    if venv.exists() {
        return venv;
    }
    log("dev_python: .venv not found, falling back to python on PATH");
    PathBuf::from(if cfg!(target_os = "windows") { "python" } else { "python3" })
}

// Dev runs the source directly, so an edit to backend.py only needs an app restart, not a recompile
#[cfg(debug_assertions)]
fn dev_backend_script() -> &'static str {
    if PathBuf::from("backend.py").exists() { "backend.py" } else { "backend.pyc" }
}

fn read_backend_port(res_dir: &PathBuf, pid: u32) -> u16 {
    let res_dir = normalize_path(res_dir);
    let port_file = res_dir.join(format!("port_{pid}.json"));
    log(&format!("Looking for port file: {port_file:?}"));

    for attempt in 1..=100 {
        if let Ok(content) = fs::read_to_string(&port_file) {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&content) {
                if let Some(port) = val.get("port").and_then(|p| p.as_u64()) {
                    log(&format!("Found port {port} on attempt {attempt}"));
                    return port as u16;
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }

    log("WARNING: Could not read port file, falling back to 8765");
    8765
}

// Localhost calls use ureq: reqwest::blocking spins up a runtime thread per client,
// and that thread overflowed its stack when the macro started
fn local_agent() -> &'static ureq::Agent {
    static AGENT: std::sync::OnceLock<ureq::Agent> = std::sync::OnceLock::new();
    AGENT.get_or_init(|| {
        ureq::AgentBuilder::new()
            .timeout(std::time::Duration::from_secs(3))
            .build()
    })
}

fn fetch_live_state(port: u16) -> Option<serde_json::Value> {
    local_agent()
        .get(&format!("http://127.0.0.1:{port}/state?live=1"))
        .call()
        .ok()?
        .into_json()
        .ok()
}

fn wait_for_backend(port: u16) -> bool {
    log(&format!("Waiting for backend on port {port}..."));
    for i in 0..60 {
        if local_agent().get(&format!("http://127.0.0.1:{port}/health")).call().is_ok() {
            log(&format!("Backend ready after {} attempts", i + 1));
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }
    log("Backend failed to start in time");
    false
}

fn store_child(app: &AppHandle, child: Child) {
    if let Some(proc) = app.try_state::<PythonProcess>() {
        if let Ok(mut guard) = proc.0.lock() {
            *guard = Some(child);
        }
    }
}

fn publish_backend_port(app: &AppHandle, port: u16) {
    if let Some(p) = app.try_state::<BackendPort>() {
        if let Ok(mut g) = p.0.lock() {
            *g = port;
        }
    }

    let payload = serde_json::json!({ "port": port });
    for label in &["fish", "stats"] {
        if let Some(win) = app.get_webview_window(label) {
            if let Err(e) = win.emit("backend-port-ready", &payload) {
                log(&format!("Failed to emit backend-port-ready to {label}: {e}"));
            }
            let _ = win.eval(&format!("window.__BACKEND_PORT__ = {port};"));
        }
    }
}

fn terminate_child(app: &AppHandle) {
    if let Some(proc) = app.try_state::<PythonProcess>() {
        if let Ok(mut guard) = proc.0.lock() {
            if let Some(mut child) = guard.take() {
                log(&format!("Terminating child PID: {}", child.id()));
                #[cfg(target_os = "windows")]
                {
                    let pid = child.id();
                    let _ = Command::new("taskkill").args(["/F", "/T", "/PID", &pid.to_string()]).output();
                }
                #[cfg(not(target_os = "windows"))]
                {
                    let _ = child.kill();
                }
                let start = std::time::Instant::now();
                loop {
                    match child.try_wait() {
                        Ok(Some(status)) => { log(&format!("Child exited with: {status}")); break; }
                        Ok(None) => {
                            if start.elapsed().as_secs() > 5 {
                                log("Child did not exit in 5s, forcing kill");
                                let _ = child.kill();
                                let _ = child.wait();
                                break;
                            }
                            std::thread::sleep(std::time::Duration::from_millis(100));
                        }
                        Err(e) => { log(&format!("Error waiting for child: {e}")); break; }
                    }
                }
            }
        }
    }
}

fn full_shutdown(app: &AppHandle, launcher_pid: u32, res_dir: &PathBuf) {
    terminate_child(app);
    kill_backend_processes();
    let port_file = normalize_path(res_dir).join(format!("port_{launcher_pid}.json"));
    let _ = fs::remove_file(&port_file);
}

fn restore_window(app: &AppHandle, win: &tauri::WebviewWindow, store_name: &'static str) {
    let store = tauri_plugin_store::StoreBuilder::new(app, store_name)
        .build()
        .expect("Failed to build store");

    let mut restored = false;

    if let (Some(w), Some(h), Some(x), Some(y)) = (
        store.get("w").and_then(|v: serde_json::Value| v.as_f64()),
        store.get("h").and_then(|v: serde_json::Value| v.as_f64()),
        store.get("x").and_then(|v: serde_json::Value| v.as_i64()),
        store.get("y").and_then(|v: serde_json::Value| v.as_i64()),
    ) {
        if is_position_visible(x, y, app) {
            let _ = win.set_size(tauri::LogicalSize::new(w.max(400.0), h.max(300.0)));
            let _ = win.set_position(tauri::LogicalPosition::new(x as f64, y as f64));
            restored = true;
        } else {
            log("main window: saved position off-screen, using default");
        }
    }

    if !restored {
        if let Ok(Some(monitor)) = win.current_monitor() {
            let scale  = monitor.scale_factor();
            let msize  = monitor.size();
            let width  = (msize.width  as f64 / scale * 0.365) as u32;
            let height = (msize.height as f64 / scale * 0.475) as u32;
            let _ = win.set_size(tauri::LogicalSize::new(width, height));
            let _ = win.center();
        }
    }

    let win_clone = win.clone();
    win.on_window_event(move |event| {
        if let tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Moved(_) = event {
            if let (Ok(size), Ok(pos)) = (win_clone.inner_size(), win_clone.outer_position()) {
                if let Ok(Some(monitor)) = win_clone.current_monitor() {
                    let scale = monitor.scale_factor();
                    if let Ok(store) = tauri_plugin_store::StoreBuilder::new(win_clone.app_handle(), store_name).build() {
                        store.set("w", serde_json::json!(size.width as f64 / scale));
                        store.set("h", serde_json::json!(size.height as f64 / scale));
                        store.set("x", serde_json::json!(pos.x));
                        store.set("y", serde_json::json!(pos.y));
                        let _ = store.save();
                    }
                }
            }
        }
    });
}

fn setup_stats_window(app: &AppHandle) {
    let Some(stats_win) = app.get_webview_window("stats") else { return };

    let store = tauri_plugin_store::StoreBuilder::new(app, "stats_position.json")
        .build()
        .expect("Failed to build store");

    if let Ok(Some(monitor)) = stats_win.current_monitor() {
        let scale  = monitor.scale_factor();
        let msize  = monitor.size();
        let default_width  = (msize.width  as f64 / scale * 0.125) as u32;
        let default_height = (msize.height as f64 / scale * 0.16) as u32;
        let saved_w = store.get("w").and_then(|v: serde_json::Value| v.as_f64());
        let saved_h = store.get("h").and_then(|v: serde_json::Value| v.as_f64());
        let (width, height) = match (saved_w, saved_h) {
            (Some(w), Some(h)) => (w.max(100.0) as u32, h.max(50.0) as u32),
            _ => (default_width, default_height),
        };
        let _ = stats_win.set_size(tauri::LogicalSize::new(width, height));
        let saved_x = store.get("x").and_then(|v: serde_json::Value| v.as_i64());
        let saved_y = store.get("y").and_then(|v: serde_json::Value| v.as_i64());
        match (saved_x, saved_y) {
            (Some(x), Some(y)) if is_position_visible(x, y, app) => {
                let _ = stats_win.set_position(PhysicalPosition::new(x as i32, y as i32));
            }
            _ => {
                // Top-left by default: GPO's catch notices (which fruit detection reads) sit top-centre
                let _ = stats_win.set_position(PhysicalPosition::new(20, (msize.height as f64 * 0.25) as i32));
            }
        }
    }

    let stats_win_clone = stats_win.clone();
    stats_win.on_window_event(move |event| {
        match event {
            tauri::WindowEvent::Moved(pos) => {
                if let Ok(store) = tauri_plugin_store::StoreBuilder::new(stats_win_clone.app_handle(), "stats_position.json").build() {
                    store.set("x", serde_json::json!(pos.x));
                    store.set("y", serde_json::json!(pos.y));
                    let _ = store.save();
                }
            }
            tauri::WindowEvent::Resized(size) => {
                if let Ok(Some(monitor)) = stats_win_clone.current_monitor() {
                    let scale = monitor.scale_factor();
                    if let Ok(store) = tauri_plugin_store::StoreBuilder::new(stats_win_clone.app_handle(), "stats_position.json").build() {
                        store.set("w", serde_json::json!(size.width as f64 / scale));
                        store.set("h", serde_json::json!(size.height as f64 / scale));
                        let _ = store.save();
                    }
                }
            }
            _ => {}
        }
    });
}

// One watcher for both window behaviours that follow backend settings: the main window's always-on-top and the
// overlay, which is only shown while the macro runs. Windows are only touched when a value actually changes
fn start_window_watcher(app: &AppHandle, backend_port: u16) {
    static STARTED: AtomicBool = AtomicBool::new(false);
    if STARTED.swap(true, Ordering::SeqCst) {
        return;
    }

    let app = app.clone();
    std::thread::spawn(move || {
        let mut last_on_top: Option<bool> = None;
        let mut last_show_overlay: Option<bool> = None;
        loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
            let Some(state) = fetch_live_state(backend_port) else { continue };

            let on_top = state.get("alwaysOnTop").and_then(|v| v.as_bool()).unwrap_or(false);
            if last_on_top != Some(on_top) {
                if let Some(win) = app.get_webview_window("fish") {
                    let _ = win.set_always_on_top(on_top);
                }
                last_on_top = Some(on_top);
            }

            let enabled = state.get("showDebugOverlay").and_then(|v| v.as_bool()).unwrap_or(false);
            let running = state.get("isRunning").and_then(|v| v.as_bool()).unwrap_or(false);
            let show_overlay = enabled && running;
            if last_show_overlay != Some(show_overlay) {
                if let Some(stats) = app.get_webview_window("stats") {
                    let _ = if show_overlay { stats.show() } else { stats.hide() };
                }
                last_show_overlay = Some(show_overlay);
            }
        }
    });
}

#[tauri::command]
fn get_backend_port(app: AppHandle) -> u16 {
    app.try_state::<BackendPort>()
        .and_then(|p| p.0.lock().ok().map(|g| *g))
        .unwrap_or(8765)
}

#[tauri::command]
fn get_system_info() -> serde_json::Value {
    let mut sys = System::new();
    sys.refresh_memory();
    let available_gb = sys.available_memory() as f64 / 1_073_741_824.0;
    serde_json::json!({ "ram_available_gb": (available_gb * 10.0).round() / 10.0 })
}

#[tauri::command]
fn kill_conflicting_processes() -> serde_json::Value {
    let killed = kill_backend_processes();
    log(&format!("kill_conflicting_processes: terminated {killed} process(es)"));
    serde_json::json!({ "killed": killed })
}

#[tauri::command]
fn launch_macro(app: AppHandle, macro_name: String) -> Result<serde_json::Value, String> {
    if macro_name != "fishing" {
        return Err(format!("Unknown macro '{macro_name}'"));
    }

    let launcher_pid = std::process::id();
    let res_dir = resource_dir(&app);

    kill_backend_processes();

    #[cfg(debug_assertions)]
    {
        let script = dev_backend_script();
        let child = Command::new(dev_python())
            .arg(script)
            .arg("--pid")
            .arg(launcher_pid.to_string())
            .spawn()
            .map_err(|e| format!("Failed to start debug backend ({script}): {e}"))?;
        log(&format!("DEBUG: {script} spawned"));
        store_child(&app, child);
    }

    #[cfg(not(debug_assertions))]
    {
        let child = spawn_backend_process(&app, launcher_pid)?;
        store_child(&app, child);
    }

    let port = read_backend_port(&res_dir, launcher_pid);
    if !wait_for_backend(port) {
        return Err("Backend did not start in time".to_string());
    }

    publish_backend_port(&app, port);

    if let Some(hub) = app.get_webview_window("hub") {
        let _ = hub.hide();
    }
    if let Some(win) = app.get_webview_window("fish") {
        win.show().map_err(|e| e.to_string())?;
        win.set_focus().map_err(|e| e.to_string())?;
        restore_window(&app, &win, "main_window.json");
        setup_stats_window(&app);
        start_window_watcher(&app, port);
    }

    log(&format!("launch_macro: done, port {port}"));
    Ok(serde_json::json!({ "port": port }))
}

#[tauri::command]
fn open_main_window(app: AppHandle) -> Result<(), String> {
    if let Some(hub) = app.get_webview_window("hub") {
        hub.show().map_err(|e| e.to_string())?;
        hub.set_focus().map_err(|e| e.to_string())?;
    }
    if let Some(launcher) = app.get_webview_window("launcher") {
        let _ = launcher.close();
    }
    Ok(())
}

#[tauri::command]
fn open_browser(url: String) -> Result<(), String> {
    // Only web links: this goes through the shell, so anything else could launch a local program
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("Only http(s) links can be opened".to_string());
    }
    #[cfg(target_os = "windows")]
    Command::new("cmd").args(["/C", "start", "", &url]).spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    Command::new("open").arg(&url).spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "linux")]
    Command::new("xdg-open").arg(&url).spawn().map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    let _ = fs::remove_file(logs_dir().join("debug.txt"));

    let launcher_pid = std::process::id();
    log(&format!("Launcher PID: {launcher_pid}"));

    tauri::Builder::default()
        .manage(PythonProcess(Mutex::new(None)))
        .manage(BackendPort(Mutex::new(0_u16)))
        .setup(move |app| {
            if let Some(win) = app.get_webview_window("launcher") {
                let _ = win.show();
                let _ = win.set_focus();
            }
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let label = window.label();
                if label == "fish" || label == "hub" {
                    api.prevent_close();
                    let app_handle = window.app_handle().clone();
                    let res_dir = resource_dir(&app_handle);
                    std::thread::spawn(move || {
                        full_shutdown(&app_handle, launcher_pid, &res_dir);
                        log("Exiting application");
                        app_handle.exit(0);
                    });
                }
            }
        })
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::default().build())
        .invoke_handler(tauri::generate_handler![
            get_backend_port,
            frontend_log,
            kill_conflicting_processes,
            launch_macro,
            open_main_window,
            open_browser,
            get_system_info,
        ])
        .run(tauri::generate_context!())
        .expect("Error running Tauri application");
}
