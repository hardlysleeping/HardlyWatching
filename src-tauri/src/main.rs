// HardlyWatching desktop shell.
//
// This binary is a *window onto a URL*, deliberately not a copy of the UI. The
// web UI (index.html / shorts.html) ships inside the Python server, so the shell
// loads http://host:port from whichever server it is pointed at. One copy of the
// frontend, and the app can never drift from the server's UI.
//
// Since M2-WIRE the Python server also ships inside the app: a frozen
// PyInstaller *onedir* tree (`hardlywatching-server` + `_internal/` + `bin/`)
// installed at `<prefix>/lib/HardlyWatching/sidecar` in the .deb and inside the
// AppDir at the same path, started only when nothing else answers.
// The user needs no Python, no venv and no separate `hardlywatching.service`.
//
// Which server the window points at, in order:
//   1. environment variable HW_SERVER_URL             -> attach, never spawn
//   2. key "serverUrl" in app-config.json             -> attach, never spawn
//      (Linux: ~/.config/ing.hardlysleep.hardlywatching/app-config.json)
//   3. the bundled sidecar's own `--attach`           -> attach, never killed
//      (finds the instance that owns the state-file lock, the same instance
//      `hardlywatching.service` / a terminal `server.py` is running)
//   4. a server already answering /api/health on the default URL (8765, or
//      $INSTA_PORT)                                    -> attach, never killed
//   5. otherwise: spawn the bundled sidecar with --no-browser, wait for
//      /api/health, load it, and stop it again on exit (only this child).
//
// 1 and 2 mean "the user pointed this shell somewhere": the shell never starts
// a second server behind their back. 3 and 4 are what make the desktop app a
// peer of the systemd unit instead of a rival: the app attaches to the running
// service, it does not start a duplicate, and it never stops a server it did
// not start.
//
// Resolution runs in main() *before* the Tauri/GTK event loop is created, so the
// resolved URL is printed and verifiable with no display at all. With a display
// the window then loads that URL. If an attached target is not listening when
// the window opens, the window title says so and a background retry loads the
// UI as soon as the server answers -- no silent blank window. If the bundled
// server cannot be found or started, the window shows a readable error page
// instead of a blank frame, and a headless run exits non-zero.

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Url, WebviewUrl, WebviewWindowBuilder};

const APP_IDENTIFIER: &str = "ing.hardlysleep.hardlywatching";
const PRODUCT_NAME: &str = "HardlyWatching";
const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8765";
const CONFIG_FILE_NAME: &str = "app-config.json";
const WINDOW_LABEL: &str = "main";
const WINDOW_TITLE: &str = "HardlyWatching";
const WINDOW_WIDTH: f64 = 1280.0;
const WINDOW_HEIGHT: f64 = 860.0;
const WINDOW_MIN_WIDTH: f64 = 380.0;
const WINDOW_MIN_HEIGHT: f64 = 520.0;

/// The frozen server, as laid out by packaging/sidecar/build.sh.
#[cfg(windows)]
const SIDECAR_BIN: &str = "hardlywatching-server.exe";
#[cfg(not(windows))]
const SIDECAR_BIN: &str = "hardlywatching-server";
const SIDECAR_DIR: &str = "sidecar";
/// Escape hatch for development: point the shell at a hand-built
/// dist/sidecar/hardlywatching-server without bundling anything.
const SIDECAR_ENV: &str = "HWA_SIDECAR";

const ATTACH_TIMEOUT: Duration = Duration::from_secs(15);
const START_TIMEOUT: Duration = Duration::from_secs(45);
const STOP_GRACE: Duration = Duration::from_secs(6);

/// Where the URL came from, for logging.
enum Source {
    Env,
    Config(PathBuf),
    /// The bundled server told us where the running instance is (`--attach`).
    SidecarAttach(PathBuf),
    /// Something was already answering /api/health on the default URL.
    AlreadyRunning,
    /// We started it ourselves.
    Spawned(PathBuf),
}

impl Source {
    fn describe(&self) -> String {
        match self {
            Source::Env => "env HW_SERVER_URL".to_string(),
            Source::Config(path) => format!("config {}", path.display()),
            Source::SidecarAttach(path) => {
                format!("bundled server --attach ({})", path.display())
            }
            Source::AlreadyRunning => "already running (nothing was started)".to_string(),
            Source::Spawned(path) => format!("bundled server spawned ({})", path.display()),
        }
    }
}

