"""End-to-end: two real mcgram processes over stdio, as two Claude Code sessions.

The reported bug (`Failed to reconnect to mcgram: -32000`) happened at the MCP
handshake, so it can only be truly verified by running two actual processes and
completing `initialize` on both. Everything below speaks real JSON-RPC over real
pipes — no mocks of the server itself.

`api_root` points at a closed local port so the poll loop fails fast offline
instead of reaching api.telegram.org.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(120)

_UNREACHABLE = "http://127.0.0.1:9"  # discard port — refuses instantly


def _write_config(home: Path) -> Path:
    cfg = home / "config.yaml"
    cfg.write_text(
        f"""
bot:
  token_env: MCGRAM_BOT_TOKEN
  operator_chat_id: 12345
audit:
  path: {(home / 'audit.jsonl').as_posix()}
api_root: {_UNREACHABLE}
""",
        encoding="utf-8",
    )
    return cfg


class _McgramProcess:
    """Minimal MCP stdio client: initialize, list tools, call tools.

    stderr goes to a FILE, not a pipe. The poll loop logs a warning per failed
    getUpdates, and an undrained stderr pipe fills its OS buffer and blocks the
    child mid-write — which looks exactly like a server hang.
    """

    def __init__(self, config: Path, repo_root: Path, log_dir: Path, tag: str) -> None:
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
    return payload.get("error") == "polling_not_owned"


def _split_owner(
    a: _McgramProcess, b: _McgramProcess
) -> tuple[_McgramProcess, _McgramProcess]:
    """Return (owner, degraded). Probe with timeout_s=1 — in the owner this ask
    really posts, so a long timeout would stall the test for that long."""
    probe = b.call_tool("ask", {"question": "probe", "timeout_s": 1})
    return (a, b) if _is_degraded(probe) else (b, a)


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
        owner, degraded = _split_owner(first, second)

        # timeout_s=120 with a wall-clock assertion: the whole point is that a
        # degraded `ask` must NOT sit on a timeout it can never win.
        t0 = time.monotonic()
        out = degraded.call_tool(
            "ask", {"question": "deploy?", "options": ["Yes", "No"], "timeout_s": 120}
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 5, f"degraded ask took {elapsed:.1f}s — it waited on the timeout"
        assert out["error"] == "polling_not_owned"
        assert out["poll_owner_pid"] == owner.proc.pid  # names the session to ask in
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
        out = only.call_tool("ask", {"question": "ok?", "timeout_s": 2})
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
            if not _is_degraded(waiter.call_tool("ask", {"question": "ok?", "timeout_s": 1})):
                break
        else:
            pytest.fail("degraded instance never took over polling after owner died")
    finally:
        second.close()
        first.close()
