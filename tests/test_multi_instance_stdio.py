"""End-to-end: two real mcgram processes over stdio, as two Claude Code sessions.

The reported bug (`Failed to reconnect to mcgram: -32000`) happened at the MCP
handshake, so it can only be truly verified by running two actual processes and
completing `initialize` on both. Everything below speaks real JSON-RPC over real
pipes — no mocks of the server itself.

`api_root` points at a closed local port so the poll loop fails fast offline
instead of reaching api.telegram.org. The broker tests instead point it at
`_TelegramStub`, an `http.server` in a thread: stdlib only, no new test
dependency, and only the owner ever calls it — a non-owner never talks to
Telegram — so five endpoints cover the whole flow.

The module-level `timeout(120)` overrides CI's `--timeout=30`; these tests
start real interpreters and rely on that headroom.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from mcgram.config import Settings

pytestmark = pytest.mark.timeout(120)

_UNREACHABLE = "http://127.0.0.1:9"  # discard port — refuses instantly
_OPERATOR = 12345


def _write_config(home: Path, *, api_root: str = _UNREACHABLE, broker: bool = False) -> Path:
    cfg = home / "config.yaml"
    cfg.write_text(
        f"""
bot:
  token_env: MCGRAM_BOT_TOKEN
  operator_chat_id: {_OPERATOR}
audit:
  path: {(home / 'audit.jsonl').as_posix()}
broker:
  enabled: {str(broker).lower()}
  spool_dir: {(home / 'spool').as_posix()}
  poll_interval_s: 0.05
api_root: {api_root}
""",
        encoding="utf-8",
    )
    return cfg


class _TelegramStub:
    """Just enough Bot API for the owner: getUpdates, sendMessage, the two
    edits, answerCallbackQuery. Tests inject updates with `push`."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in
                        parse_qs(self.rfile.read(length).decode("utf-8")).items()}
                body = json.dumps({"ok": True, "result": stub.handle(
                    self.path.rsplit("/", 1)[-1], form)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def handle(self, method: str, form: dict[str, str]) -> Any:
        if method == "getUpdates":
            offset = int(form.get("offset", 0))
            with self._cond:
                # Short hold instead of the real 25s so shutdown stays quick.
                self._cond.wait_for(lambda: any(u["update_id"] >= offset
                                                for u in self._updates), timeout=1.0)
                return [u for u in self._updates if u["update_id"] >= offset]
        if method == "sendMessage":
            with self._cond:
                msg = {"message_id": 500 + len(self.sent), "text": form["text"],
                       "chat": {"id": int(form["chat_id"])},
                       "reply_markup": json.loads(form.get("reply_markup", "null"))}
                self.sent.append(msg)
                return msg
        return True

    def push(self, update: dict[str, Any]) -> None:
        with self._cond:
            self._updates.append({"update_id": len(self._updates) + 1, **update})
            self._cond.notify_all()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _McgramProcess:
    """Minimal MCP stdio client: initialize, list tools, call tools.

    stderr goes to a FILE, not a pipe. The poll loop logs a warning per failed
    getUpdates, and an undrained stderr pipe fills its OS buffer and blocks the
    child mid-write — which looks exactly like a server hang.
    """

    def __init__(
        self, config: Path, repo_root: Path, log_dir: Path, tag: str,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        env = {
            **os.environ,
            "MCGRAM_CONFIG": str(config),
            "MCGRAM_BOT_TOKEN": "fake-token",
            "PYTHONPATH": str(repo_root / "src"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            # Re-check the poll lock fast so takeover is testable in seconds
            # rather than the 30s production cadence.
            "MCGRAM_POLL_RETRY_S": "0.5",
            **(extra_env or {}),
        }
        self.stderr_path = log_dir / f"{tag}.stderr.log"
        self._stderr = self.stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from mcgram.server import main; main()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": params or {}}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"{method}: server closed stdout "
                    f"(exit={self.proc.poll()})\n{self._drain_stderr()}"
                )
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp

    def _notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def initialize(self) -> dict:
        resp = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        })
        self._notify("notifications/initialized")
        return resp

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self._request("tools/call", {"name": name, "arguments": arguments})
        return json.loads(resp["result"]["content"][0]["text"])

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self._request("tools/list")["result"]["tools"]]

    def _drain_stderr(self) -> str:
        self._stderr.flush()
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")

    def close(self) -> str:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._stderr.close()
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _is_degraded(payload: dict) -> bool:
    return payload.get("error") in {"polling_not_owned", "broker_unavailable"}


