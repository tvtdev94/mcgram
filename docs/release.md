# Release process

Publishing is **manual** — run from a machine with PyPI credentials. There is no
publish workflow; `.github/workflows/ci.yml` only runs lint, typecheck, and
tests. Pushing a tag does **not** publish anything.

Credentials live in `~/.pypirc` (never in the repo).

## Steps

```bash
# 0. main is clean, CI is green
git checkout main && git pull
uv run ruff check src/ tests/
uv run pytest -q

# 1. bump the version in all four places (they must agree)
#    - pyproject.toml            → [project] version
#    - src/mcgram/__init__.py    → __version__
#    - CHANGELOG.md              → new section for the release
#    - README.md                 → badges, if the numbers they quote changed

# 2. commit + tag
git commit -am "chore(release): X.Y.Z"
git tag vX.Y.Z
git push && git push --tags

# 3. build + publish
uv build                    # → dist/
uv publish                  # reads ~/.pypirc

# 4. GitHub release (do not skip — v0.5.0 shipped with a tag and no release)
gh release create vX.Y.Z --title "vX.Y.Z — <headline>" --notes-from-tag

# 5. verify
pip index versions mcgram   # or: open https://pypi.org/project/mcgram/
```

## Checks before publishing

- `uv run pytest -q` is green **locally** and the CI run for the release commit
  is green. Versions 0.3.0, 0.4.0, and 0.5.0 all went out on red CI.
- Version agrees across `pyproject.toml`, `src/mcgram/__init__.py`, and the top
  `CHANGELOG.md` section.
- `mcgram doctor` still passes against a real config.
- `dist/` holds only the artifacts for the version being shipped — `uv build`
  does not clear stale files. Delete it first if unsure.

## Notes

- **PyPI uploads are immutable.** A version can be yanked but never replaced;
  a mistake costs a patch bump.
- The sdist currently includes the README images (several MB). Harmless, just
  larger than it needs to be.
- Automating this via GitHub Actions + PyPI Trusted Publishing was considered
  and deliberately skipped: at this release cadence the manual path is simpler
  than a workflow to maintain.
