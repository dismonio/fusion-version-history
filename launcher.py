"""Local web launcher for fusion-version-history.

Stdlib only -- boots an HTTP server on 127.0.0.1:8765, auto-opens the browser
to a single-page UI with Refresh / Open Viewer / Re-auth buttons and a live
log streamed via Server-Sent Events. Designed so non-Python-comfortable users
can double-click a shortcut and not deal with a terminal.

Security: binds 127.0.0.1 only (never accessible from the network); all
subprocess invocations use list args (no shell=True); the UI only ever sends
fixed action names, never raw command strings.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Iterator

# ---- region: config ---------------------------------------------------------

LAUNCHER_HOST = "127.0.0.1"
LAUNCHER_PORT = 8765
PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
WALKER_PY     = os.path.join(PROJECT_DIR, "fusion_version_history.py")
VIEW_PY       = os.path.join(PROJECT_DIR, "view.py")
DB_PATH       = os.path.join(PROJECT_DIR, "fusion_versions.db")
HTML_PATH     = os.path.join(PROJECT_DIR, "fusion_versions.html")


# ---- region: task management ------------------------------------------------

class Task:
    """A running (or finished) subprocess with a captured log of its stdout.

    SSE clients subscribe via stream_from(offset) which yields lines as they
    arrive and returns when the process exits."""

    def __init__(self, label: str, argv: list[str]):
        self.label   = label
        self.argv    = argv
        self.lines: list[str] = []
        self.cond    = threading.Condition()
        self.done    = False
        self.exit_code: int | None = None
        self.proc: subprocess.Popen | None = None
        self.started_at = time.time()

    def append(self, line: str) -> None:
        with self.cond:
            self.lines.append(line)
            self.cond.notify_all()

    def finish(self, code: int) -> None:
        with self.cond:
            self.done = True
            self.exit_code = code
            self.cond.notify_all()

    def stream_from(self, offset: int) -> Iterator[tuple[int, str]]:
        """Yield (index, line) from offset onward, blocking until new lines
        arrive or the task completes. Returns once all lines are yielded and
        the process is done."""
        while True:
            with self.cond:
                while offset >= len(self.lines) and not self.done:
                    self.cond.wait(timeout=15)
                snap = self.lines[offset:]
                done = self.done
                end_offset = len(self.lines)
            for line in snap:
                yield offset, line
                offset += 1
            if done and offset >= end_offset:
                return


_current_task: Task | None = None
_task_lock = threading.Lock()


def _start_task(label: str, argv: list[str]) -> Task | None:
    """Start a new subprocess task. Returns None if one is already running."""
    global _current_task
    with _task_lock:
        if _current_task and not _current_task.done:
            return None
        task = Task(label, argv)
        _current_task = task

    # Inherit env vars (APS_CLIENT_ID etc. come from the user's environment).
    creationflags = 0
    if sys.platform == "win32":
        # Don't pop a new console window for the subprocess.
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        argv,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    task.proc = proc

    def _reader() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ''):
            task.append(raw.rstrip("\n"))
        proc.stdout.close()
        rc = proc.wait()
        task.append(f"--- exit code: {rc} ---")
        task.finish(rc)

    threading.Thread(target=_reader, daemon=True, name=f"task:{label}").start()
    return task


def _current() -> Task | None:
    with _task_lock:
        return _current_task


# ---- region: status helpers -------------------------------------------------

def _read_status() -> dict:
    out = {
        "db_exists": os.path.exists(DB_PATH),
        "html_exists": os.path.exists(HTML_PATH),
        "hub_count": 0,
        "project_count": 0,
        "item_count": 0,
        "version_count": 0,
        "db_mtime": None,
        "html_mtime": None,
        "task": None,
    }
    if out["db_exists"]:
        out["db_mtime"] = os.path.getmtime(DB_PATH)
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            try:
                out["hub_count"]     = conn.execute("SELECT COUNT(*) FROM hubs").fetchone()[0]
                out["project_count"] = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                out["item_count"]    = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                out["version_count"] = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    if out["html_exists"]:
        out["html_mtime"] = os.path.getmtime(HTML_PATH)
    t = _current()
    if t:
        out["task"] = {
            "label": t.label,
            "done": t.done,
            "exit_code": t.exit_code,
            "lines": len(t.lines),
            "started_at": t.started_at,
        }
    out["client_id_set"]     = bool(os.environ.get("APS_CLIENT_ID"))
    out["client_secret_set"] = bool(os.environ.get("APS_CLIENT_SECRET"))
    return out


_hubs_cache: list[dict] | None = None
_hubs_cache_lock = threading.Lock()


def _list_hubs_blocking() -> tuple[list[dict] | None, str]:
    """Shell out to --list-hubs-json (cached). Returns (hubs, error_msg)."""
    global _hubs_cache
    with _hubs_cache_lock:
        if _hubs_cache is not None:
            return _hubs_cache, ""
    try:
        proc = subprocess.run(
            [sys.executable, WALKER_PY, "--list-hubs-json"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "Hub discovery timed out (auth flow may be waiting on browser callback)."
    if proc.returncode != 0:
        return None, (proc.stderr.strip() or proc.stdout.strip() or "Hub discovery failed.")
    try:
        hubs = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return None, f"Could not parse hub list: {e}"
    with _hubs_cache_lock:
        _hubs_cache = hubs
    return hubs, ""


# ---- region: HTML page ------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fusion Version History Launcher</title>
<style>
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: #f4f5f7; color: #1f2328;
}
header { background: #fff; border-bottom: 1px solid #d6d9de; padding: 12px 18px; }
header h1 { font-size: 16px; margin: 0; font-weight: 600; }
header .sub { color: #6b7280; font-size: 12px; margin-top: 2px; }
main { max-width: 980px; margin: 0 auto; padding: 18px; display: grid; gap: 14px; }
.card { background: #fff; border: 1px solid #d6d9de; border-radius: 6px; padding: 14px 16px; }
.card h2 { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: #4b5563;
  text-transform: uppercase; letter-spacing: 0.4px; }
.row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap: 10px; margin-top: 4px; }
.stat { background: #f9fafb; border: 1px solid #eef0f3; border-radius: 5px; padding: 8px 10px; }
.stat .label { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; }
.stat .value { font-size: 18px; font-weight: 600; color: #111827; font-variant-numeric: tabular-nums; }
.stat .age   { font-size: 11px; color: #9ca3af; margin-top: 2px; }
button {
  font: inherit; font-size: 13px; padding: 7px 14px;
  border: 1px solid #d1d5db; background: #fff; border-radius: 4px; cursor: pointer;
}
button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
button.primary:hover:not(:disabled) { background: #1d4ed8; }
button:hover:not(:disabled) { background: #f9fafb; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
select { font: inherit; font-size: 13px; padding: 6px 8px; border: 1px solid #d1d5db; border-radius: 4px; }
.warn { background: #fef3c7; border-color: #fcd34d; color: #92400e; }
.warn a { color: #92400e; }
#log {
  background: #0f172a; color: #e2e8f0; font-family: ui-monospace, Consolas, monospace;
  font-size: 12px; padding: 10px; border-radius: 4px; min-height: 180px; max-height: 420px;
  overflow: auto; white-space: pre-wrap; word-break: break-word;
}
#log .stale { color: #64748b; }
.muted { color: #6b7280; font-size: 12px; }
.kbd { font-family: ui-monospace, Consolas, monospace; background: #f3f4f6; border: 1px solid #d1d5db;
       padding: 1px 5px; border-radius: 3px; font-size: 11px; }
.task-pill {
  display: inline-block; padding: 2px 8px; border-radius: 11px; font-size: 11px;
  background: #e0e7ff; color: #3730a3; margin-left: 6px;
}
.task-pill.done { background: #d1fae5; color: #047857; }
.task-pill.err  { background: #fee2e2; color: #b91c1c; }
</style>
</head>
<body>
<header>
  <h1>Fusion Version History</h1>
  <div class="sub">Local launcher &middot; <span class="kbd">127.0.0.1:8765</span></div>
</header>
<main>

<div id="setup-warn" class="card warn" style="display:none">
  <h2>APS credentials not set</h2>
  <div>
    The walker needs <span class="kbd">APS_CLIENT_ID</span> (and
    <span class="kbd">APS_CLIENT_SECRET</span> if your app is a Traditional Web App)
    in environment variables. Register an app at
    <a href="https://aps.autodesk.com/myapps" target="_blank" rel="noopener">aps.autodesk.com/myapps</a>,
    then in PowerShell:
    <pre style="background:#fffbeb;padding:8px;border-radius:4px;font-size:12px;">setx APS_CLIENT_ID "your-client-id"
setx APS_CLIENT_SECRET "your-secret"  (only for Traditional Web App)</pre>
    Open a fresh terminal, then re-run this launcher.
  </div>
</div>

<div class="card">
  <h2>Status</h2>
  <div class="stats" id="stats"></div>
  <div class="muted" id="mtimes" style="margin-top:8px"></div>
</div>

<div class="card">
  <h2>Actions <span id="task-status"></span></h2>
  <div class="row" style="margin-bottom:8px">
    <label for="hub-select" class="muted">Hub:</label>
    <select id="hub-select"><option value="">(loading...)</option></select>
    <button id="btn-load-hubs" type="button">Reload hubs</button>
  </div>
  <div class="row">
    <button id="btn-refresh-incr" class="primary" type="button" disabled>Refresh (incremental)</button>
    <button id="btn-refresh-full" type="button" disabled>Refresh (full walk)</button>
    <button id="btn-regen" type="button">Regenerate viewer</button>
    <button id="btn-open" type="button">Open viewer</button>
    <button id="btn-reauth" type="button">Re-authenticate</button>
  </div>
</div>

<div class="card">
  <h2>Live log</h2>
  <div id="log"><span class="stale">Idle. Click an action above.</span></div>
</div>

</main>

<script>
const $ = id => document.getElementById(id);
const log = $('log');
const stats = $('stats');
const mtimes = $('mtimes');
const setupWarn = $('setup-warn');
const taskStatus = $('task-status');
const hubSelect = $('hub-select');
const btnIncr = $('btn-refresh-incr');
const btnFull = $('btn-refresh-full');

function fmtAge(secAgo) {
  if (secAgo == null) return '';
  if (secAgo < 60) return Math.round(secAgo) + 's ago';
  if (secAgo < 3600) return Math.round(secAgo/60) + 'm ago';
  if (secAgo < 86400) return Math.round(secAgo/3600) + 'h ago';
  return Math.round(secAgo/86400) + 'd ago';
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    setupWarn.style.display = s.client_id_set ? 'none' : '';
    stats.innerHTML = '';
    for (const [label, key] of [['Hubs','hub_count'],['Projects','project_count'],['Items','item_count'],['Versions','version_count']]) {
      const d = document.createElement('div'); d.className = 'stat';
      d.innerHTML = `<div class="label">${label}</div><div class="value">${s[key].toLocaleString()}</div>`;
      stats.appendChild(d);
    }
    const now = Date.now() / 1000;
    const parts = [];
    if (s.db_mtime)   parts.push('DB updated ' + fmtAge(now - s.db_mtime));
    if (s.html_mtime) parts.push('viewer regenerated ' + fmtAge(now - s.html_mtime));
    mtimes.textContent = parts.join(' · ');

    if (s.task) {
      const pill = document.createElement('span');
      pill.className = 'task-pill' + (s.task.done ? (s.task.exit_code === 0 ? ' done' : ' err') : '');
      pill.textContent = s.task.done
        ? `${s.task.label}: done (${s.task.exit_code})`
        : `${s.task.label}: running...`;
      taskStatus.innerHTML = '';
      taskStatus.appendChild(pill);
    } else {
      taskStatus.innerHTML = '';
    }
    return s;
  } catch (e) {
    console.warn(e);
    return null;
  }
}

async function loadHubs() {
  hubSelect.innerHTML = '<option value="">(loading...)</option>';
  btnIncr.disabled = true; btnFull.disabled = true;
  try {
    const r = await fetch('/api/hubs');
    const data = await r.json();
    if (data.error) {
      hubSelect.innerHTML = '<option value="">(error)</option>';
      appendLog('Hub discovery failed: ' + data.error + '\n', 'err');
      return;
    }
    hubSelect.innerHTML = '';
    if (!data.hubs || data.hubs.length === 0) {
      hubSelect.innerHTML = '<option value="">(no hubs)</option>';
      return;
    }
    for (const h of data.hubs) {
      const opt = document.createElement('option');
      opt.value = h.name; opt.textContent = h.name;
      hubSelect.appendChild(opt);
    }
    const saved = localStorage.getItem('fvh.hub');
    if (saved && data.hubs.some(h => h.name === saved)) hubSelect.value = saved;
    btnIncr.disabled = false; btnFull.disabled = false;
  } catch (e) {
    hubSelect.innerHTML = '<option value="">(error)</option>';
    appendLog('Hub fetch failed: ' + e + '\n', 'err');
  }
}

hubSelect.addEventListener('change', () => {
  localStorage.setItem('fvh.hub', hubSelect.value);
});

function appendLog(line, cls) {
  if (log.querySelector('.stale')) log.innerHTML = '';
  const node = cls ? document.createElement('span') : document.createTextNode(line);
  if (cls) { node.className = cls; node.textContent = line; }
  log.appendChild(node);
  if (typeof node === 'object' && !node.tagName) {
    // text node -- append a newline-aware wrapper isn't needed; pre-wrap handles it
  }
  log.scrollTop = log.scrollHeight;
}

let currentStream = null;
function subscribeStream() {
  if (currentStream) currentStream.close();
  log.innerHTML = '';
  const es = new EventSource('/api/stream');
  currentStream = es;
  es.onmessage = (e) => appendLog(e.data + '\n');
  es.addEventListener('done', () => { es.close(); currentStream = null; refreshStatus(); });
  es.onerror = () => { es.close(); currentStream = null; };
}

async function startAction(name, body) {
  const r = await fetch('/api/run/' + name, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  const data = await r.json();
  if (data.error) {
    appendLog('Error: ' + data.error + '\n', 'err');
    return;
  }
  subscribeStream();
  refreshStatus();
}

$('btn-refresh-incr').addEventListener('click', () => startAction('walk', { hub: hubSelect.value, since: true }));
$('btn-refresh-full').addEventListener('click', () => startAction('walk', { hub: hubSelect.value, since: false }));
$('btn-regen').addEventListener('click', () => startAction('regen', {}));
$('btn-reauth').addEventListener('click', () => {
  if (!confirm('Re-authenticate? Your browser will open the Autodesk consent page in a new tab.')) return;
  startAction('reauth', {});
});
$('btn-open').addEventListener('click', () => { window.open('/viewer', '_blank'); });
$('btn-load-hubs').addEventListener('click', () => loadHubs());

refreshStatus();
loadHubs();
setInterval(refreshStatus, 4000);
</script>
</body>
</html>
"""


