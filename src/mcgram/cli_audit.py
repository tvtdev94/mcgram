"""`mcgram audit` — analyze audit.jsonl (+ rotated backups). dbread-equivalent flags."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s: str) -> timedelta:
    m = _DUR_RE.match(s)
    if not m:
        raise ValueError(f"bad duration {s!r}; expected e.g. 1h, 30m, 7d")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECS[m.group(2).lower()])


def _rotated_paths(base: str) -> list[str]:
    paths = [f"{base}.3", f"{base}.2", f"{base}.1", base]
    return [p for p in paths if os.path.exists(p)]


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _iter_entries(
    base: str, *, since: timedelta | None = None, tool: str | None = None
) -> Iterator[dict[str, Any]]:
    cutoff = datetime.now(UTC) - since if since else None
    for path in _rotated_paths(base):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if tool and rec.get("tool") != tool:
                        continue
                    if cutoff is not None:
                        ts = _parse_ts(rec.get("ts", ""))
                        if ts is None:
                            continue
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if ts < cutoff:
                            continue
                    yield rec
        except OSError:
            continue


def _summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    for rec in entries:
        status_counts[rec.get("status", "?")] = status_counts.get(rec.get("status", "?"), 0) + 1
        tool_counts[rec.get("tool", "?")] = tool_counts.get(rec.get("tool", "?"), 0) + 1
        if rec.get("status") == "rejected":
            r = rec.get("reason", "unknown")
            reason_counts[r] = reason_counts.get(r, 0) + 1
    return {
        "total": len(entries),
        "status": status_counts,
        "tool": tool_counts,
        "rejected_reasons": reason_counts,
    }


def _fmt_summary(s: dict[str, Any]) -> str:
    out = [f"Total entries: {s['total']}", "", "Status:"]
    for k, n in sorted(s["status"].items(), key=lambda x: -x[1]):
        out.append(f"  {k:<12} {n}")
    out.append("")
    out.append("By tool:")
    for k, n in sorted(s["tool"].items(), key=lambda x: -x[1]):
        out.append(f"  {k:<20} {n}")
    if s["rejected_reasons"]:
        out.append("")
        out.append("Rejection reasons:")
        for r, n in sorted(s["rejected_reasons"].items(), key=lambda x: -x[1]):
            out.append(f"  {n:>5}  {r}")
    return "\n".join(out)


def _fmt_rejected(entries: list[dict[str, Any]]) -> str:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in entries:
        if rec.get("status") != "rejected":
            continue
        groups.setdefault(rec.get("reason", "unknown"), []).append(rec)
    if not groups:
        return "No rejections found."
    out: list[str] = []
    for reason, recs in sorted(groups.items(), key=lambda x: -len(x[1])):
        out.append(f"[{len(recs)}] {reason}")
        for r in recs[:5]:
            out.append(f"    {r.get('ts','?')}  [{r.get('tool','?')}]  {r}")
        if len(recs) > 5:
            out.append(f"    ... +{len(recs) - 5} more")
    return "\n".join(out)


def _tail(base: str, tool: str | None) -> int:
    path = base
    pos = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            time.sleep(1)
            continue
        if size < pos:
            pos = 0
        if size > pos:
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if tool and rec.get("tool") != tool:
                        continue
                    sys.stdout.write(line if line.endswith("\n") else line + "\n")
                    sys.stdout.flush()
                pos = f.tell()
        time.sleep(1)


def _default_audit_path() -> str:
    env = os.environ.get("MCGRAM_AUDIT_PATH")
    if env:
        return str(Path(env).expanduser())
    cfg = os.environ.get("MCGRAM_CONFIG")
    if cfg:
        try:
            from .config import Settings
            return Settings.load(cfg).audit.path
        except Exception:
            pass
    return str(Path("~/.mcgram/audit.jsonl").expanduser())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcgram audit", description="Analyze audit.jsonl")
    p.add_argument("--path", default=None, help="override audit.jsonl path")
    p.add_argument("--since", help="e.g. 1h, 30m, 7d")
    p.add_argument("--tool", help="filter by tool name")
    p.add_argument("--rejected", action="store_true", help="show only rejections grouped")
    p.add_argument("--tail", action="store_true", help="follow new entries")
    args = p.parse_args(argv)

    base = args.path or _default_audit_path()
    if args.tail:
        try:
            return _tail(base, args.tool)
        except KeyboardInterrupt:
            return 0

    since = _parse_duration(args.since) if args.since else None
    entries = list(_iter_entries(base, since=since, tool=args.tool))
    if not entries:
        suffix = " (filtered)" if (since or args.tool) else ""
        print(f"No audit entries found at {base}{suffix}")
        return 0

    if args.rejected:
        print(_fmt_rejected(entries))
    else:
        print(_fmt_summary(_summarize(entries)))
    return 0
