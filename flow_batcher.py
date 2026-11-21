from datetime import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
OUT_DIR = BASE_DIR / "numbered_output"
OUT_DIR.mkdir(exist_ok=True)


def get_flow_binary() -> Path:
    system = platform.system().lower()
    if system == "windows":
        name = "flow-windows.exe"
    elif system == "darwin":
        name = "flow-macos"
    else:
        name = "flow-linux"

    for candidate in (
        BASE_DIR / "bin" / name,
        BASE_DIR.parent / "bin" / name,
        BASE_DIR / "flow-agent" / "bin" / name,
    ):
        if candidate.exists():
            return candidate
    return BASE_DIR / "bin" / name


FLOW_BIN = get_flow_binary()

state_lock = threading.Lock()
job_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "model": "harbor_seal",
    "aspect": "16:9",
    "pad_width": 3,
    "batch_dir": "",
    "batch_name": "",
    "items": [],
}


def run_single_prompt(index: int, prompt: str, model: str, aspect: str, pad_width: int, batch_dir: Path):
    with state_lock:
        job_state["items"][index - 1]["status"] = "generating"
        job_state["items"][index - 1]["error"] = ""

    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd = [str(FLOW_BIN), "image"]
    if model:
        cmd.extend(["-model", model])
    cmd.extend(["-aspect", aspect, "-prompt", prompt])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(FLOW_BIN.parent.parent),
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=creation_flags,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")

        match = re.search(r"([^\s\"']+\.(?:jpg|jpeg|png|webp))", combined, re.IGNORECASE)
        if proc.returncode == 0 and match:
            raw_path = Path(match.group(1))
            src = raw_path if raw_path.is_absolute() else (FLOW_BIN.parent.parent / raw_path)
            if src.exists():
                ext = src.suffix or ".jpg"
                dest_name = f"{str(index).zfill(pad_width)}{ext}"
                dest = batch_dir / dest_name
                shutil.move(str(src), str(dest))
                with state_lock:
                    job_state["items"][index - 1]["status"] = "done"
                    job_state["items"][index - 1]["file"] = dest_name
                    job_state["completed"] += 1
                return

        err_msg = combined.strip().splitlines()[-1] if combined.strip() else "Unknown error"
        with state_lock:
            job_state["items"][index - 1]["status"] = "error"
            job_state["items"][index - 1]["error"] = err_msg
            job_state["completed"] += 1

    except Exception as e:
        with state_lock:
            job_state["items"][index - 1]["status"] = "error"
            job_state["items"][index - 1]["error"] = str(e)
            job_state["completed"] += 1