/// What main() decided to do about a server.
enum Plan {
    /// Point the window at this URL. Nothing was started, so nothing is stopped.
    Attach { url: Url, source: Source },
    /// We started this child; it is ours to stop on exit.
    Spawn {
        url: Url,
        sidecar: PathBuf,
        child: OwnedChild,
    },
    /// Headless only: this is what we *would* have spawned.
    WouldSpawn { url: Url, sidecar: PathBuf },
    /// Nothing usable, and it is not a matter of waiting: say so in a window.
    Fail { message: String },
}

/// A server process this app started, and the only one it may terminate.
struct OwnedChild {
    path: PathBuf,
    child: Child,
    healthy: bool,
    stopped: bool,
}

impl OwnedChild {
    fn pid(&self) -> u32 {
        self.child.id()
    }

    /// SIGTERM (so server.py removes its lock), wait, then SIGKILL.
    /// Only ever called for a child this process spawned.
    fn stop(&mut self) {
        if self.stopped {
            return;
        }
        self.stopped = true;
        let pid = self.pid();
        if self.healthy {
            println!(
                "hardlywatching: stopping the server we started ({} pid {pid}, SIGTERM)",
                self.path.display()
            );
            send_signal(pid, SIGTERM);
            if wait_for_exit(&mut self.child, STOP_GRACE).is_none() {
                eprintln!("hardlywatching: pid {pid} ignored SIGTERM for {}s, killing it", STOP_GRACE.as_secs());
                send_signal(pid, SIGKILL);
                let _ = wait_for_exit(&mut self.child, Duration::from_secs(3));
            }
        } else {
            // Never answered /api/health: it must still be reaped, not leaked.
            eprintln!("hardlywatching: server pid {pid} never became healthy, killing it");
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
        match self.child.try_wait() {
            Ok(Some(status)) => println!("hardlywatching: server pid {pid} exited ({status})"),
            Ok(None) => eprintln!("hardlywatching: WARNING server pid {pid} is still running"),
            Err(e) => eprintln!("hardlywatching: could not wait for pid {pid}: {e}"),
        }
        flush_stdout();
    }
}

impl Drop for OwnedChild {
    fn drop(&mut self) {
        // Backstop for paths that skip the explicit shutdown (early return, an
        // unwinding panic in main). SIGTERM keeps server.py's lock cleanup.
        if !self.stopped {
            let pid = self.pid();
            eprintln!("hardlywatching: removing leftover server pid {pid} on drop");
            send_signal(pid, SIGTERM);
            if wait_for_exit(&mut self.child, Duration::from_secs(2)).is_none() {
                let _ = self.child.kill();
                let _ = self.child.wait();
            }
            self.stopped = true;
        }
    }
}

// ---------------------------------------------------------------------------
// Tiny process helpers (no extra dependency beyond libc for the signals)
// ---------------------------------------------------------------------------

#[cfg(unix)]
const SIGTERM: i32 = 15;
#[cfg(unix)]
const SIGKILL: i32 = 9;

#[cfg(unix)]
fn send_signal(pid: u32, sig: i32) {
    unsafe {
        libc::kill(pid as libc::pid_t, sig);
    }
}

#[cfg(not(unix))]
fn send_signal(_pid: u32, _sig: i32) {}
/// Poll until the child exits or the deadline passes. Returns None on timeout.
fn wait_for_exit(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) => {}
            Err(_) => return None,
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(Duration::from_millis(50));
    }
}

/// Run a command, capturing stdout/stderr, hard-bounded by `timeout`.
/// Returns (exit code, stdout, stderr). Used for `--attach`, which must never
/// hang the launch if a stale lock points at a wedged process.
fn run_capture(mut cmd: Command, timeout: Duration) -> Result<(i32, String, String), String> {
    let mut child = cmd.spawn().map_err(|e| format!("could not run: {e}"))?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let out_reader = stdout.map(|mut s| {
        thread::spawn(move || {
            let mut text = String::new();
            let _ = s.read_to_string(&mut text);
            text
        })
    });
    let err_reader = stderr.map(|mut s| {
        thread::spawn(move || {
            let mut text = String::new();
            let _ = s.read_to_string(&mut text);
            text
        })
    });

    let status = match wait_for_exit(&mut child, timeout) {
        Some(status) => Some(status),
        None => {
            let _ = child.kill();
            let _ = child.wait();
            None
        }
    };

    let out = out_reader.and_then(|h| h.join().ok()).unwrap_or_default();
    let err = err_reader.and_then(|h| h.join().ok()).unwrap_or_default();
    match status {
        Some(status) => Ok((status.code().unwrap_or(-1), out, err)),
        None => Err(format!("timed out after {}s", timeout.as_secs())),
    }
}