# ---- region: HTTP handler ---------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    # Silence the default access log (one line per request is noisy in the
    # launcher's console).
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    # ---- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send_html(INDEX_HTML)
        if path == "/api/status":
            return self._send_json(_read_status())
        if path == "/api/hubs":
            hubs, err = _list_hubs_blocking()
            return self._send_json({"hubs": hubs, "error": err})
        if path == "/api/stream":
            return self._send_stream()
        if path == "/viewer":
            return self._open_viewer_redirect()
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._send_json({"error": "invalid JSON body"}, status=400)

        if path == "/api/run/walk":
            return self._start_walk(body)
        if path == "/api/run/regen":
            return self._start_regen()
        if path == "/api/run/reauth":
            return self._start_reauth()
        self.send_error(404)

    # ---- response helpers -----------------------------------------------

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: object, status: int = 200) -> None:
        data = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _open_viewer_redirect(self) -> None:
        # Browsers block http:// -> file:// navigation for security, so we
        # serve the viewer's HTML inline at /viewer instead of redirecting.
        # Regenerate first if HTML is stale or missing.
        if not os.path.exists(HTML_PATH) or (
            os.path.exists(DB_PATH)
            and os.path.getmtime(DB_PATH) > os.path.getmtime(HTML_PATH)
        ):
            self._start_regen_blocking()
        if not os.path.exists(HTML_PATH):
            return self._send_html(
                "<!doctype html><meta charset=utf-8>"
                "<h2>Viewer HTML not generated yet</h2>"
                "<p>Run a refresh first (the DB needs at least one walk), then click "
                '"Regenerate viewer" before opening.</p>'
            )
        try:
            with open(HTML_PATH, "rb") as fh:
                data = fh.read()
        except OSError as e:
            return self._send_html(
                f"<!doctype html><meta charset=utf-8><h2>Could not read viewer</h2><pre>{e}</pre>"
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # The viewer is regenerated on each refresh; no caching.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- SSE stream ------------------------------------------------------

    def _send_stream(self) -> None:
        task = _current()
        if not task:
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for _idx, line in task.stream_from(0):
                payload = f"data: {line}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
            self.wfile.write(b"event: done\ndata: \n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- action launchers -----------------------------------------------

    def _start_walk(self, body: dict) -> None:
        hub = (body.get("hub") or "").strip()
        since = bool(body.get("since"))
        argv = [sys.executable, WALKER_PY]
        if hub:
            argv += ["--hub", hub]
        if since:
            argv += ["--since-last-run"]
        label = "walk:incr" if since else "walk:full"
        t = _start_task(label, argv)
        if not t:
            return self._send_json({"error": "Another task is already running."}, status=409)
        self._send_json({"started": label})

    def _start_regen(self) -> None:
        argv = [sys.executable, VIEW_PY]
        t = _start_task("regen", argv)
        if not t:
            return self._send_json({"error": "Another task is already running."}, status=409)
        self._send_json({"started": "regen"})

    def _start_regen_blocking(self) -> None:
        # Run inline so /viewer waits for the fresh HTML before redirecting.
        try:
            subprocess.run(
                [sys.executable, VIEW_PY],
                cwd=PROJECT_DIR, timeout=120, capture_output=True,
            )
        except Exception:
            pass

    def _start_reauth(self) -> None:
        argv = [sys.executable, WALKER_PY, "--reauth", "--list-hubs-json"]
        t = _start_task("reauth", argv)
        if not t:
            return self._send_json({"error": "Another task is already running."}, status=409)
        # Invalidate hub cache so the page reloads after re-auth completes.
        global _hubs_cache
        with _hubs_cache_lock:
            _hubs_cache = None
        self._send_json({"started": "reauth"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---- region: main -----------------------------------------------------------

def main() -> int:
    addr = (LAUNCHER_HOST, LAUNCHER_PORT)
    try:
        srv = ThreadingHTTPServer(addr, Handler)
    except OSError as e:
        print(f"Could not bind {LAUNCHER_HOST}:{LAUNCHER_PORT} -- {e}")
        print("Another process may already be using this port.")
        return 1
    url = f"http://{LAUNCHER_HOST}:{LAUNCHER_PORT}/"
    print(f"Fusion Version History launcher running at {url}")
    print("Press Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