def _lock_owner_pid(home: Path) -> int:
    """Real pid of the poll-lock holder, read from the lock file it wrote.

    Not `Popen.pid`: on a uv-managed venv the `python.exe` we spawn is a
    trampoline that re-execs the interpreter as a child, so the pid we get from
    Popen is not the pid the server reports for itself.
    """
    return int((home / ".lock").read_text(encoding="utf-8").strip())


def _split_owner(
    a: _McgramProcess, b: _McgramProcess
) -> tuple[_McgramProcess, _McgramProcess]:
    """Return (owner, degraded). Probe with timeout_s=1 — in the owner this ask
    really posts, so a long timeout would stall the test for that long.

    Both instances are probed and exactly one must report `polling_not_owned`.
    Inferring "owner" from a single non-degraded probe is not safe: the owner's
    probe fails at the network layer (api_root is unreachable here), and any
    other error would then be misread as ownership.
    """
    pa = a.call_tool("ask", {"question": "probe", "options": ["ok"], "timeout_s": 1})
    pb = b.call_tool("ask", {"question": "probe", "options": ["ok"], "timeout_s": 1})
    da, db = _is_degraded(pa), _is_degraded(pb)
    assert da != db, (
        f"expected exactly one degraded instance, got a={pa!r} b={pb!r}"
    )
    # The owner reaches the network and fails there (api_root is unreachable).
    # That must arrive as a named transport error, never as a bare `internal`:
    # `internal` means a raw httpx exception escaped the tool layer.
    owner_probe = pb if da else pa
    assert owner_probe.get("error") == "transport_unreachable", (
        f"owner probe should report a named transport failure, got {owner_probe!r}"
    )
    return (b, a) if da else (a, b)


def test_two_instances_both_complete_handshake(
    tmp_path: Path, repo_root: Path
) -> None:
    """The regression itself: session 2 must not die with -32000."""
    cfg = _write_config(tmp_path)
    first = _McgramProcess(cfg, repo_root, tmp_path, "first")
    second = _McgramProcess(cfg, repo_root, tmp_path, "second")
    try:
        r1 = first.initialize()
        r2 = second.initialize()
        assert r1["result"]["serverInfo"]["name"] == "mcgram"
        assert r2["result"]["serverInfo"]["name"] == "mcgram"
        assert "error" not in r1 and "error" not in r2
        # Both alive after the handshake — no delayed exit.
        assert first.proc.poll() is None
        assert second.proc.poll() is None
        # Identical tool surface: `ask` stays listed in the degraded instance
        # so the MCP client's cached tool list never goes stale.
        assert first.tool_names() == second.tool_names()
        assert "ask" in second.tool_names()
    finally:
        second.close()
        first.close()


