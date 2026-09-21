# mcgram gap review — kongming, 2026-09-21

Advisory only. Scope: what does mcgram (v0.5.0, solo-maintained MCP notification bridge) still need. Judged as a small single-purpose utility, not a platform.

**TL;DR:** Engineering is in genuinely good shape — better than most solo MCP servers. The three real problems, in order: (1) a shipped correctness bug (Discord reminders claimed ✅ in README never deliver) plus a Windows+uv-broken test assertion that keeps the suite deterministically red locally (product behavior is correct; a separate latent poll-lock race is worth cheap hardening); (2) truth-drift between README/docs claims and reality (badges, `<200 LOC`, typed classifier, stale smoke-test doc); (3) the dominant gap is **distribution, not code** — 0 stars, no GitHub Releases, no MCP-directory listings, README installs from git instead of PyPI, in a niche with 7+ visible competitors. Fix the two bugs, run one hygiene pass, then spend the next chunk of effort entirely on distribution. Build almost nothing new.

---

## 1. Verified state

Commands run on clean checkout `002c4f2` (v0.5.0), Windows 11, py3.12: `pytest --collect-only`, full suite with coverage, `ruff check`, `mypy`, `wc -l`, PyPI JSON, GitHub page fetch.

### What checks out

| Claim | Verdict | Evidence |
|---|---|---|
| ~350 tests | ✅ (352) | `pytest --collect-only` → "352 tests collected" |
| ~85% coverage | ✅ (86%) | `pytest --cov` → TOTAL 2534 stmts, 364 miss, 86% |
| ruff clean | ✅ | `ruff check src/ tests/` → "All checks passed!" |
| Operator filter enforced | ✅ | `polling.py:42-47` drops + audits non-operator before dispatch; `update_dispatcher.py` 100% covered |
| `send_file` CWD jail + size cap | ✅ | `send_file.py:70-80` — `.resolve()` then `relative_to(cwd)`; symlink-out resolves outside and is rejected |
| Audit fsync per line, O_APPEND, cross-process rotate lock | ✅ | `audit.py:88-94` (`os.fsync` per write), `audit.py:103,147` rotate lock, stale reclaim `audit.py:160,193-205` |
| Rate limit per-tool token bucket | ✅ | `rate_limiter.py:9-45`; enforced at each tool, e.g. `ask.py:108-110`, `send_file.py:126-128` |
| `ask` timeout hard cap | ✅ | `ask.py:79-80` clamps to `limits.ask_timeout_max_s` |
| Poll-ownership degraded mode + takeover + fast-fail `ask` | ✅ | `poll_ownership.py:84-159`, `ask.py:105-106,141-160`; e2e-tested in `test_multi_instance_stdio.py` |
| Token/webhook never in config/stdout/audit | ✅ (spot-checked) | token only in `tg_client.py:26` base URL; audit records webhook numeric ID only (`send_file.py:159`) |
| 409 handled with flat backoff | ✅ | `polling.py:52-67` |
| PyPI published, wheel+sdist per release | ✅ | pypi.org/pypi/mcgram/json — 0.2.0→0.5.0, last 2026-08-03 |
| Onboarding polish (init auto-registers MCP, probes TG reachability, seeds ntfy welcome) | ✅ | `cli_init.py:83-120,133-150,153-182` |
| Companion skill quality (EN+VN, error-recovery table, per-transport caps) | ✅ | `data/skill/SKILL.md` — genuinely good; teaches non-retry on `polling_not_owned`/`transport_unsupported` |

### Where README/docs overstate reality