// ---------------------------------------------------------------------------
// URL handling
// ---------------------------------------------------------------------------

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

/// The first http(s) URL in a line of output, if any.
fn url_in_line(line: &str) -> Option<Url> {
    let start = line.find("http://").or_else(|| line.find("https://"))?;
    let rest = &line[start..];
    let end = rest
        .find(|c: char| c.is_whitespace() || c == '"' || c == '\'' || c == ')')
        .unwrap_or(rest.len());
    normalise_url(&rest[..end])
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

/// The URL a plain `hardlywatching` start would use: 8765 unless INSTA_PORT /
/// INSTA_HOST say otherwise (the same two variables server.py reads).
fn default_server_url() -> Url {
    let host = std::env::var("INSTA_HOST")
        .ok()
        .map(|h| h.trim().to_string())
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "127.0.0.1".to_string());
    // 0.0.0.0 / :: are bind addresses, not destinations: the app talks to loopback.
    let host = if host == "0.0.0.0" || host == "::" {
        "127.0.0.1".to_string()
    } else {
        host
    };
    let port = std::env::var("INSTA_PORT")
        .ok()
        .and_then(|p| p.trim().parse::<u16>().ok())
        .unwrap_or(8765);
    match Url::parse(&format!("http://{host}:{port}")) {
        Ok(url) => url,
        Err(_) => Url::parse(DEFAULT_SERVER_URL).expect("default server URL is valid"),
    }
}

/// Cheap reachability check: can we open a TCP connection to the URL's host/port?
/// Deliberately not an HTTP client -- this costs no extra dependency and sends
/// nothing to the server.
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

/// GET /api/health and require a 200. Raw HTTP over a socket on purpose: no
/// client dependency, and no chance of an http_proxy answering for loopback.
fn http_health_ok(url: &Url) -> bool {
    let (host, port) = match (url.host_str(), url.port_or_known_default()) {
        (Some(host), Some(port)) => (host.to_string(), port),
        _ => return false,
    };
    let addr = match (host.as_str(), port)
        .to_socket_addrs()
        .ok()
        .and_then(|mut it| it.next())
    {
        Some(addr) => addr,
        None => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(600)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    let request = format!(
        "GET /api/health HTTP/1.0\r\nHost: {host}:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = Vec::new();
    let _ = (&mut stream)
        .take(4096)
        .read_to_end(&mut head);
    let text = String::from_utf8_lossy(&head);
    match text.lines().next() {
        Some(status_line) => status_line.contains(" 200"),
        None => false,
    }
}

// ---------------------------------------------------------------------------
// Finding the bundled sidecar
// ---------------------------------------------------------------------------

/// Candidate `<base>` directories that may hold `sidecar/hardlywatching-server`.
///
/// Tauri installs the packaged server under `<prefix>/lib/<productName>/` (see
/// `bundle.linux.deb.files` and `pack-appimage.sh`), and the app binary under
/// `<prefix>/bin/`. An AppImage is that same layout rooted at `$APPDIR`. A dev
/// run has the tree next to the binary, or points at it with HWA_SIDECAR.
fn sidecar_bases() -> Vec<PathBuf> {
    let mut bases: Vec<PathBuf> = Vec::new();
    let mut push = |p: PathBuf| {
        if !bases.contains(&p) {
            bases.push(p);
        }
    };

    if let Some(exe) = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf)) {
        // dev: target/debug/sidecar/ or target/release/sidecar/
        push(exe.clone());
        // installed: <prefix>/bin/../lib/<productName>/
        if let Some(prefix) = exe.parent() {
            push(prefix.join("lib").join(PRODUCT_NAME));
            push(prefix.join("share").join(PRODUCT_NAME));
            push(prefix.join("lib").join("hardlywatching"));
        }
    }
    if let Some(appdir) = std::env::var_os("APPDIR") {
        // LinuxDE's AppImage is scanned for ELF dependencies on the build machine,
        // and a frozen Python tree's private libraries cannot be resolved there,
        // so the AppImage carries the server under usr/share (see tauri.conf.json).
        push(PathBuf::from(&appdir).join("usr").join("share").join(PRODUCT_NAME));
        push(PathBuf::from(appdir).join("usr").join("lib").join(PRODUCT_NAME));
    }
    push(PathBuf::from("/usr/lib").join(PRODUCT_NAME));
    push(PathBuf::from("/usr/share").join(PRODUCT_NAME));
    push(PathBuf::from("/usr/local/lib").join(PRODUCT_NAME));
    push(PathBuf::from("/usr/lib/hardlywatching"));
    bases
}