def run_items(targets: list[tuple[int, str]], model: str, aspect: str, pad_width: int, batch_dir: Path):
    for idx, prompt in targets:
        run_single_prompt(idx, prompt, model, aspect, pad_width, batch_dir)

    with state_lock:
        job_state["running"] = False


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
  textarea { width: 100%; height: 200px; background: #181c24; color: #fff; border: 1px solid #2d3544; border-radius: 8px; padding: 12px; font-size: 14px; resize: vertical; }
  .controls { display: flex; gap: 10px; align-items: center; margin: 14px 0 20px; flex-wrap: wrap; }
  select, button { padding: 10px 14px; border-radius: 8px; border: 1px solid #2d3544; font-size: 14px; }
  select { background: #181c24; color: #fff; }
  button { background: #2563eb; color: #fff; border: none; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-warn { background: #b45309; }
  .btn-secondary { background: #1e293b; border: 1px solid #334155; }
  .btn-danger { background: #991b1b; padding: 8px 14px; font-size: 13px; }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .row { display: flex; justify-content: space-between; align-items: center; background: #181c24; padding: 10px 14px; border-radius: 8px; border: 1px solid #262d3b; font-size: 14px; gap: 12px; }
  .prompt-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .badge { padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
  .queued { background: #334155; color: #cbd5e1; }
  .generating { background: #854d0e; color: #fef08a; }
  .done { background: #166534; color: #bbf7d0; }
  .error { background: #991b1b; color: #fecaca; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>Flow Batcher</h1>
    <button id="quitBtn" class="btn-danger" onclick="quitApp()">Quit App</button>
  </div>
  <p class="sub">Keep your <b>Google Flow</b> tab open in Chrome. Paste one prompt per line — each run saves into a new folder inside <code>numbered_output/</code>.</p>

  <textarea id="prompts" placeholder="1st prompt here&#10;2nd prompt here&#10;3rd prompt here"></textarea>

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
    <button id="retryBtn" class="btn-warn" onclick="retryFailed()" disabled>Retry Failed</button>
    <button class="btn-secondary" onclick="openFolder()">Open Output Folder</button>
  </div>

  <div id="statusBar" class="sub"></div>
  <div id="list" class="list"></div>
</div>

<script>
let pollTimer = null;

async function startBatch() {
  const raw = document.getElementById("prompts").value;
  const lines = raw.split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
  if (!lines.length) return alert("Paste at least one prompt.");

  const model = document.getElementById("model").value;
  const aspect = document.getElementById("aspect").value;

  const res = await fetch("/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompts: lines, model, aspect })
  });
  if (!res.ok) alert("A batch is already running.");
}

async function retryFailed() {
  const res = await fetch("/retry", { method: "POST" });
  if (!res.ok) alert("Nothing to retry or a batch is already running.");
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
    const failedCount = data.items.filter(it => it.status === "error").length;

    document.getElementById("startBtn").disabled = data.running;
    const retryBtn = document.getElementById("retryBtn");
    retryBtn.disabled = data.running || failedCount === 0;
    retryBtn.textContent = failedCount > 0 ? `Retry Failed (${failedCount})` : "Retry Failed";

    document.getElementById("statusBar").textContent = data.total
      ? `Progress: ${data.completed} / ${data.total}  |  Folder: numbered_output/${data.batch_name}`
      : "";
    document.getElementById("list").innerHTML = data.items.map(it => `
      <div class="row">
        <div class="prompt-text"><b>#${it.index}</b> — ${it.prompt}</div>
        <span class="badge ${it.status}" title="${it.error || it.file || ''}">
          ${it.status === "done" ? it.file : it.status}
        </span>
      </div>
    `).join("");
  } catch (e) {}
}
pollTimer = setInterval(poll, 1200);
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

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
        elif self.path == "/status":
            with state_lock:
                self._send_json(job_state)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/start":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            prompts = payload.get("prompts", [])
            model = payload.get("model", "harbor_seal")
            aspect = payload.get("aspect", "16:9")
            pad_width = max(3, len(str(len(prompts))))

            batch_name = datetime.now().strftime("batch_%Y-%m-%d_%H-%M-%S")
            batch_dir = OUT_DIR / batch_name
            batch_dir.mkdir(parents=True, exist_ok=True)

            with state_lock:
                if job_state["running"]:
                    return self._send_json({"error": "busy"}, 409)
                job_state["running"] = True
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
            self._send_json({"ok": True})

        elif self.path == "/retry":
            with state_lock:
                if job_state["running"]:
                    return self._send_json({"error": "busy"}, 409)
                failed = [it for it in job_state["items"] if it["status"] == "error"]
                if not failed:
                    return self._send_json({"error": "none_failed"}, 400)

                for it in failed:
                    it["status"] = "queued"
                    it["error"] = ""
                job_state["completed"] -= len(failed)
                job_state["running"] = True

                targets = [(it["index"], it["prompt"]) for it in failed]
                model = job_state["model"]
                aspect = job_state["aspect"]
                pad_width = job_state["pad_width"]
                batch_dir = Path(job_state["batch_dir"]) if job_state["batch_dir"] else OUT_DIR

            threading.Thread(
                target=run_items,
                args=(targets, model, aspect, pad_width, batch_dir),
                daemon=True,
            ).start()
            self._send_json({"ok": True})

        elif self.path == "/open-folder":
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
            self._send_json({"ok": True})
            threading.Timer(0.2, lambda: os._exit(0)).start()

        else:
            self.send_error(404)


if __name__ == "__main__":
    port = 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"Flow Batcher running at http://127.0.0.1:{port} (Close this window to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass