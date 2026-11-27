"""Offline tests for flow_batcher.py (output parsing, retry, cancel, HTTP, single-instance)."""

from __future__ import annotations

import email.message
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import flow_batcher as fb  # noqa: E402


def _call_handler(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """Invoke `fb.Handler` completely in-memory with no TCP sockets."""
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = email.message.Message()
    if raw:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))

    handler = object.__new__(fb.Handler)
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.client_address = ("127.0.0.1", 0)
    handler._headers_buffer = []

    status_box = {"code": 200}

    def fake_send_response(code, message=None):
        status_box["code"] = code

    def fake_send_header(key, value):
        pass

    def fake_end_headers():
        pass

    def fake_send_error(code, message=None, explain=None):
        status_box["code"] = code

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers
    handler.send_error = fake_send_error

    if method == "GET":
        handler.do_GET()
    elif method == "POST":
        handler.do_POST()
    else:
        raise ValueError(method)

    out = handler.wfile.getvalue().decode("utf-8")
    parsed = json.loads(out) if out.startswith("{") else {"raw": out}
    return status_box["code"], parsed


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


@pytest.fixture()
def batcher_env(tmp_path, monkeypatch):
    work_dir = tmp_path / "Flow Batcher Work Dir"
    bin_dir = work_dir / "bin"
    bin_dir.mkdir(parents=True)
    fake_bin = bin_dir / "flow-windows.exe"
    fake_bin.write_bytes(b"stub")

    out_dir = tmp_path / "numbered_output"
    out_dir.mkdir()

    monkeypatch.setattr(fb, "FLOW_BIN", fake_bin)
    monkeypatch.setattr(fb, "OUT_DIR", out_dir)
    monkeypatch.setattr(fb, "active_proc", None)
    fb.cancel_event.clear()
    with fb.state_lock:
        fb.job_state.update(
            {
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
        )
    return work_dir, out_dir


def test_extract_output_path_handles_spaces_in_json_path(tmp_path):
    work_dir = tmp_path / "OneDrive - Personal" / "Flow Batcher"
    img = work_dir / "output" / "acct-1234.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"jpeg-data")

    stdout = (
        "flow-go image\n"
        + json.dumps({"status": "succeeded", "files": [{"path": str(img)}]}, indent=2)
    )
    found, parsed = fb.extract_output_path(stdout, "", work_dir)
    assert found == img
    assert parsed["status"] == "succeeded"


def test_extract_output_path_does_not_scrape_prompt_when_json_has_empty_files(tmp_path):
    # Even if a decoy file matching a prompt word exists, structured empty JSON must win.
    decoy = tmp_path / "cat.jpg"
    decoy.write_bytes(b"decoy")

    stdout = json.dumps({"status": "empty", "job_id": "j1", "files": []}, indent=2)
    stderr = "cli: run image -prompt draw cat.jpg"
    found, parsed = fb.extract_output_path(stdout, stderr, tmp_path)
    assert found is None
    assert parsed["status"] == "empty"


def test_extract_output_path_fallback_saved_log_line(tmp_path):
    img = tmp_path / "output" / "acct-abc.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"jpeg")

    found, parsed = fb.extract_output_path(
        "", "2026/11/21 23:39:44 engine: saved acct-abc.jpg (0.9 MB, image/jpeg)\n", tmp_path
    )
    assert found == img
    assert parsed is None


def test_extract_error_message_prefers_real_errors_over_trailing_noise():
    stdout = 'bridge extension attached, cookies read live\n'
    stderr = (
        "2026/11/21 23:37:05 cli: error: PUBLIC_ERROR_UNUSUAL_ACTIVITY\n"
        "bridge extension attached, cookies read live\n"
    )
    assert (
        fb.extract_error_message(stdout, stderr, None, 1)
        == "2026/11/21 23:37:05 cli: error: PUBLIC_ERROR_UNUSUAL_ACTIVITY"
    )
    assert (
        fb.extract_error_message("bridge not started\nsome trailing info", "", None, 1)
        == "bridge not started"
    )
    assert fb.extract_error_message("", "", None, 2) == "Process exited with code 2"
    assert fb.extract_error_message("plain failure\n", "", None, 1) == "plain failure"


def test_move_overwrite_replaces_existing_destination(tmp_path):
    src = tmp_path / "new.jpg"
    dest = tmp_path / "batch" / "001.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old-bytes")
    src.write_bytes(b"new-bytes")

    fb._move_overwrite(src, dest)
    assert dest.read_bytes() == b"new-bytes"
    assert not src.exists()


def test_html_page_has_escape_helper_and_stop_control():
    assert "function escapeHtml(" in fb.HTML_PAGE
    assert "escapeHtml(it.prompt)" in fb.HTML_PAGE
    assert "escapeHtml(it.error" in fb.HTML_PAGE
    assert 'id="stopBtn"' in fb.HTML_PAGE
    assert 'id="promptCount"' in fb.HTML_PAGE
    assert "updatePromptCount" in fb.HTML_PAGE


def test_start_rejects_empty_prompts_and_busy_without_orphan_dirs(batcher_env):
    _, out_dir = batcher_env

    code, body = _call_handler("POST", "/start", {"prompts": ["  ", ""]})
    assert code == 400
    assert body["error"] == "no_prompts"
    assert list(out_dir.iterdir()) == []

    with fb.state_lock:
        fb.job_state["running"] = True

    code, body = _call_handler("POST", "/start", {"prompts": ["a sunset"]})
    assert code == 409
    assert body["error"] == "busy"
    assert list(out_dir.iterdir()) == []


