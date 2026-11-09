---
phase: 5
title: "Docs and verification"
status: pending
priority: P2
dependencies: [3, 4]
effort: ""
---

# Phase 5: Docs and verification

## Overview

Kiem chung end-to-end voi 2 process that, cap nhat tai lieu theo **bang doi chieu
dong-theo-dong**, va chay quality gate.

Red team chi ra ban thao dau: (a) tiêu chi `<200 LOC` khong the pass, (b) fallback
test (`@pytest.mark.flaky`, `aiohttp`) khong thuc thi duoc trong repo nay, (c)
threat model chi duoc dac ta bang mot cau nen mot mitigation row **da publish** se
tro thanh sai.

## Requirements

**Functional:**
- Test 2 process that: 2 `mcgram` subprocess cung `MCGRAM_CONFIG`, non-owner `ask` duoc
- **Threat model sua theo dong cu the** — mot row hien tai se thanh sai
- README + SKILL.md + architecture.md phan anh hanh vi moi
- `mcgram doctor` bao trang thai spool
- CHANGELOG + version bump

**Non-functional:**
- `uv run pytest -q` xanh; `uv run ruff check src/ tests/` sach
- **Moi module MOI < 200 LOC** — xem muc LOC duoi
- **Phuong an fake Telegram chot TRONG phase nay**, khong hoan

## Tieu chi LOC: viet lai cho dung su that

Ban thao dau: *"Moi module (ke ca file sua) < 200 LOC"*. **Khong the pass.**
`wc -l` hien tai:

| File | LOC | Plan sua? |
|---|---:|---|
| `config.py` | **288** | Co — phase 1 (~+14) |
| `cli_init.py` | **255** | Khong |
| `server.py` | **255** | Co — phase 3+4 (~+25) |
| `audit.py` | **242** | Khong |
| `cli_audit.py` | 191 | Khong (sat nguong) |
| `tools/ask.py` | 182 | Co — nhung duong broker o module rieng |
| `ask_registry.py` | 163 | **KHONG** (kien truc moi khong cham) |