def test_second_instance_ask_fails_fast_and_can_still_send(
    tmp_path: Path, repo_root: Path
) -> None:
    """Degraded instance: `ask` errors immediately, sends still route out."""
    cfg = _write_config(tmp_path)
    first = _McgramProcess(cfg, repo_root, tmp_path, "first")
    second = _McgramProcess(cfg, repo_root, tmp_path, "second")
    try:
        first.initialize()
        second.initialize()
        _owner, degraded = _split_owner(first, second)

        # timeout_s=120 with a wall-clock assertion: the whole point is that a
        # degraded `ask` must NOT sit on a timeout it can never win.
        t0 = time.monotonic()
        out = degraded.call_tool(
            "ask", {"question": "deploy?", "options": ["Yes", "No"], "timeout_s": 120}
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 5, f"degraded ask took {elapsed:.1f}s — it waited on the timeout"
        assert out["error"] == "polling_not_owned"
        assert out["poll_owner_pid"] == _lock_owner_pid(tmp_path)  # names the session to ask in
        assert out.get("source") != "timeout"  # never misreported as ignored

        # send_message still runs: it reaches the (unreachable) API and reports a
        # network error rather than being disabled outright.
        sent = degraded.call_tool("send_message", {"text": "build passed"})
        assert sent.get("error") != "polling_not_owned"

        # Reminders work in the degraded instance — one-way HTTP.
        rem = degraded.call_tool("set_reminder", {"text": "check logs", "delay_s": 600})
        assert rem["reminder_id"].startswith("r_")
        assert len(degraded.call_tool("list_reminders", {})["reminders"]) == 1
    finally:
        second.close()
        first.close()


def test_ask_works_in_the_owning_instance(tmp_path: Path, repo_root: Path) -> None:
    """The owner reaches the Telegram call — proof it is not degraded.

    api_root is unreachable here, so a real send fails at the network layer.
    That failure is the point: only an instance that owns polling gets far
    enough to attempt it.
    """
    cfg = _write_config(tmp_path)
    only = _McgramProcess(cfg, repo_root, tmp_path, "only")
    try:
        only.initialize()
        out = only.call_tool("ask", {"question": "ok?", "options": ["ok"], "timeout_s": 2})
        assert out.get("error") != "polling_not_owned"
    finally:
        only.close()


def test_ownership_transfers_when_owner_exits(tmp_path: Path, repo_root: Path) -> None:
    """Killing session 1 must give session 2 `ask` without a restart."""
    cfg = _write_config(tmp_path)
    first = _McgramProcess(cfg, repo_root, tmp_path, "first")
    second = _McgramProcess(cfg, repo_root, tmp_path, "second")
    try:
        first.initialize()
        second.initialize()
        owner, waiter = _split_owner(first, second)

        # SIGKILL-equivalent: the lock file survives, so promotion also proves
        # the stale-lock path works, not just a clean release.
        owner.proc.kill()
        owner.proc.wait(timeout=10)

        # The supervisor re-checks on an interval; poll the tool until promoted.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = {"question": "ok?", "options": ["ok"], "timeout_s": 1}
            if not _is_degraded(waiter.call_tool("ask", probe)):
                break
        else:
            pytest.fail("degraded instance never took over polling after owner died")
    finally:
        second.close()
        first.close()


# --- broker: the non-polling session asks through the polling one -----------


def _wait(predicate: Any, what: str, timeout: float = 30.0) -> None:
    """Wait on produced state, never on a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
def stub() -> Any:
    s = _TelegramStub()
    yield s
    s.close()


def _start_owner_then_requester(
    tmp_path: Path, repo_root: Path, stub: _TelegramStub
) -> tuple[_McgramProcess, _McgramProcess]:
    """Owner first; requester only after the owner's heartbeat is up. The
    requester never retries the lock during the test, so it stays a
    non-owner even after the owner dies."""
    cfg = _write_config(tmp_path, api_root=stub.url, broker=True)
    owner = _McgramProcess(cfg, repo_root, tmp_path, "owner")
    owner.initialize()
    heartbeat = Settings.load(cfg).spool_root / "asks" / "u0.heartbeat.json"
    _wait(heartbeat.exists,
          "the owner's broker heartbeat")
    requester = _McgramProcess(cfg, repo_root, tmp_path, "requester",
                               extra_env={"MCGRAM_POLL_RETRY_S": "600"})
    requester.initialize()
    return owner, requester


def test_non_polling_session_asks_through_the_broker(
    tmp_path: Path, repo_root: Path, stub: _TelegramStub
) -> None:
    owner, requester = _start_owner_then_requester(tmp_path, repo_root, stub)
    try:
        result: dict[str, Any] = {}
        call = threading.Thread(target=lambda: result.update(requester.call_tool(
            "ask", {"question": "Ship it?", "options": ["Yes", "No"], "timeout_s": 30})))
        call.start()
        _wait(lambda: bool(stub.sent), "the owner to post the brokered question")
        [posted] = stub.sent
        assert posted["text"] == "Ship it?"
        tap = posted["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        stub.push({"callback_query": {"id": "cb1", "data": tap, "message": {
            "message_id": posted["message_id"], "chat": {"id": _OPERATOR}}}})
        call.join(timeout=30)
        assert not call.is_alive()
        assert (result.get("value"), result.get("source"), result.get("via")) == \
            ("No", "button", "broker"), result
        assert len(stub.sent) == 1  # posted once, by the owner only
    finally:
        requester.close()
        owner.close()


def test_requester_fails_fast_without_posting_when_the_owner_dies(
    tmp_path: Path, repo_root: Path, stub: _TelegramStub
) -> None:
    owner, requester = _start_owner_then_requester(tmp_path, repo_root, stub)
    try:
        owner.proc.kill()  # no cleanup runs: the heartbeat file stays behind
        owner.proc.wait(timeout=10)
        _wait(lambda: not _lock_holder_alive(tmp_path), "the owner process to exit")
        t0 = time.monotonic()
        out = requester.call_tool(
            "ask", {"question": "deploy?", "options": ["ok"], "timeout_s": 120})
        assert time.monotonic() - t0 < 5
        assert out["error"] == "polling_not_owned"
        assert "no longer running" in out["broker"]
        assert stub.sent == []
    finally:
        requester.close()
        owner.close()


def _lock_holder_alive(home: Path) -> bool:
    from mcgram.lock import _pid_alive

    return _pid_alive(_lock_owner_pid(home))