def test_retry_applies_new_model_and_aspect_and_preserves_folder(batcher_env, monkeypatch):
    work_dir, out_dir = batcher_env
    seen_cmds: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen_cmds.append(list(cmd))
            self.cmd = cmd
            self.returncode = 0

        def communicate(self, timeout=None):
            if "-model" in self.cmd and self.cmd[self.cmd.index("-model") + 1] == "narwhal":
                self.returncode = 1
                return "", "2026/11/21 cli: error: model NARWHAL rejected\n"

            img = work_dir / "output" / f"gen-{len(seen_cmds)}.jpg"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"img-bytes")
            self.returncode = 0
            return json.dumps({"status": "succeeded", "files": [{"path": str(img)}]}), ""

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(fb.subprocess, "Popen", FakePopen)

    code, body = _call_handler(
        "POST",
        "/start",
        {"prompts": ["first <tag> \\\\", "second"], "model": "narwhal", "aspect": "16:9"},
    )
    assert code == 200
    batch_name = body["batch_name"]

    _wait_until(lambda: not fb.job_state["running"])
    _, status = _call_handler("GET", "/status")
    assert status["completed"] == 2
    assert [it["status"] for it in status["items"]] == ["error", "error"]
    assert "model NARWHAL rejected" in status["items"][0]["error"]
    assert seen_cmds[0][1:4] == ["image", "-captcha", "broker"]
    assert seen_cmds[0][seen_cmds[0].index("-prompt") + 1] == "first <tag>"

    # Retry after switching model + aspect in the UI
    code, retry_body = _call_handler(
        "POST", "/retry", {"model": "harbor_seal", "aspect": "9:16"}
    )
    assert code == 200
    assert retry_body["model"] == "harbor_seal"
    assert retry_body["aspect"] == "9:16"

    _wait_until(lambda: not fb.job_state["running"])
    _, status = _call_handler("GET", "/status")
    assert status["batch_name"] == batch_name
    assert status["model"] == "harbor_seal"
    assert status["aspect"] == "9:16"
    assert status["completed"] == 2
    assert [it["status"] for it in status["items"]] == ["done", "done"]
    assert [it["file"] for it in status["items"]] == ["001.jpg", "002.jpg"]

    batch_dir = out_dir / batch_name
    assert (batch_dir / "001.jpg").is_file()
    assert (batch_dir / "002.jpg").is_file()
    assert "-model" in seen_cmds[-1]
    assert seen_cmds[-1][seen_cmds[-1].index("-model") + 1] == "harbor_seal"
    assert seen_cmds[-1][seen_cmds[-1].index("-aspect") + 1] == "9:16"


def test_cancel_stops_batch_and_retry_finishes_cancelled_items(batcher_env, monkeypatch):
    work_dir, out_dir = batcher_env
    started = threading.Event()
    should_block = {"value": True}
    counter = {"n": 0}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = 0
            self._killed = threading.Event()

        def communicate(self, timeout=None):
            if should_block["value"]:
                started.set()
                self._killed.wait(timeout=5)
                self.returncode = -1
                return "", ""
            counter["n"] += 1
            img = work_dir / "output" / f"ok-{counter['n']}.jpg"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"ok")
            return json.dumps({"status": "succeeded", "files": [{"path": str(img)}]}), ""

        def terminate(self):
            self._killed.set()

        def kill(self):
            self._killed.set()

    monkeypatch.setattr(fb.subprocess, "Popen", FakePopen)

    code, body = _call_handler(
        "POST", "/start", {"prompts": ["p1", "p2", "p3"], "model": "harbor_seal", "aspect": "1:1"}
    )
    assert code == 200
    assert started.wait(timeout=3)

    code, _ = _call_handler("POST", "/cancel")
    assert code == 200
    _wait_until(lambda: not fb.job_state["running"])

    _, status = _call_handler("GET", "/status")
    assert [it["status"] for it in status["items"]] == ["cancelled", "cancelled", "cancelled"]
    assert status["completed"] == 3

    # Retry should pick up all cancelled items and complete them
    should_block["value"] = False
    code, _ = _call_handler("POST", "/retry", {"model": "harbor_seal", "aspect": "1:1"})
    assert code == 200
    _wait_until(lambda: not fb.job_state["running"])

    _, status = _call_handler("GET", "/status")
    assert [it["status"] for it in status["items"]] == ["done", "done", "done"]
    assert [it["file"] for it in status["items"]] == ["001.jpg", "002.jpg", "003.jpg"]
    assert (out_dir / body["batch_name"] / "003.jpg").is_file()


def test_health_and_create_server_reuse_or_fallback(batcher_env, monkeypatch):
    code, health = _call_handler("GET", "/health")
    assert code == 200
    assert health["app"] == "flow-batcher"
    assert health["binary_exists"] is True

    # Case 1: Port busy by existing Flow Batcher -> returns (None, preferred_port)
    class BusyServer:
        def __init__(self, addr, handler_cls):
            if addr[1] == 8765:
                raise OSError("address already in use")
            self.server_address = ("127.0.0.1", 54321)

    monkeypatch.setattr(fb, "FlowHTTPServer", BusyServer)
    monkeypatch.setattr(fb, "is_existing_instance", lambda port: True)
    srv, actual_port = fb.create_server(8765)
    assert srv is None
    assert actual_port == 8765

    # Case 2: Port busy by another service -> falls back to ephemeral port
    monkeypatch.setattr(fb, "is_existing_instance", lambda port: False)
    fallback_srv, fallback_port = fb.create_server(8765)
    assert fallback_srv is not None
    assert fallback_port == 54321
