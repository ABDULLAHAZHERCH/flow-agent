from datetime import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
OUT_DIR = BASE_DIR / "numbered_output"
OUT_DIR.mkdir(exist_ok=True)


def get_flow_binary() -> Path:
    system = platform.system().lower()
    if system == "windows":
        names = ("flow-windows.exe", "flow.exe")
    elif system == "darwin":
        names = ("flow-macos", "flow")
    else:
        names = ("flow-linux", "flow")

    search_dirs = (
        BASE_DIR / "bin",
        BASE_DIR.parent / "bin",
        BASE_DIR / "flow-agent" / "bin",
    )
    for directory in search_dirs:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return BASE_DIR / "bin" / names[0]


FLOW_BIN = get_flow_binary()

state_lock = threading.Lock()
cancel_event = threading.Event()
active_proc: subprocess.Popen | None = None

job_state = {
    "running": False,
    "cancelling": False,
    "total": 0,
    "completed": 0,
    "model": "harbor_seal",
    "aspect": "16:9",
    "pad_width": 3,
    "batch_dir": "",
    "batch_name": "",
    "items": [],
}


def extract_json(stdout: str):
    """Extract the trailing JSON object or array emitted by the Flow engine."""
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped.startswith(("{", "[")):
            continue
        candidate = "\n".join(lines[index:]) if stripped in ("{", "[") else stripped
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _resolve_existing_image(raw_path: str, work_dir: Path) -> Path | None:
    cleaned = raw_path.strip().strip("\"'")
    if not cleaned:
        return None
    p = Path(cleaned)
    candidates = [p] if p.is_absolute() else [work_dir / p, work_dir / "output" / p.name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def extract_output_path(stdout: str, stderr: str, work_dir: Path) -> tuple[Path | None, dict | list | None]:
    """Locate the generated image file from engine JSON output, with safe fallbacks."""
    parsed = extract_json(stdout)
    if isinstance(parsed, dict):
        files = parsed.get("files") or []
        for entry in files:
            raw = entry.get("path") if isinstance(entry, dict) else (entry if isinstance(entry, str) else None)
            if raw:
                found = _resolve_existing_image(str(raw), work_dir)
                if found is not None:
                    return found, parsed
        if "files" in parsed or "status" in parsed:
            return None, parsed

    combined = (stdout or "") + "\n" + (stderr or "")

    # Engine log format: "engine: saved acct-xxxx.jpg (...)"
    for match in re.finditer(r"\bsaved\s+([^\r\n(]+?\.(?:jpg|jpeg|png|webp))", combined, re.IGNORECASE):
        found = _resolve_existing_image(match.group(1), work_dir)
        if found is not None:
            return found, parsed

    # Quoted paths (handles spaces in folder names)
    for stream in (stdout or "", stderr or ""):
        for match in re.finditer(r'["\']([^"\'\r\n]+?\.(?:jpg|jpeg|png|webp))["\']', stream, re.IGNORECASE):
            found = _resolve_existing_image(match.group(1), work_dir)
            if found is not None:
                return found, parsed

    # Unquoted non-whitespace fallback
    for stream in (stdout or "", stderr or ""):
        for match in re.finditer(r"([^\s\"']+\.(?:jpg|jpeg|png|webp))", stream, re.IGNORECASE):
            found = _resolve_existing_image(match.group(1), work_dir)
            if found is not None:
                return found, parsed

    return None, parsed


def extract_error_message(stdout: str, stderr: str, parsed, returncode: int) -> str:
    """Pick the most informative error message from combined stdout/stderr."""
    combined = (stdout or "") + "\n" + (stderr or "")
    lines = [line.strip() for line in combined.splitlines() if line.strip()]

    for line in reversed(lines):
        low = line.lower()
        if "error:" in low or "public_error" in low or "not started" in low:
            return line

    if isinstance(parsed, dict):
        if parsed.get("error"):
            return str(parsed["error"])
        if not (stderr or "").strip() and parsed.get("status"):
            return f"No image produced (status={parsed['status']})"

    if lines:
        return lines[-1]

    return f"Process exited with code {returncode}"


def _move_overwrite(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))


def _recount_completed_locked() -> None:
    job_state["completed"] = sum(
        1 for it in job_state["items"] if it["status"] in ("done", "error", "cancelled")
    )


def _terminate_active_proc() -> None:
    with state_lock:
        proc = active_proc
    if proc is None:
        return
    try:
        proc.terminate()
    except OSError:
        pass


def _make_unique_batch_dir(out_dir: Path) -> tuple[str, Path]:
    base_name = datetime.now().strftime("batch_%Y-%m-%d_%H-%M-%S")
    batch_name = base_name
    batch_dir = out_dir / batch_name
    suffix = 2
    while batch_dir.exists():
        batch_name = f"{base_name}_{suffix}"
        batch_dir = out_dir / batch_name
        suffix += 1
    batch_dir.mkdir(parents=True, exist_ok=True)
    return batch_name, batch_dir


def run_single_prompt(
    index: int,
    prompt: str,
    model: str,
    aspect: str,
    pad_width: int,
    batch_dir: Path,
) -> bool:
    """Run one prompt. Returns False if cancelled, True otherwise."""
    global active_proc

    if cancel_event.is_set():
        with state_lock:
            job_state["items"][index - 1]["status"] = "cancelled"
            job_state["items"][index - 1]["error"] = "Cancelled by user"
            _recount_completed_locked()
        return False

    if not FLOW_BIN.exists():
        with state_lock:
            job_state["items"][index - 1]["status"] = "error"
            job_state["items"][index - 1]["error"] = f"Engine binary not found: {FLOW_BIN}"
            _recount_completed_locked()
        return True

    with state_lock:
        job_state["items"][index - 1]["status"] = "generating"
        job_state["items"][index - 1]["error"] = ""
        job_state["items"][index - 1]["file"] = ""

    prompt = prompt.rstrip("\\").strip()
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd = [str(FLOW_BIN), "image", "-captcha", "broker"]
    if model:
        cmd.extend(["-model", model])
    cmd.extend(["-aspect", aspect, "-prompt", prompt])

    work_dir = FLOW_BIN.parent.parent
    proc: subprocess.Popen | None = None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        with state_lock:
            active_proc = proc
        if cancel_event.is_set():
            proc.terminate()

        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            with state_lock:
                job_state["items"][index - 1]["status"] = "error"
                job_state["items"][index - 1]["error"] = "Timed out after 300s"
                _recount_completed_locked()
            return True

        if cancel_event.is_set():
            with state_lock:
                job_state["items"][index - 1]["status"] = "cancelled"
                job_state["items"][index - 1]["error"] = "Cancelled by user"
                _recount_completed_locked()
            return False

        src, parsed = extract_output_path(stdout or "", stderr or "", work_dir)
        if proc.returncode == 0 and src is not None:
            ext = src.suffix or ".jpg"
            dest_name = f"{str(index).zfill(pad_width)}{ext}"
            dest = batch_dir / dest_name
            _move_overwrite(src, dest)
            with state_lock:
                job_state["items"][index - 1]["status"] = "done"
                job_state["items"][index - 1]["file"] = dest_name
                job_state["items"][index - 1]["error"] = ""
                _recount_completed_locked()
            return True

        err_msg = extract_error_message(stdout or "", stderr or "", parsed, proc.returncode)
        with state_lock:
            job_state["items"][index - 1]["status"] = "error"
            job_state["items"][index - 1]["error"] = err_msg
            _recount_completed_locked()
        return True

    except Exception as e:
        with state_lock:
            if cancel_event.is_set():
                job_state["items"][index - 1]["status"] = "cancelled"
                job_state["items"][index - 1]["error"] = "Cancelled by user"
            else:
                job_state["items"][index - 1]["status"] = "error"
                job_state["items"][index - 1]["error"] = str(e)
            _recount_completed_locked()
        return not cancel_event.is_set()
    finally:
        with state_lock:
            if active_proc is proc:
                active_proc = None