Bon file da vuot 200 **truoc** plan nay. Va `README.md:268` (*"All modules <200
LOC"*) cung `docs/architecture.md:48` (*"Every module is < 200 LOC"*) **da sai tu
truoc** — ban thao dau khong liet ke ca hai trong danh sach doc can sua.

**Tieu chi cuoi cung:**
- Moi module **moi** (`spool.py`, `ask_broker.py`, `ask_server.py`,
  `ask_via_broker.py`, `reminder_broker.py`) < 200 LOC **do bang dong code**
  (bo blank + comment + docstring). Ly do: docstring cua `spool.py` mang ket
  luan red team khong tai tao duoc tu code (vi sao `write_exclusive` ghi
  in-place, vi sao record thieu `expires_at` bi coi la hong). Cat chung de dat
  `wc -l` la toi uu con so nguoc lai muc tieu
- File duoc sua **khong vuot size class hien tai**: `config.py` ≤ 325 (`wc -l`;
  nang tu 310 sau phase 1 — `BrokerConfig` la 19 dong schema thuan, tach sang
  `config_models.py` cho 19 dong la YAGNI va mau thuan voi lenh cam refactor
  file blast-radius cao o duoi), `server.py` ≤ 285, `tools/ask.py` < 200,
  `reminders.py` < 200
- Overage san co cua `config.py`/`cli_init.py`/`server.py`/`audit.py` **ngoai
  scope** — khong split trong plan nay (refactor 2 file blast-radius cao nhat
  repo giua plan, khong phase, khong rollback note)
- **Sua `README.md:268` va `docs/architecture.md:48`** cho khop su that: doi
  thanh *"Most modules <200 LOC"* hoac neu con so thuc

## Test 2 process: chot phuong an ngay

Ban thao dau hoan sang "quyet dinh khi implement" va de xuat hai fallback **khong
thuc thi duoc**:

1. `@pytest.mark.flaky` — `pyproject.toml:82` co `addopts = "-ra --strict-markers"`
   va `:84-86` chi register `integration`. Marker chua register la **hard error**.
   `flaky` va `pytest-rerunfailures` deu khong co trong `uv.lock` (da grep: 0).
2. `aiohttp` cho HTTP stub — **khong** phai dependency (da grep `uv.lock`: khong
   match). `http.server` la sync, can 1 thread cho moi `getUpdates` long-poll.

**Chot: dung `http.server` trong thread rieng.** Ly do: stdlib, khong them
dependency, va so endpoint can fake it — voi kien truc owner-does-everything,
non-owner **khong** goi Telegram, nen chi owner can:

| Endpoint | Vi sao can |
|---|---|
| `getUpdates` | Long-poll; tra update khi test inject |
| `sendMessage` | `registry.open()` post cau hoi |
| `editMessageText` | `_mark_answered` / `_mark_timed_out` |
| `editMessageReplyMarkup` | `_strip_keyboard` |
| `answerCallbackQuery` | `_handle_callback` |

`Settings.api_root` da config duoc (`config.py:139`), va
`tests/test_multi_instance_stdio.py:25` hien tro vao discard port → phai sua
`_write_config` de tro vao stub.

Ghi chu: `test_multi_instance_stdio.py:23` co `pytest.mark.timeout(120)` o module
level, override `--timeout=30` cua CI. Phase nay dua vao dieu do — ghi ro trong
docstring.

**Neu Windows CI flaky:** khong dung `@pytest.mark.flaky` (khong ton tai). Dung
barrier tat dinh (poll dieu kien thay vi `sleep`) + timeout rong. Neu van flaky,
**register marker `flaky` trong `pyproject.toml:84-86`** nhu mot buoc tuong minh —
khong gia dinh no san co.

## Threat model: sua theo dong, khong theo y

Ban thao dau chi ghi "them muc spool: Spoofing, Tampering, Info disclosure". Ba
cho se sai/thieu:

| Dong | Hien tai | Sau plan |
|---|---|---|
| `:28` | *"`ask` requires poll ownership and returns `polling_not_owned` immediately. The `AskRegistry` exists only in the polling process, so no non-owner can hold a pending question."* | **Ca hai cau thanh sai khi broker bat.** Viet lai: mitigation chi con dung khi `broker.enabled: false` (default); khi bat, thay bang heartbeat + provenance binding |
| `:12` | Trust-boundary diagram liet ke `~/.mcgram/{config.yaml, .env, audit.jsonl, .lock}` | Them `spool/` — artifact duy nhat ma **write** access chuyen thanh kha nang ra quyet dinh |
| `:80` | *"Anyone with `~/.mcgram/` read access — they have your bot token"* | Nang luc moi la **write**, va write ≠ token. Them dong rieng |

**Them muc moi** cho spool, noi dung phai trung thuc:
- **Spoofing**: ghi answer file = gia mao cau tra loi cua operator. Giam thieu:
  default off, `answered_by_pid` cross-check lock, audit. **Khong** chan duoc ke
  cung uid tuyet doi
- **Tampering**: sua request → doi cau hoi operator thay. Mode 0600 + TTL ngan
- **Info disclosure**: voi kien truc owner-does-everything, **question text + options
  co tren dia** (owner can de post). Ban thao dau khang dinh "cau hoi khong ghi ra
  dia" — **khong con dung**, khong duoc lap lai claim do
- **Ranh gioi tin cay**: cung uid = da tin cay mot phan (`.env` cung o do), NHUNG
  ghi spool cho them nang luc *ra quyet dinh* ma token khong cho

## `redact_text` khong ap len spool (da chot voi user)

`redact_text` hom nay chi tac dong `audit.jsonl` (`audit.py:69` — thay `text`
bang `<redacted>`, giu `text_len`). Spool **khong** duoc redact, va day khong
phai lua chon ve muc do bao ve — la rang buoc chuc nang:

| Truong | Vi sao khong redact duoc |
|---|---|
| `question`, `options` (request file) | Owner doc de goi `sendMessage`. Redact → khong post duoc cau hoi |
| `value` (answer file) | Chinh la ket qua non-owner doc len tra cho Claude. Redact → Claude nhan `"<redacted>"` thay vi cau tra loi |

Spool la **kenh truyen**, khong phai log. Log redact duoc vi khong ai doc lai de
hanh dong; spool thi co.

**Chot: chi document, khong chan.** Khong warn luc boot, khong tu choi config.
User bat `redact_text: true` dang ky vong "noi dung khong nam tren dia" — broker
pha ky vong do, nen phai noi ro o ba cho:

1. `docs/security-threat-model.md` — muc spool, dong "Info disclosure": ghi ro
   `redact_text` **khong** ap len spool, kem ly do chuc nang o tren
2. `src/mcgram/data/config.example.yaml` — comment canh `broker.enabled`:
   *"question/answer text is written to `~/.mcgram/spool/` while a question is
   open (mode 0600, short TTL, swept). `audit.redact_text` does not apply there."*
3. `README.md` muc config example — cung mot cau, ngan hon

Giam thieu con lai la co che, khong phai redact: mode 0600, TTL ngan, sweep don
ca record mo coi.

## Bang doi chieu doc

| Vi tri | Hien tai | Sau plan |
|---|---|---|
| `README.md:105` (bang tools) | *"Telegram-only, and only in the polling session"* | *"Telegram-only; moi session qua broker (opt-in)"* |
| `README.md:110` | *"`polling_not_owned` in a session that isn't the one polling"* | Cap nhat: chi khi broker tat hoac khong co owner song |
| `README.md:219-240` (muc Multiple sessions) | Bang `ask` cot "Other instances" = ❌; doan *"fails immediately in a send-only instance"* | Viet lai — xem duoi |
| `README.md:244-246` | *"list_reminders in one session won't show reminders set in another"* | Sai sau phase 4 — viet lai: thay duoc kem `owner_alive`, van mat khi restart |
| `README.md:262-266` | Module tree | Them `spool · ask_broker · ask_server · ask_via_broker · reminder_broker` |
| `README.md:268` | *"All modules <200 LOC, 268 tests"* | **Da sai tu truoc** — sua ca hai so |
| `README.md:141-183` (config example) | Thieu `broker:` | Them, kem comment "default off, doc threat model truoc khi bat" |
| `docs/architecture.md:48` | *"Every module is < 200 LOC"* | **Da sai tu truoc** — sua |
| `docs/architecture.md:20-31` | Module map | Them module moi + so do luong broker |
| `docs/architecture.md:127` | *"waiting could only end in a `timeout` result that misreports..."* | Van dung — nhung them: broker giai quyet bang heartbeat, khong bang cho |
| `SKILL.md:36` | *"`ask` ✅ (polling session only)"* | Cap nhat |
| `SKILL.md:43` | Section heading *"`ask` needs the polling session"* | Doi ten |
| `SKILL.md:47` | *"do not retry"* cho `polling_not_owned` | Van dung (khong co owner song thi retry vo ich) — them `broker_unavailable` |
| `SKILL.md:97` | Giai thich `polling_not_owned` | Cap nhat: co the la broker tat hoac owner chet |
| `docs/security-threat-model.md:12,28,80` | Xem bang tren | |
| `docs/manual-smoke-test.md:32` | `cancel_reminder` tra `{ok}` | **Khong doi** (giu contract `ok`) |

**README muc Multiple sessions — ban viet lai:**
```
Run as many sessions as you like. Exactly one polls Telegram (`getUpdates`
allows a single client per bot token). By default only that session can `ask`.

Opt in to `broker.enabled: true` and every session can `ask`: the non-polling
session writes the question to a small file spool in `~/.mcgram/spool/`, the
polling session posts it and relays your answer back.

| | Polling instance | Other instances |
|---|:---:|:---:|
| send_message / send_file / send_video | ✅ | ✅ |
| reminders (set / list / cancel)       | ✅ | ✅ |
| ask                                    | ✅ | ✅ with broker, ❌ without |

The broker is **off by default** because it changes the trust boundary of `ask`:
any process running as your user can write an answer file. Read
[docs/security-threat-model.md](...) before enabling it.

Without the broker, `ask` fails immediately in a send-only instance (with the
polling pid) — it never blocks on a timeout it can't win. With the broker, it
still fails fast when no live owner is there to serve the request.
```

## Related Code Files

- Modify: `tests/test_multi_instance_stdio.py` — stub server, `_write_config`, `_is_degraded`
- Modify: `README.md` — 7 vi tri theo bang
- Modify: `docs/architecture.md` — 3 vi tri
- Modify: `docs/security-threat-model.md` — 3 dong + muc moi
- Modify: `src/mcgram/data/skill/SKILL.md` — 4 vi tri
- Modify: `CHANGELOG.md`
- Modify: `src/mcgram/cli_doctor.py` — check spool
- Modify: `pyproject.toml` — version bump (+ register marker `flaky` neu can)

## Implementation Steps

1. **Stub Telegram** trong `test_multi_instance_stdio.py`: `http.server` +
   `ThreadingHTTPServer` trong thread, 5 endpoint tren. Sua `_write_config` tro
   `api_root` vao stub. Ghi ly do chon `http.server` vao docstring.

2. **Test 2 process**: A (owner) + B (non-owner) cung config + spool. B goi `ask`
   qua stdio MCP. Test inject update vao stub → A nhan → serve → B nhan answer.
   Them case: **A bi kill → B `ask` fail fast, khong post gi**.

3. **`mcgram doctor`**: them row spool. `broker.enabled: false` → `SKIP` (pattern
   `_skip()` co san, `cli_doctor.py:30-31`). Bat → `✅ spool safe (0700, owned)`
   hoac `⚠️ <reason tu probe()>`. Dat row sau row lock.

4. **README** — 7 vi tri theo bang.

5. **`docs/architecture.md`** — module map + so do luong + sua dong `:48`.

6. **`docs/security-threat-model.md`** — 3 dong + muc spool theo dac ta tren.

7. **`SKILL.md`** — 4 vi tri. Day them huong dan: `broker_unavailable` va
   `polling_not_owned` deu la "khong retry o session nay".

8. **CHANGELOG** — `### Added` broker (opt-in); `### Changed` `list_reminders`
   them field; **khong** co `### Breaking` (contract `ok` giu nguyen) → **minor
   bump** la dung.

9. **Quality gate:**
   ```bash
   uv run pytest -q
   uv run ruff check src/ tests/
   wc -l src/mcgram/*.py src/mcgram/tools/*.py | sort -n | tail -15
   ```
   Doi chieu voi bang LOC tren, khong voi tieu chi cu.

10. **Grep-check truoc khi dong phase:**
    ```bash
    grep -rn "only in the polling session\|AskRegistry exists only\|All modules <200\|Every module is < 200" README.md docs/ src/mcgram/data/skill/
    ```
    Phai tra 0 ket qua.

## Success Criteria

- [ ] Test 2 process that pass tren ca ubuntu va windows CI
- [ ] **Test "owner chet → non-owner fail fast, khong post"** pass
- [ ] `uv run pytest -q` xanh; `ruff check` sach
- [ ] **Moi module MOI < 200 LOC**; file sua khong vuot size class
- [ ] **`README.md:268` va `docs/architecture.md:48` da sua** (claim `<200 LOC` cu)
- [ ] **`docs/security-threat-model.md:28` khong con khang dinh mitigation da mat**
- [ ] Threat model neu ro: question text tren dia, va write ≠ token
- [ ] Grep-check (buoc 10) tra 0 ket qua
- [ ] `mcgram doctor` bao trang thai spool kem reason
- [ ] `SKILL.md` 4 vi tri da cap nhat
- [ ] CHANGELOG: minor bump, khong breaking (contract `ok` giu)
- [ ] **Khong dung `@pytest.mark.flaky` tru khi da register trong pyproject.toml**

## Risk Assessment

| Rui ro | Giam thieu |
|---|---|
| Test 2 process flaky tren Windows CI | Barrier tat dinh thay `sleep`; timeout rong (module da co `timeout(120)`). `flaky` marker chi dung sau khi register |
| Stub Telegram thieu endpoint | Da liet ke du 5; kien truc moi giam so endpoint vi non-owner khong goi Telegram |
| Doc sua thieu cho | Bang doi chieu la checklist; grep-check buoc 10 la chot chan |
| Threat model van con claim sai | 3 dong cu the + grep-check |
| Version bump sai semver | Contract `ok` giu → khong breaking → minor. Neu doi y ve `cancelled` thi phai major |

## Unresolved questions

Khong con. Hai cau truoc day da chot — xem `plan.md` muc "Unresolved questions"
va muc "`redact_text` va spool" ben tren.