/// Every path we would accept as "the bundled server", in priority order.
/// `HWA_SIDECAR` is authoritative when set, so a test (or a user chasing a bug)
/// can say "use this one" or "use none" and get exactly that.
fn sidecar_candidates() -> Vec<PathBuf> {
    if let Some(explicit) = std::env::var_os(SIDECAR_ENV) {
        return vec![PathBuf::from(explicit)];
    }
    let mut candidates = Vec::new();
    for base in sidecar_bases() {
        candidates.push(base.join(SIDECAR_DIR).join(SIDECAR_BIN));
        candidates.push(base.join(SIDECAR_BIN));
    }
    candidates
}

fn find_sidecar() -> Option<PathBuf> {
    sidecar_candidates()
        .into_iter()
        .find(|path| path.is_file())
}

// ---------------------------------------------------------------------------
// Attach: ask the bundled server where the running instance is
// ---------------------------------------------------------------------------

/// `hardlywatching-server --attach`: the backend's own answer to "is this
/// machine already running one, and where?". Exit 0 plus a URL means yes.
/// It starts nothing, so whatever it finds is not ours to stop.
fn sidecar_attach(sidecar: &Path) -> Option<Url> {
    let mut cmd = Command::new(sidecar);
    cmd.arg("--attach")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // The environment is inherited on purpose: INSTA_STATE / INSTA_LOCK decide
    // which instance we are asking about, and the app must ask about the same
    // state file the user's own CLI or service uses.
    match run_capture(cmd, ATTACH_TIMEOUT) {
        Ok((code, out, err)) => {
            let line = out
                .lines()
                .rev()
                .find(|l| l.contains("http://") || l.contains("https://"));
            let url = line.and_then(url_in_line);
            match (code, url) {
                (0, Some(url)) => {
                    println!("hardlywatching: {} --attach says {url}", sidecar.display());
                    Some(url)
                }
                (0, None) => {
                    println!(
                        "hardlywatching: {} --attach reported no running instance ({})",
                        sidecar.display(),
                        out.trim()
                    );
                    None
                }
                (code, _) => {
                    let detail = if err.trim().is_empty() { out.trim() } else { err.trim() };
                    println!(
                        "hardlywatching: {} --attach exited {code}: {}",
                        sidecar.display(),
                        detail
                    );
                    None
                }
            }
        }
        Err(e) => {
            eprintln!(
                "hardlywatching: WARNING could not ask {} for a running instance: {e}",
                sidecar.display()
            );
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Spawn: start our own copy of the bundled server
// ---------------------------------------------------------------------------

/// The port to ask the bundled server for: $INSTA_PORT when set (a sandbox or a
/// service unit), otherwise a free one the kernel hands us. Passing an explicit
/// port is what makes the readiness check below unambiguous.
fn spawn_port() -> Option<u16> {
    if let Some(port) = std::env::var("INSTA_PORT")
        .ok()
        .and_then(|p| p.trim().parse::<u16>().ok())
    {
        if port >= 1024 {
            return Some(port);
        }
    }
    let listener = TcpListener::bind(("127.0.0.1", 0)).ok()?;
    let port = listener.local_addr().ok()?.port();
    drop(listener);
    Some(port)
}

/// Result of waiting for a freshly spawned server to answer /api/health.
struct Ready {
    healthy: bool,
    /// Set when the spawn lost a race and the backend said "already running at
    /// <url>" and exited 0: then there is nothing of ours to stop.
    already: Option<Url>,
    tail: Vec<String>,
}

fn wait_ready(child: &mut Child, url: &Url, lines: &Receiver<String>, timeout: Duration) -> Ready {
    let mut tail: Vec<String> = Vec::new();
    let mut announced: Option<Url> = None;
    let mut already: Option<Url> = None;
    let mut exited: Option<String> = None;
    let deadline = Instant::now() + timeout;

    loop {
        // Drain whatever the server has said so far. This is for the log and for
        // the "already running" case; readiness itself is the health check.
        loop {
            match lines.try_recv() {
                Ok(line) => {
                    if line.contains("already running at") {
                        already = url_in_line(&line).or(already);
                    }
                    if line.contains("is running at") && announced.is_none() {
                        announced = url_in_line(&line);
                    }
                    tail.push(line);
                    if tail.len() > 60 {
                        tail.remove(0);
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }

        // server.py calls find_free_port() itself, so honour a different port.
        if let Some(other) = announced.clone() {
            if other != *url && http_health_ok(&other) {
                return Ready { healthy: true, already: None, tail };
            }
        }
        if http_health_ok(url) {
            return Ready { healthy: true, already: None, tail };
        }
        if exited.is_none() {
            match child.try_wait() {
                Ok(Some(status)) => exited = Some(format!("server exited early ({status})")),
                Ok(None) => {}
                Err(e) => exited = Some(format!("could not wait for the server: {e}")),
            }
        }
        if let Some(why) = exited.clone() {
            // Give the reader thread a moment to flush the last lines.
            thread::sleep(Duration::from_millis(150));
            while let Ok(line) = lines.try_recv() {
                if line.contains("already running at") {
                    already = url_in_line(&line).or(already);
                }
                tail.push(line);
            }
            if let Some(url) = already.clone() {
                return Ready { healthy: false, already: Some(url), tail };
            }
            tail.push(why);
            return Ready { healthy: false, already: None, tail };
        }
        if Instant::now() >= deadline {
            tail.push(format!("no /api/health answer after {}s", timeout.as_secs()));
            return Ready { healthy: false, already: None, tail };
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// Pipe a child's stdout/stderr into this app's log, and onwards to a channel.
fn pump<R: Read + Send + 'static>(stream: R, name: &'static str, tx: mpsc::Sender<String>) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            match line {
                Ok(line) => {
                    println!("hardlywatching[{name}] {line}");
                    let _ = std::io::stdout().flush();
                    if tx.send(line).is_err() {
                        // The launch gave up on us; keep draining so the child
                        // never blocks on a full pipe.
                        continue;
                    }
                }
                Err(_) => break,
            }
        }
    });
}

fn spawn_sidecar(sidecar: &Path) -> Plan {
    let port = match spawn_port() {
        Some(port) => port,
        None => {
            return Plan::Fail {
                message: "could not find a free local port for the bundled server".to_string(),
            }
        }
    };
    let url = match Url::parse(&format!("http://127.0.0.1:{port}")) {
        Ok(url) => url,
        Err(e) => {
            return Plan::Fail {
                message: format!("built an invalid server URL for port {port}: {e}"),
            }
        }
    };

    println!(
        "hardlywatching: spawning bundled server: {} --no-browser --port {port}",
        sidecar.display()
    );
    flush_stdout();

    let mut cmd = Command::new(sidecar);
    cmd.arg("--no-browser")
        .arg("--port")
        .arg(port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // Belt and braces: the flag already prevents a tab, this keeps a frozen
        // Python from block-buffering its log into the pipe.
        .env("INSTA_NO_BROWSER", "1")
        .env("PYTHONUNBUFFERED", "1");

    // Linux: die with the shell even on SIGKILL, so a crashed desktop app can
    // never leave a server holding the state-file lock.
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        let parent = std::process::id() as i32;
        unsafe {
            cmd.pre_exec(move || {
                // PR_SET_PDEATHSIG = 1
                libc::prctl(1, libc::SIGTERM as libc::c_ulong, 0, 0, 0);
                if libc::getppid() != parent {
                    // The shell died between fork and prctl: nothing to outlive.
                    libc::_exit(1);
                }
                Ok(())
            });
        }
    }

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => {
            return Plan::Fail {
                message: format!(
                    "The bundled server could not be started.\n\n{}\n\n{}",
                    sidecar.display(),
                    e
                ),
            }
        }
    };
    let pid = child.id();
    println!("hardlywatching: spawned bundled server pid {pid} (its log follows)");
    flush_stdout();

    let (tx, rx) = mpsc::channel();
    if let Some(out) = child.stdout.take() {
        pump(out, "server", tx.clone());
    }
    if let Some(err) = child.stderr.take() {
        pump(err, "server", tx.clone());
    }

    let ready = wait_ready(&mut child, &url, &rx, START_TIMEOUT);

    if let Some(other) = ready.already {
        // Another instance won the lock between --attach and the spawn. Ours
        // exited cleanly, so there is nothing to stop: attach to theirs.
        let _ = child.wait();
        println!("hardlywatching: bundled server exited, attaching to the running instance at {other}");
        return Plan::Attach { url: other, source: Source::AlreadyRunning };
    }

    if !ready.healthy {
        let _ = child.kill();
        let _ = child.wait();
        let mut message = format!(
            "The bundled server did not come up.\n\n{}\n\n",
            sidecar.display()
        );
        if ready.tail.is_empty() {
            message.push_str("It said nothing at all before the timeout.\n");
        } else {
            message.push_str("Last output:\n\n");
            for line in &ready.tail {
                message.push_str(line);
                message.push('\n');
            }
        }
        return Plan::Fail { message };
    }

    let owned = OwnedChild { path: sidecar.to_path_buf(), child, healthy: true, stopped: false };
    Plan::Spawn {
        url,
        sidecar: sidecar.to_path_buf(),
        child: owned,
    }
}

// ---------------------------------------------------------------------------
// The decision
// ---------------------------------------------------------------------------

fn plan_server(allow_spawn: bool) -> Plan {
    // 1. An explicit target: the user (or a test) pointed this shell somewhere.
    //    Attach, and never start or stop anything of our own.
    if let Some(url) = std::env::var("HW_SERVER_URL")
        .ok()
        .and_then(|v| normalise_url(&v))
    {
        return Plan::Attach { url, source: Source::Env };
    }
    // 2. The same, from app-config.json.
    if let Some((url, path)) = config_server_url() {
        return Plan::Attach {
            url,
            source: Source::Config(path),
        };
    }

    let sidecar = find_sidecar();
    if let Some(path) = &sidecar {
        println!("hardlywatching: bundled server = {}", path.display());
    } else {
        println!(
            "hardlywatching: no bundled server found (looked at {})",
            sidecar_candidates()
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        );
    }

    // 3. Is this machine already running one? The backend owns that answer.
    //    This is the `hardlywatching.service` / terminal-instance case: attach,
    //    and leave it alone.
    if let Some(path) = &sidecar {
        if let Some(url) = sidecar_attach(path) {
            return Plan::Attach {
                url,
                source: Source::SidecarAttach(path.clone()),
            };
        }
    }

    // 4. No lock, but something is answering where a plain start would go. Same
    //    conclusion: use it, do not start a rival.
    let default = default_server_url();
    if http_health_ok(&default) {
        println!("hardlywatching: a server already answers /api/health on {default}");
        return Plan::Attach {
            url: default,
            source: Source::AlreadyRunning,
        };
    }

    // 5. Nothing running: start the bundled copy.
    match sidecar {
        None => Plan::Fail {
            message: format!(
                "No HardlyWatching server is running, and this bundle has no \
                 bundled server to start.\n\nLooked for {SIDECAR_BIN} in:\n{}",
                sidecar_candidates()
                    .iter()
                    .map(|p| format!("  {}", p.display()))
                    .collect::<Vec<_>>()
                    .join("\n")
            ),
        },
        Some(path) if !allow_spawn => Plan::WouldSpawn { url: default, sidecar: path },
        Some(path) => spawn_sidecar(&path),
    }
}

// ---------------------------------------------------------------------------
// Window plumbing
// ---------------------------------------------------------------------------

fn has_display() -> bool {
    ["DISPLAY", "WAYLAND_DISPLAY"]
        .iter()
        .any(|key| std::env::var(key).map(|v| !v.trim().is_empty()).unwrap_or(false))
}

fn flush_stdout() {
    let _ = std::io::stdout().flush();
}

/// While an attached server is unreachable, keep probing; load the real UI as
/// soon as it answers and put the plain title back. This is what makes opening
/// the shell before (or alongside) the backend survivable instead of a dead
/// window. It never applies to a server we spawned: that one was healthy before
/// the window existed.
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

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Write a readable failure page and return it as a file:// URL. A blank frame
/// is not an acceptable way to report a broken install.
fn error_page_url(message: &str) -> Option<Url> {
    let dir = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))
        .unwrap_or_else(std::env::temp_dir)
        .join(APP_IDENTIFIER);
    let _ = fs::create_dir_all(&dir);
    let path = dir.join("startup-error.html");

    let body = format!(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\
<title>{PRODUCT_NAME} — cannot start</title>\n\
<style>body{{margin:0;padding:2.5rem;font:15px/1.6 system-ui,sans-serif;\
background:#12100f;color:#f2ece6}}h1{{font-size:1.25rem;margin:0 0 1rem}}\
pre{{white-space:pre-wrap;word-break:break-word;background:#1e1a17;padding:1rem;\
border-radius:8px;border:1px solid #3a322c;color:#e9c9a8}}p{{color:#b9aaa0}}</style>\
</head><body><h1>{PRODUCT_NAME} could not open its server</h1>\
<pre>{}</pre>\
<p>If HardlyWatching is installed as a service, starting it (or the app again) \
will make this window load the UI. Set HW_SERVER_URL to point the app at a \
specific server.</p></body></html>\n",
        html_escape(message)
    );
    fs::write(&path, body).ok()?;
    let url = Url::from_file_path(&path).ok()?;
    println!("hardlywatching: error page written to {}", path.display());
    Some(url)
}

fn main() {
    // Decide first: this is the part that is verifiable headlessly, and it must
    // not depend on a working display. (Tauri creates the GTK event loop inside
    // run(), which is why this cannot live in setup().)
    let display = has_display();
    let plan = plan_server(display);

    // Whatever the plan, report it in the same shape every time so a log reader
    // (and the docs) can rely on it.
    match plan {
        Plan::Attach { url, source } => {
            report(&url, &source);
            if !display {
                println!(
                    "hardlywatching: no DISPLAY/WAYLAND_DISPLAY -- not opening a window. Would load {url}"
                );
                flush_stdout();
                return;
            }
            let reachable = probe(&url).is_ok();
            run_window(WindowPlan::Attach { url, reachable }, None);
        }
        Plan::Spawn { url, sidecar, child } => {
            let source = Source::Spawned(sidecar);
            report(&url, &source);
            if !display {
                // A headless start must not leave a server behind; it never
                // spawns, so this branch is unreachable in practice. Stop it
                // anyway rather than leak a process.
                let mut child = child;
                child.stop();
                println!(
                    "hardlywatching: no DISPLAY/WAYLAND_DISPLAY -- not opening a window. Would load {url}"
                );
                flush_stdout();
                return;
            }
            let slot = Arc::new(Mutex::new(Some(child)));
            run_window(WindowPlan::Attach { url, reachable: true }, Some(slot));
        }
        Plan::WouldSpawn { url, sidecar } => {
            println!("hardlywatching: server url = {url}");
            println!(
                "hardlywatching: url source = would start the bundled server ({})",
                sidecar.display()
            );
            println!(
                "hardlywatching: no DISPLAY/WAYLAND_DISPLAY -- not opening a window. Would load {url}"
            );
            flush_stdout();
        }
        Plan::Fail { message } => {
            eprintln!("hardlywatching: FATAL {message}");
            flush_stdout();
            if !display {
                // Headless and nothing to show: this is a real failure.
                std::process::exit(1);
            }
            run_window(WindowPlan::Failure(message), None);
        }
    }
}

fn report(url: &Url, source: &Source) {
    println!("hardlywatching: server url = {url}");
    println!("hardlywatching: url source = {}", source.describe());
    match probe(url) {
        Ok(()) => println!("hardlywatching: {url} is listening"),
        Err(why) => eprintln!(
            "hardlywatching: WARNING cannot reach {url} ({why}). \
             The window will say so and retry every 3s."
        ),
    }
    flush_stdout();
}

enum WindowPlan {
    Attach { url: Url, reachable: bool },
    Failure(String),
}

/// Create the window and run the event loop. `slot` is the server this app
/// started (if any) and is stopped on exit, whether or not the window survives.
fn run_window(plan: WindowPlan, slot: Option<Arc<Mutex<Option<OwnedChild>>>>) {
    let slot_for_exit = slot.clone();

    // What to tell the user if the window cannot be created at all.
    let hint = match &plan {
        WindowPlan::Attach { url, .. } => format!(
            "the server is running at {url} -- open it in a browser, or check \
             DISPLAY/WAYLAND_DISPLAY and XAUTHORITY"
        ),
        WindowPlan::Failure(message) => message.clone(),
    };

    let builder = tauri::Builder::default().setup(move |app| {
        let (window, unreachable_url) = match plan {
            WindowPlan::Attach { url, reachable } => {
                let mut builder = WebviewWindowBuilder::new(
                    app,
                    WINDOW_LABEL,
                    WebviewUrl::External(url.clone()),
                )
                .title(WINDOW_TITLE)
                .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
                .min_inner_size(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
                .resizable(true);
                if !reachable {
                    builder = builder.title(&format!(
                        "{WINDOW_TITLE} — server unreachable at {url}"
                    ));
                }
                let window = match builder.build() {
                    Ok(window) => window,
                    Err(e) => {
                        eprintln!("hardlywatching: FATAL could not create the window: {e}");
                        eprintln!("hardlywatching: resolved server url was {url}");
                        std::process::exit(1);
                    }
                };
                println!(
                    "hardlywatching: window '{WINDOW_LABEL}' {}x{} (min {}x{}) loading {url}",
                    WINDOW_WIDTH as u32,
                    WINDOW_HEIGHT as u32,
                    WINDOW_MIN_WIDTH as u32,
                    WINDOW_MIN_HEIGHT as u32
                );
                flush_stdout();
                (window, if reachable { None } else { Some(url) })
            }
            WindowPlan::Failure(message) => {
                let target = match error_page_url(&message) {
                    Some(url) => WebviewUrl::External(url),
                    // Last resort: no writable cache dir. Still not a blank
                    // frame -- this is WebKit's own "cannot open page" text.
                    None => WebviewUrl::External(
                        Url::parse("about:blank").expect("about:blank is a valid URL"),
                    ),
                };
                let builder = WebviewWindowBuilder::new(app, WINDOW_LABEL, target)
                    .title(&format!("{WINDOW_TITLE} — could not start the server"))
                    .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
                    .min_inner_size(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
                    .resizable(true);
                let window = match builder.build() {
                    Ok(window) => window,
                    Err(e) => {
                        eprintln!("hardlywatching: FATAL could not create the window: {e}");
                        std::process::exit(1);
                    }
                };
                println!(
                    "hardlywatching: window '{WINDOW_LABEL}' is showing the startup error"
                );
                flush_stdout();
                (window, None)
            }
        };

        if let Some(url) = unreachable_url {
            retry_until_reachable(app.handle().clone(), window, url);
        }
        Ok(())
    });

    // GTK/WebKit are initialised inside `build()` (tao's EventLoop::new) and
    // inside `run()`. On a session whose display cannot be opened they panic;
    // with panic=abort that was a core dump and no explanation. Catch it, say
    // something a person can act on, and leave with a non-zero status.
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(|_| {}));
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let app = match builder.build(tauri::generate_context!()) {
            Ok(app) => app,
            Err(e) => {
                eprintln!("hardlywatching: FATAL could not build the window: {e}");
                eprintln!("hardlywatching: {hint}");
                flush_stdout();
                std::process::exit(2);
            }
        };
        app.run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                stop_slot(&slot_for_exit);
            }
        });
    }));
    std::panic::set_hook(previous_hook);

    // The event loop returned: the window closed. Stop our server before the
    // process goes away, so no orphan is left holding the port or the lock.
    stop_slot(&slot);

    if let Err(payload) = outcome {
        let why = panic_text(payload);
        eprintln!("hardlywatching: FATAL could not open a window: {why}");
        eprintln!("hardlywatching: {hint}");
        flush_stdout();
        std::process::exit(2);
    }
}

fn panic_text(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown error".to_string()
    }
}

fn stop_slot(slot: &Option<Arc<Mutex<Option<OwnedChild>>>>) {
    let Some(slot) = slot else { return };
    let taken = match slot.lock() {
        Ok(mut guard) => guard.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    };
    if let Some(mut child) = taken {
        child.stop();
    }
}