1. **Discord `set_reminder` is broken but claimed working.** README.md:107 table shows `set_reminder` ✅ for Discord. Reality: `ReminderScheduler` receives only tg+ntfy clients (`server.py:228-230`); `_require_client_for` never checks discord (`reminders.py:76-84`); a discord destination falls into the telegram branch of `_fire` and dies on `assert dest.chat_id is not None` (`reminders.py:96`) → caught by generic except (`reminders.py:103`) → **creation returns success (`reminders.py:73`) and delivery never happens**; the only trace is an audit line `status:"error"` (`reminders.py:103-108`) — nothing surfaces to Claude or the user. Zero tests: `grep discord tests/test_tools_reminder.py` and `grep reminder tests/test_tools_discord.py` both empty. `set_reminder` schema even says "to a Telegram channel" (`set_reminder.py:17`).
2. **"All modules <200 LOC"** (README.md:284, architecture.md:52) — false for 7 modules: config.py 396, cli_channel.py 330, discord_client.py 291, server.py 267, cli_init.py 260, cli_doctor.py 247, audit.py 242 (`wc -l`). Not a quality problem — the modules are fine — but a checkable claim that's wrong invites distrust of the rest.
3. **Test defect, deterministic on Windows+uv (not a product bug): `test_second_instance_ask_fails_fast_and_can_still_send` fails locally** on `assert out["poll_owner_pid"] == owner.proc.pid` (test_multi_instance_stdio.py:193). Re-verified 5× consecutive runs → the same single failure every time (not flaky). Cause: uv's Windows venv `python.exe` is a trampoline that spawns the real interpreter as a child, so `Popen.pid` ≠ the server's `os.getpid()` (demonstrated: Popen pid 4040 vs child `os.getpid()` 8208 for one spawn). The server reports its own pid correctly; the test compares it to the trampoline's pid. Green on CI only because CI's python layout differs. Fix belongs in the test: have each instance report its own pid over MCP and assert against that. (Lock isolation was confirmed while diagnosing: `home_dir` = parent of `audit.path` (config.py:365-371) and the test puts `audit.path` under `tmp_path` (test_multi_instance_stdio.py:36), so the lock is `tmp_path/.lock`, never the real `~/.mcgram/.lock`.)
4. **Latent, no observed failure: lock write/read race.** `acquire()` creates the lock file with `O_EXCL`, then writes the PID as a second step (`lock.py:86-96`); a reader hitting the not-yet-written file gets `_read_pid → None`, which `lock.py:89` treats as **stale → unlinks a live owner's lock** (`lock.py:91-94`), letting two processes both believe they own polling. Explicitly NOT the cause of the test failure above (this review initially misattributed it; corrected against the trampoline-pid evidence). Tiny window, cheap hardening — see Rank 2.
5. **Static badges** (README.md:13-14: "tests 350", "coverage 85%") drift by construction; actuals are 352/86% today. Codecov upload already exists in CI (`ci.yml:45-50`) — the real badge is free.
6. **`Typing :: Typed` classifier without `py.typed`** (pyproject.toml:34; `ls src/mcgram/py.typed` → not found). Downstream type checkers see nothing. Also mypy `--strict` has **23 errors in 8 files** while CI masks it with `continue-on-error: true` (ci.yml:40). Errors are shallow (missing `Future[...]`/`Task[...]` params, `no-any-return`, untyped MCP decorators) — plus one real smell: `ask.py:153` int-assigned-to-str flagged.
7. **Stale docs**: manual-smoke-test.md:17,33 says "prints mcgram 0.1.0" and "6 tools" (now 0.5.0 / 7 tools); README.md:267 "(Future v0.2.)" for remote control while at v0.5; README CLI section (README.md:136-144) omits `clear-lock` which exists (`cli.py:24,60-72`); `cli.py:10` help header says "Telegram + ntfy.sh" only, no Discord.
8. **sdist is ~5.4 MB** vs 79.6 KB wheel (PyPI). `docs/images` is 5.3 MB (`du -sh`) — hero/logo PNGs ship in every source install. Cosmetic but sloppy for a utility.
9. **No GitHub Releases, no repo topics** despite git tags v0.2.0–v0.5.0 (`git tag`; GitHub page shows empty Releases section, no topics, 0 stars/0 forks/0 watchers/0 issues).

### Other verified behavior worth knowing (not overstated, just sharp edges)