def run_items(targets: list[tuple[int, str]], model: str, aspect: str, pad_width: int, batch_dir: Path):
    try:
        for idx, prompt in targets:
            if cancel_event.is_set():
                break
            keep_going = run_single_prompt(idx, prompt, model, aspect, pad_width, batch_dir)
            if not keep_going:
                break
    finally:
        with state_lock:
            if cancel_event.is_set():
                for idx, _ in targets:
                    if job_state["items"][idx - 1]["status"] == "queued":
                        job_state["items"][idx - 1]["status"] = "cancelled"
                        job_state["items"][idx - 1]["error"] = "Cancelled by user"
            _recount_completed_locked()
            job_state["running"] = False
            job_state["cancelling"] = False


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Flow Batcher</title>
<style>
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  body { margin: 0; background: #0f1115; color: #f1f5f9; padding: 32px 16px; }
  .wrap { max-width: 780px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  h1 { margin: 0; font-size: 24px; }
  .sub { color: #94a3b8; margin: 0 0 24px; font-size: 14px; }
  .prompt-box { position: relative; }
  textarea { width: 100%; height: 200px; background: #181c24; color: #fff; border: 1px solid #2d3544; border-radius: 8px; padding: 12px 12px 32px; font-size: 14px; resize: vertical; display: block; }
  .prompt-count { position: absolute; right: 18px; bottom: 10px; background: rgba(15, 17, 21, 0.88); color: #94a3b8; border: 1px solid #2d3544; border-radius: 6px; padding: 2px 8px; font-size: 12px; font-weight: 500; pointer-events: none; user-select: none; }
  .prompt-count.has-prompts { color: #93c5fd; border-color: #1d4ed8; }
  .controls { display: flex; gap: 10px; align-items: center; margin: 14px 0 20px; flex-wrap: wrap; }
  select, button { padding: 10px 14px; border-radius: 8px; border: 1px solid #2d3544; font-size: 14px; }
  select { background: #181c24; color: #fff; }
  button { background: #2563eb; color: #fff; border: none; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-warn { background: #b45309; }
  .btn-stop { background: #dc2626; }
  .btn-secondary { background: #1e293b; border: 1px solid #334155; }
  .btn-danger { background: #991b1b; padding: 8px 14px; font-size: 13px; }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .row { display: flex; justify-content: space-between; align-items: flex-start; background: #181c24; padding: 10px 14px; border-radius: 8px; border: 1px solid #262d3b; font-size: 14px; gap: 12px; }
  .row-main { min-width: 0; flex: 1; }
  .prompt-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error-msg { color: #fca5a5; font-size: 12px; margin-top: 4px; word-break: break-word; }
  .badge { padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 600; text-transform: uppercase; flex-shrink: 0; }
  .queued { background: #334155; color: #cbd5e1; }
  .generating { background: #854d0e; color: #fef08a; }
  .done { background: #166534; color: #bbf7d0; }
  .error { background: #991b1b; color: #fecaca; }
  .cancelled { background: #475569; color: #e2e8f0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>Flow Batcher</h1>
    <button id="quitBtn" class="btn-danger" onclick="quitApp()">Quit App</button>
  </div>
  <p class="sub">Keep your <b>Google Flow</b> tab open in Chrome. Paste one prompt per line — each run saves into a new folder inside <code>numbered_output/</code>.</p>

  <div class="prompt-box">
    <textarea id="prompts" placeholder="1st prompt here&#10;2nd prompt here&#10;3rd prompt here"></textarea>
    <div id="promptCount" class="prompt-count" aria-live="polite">0 prompts</div>
  </div>

  <div class="controls">
    <select id="model">
      <option value="harbor_seal">Lite (harbor_seal)</option>
      <option value="narwhal">Standard (narwhal)</option>
      <option value="gem_pix_2">Pro (gem_pix_2)</option>
      <option value="">Auto (Account Default)</option>
    </select>
    <select id="aspect">
      <option value="16:9">16:9 (Landscape)</option>
      <option value="9:16">9:16 (Portrait)</option>
      <option value="1:1">1:1 (Square)</option>
      <option value="4:3">4:3</option>
      <option value="3:4">3:4</option>
    </select>
    <button id="startBtn" onclick="startBatch()">Start Generating</button>
    <button id="stopBtn" class="btn-stop" onclick="stopBatch()" disabled>Stop</button>
    <button id="retryBtn" class="btn-warn" onclick="retryFailed()" disabled>Retry Failed</button>
    <button class="btn-secondary" onclick="openFolder()">Open Output Folder</button>
  </div>

  <div id="statusBar" class="sub"></div>
  <div id="list" class="list"></div>
</div>

<script>
let pollTimer = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function parsePrompts(raw) {
  return raw.split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
}

function updatePromptCount() {
  const count = parsePrompts(document.getElementById("prompts").value).length;
  const el = document.getElementById("promptCount");
  el.textContent = count === 1 ? "1 prompt" : `${count} prompts`;
  el.classList.toggle("has-prompts", count > 0);
}

async function startBatch() {
  const raw = document.getElementById("prompts").value;
  const lines = parsePrompts(raw);
  if (!lines.length) return alert("Paste at least one prompt.");

  const model = document.getElementById("model").value;
  const aspect = document.getElementById("aspect").value;

  const res = await fetch("/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompts: lines, model, aspect })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.error === "busy" ? "A batch is already running." : "Could not start batch.");
  }
  poll();
}

async function stopBatch() {
  const res = await fetch("/cancel", { method: "POST" });
  if (!res.ok) return;
  poll();
}

async function retryFailed() {
  const model = document.getElementById("model").value;
  const aspect = document.getElementById("aspect").value;
  const res = await fetch("/retry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, aspect })
  });
  if (!res.ok) alert("Nothing to retry or a batch is already running.");
  poll();
}

async function openFolder() {
  await fetch("/open-folder", { method: "POST" });
}

async function quitApp() {
  if (!confirm("Stop Flow Batcher?")) return;
  clearInterval(pollTimer);
  try { await fetch("/shutdown", { method: "POST" }); } catch (e) {}
  document.body.innerHTML = '<div class="wrap"><h1>Flow Batcher Stopped</h1><p class="sub">You can safely close this browser tab.</p></div>';
}

async function poll() {
  try {
    const res = await fetch("/status");
    const data = await res.json();
    const retryableCount = data.items.filter(it => it.status === "error" || it.status === "cancelled").length;
    const hasCancelled = data.items.some(it => it.status === "cancelled");

    document.getElementById("startBtn").disabled = data.running;
    const stopBtn = document.getElementById("stopBtn");
    stopBtn.disabled = !data.running || data.cancelling;
    stopBtn.textContent = data.cancelling ? "Stopping..." : "Stop";

    const retryBtn = document.getElementById("retryBtn");
    retryBtn.disabled = data.running || retryableCount === 0;
    const retryLabel = hasCancelled ? "Retry Unfinished" : "Retry Failed";
    retryBtn.textContent = retryableCount > 0 ? `${retryLabel} (${retryableCount})` : "Retry Failed";

    document.getElementById("statusBar").textContent = data.total
      ? `Progress: ${data.completed} / ${data.total}  |  Model: ${data.model || "auto"}  |  Folder: numbered_output/${data.batch_name}`
      : "";
    document.getElementById("list").innerHTML = data.items.map(it => {
      const safePrompt = escapeHtml(it.prompt);
      const safeError = escapeHtml(it.error || "");
      const safeFile = escapeHtml(it.file || "");
      const safeStatus = escapeHtml(it.status);
      const badgeLabel = it.status === "done" ? safeFile : safeStatus;
      const errBlock = it.error && it.status === "error"
        ? `<div class="error-msg">${safeError}</div>`
        : "";
      return `
        <div class="row">
          <div class="row-main">
            <div class="prompt-text" title="${safePrompt}"><b>#${Number(it.index)}</b> — ${safePrompt}</div>
            ${errBlock}
          </div>
          <span class="badge ${safeStatus}" title="${safeError || safeFile}">
            ${badgeLabel}
          </span>
        </div>
      `;
    }).join("");
  } catch (e) {}
}
document.getElementById("prompts").addEventListener("input", updatePromptCount);
updatePromptCount();
pollTimer = setInterval(poll, 1200);
poll();
</script>
</body>
</html>
"""


class FlowHTTPServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR allows multiple processes to silently bind the same port.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _read_json_body(self) -> tuple[dict | None, bool]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}, True
        try:
            raw = self.rfile.read(length).decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return None, False
            return parsed, True
        except (ValueError, UnicodeDecodeError):
            return None, False

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self._send_json(
                {
                    "app": "flow-batcher",
                    "ok": True,
                    "binary": str(FLOW_BIN),
                    "binary_exists": FLOW_BIN.exists(),
                }
            )
        elif self.path == "/status":
            with state_lock:
                self._send_json(job_state)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/start":
            payload, ok = self._read_json_body()
            if not ok or payload is None:
                return self._send_json({"error": "invalid_json"}, 400)

            raw_prompts = payload.get("prompts", [])
            if not isinstance(raw_prompts, list):
                return self._send_json({"error": "invalid_prompts"}, 400)
            prompts = [str(p).strip() for p in raw_prompts if str(p).strip()]
            if not prompts:
                return self._send_json({"error": "no_prompts"}, 400)

            model = str(payload.get("model", "harbor_seal"))
            aspect = str(payload.get("aspect", "16:9"))
            pad_width = max(3, len(str(len(prompts))))

            with state_lock:
                if job_state["running"]:
                    return self._send_json({"error": "busy"}, 409)

                OUT_DIR.mkdir(exist_ok=True)
                batch_name, batch_dir = _make_unique_batch_dir(OUT_DIR)
                cancel_event.clear()

                job_state["running"] = True
                job_state["cancelling"] = False
                job_state["total"] = len(prompts)
                job_state["completed"] = 0
                job_state["model"] = model
                job_state["aspect"] = aspect
                job_state["pad_width"] = pad_width
                job_state["batch_dir"] = str(batch_dir)
                job_state["batch_name"] = batch_name
                job_state["items"] = [
                    {"index": i, "prompt": p, "status": "queued", "file": "", "error": ""}
                    for i, p in enumerate(prompts, start=1)
                ]

            targets = list(enumerate(prompts, start=1))
            threading.Thread(
                target=run_items,
                args=(targets, model, aspect, pad_width, batch_dir),
                daemon=True,
            ).start()
            self._send_json({"ok": True, "batch_name": batch_name})

        elif self.path == "/retry":
            payload, ok = self._read_json_body()
            if not ok or payload is None:
                return self._send_json({"error": "invalid_json"}, 400)

            with state_lock:
                if job_state["running"]:
                    return self._send_json({"error": "busy"}, 409)
                retryable = [
                    it for it in job_state["items"] if it["status"] in ("error", "cancelled")
                ]
                if not retryable:
                    return self._send_json({"error": "none_failed"}, 400)

                if "model" in payload:
                    job_state["model"] = str(payload["model"])
                if "aspect" in payload:
                    job_state["aspect"] = str(payload["aspect"])

                for it in retryable:
                    it["status"] = "queued"
                    it["error"] = ""
                _recount_completed_locked()
                cancel_event.clear()
                job_state["running"] = True
                job_state["cancelling"] = False

                targets = [(it["index"], it["prompt"]) for it in retryable]
                model = job_state["model"]
                aspect = job_state["aspect"]
                pad_width = job_state["pad_width"]
                batch_dir = Path(job_state["batch_dir"]) if job_state["batch_dir"] else OUT_DIR

            threading.Thread(
                target=run_items,
                args=(targets, model, aspect, pad_width, batch_dir),
                daemon=True,
            ).start()
            self._send_json({"ok": True, "count": len(targets), "model": model, "aspect": aspect})

        elif self.path == "/cancel":
            with state_lock:
                if not job_state["running"]:
                    return self._send_json({"error": "not_running"}, 400)
                job_state["cancelling"] = True
                cancel_event.set()
            _terminate_active_proc()
            self._send_json({"ok": True})

        elif self.path == "/open-folder":
            OUT_DIR.mkdir(exist_ok=True)
            with state_lock:
                target_dir = Path(job_state["batch_dir"]) if job_state["batch_dir"] else OUT_DIR
            if not target_dir.exists():
                target_dir = OUT_DIR

            if os.name == "nt":
                os.startfile(str(target_dir))
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(target_dir)])
            else:
                subprocess.Popen(["xdg-open", str(target_dir)])
            self._send_json({"ok": True})

        elif self.path == "/shutdown":
            cancel_event.set()
            _terminate_active_proc()
            self._send_json({"ok": True})
            threading.Timer(0.2, lambda: os._exit(0)).start()

        else:
            self.send_error(404)


def is_existing_instance(port: int) -> bool:
    """Return True if another Flow Batcher instance is already listening on `port`."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and data.get("app") == "flow-batcher"
    except (OSError, urllib.error.URLError, ValueError):
        return False


def create_server(preferred_port: int = 8765) -> tuple[FlowHTTPServer | None, int]:
    """Bind the preferred port, reuse an existing Flow Batcher instance, or pick a free port."""
    try:
        server = FlowHTTPServer(("127.0.0.1", preferred_port), Handler)
        return server, preferred_port
    except OSError:
        if is_existing_instance(preferred_port):
            return None, preferred_port
        server = FlowHTTPServer(("127.0.0.1", 0), Handler)
        return server, int(server.server_address[1])


if __name__ == "__main__":
    server, port = create_server(8765)
    url = f"http://127.0.0.1:{port}"
    if server is None:
        webbrowser.open(url)
        print(f"Flow Batcher is already running at {url}")
        raise SystemExit(0)

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Flow Batcher running at {url} (Close this window to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cancel_event.set()
        _terminate_active_proc()
        server.server_close()
