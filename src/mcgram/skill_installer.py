"""Install the bundled Claude Code skill to ~/.claude/skills/mcgram/SKILL.md."""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def _bundled_skill_text() -> str:
    """Read the bundled SKILL.md shipped inside the wheel."""
    return resources.files("mcgram.data.skill").joinpath("SKILL.md").read_text(encoding="utf-8")


def claude_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def install_skill(*, force: bool = False, quiet: bool = False) -> int:
    """Install or refresh ~/.claude/skills/mcgram/SKILL.md. Idempotent.

    Returns 0 on success or no-op. ~/.claude must already exist (owned by Claude Code).
    """
    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        if not quiet:
            print("skip     ~/.claude not found (install Claude Code first)")
        return 0
    target_dir = claude_skills_dir() / "mcgram"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    if target.exists() and not force:
        if not quiet:
            print(f"skipped  {target} (already exists; use --force to reinstall)")
        return 0
    target.write_text(_bundled_skill_text(), encoding="utf-8")
    print(f"{'updated' if force else 'created'}  {target}")
    return 0
