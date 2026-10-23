// HardlyWatching desktop shell.
//
// This binary is a *window onto a URL*, deliberately not a copy of the UI. The
// web UI (index.html / shorts.html) ships inside the Python server, so the shell
// loads http://host:port from whichever server it is pointed at. One copy of the
// frontend, and the app can never drift from the server's UI.
//
// Server URL resolution order:
//   1. environment variable HW_SERVER_URL
//   2. key "serverUrl" in app-config.json inside the Tauri app config dir
//      (Linux: ~/.config/ing.hardlysleep.hardlywatching/app-config.json)
//   3. http://127.0.0.1:8765
//
// Resolution runs in main() *before* the Tauri/GTK event loop is created, so the
// resolved URL is printed and verifiable with no display at all. With a display
// the window then loads that URL. If the target is not listening when the window
// opens, the window title says so and a background retry loads the UI as soon as
// the server answers -- no silent blank window.

use std::fs;
use std::io::Write;
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Url, WebviewUrl, WebviewWindowBuilder};

const APP_IDENTIFIER: &str = "ing.hardlysleep.hardlywatching";
const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8765";
const CONFIG_FILE_NAME: &str = "app-config.json";
const WINDOW_LABEL: &str = "main";
const WINDOW_TITLE: &str = "HardlyWatching";
const WINDOW_WIDTH: f64 = 1280.0;
const WINDOW_HEIGHT: f64 = 860.0;
const WINDOW_MIN_WIDTH: f64 = 380.0;
const WINDOW_MIN_HEIGHT: f64 = 520.0;

/// Where the URL came from, for logging.
enum Source {
    Env,
    Config(PathBuf),
    Default,
}

impl Source {
    fn describe(&self) -> String {
        match self {
            Source::Env => "env HW_SERVER_URL".to_string(),
            Source::Config(path) => format!("config {}", path.display()),
            Source::Default => format!("default (no HW_SERVER_URL, no usable {CONFIG_FILE_NAME})"),
        }
    }
}

/// Normalise a user-supplied server string into something WebKit will load.
/// Accepts "127.0.0.1:8899" as well as a full URL, and never keeps a trailing slash.
fn normalise_url(raw: &str) -> Option<Url> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let with_scheme = if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        trimmed.to_string()
    } else {
        format!("http://{trimmed}")
    };
    Url::parse(with_scheme.trim_end_matches('/')).ok()
}

/// The Tauri app config dir, computed without an AppHandle so it is available
/// before the event loop exists. On Linux this is exactly what Tauri computes:
/// `$XDG_CONFIG_HOME/<identifier>` (default `~/.config/<identifier>`).
fn app_config_dir() -> Option<PathBuf> {
    if cfg!(target_os = "windows") {
        std::env::var_os("APPDATA").map(|base| PathBuf::from(base).join(APP_IDENTIFIER))
    } else if cfg!(target_os = "macos") {
        std::env::var_os("HOME").map(|home| {
            PathBuf::from(home)
                .join("Library")
                .join("Application Support")
                .join(APP_IDENTIFIER)
        })
    } else {
        std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
            .map(|base| base.join(APP_IDENTIFIER))
    }
}

/// serverUrl from app-config.json in the app config dir, if present and sane.
fn config_server_url() -> Option<(Url, PathBuf)> {
    let path = app_config_dir()?.join(CONFIG_FILE_NAME);
    let raw = fs::read_to_string(&path).ok()?;
    let parsed: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let value = parsed.get("serverUrl")?.as_str()?;
    normalise_url(value).map(|url| (url, path))
}

fn resolve_server_url() -> (Url, Source) {
    if let Some(url) = std::env::var("HW_SERVER_URL")
        .ok()
        .and_then(|v| normalise_url(&v))
    {
        return (url, Source::Env);
    }
    if let Some((url, path)) = config_server_url() {
        return (url, Source::Config(path));
    }
    (
        Url::parse(DEFAULT_SERVER_URL).expect("default server URL is valid"),
        Source::Default,
    )
}

