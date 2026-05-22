"""cli_audit unit tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcgram.cli_audit import _iter_entries, _parse_duration, _summarize, main


def _write(p: Path, recs: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def test_parse_duration() -> None:
    assert _parse_duration("30s") == timedelta(seconds=30)
    assert _parse_duration("5m") == timedelta(minutes=5)
    assert _parse_duration("2h") == timedelta(hours=2)
    assert _parse_duration("7d") == timedelta(days=7)


def test_parse_duration_bad() -> None:
    with pytest.raises(ValueError):
        _parse_duration("forever")


def test_summarize(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [
        {"ts": now, "tool": "send_message", "status": "ok"},
        {"ts": now, "tool": "send_message", "status": "ok"},
        {"ts": now, "tool": "send_file", "status": "rejected", "reason": "file_too_large"},
        {"ts": now, "tool": "ask", "status": "rejected", "reason": "rate_limit"},
    ])
    summary = _summarize(list(_iter_entries(str(p))))
    assert summary["total"] == 4
    assert summary["status"]["ok"] == 2
    assert summary["status"]["rejected"] == 2
    assert summary["rejected_reasons"]["file_too_large"] == 1


def test_filter_by_tool(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [
        {"ts": now, "tool": "send_message", "status": "ok"},
        {"ts": now, "tool": "send_file", "status": "ok"},
    ])
    entries = list(_iter_entries(str(p), tool="send_file"))
    assert [r["tool"] for r in entries] == ["send_file"]


def test_filter_by_since(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    new = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [
        {"ts": old, "tool": "send_message", "status": "ok"},
        {"ts": new, "tool": "send_message", "status": "ok"},
    ])
    recent = list(_iter_entries(str(p), since=timedelta(hours=1)))
    assert len(recent) == 1
    assert recent[0]["ts"] == new


def test_main_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [{"ts": now, "tool": "send_message", "status": "ok"}])
    rc = main(["--path", str(p)])
    assert rc == 0
    assert "Total entries" in capsys.readouterr().out


def test_main_no_entries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text("", encoding="utf-8")
    rc = main(["--path", str(p)])
    assert rc == 0
    assert "No audit entries" in capsys.readouterr().out


def test_main_rejected_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [
        {"ts": now, "tool": "send_file", "status": "rejected", "reason": "file_too_large"},
        {"ts": now, "tool": "send_message", "status": "ok"},
    ])
    main(["--path", str(p), "--rejected"])
    out = capsys.readouterr().out
    assert "file_too_large" in out
    assert "ok" not in out.split("file_too_large")[0]  # no ok lines mixed in