- **Freetext `ask` consumes any operator message**: oldest pending ask in the chat wins (`ask_registry.py:105-117`). User casually texting the bot mid-ask answers the question. Guard is `msg_ts > posted_at` comparing **Telegram server time vs local `time.time()`** (`ask_registry.py:67,108`) — a local clock ahead of Telegram's silently drops legit replies. Documented nowhere.
- **Raw network errors are unwrapped**: `tg_client._call` maps only HTTP statuses (`tg_client.py:54-63`); `httpx.ConnectError`/timeouts propagate raw, tool handlers catch only `(TelegramError, NtfyError, DiscordError)` (`send_file.py:137`), so offline sends surface as `{"error":"internal","reason":"ConnectError"}` via the catch-all (`server.py:80-82`) — no per-tool audit record, opaque message to Claude. For a tool whose main failure mode is "network briefly down", this is the weakest error path. (Upside checked: httpx transport-error strings don't embed the tokened URL, so no leak — verify once with a forced ConnectError.)
- Windows PID-reuse can make a stale lock look alive (`lock.py:39-56` liveness by PID only) → degraded forever until `mcgram clear-lock`. Rare; escape hatch exists.
- Coverage holes are concentrated where subprocess e2e tests cover but coverage can't see: server.py 67%, cli_audit.py 64%, cli_doctor.py 73%, send_video.py 74%. Acceptable.

---

## 2. Gaps ranked by impact

### Rank 1 — Distribution/adoption (impact: existential for the project's public purpose; effort mixed)

The code is 90th-percentile for this category; the reach is 0th. Verified: 0 stars, 0 forks, no Releases, no topics; a targeted web search for "mcgram MCP Telegram Claude Code" returns **only competitors** (mcp-telegram-claudecode, telegram-mcp-bridge, claude-telegram-bridge ×2, telegram-claude-mcp, claude-telegram-alerts, kstonekuan's worker). Not listed on Glama, LobeHub, mcpmarket, PulseMCP, mcp.so, or the MCP registry. README.md:55,77 tells users to `uv tool install git+https://…` even though the package is on PyPI — bypassing your own versioned releases.

mcgram's honest differentiators vs that field: 3 transports incl. no-token ntfy; multi-session poll-ownership correctness (competitors mostly die or 409-loop); fsync'd audit trail; `doctor`/`init` polish; **EN+VN skill** (unique — nobody else serves Vietnamese-speaking Claude Code users).

Sub-items, each S effort: README installs from PyPI; repo topics (`mcp`, `claude-code`, `telegram`, `ntfy`, `discord`, `notifications`); publish GH Releases from existing tags (paste CHANGELOG sections); submit to MCP registry + 2-3 directories; a 20-second GIF of the `ask` button round-trip (hero.png exists, motion sells this product); one launch post each in EN (r/ClaudeAI or HN Show) and VN (viblo.asia — the VN skill is the hook). **Worth doing: yes, this is the highest-leverage work available.** Only caveat: if the goal is personal-utility-that-happens-to-be-public, skip this rank entirely (see unresolved Q1).

### Rank 2 — Correctness debt: one shipped bug, one broken test, one latent race (effort S each)

1. **Discord reminders** (details §1.1). Two honest fixes: (a) implement — pass `discord_client` into `ReminderScheduler` and route `_fire` through the existing `dispatch.send_text` instead of the duplicated per-transport branch (DRY win: `reminders.py:92-97` reimplements `dispatch.py:37-78`); or (b) reject discord at `create()` and flip README/skill tables to ❌. Either is S. Doing (a) is barely more work than (b) and keeps the README table clean. **Worth doing: yes, immediately — a tool that returns `{reminder_id, fires_at}` and never fires is the worst kind of bug for a "trust me to tap you on the shoulder" product.**
2. **Windows+uv test assertion** (details §1.3). `test_multi_instance_stdio.py:193` asserts `poll_owner_pid == Popen.pid`; on a uv Windows venv `Popen.pid` is the trampoline's pid, so the suite is deterministically red locally while the product behaves correctly. Fix: assert against a pid each instance reports about itself over MCP (small tool-response addition or test-only probe). Effort S. **Worth doing: yes** — a suite that cannot go green on the maintainer's own OS hides real regressions.
3. **Latent lock create/read race** (details §1.4). Hardening: in `lock.py:89`, treat `held_pid is None` as *held-by-unknown, back off and retry next cycle* rather than stale-unlink; or write PID to a temp file + `os.replace` so the lock is never observably empty. Add a unit test with an empty lock file. Effort S. **Worth doing: yes, demoted — no observed failure backs it**; it rides along in the same patch because the fix is a few lines and concurrent session startup (two Claude Code sessions launching together) is the feature's whole point.

### Rank 3 — Error-path quality (impact: confusing failures in the tool's most common real-world failure mode; effort S/M)

Wrap httpx transport errors at the three clients into their transport error types (e.g. `TelegramError(0, f"network: {type(e).__name__}")`), so tools' existing except-arms catch them, audit records them, and Claude gets `telegram_api`/`ntfy_api`/`discord_api` + reason instead of `internal`. The skill already documents recovery for those error codes (`SKILL.md:146`) — today network failures bypass that guidance. No retry logic needed (KISS; Claude retries). **Worth doing: yes.**

### Rank 4 — Release/typing hygiene (impact: credibility + maintainer safety; effort S each, ~half a day total)

- Codecov badge replaces static test/coverage badges; delete or auto-derive the "<200 LOC" line.
- Fix 23 mypy errors, drop `continue-on-error`, add `py.typed` (or drop the classifier — but the errors are shallow, fixing is cheap and locks in the discipline you already pay for).
- `release.yml` with PyPI Trusted Publishing on tag push + GH Release creation. Removes manual-upload risk (token on disk) and produces the Releases page Rank 1 needs.
- Exclude `docs/images` (and `plans/`) from sdist via `[tool.hatch.build.targets.sdist]` → sdist drops ~5.3 MB.
- CI matrix: add 3.13 (and 3.14 if deps allow); add classifiers.
- Doc drift sweep: manual-smoke-test versions/counts, README `clear-lock`, README:267 "v0.2", cli.py:10 header.
**Worth doing: yes, as one batbatch "0.5.1 hygiene" pass — none individually urgent.**

### Rank 5 — Product gaps from "Known limitations" (mostly: leave alone)

| Limitation | Real pain? | Verdict |
|---|---|---|
| Reminders lost on restart (README.md:259-261) | Mild. Key insight: there is **no daemon** — a persisted reminder still can't fire while no session is open, so persistence only upgrades "lost on restart" to "fires late on next start". | **Defer.** If demand appears: JSON file in `~/.mcgram/`, rehydrate on boot, fire overdue ones immediately with an "(overdue)" prefix. Effort M. Do NOT build cross-session shared state. |
| `ask` blocks the MCP call (README.md:262) | Inherent to MCP tools; skill already coaches short timeouts (SKILL.md:125). An async ask/poll-for-answer pair would complicate the tool surface for marginal gain. | **Accept as-is.** |
| `ask` Telegram-only (README.md:263) | Acceptable. Only credible extension: ntfy action buttons POSTing to a reply topic mcgram polls — real but M/L and adds polling infra per ask. Discord 2-way needs a full bot token + gateway. | **Don't build now.** Revisit only on concrete user demand. |
| No remote control (README.md:267) | See §3 — this is the big trap. | **Don't build.** |
| Same token on 2 machines → 409 (README.md:269) | Documented honestly, backs off cleanly (`polling.py:52-67`). Multi-machine coordination is out of scope by design. | **Accept.** |
| ntfy topic discoverability / 15 MB cap | Documented with entropy numbers (README.md:69). | **Accept.** |

### Rank 6 — Test/typing niceties (low)

- Discord-reminder regression test (comes free with Rank 2 fix).
- A clock-skew note (or ±few-seconds tolerance) for `ask_registry.py:108-111` freetext matching. Effort S, priority low.
- cli_audit/cli_doctor coverage — fine as is; these fail loudly in interactive use.

---

## 3. What NOT to build

Blunt list, for a solo-maintained utility whose whole value is "small, correct, trustworthy":

1. **Remote control from Telegram** (README.md:267 "Future v0.2"). This flips the trust model: today the phone can only *answer questions mcgram asked*; remote control means the phone can *originate actions on the dev machine* — auth, command allowlists, session routing, prompt-injection-from-chat, a whole new STRIDE table. Competitors (mcp-telegram-claudecode, claude-telegram-bridge) already fight over that space; it's a different product with a different risk budget. Delete the "Future v0.2" promise from the README.
2. **More transports** (Slack, Matrix, WhatsApp, email…). Each one = client + doctor checks + init flow + skill rows + threat-model rows + doc tables, forever. Three is already the ceiling for one maintainer. The transport matrix in README.md:100-108 is the tell — every new column multiplies it.
3. **Webhook/VPS mode for Telegram.** "No daemon, no VPS" (README.md:35) is the pitch, not a limitation.
4. **Cross-session/shared reminder store** (SQLite, daemon, IPC). The per-session model matches the per-session process lifecycle. At most the JSON rehydrate in §2 Rank 5.
5. **Cron/recurring reminders, natural-language scheduling.** The 24h cap (config.py:133) is a correct product boundary for an in-process scheduler.
6. **Reading chat history / arbitrary inbound message handling** beyond the operator-filtered ask replies. Expands the operator-filter attack surface for no notification value.
7. **A second 2-way transport** (Discord gateway bot) — see Rank 5.
8. **Config UI / TUI, plugin system, per-tool rate-limit knobs.** YAML + CLI is right-sized.

---

## 4. Recommended next 3 moves

### Move 1 — "Make the README true" patch (0.5.1) — ~1 day
Fix the shipped bug, the broken test, the latent race + the untruth sweep, nothing else.
- Discord reminders: route `ReminderScheduler._fire` through `dispatch.send_text`; pass `discord_client`; validate discord dest in `create()`; regression test with mocked webhook.
- `test_multi_instance_stdio.py:193`: compare `poll_owner_pid` to a pid the owning instance reports about itself over MCP, not `Popen.pid` (uv Windows trampoline pid ≠ server pid). Required first — the suite cannot go green on Windows+uv without it.
- `lock.py`: empty/unparseable PID = held-not-stale (or atomic temp+replace write); unit test with empty lock file. Latent hardening — no observed failure.
- Doc truth: badges → Codecov; drop/fix "<200 LOC"; smoke-test doc versions; README `clear-lock` + remove "Future v0.2"; cli.py help header; set_reminder schema text.
**Acceptance:** `uv run pytest` fully green 5 consecutive runs on Windows+uv (achievable only after the test-assertion fix); a discord `set_reminder` e2e (mock) delivers; no README claim contradicted by `wc -l`/pytest output; CHANGELOG entry.

### Move 2 — Release hardening (0.6.0) — ~1 day
- Wrap httpx transport errors into transport error types at all three clients; audit line on network-failed sends; tests for the offline path.
- Fix 23 mypy errors, remove `continue-on-error`, add `py.typed`.
- `release.yml`: tag push → build → PyPI Trusted Publishing → GH Release with CHANGELOG section. Backfill GH Releases for v0.3.0–v0.5.0 (10 min, manual).
- sdist excludes `docs/images` + `plans/`; CI adds py3.13.
**Acceptance:** CI green with mypy enforced; `pip download mcgram --no-binary :all:` < 300 KB; offline `send_message` returns `telegram_api`+reason and writes an audit line; GH Releases page populated; 0.6.0 published by the workflow, not by hand.

### Move 3 — Distribution push — spread over 2-3 evenings, after 0.6.0
- README quickstart: `uv tool install mcgram` (PyPI) as the primary path.
- Repo topics; short demo GIF (ask → button tap → Claude proceeds) at top of README.
- Submit: MCP registry (registry.modelcontextprotocol.io), Glama, PulseMCP, mcp.so; check LobeHub auto-index.
- One EN post (r/ClaudeAI or Show HN: "MCP that lets Claude Code ping your phone — Telegram/ntfy/Discord, no daemon") + one VN post (viblo.asia) leading with the Vietnamese-trigger skill — the only unique wedge nobody in the competitor list has.
**Acceptance:** listed on ≥2 directories; searching "claude code telegram notification mcp" surfaces mcgram on at least one result page; ≥1 install report/issue/star from a stranger within 4 weeks (proxy: any non-self GitHub traffic).

After these three: stop and listen. Reminder persistence, ntfy 2-way, anything else — only on evidence of a real user asking.

---

## Unresolved questions

1. **Is adoption actually a goal?** Repo polish says yes, zero promotion says no. Moves 1-2 are worth it either way; Move 3 only matters if mcgram is meant to be found. If it's a personal tool, replace Move 3 with the reminder-persistence JSON.
2. **PyPI publishing method today** — no workflow in `.github/workflows/`, so presumably manual `twine`/`uv publish` with a local token. Confirm before wiring Trusted Publishing (needs a one-time PyPI-side publisher config).
3. **httpx error strings + token URL** — spot-check one forced `ConnectError` to confirm no `bot<token>` URL leaks into `str(e)` written to audit (`polling.py:77`). Expected clean, unverified live.