/// Cheap reachability check: can we open a TCP connection to the URL's host/port?
/// Deliberately not an HTTP client -- the shell only needs "is anything listening",
/// and this costs no extra dependency. Nothing is sent to the server.
fn probe(url: &Url) -> Result<(), String> {
    let host = url
        .host_str()
        .ok_or_else(|| "URL has no host".to_string())?;
    let port = url
        .port_or_known_default()
        .ok_or_else(|| "URL has no port and no default".to_string())?;
    let addrs = (host, port)
        .to_socket_addrs()
        .map_err(|e| format!("cannot resolve {host}:{port}: {e}"))?;
    let mut last_error = format!("no addresses for {host}:{port}");
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, Duration::from_millis(900)) {
            Ok(_) => return Ok(()),
            Err(e) => last_error = format!("{addr}: {e}"),
        }
    }
    Err(last_error)
}

fn has_display() -> bool {
    ["DISPLAY", "WAYLAND_DISPLAY"]
        .iter()
        .any(|key| std::env::var(key).map(|v| !v.trim().is_empty()).unwrap_or(false))
}

fn flush_stdout() {
    let _ = std::io::stdout().flush();
}

/// While the server is unreachable, keep probing; load the real UI as soon as it
/// answers and put the plain title back. This is what makes opening the shell
/// before (or alongside) the backend survivable instead of a dead window.
fn retry_until_reachable(app: AppHandle, window: tauri::WebviewWindow, url: Url) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(3));
        if probe(&url).is_ok() {
            let window_for_main = window.clone();
            let url_for_main = url.clone();
            let url_for_log = url.clone();
            let _ = app.run_on_main_thread(move || match window_for_main.navigate(url_for_main) {
                Ok(()) => {
                    let _ = window_for_main.set_title(WINDOW_TITLE);
                    println!("hardlywatching: server came up, loaded {url_for_log}");
                    let _ = std::io::stdout().flush();
                }
                Err(e) => eprintln!("hardlywatching: retry navigation failed: {e}"),
            });
            return;
        }
    });
}

fn main() {
    // Resolve and report the URL first: this is the part that is verifiable
    // headlessly, and it must not depend on a working display. (Tauri creates the
    // GTK event loop inside run(), which is why this cannot live in setup().)
    let (url, source) = resolve_server_url();
    println!("hardlywatching: server url = {url}");
    println!("hardlywatching: url source = {}", source.describe());

    let reachable = probe(&url);
    match &reachable {
        Ok(()) => println!("hardlywatching: {url} is listening"),
        Err(why) => eprintln!(
            "hardlywatching: WARNING cannot reach {url} ({why}). \
             The window will say so and retry every 3s."
        ),
    }
    flush_stdout();

    if !has_display() {
        println!(
            "hardlywatching: no DISPLAY/WAYLAND_DISPLAY -- not opening a window. Would load {url}"
        );
        flush_stdout();
        return; // exit 0: resolution verified, nothing to draw
    }

    let url_for_window = url.clone();
    let reachable_ok = reachable.is_ok();
    tauri::Builder::default()
        .setup(move |app| {
            let mut builder = WebviewWindowBuilder::new(
                app,
                WINDOW_LABEL,
                WebviewUrl::External(url_for_window.clone()),
            )
            .title(WINDOW_TITLE)
            .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
            .min_inner_size(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
            .resizable(true);
            if !reachable_ok {
                builder = builder.title(&format!(
                    "{WINDOW_TITLE} — server unreachable at {url_for_window}"
                ));
            }

            let window = match builder.build() {
                Ok(window) => window,
                Err(e) => {
                    eprintln!("hardlywatching: FATAL could not create the window: {e}");
                    eprintln!("hardlywatching: resolved server url was {url_for_window}");
                    std::process::exit(1);
                }
            };
            println!(
                "hardlywatching: window '{WINDOW_LABEL}' {}x{} (min {}x{}) loading {url_for_window}",
                WINDOW_WIDTH as u32,
                WINDOW_HEIGHT as u32,
                WINDOW_MIN_WIDTH as u32,
                WINDOW_MIN_HEIGHT as u32
            );
            flush_stdout();

            if !reachable_ok {
                retry_until_reachable(app.handle().clone(), window, url_for_window.clone());
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the HardlyWatching shell");
}
