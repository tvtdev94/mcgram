---
title: "Broker cho ask + reminders da session"
description: >-
  File-spool broker cho phep `ask` chay o moi mcgram instance (khong chi
  instance so huu poll lock), va cho phep list/cancel reminders nhin thay
  cross-session. Kien truc owner-does-everything: non-owner chi ghi request,
  owner thuc hien toan bo ask hien co. Opt-in (default off) vi ha trust boundary.
status: pending
priority: P2
branch: "main"
tags:
  - mcp
  - telegram
  - ipc
  - multi-session
blockedBy: []
blocks: []
created: "2026-07-29T14:12:35.641Z"
createdBy: "ck:plan"
source: skill
---

# Broker cho ask + reminders da session

## Overview

`ask` hien chi chay duoc trong 1 process — cai so huu Telegram poll lock. Cac
session khac nhan `polling_not_owned`. Nguyen nhan: `getUpdates` chi cho 1
client/bot token (Telegram rule), nen chi 1 process nhan duoc inbound update;
va `AskRegistry._pending` + `asyncio.Future` la in-process, khong the resolve
tu process khac.

Plan nay them mot **file-spool broker** tai `~/.mcgram/spool/`, theo kien truc
**owner-does-everything**: non-owner **khong** tu post cau hoi. No ghi mot
**request file** roi cho. Owner doc request, goi `AskRegistry.open()` hien co
**nguyen ven**, roi ghi **answer file**. Non-owner watch answer file → tra ket
qua cho Claude.

Reminders dung cung spool nhung khac semantics: moi instance **publish** ban ghi
reminder cua no; `list_reminders` doc union; `cancel_reminder` ghi mot **cancel
request** ma instance so huu reminder do se doc va thuc thi.

**Van giu:** 1 poller duy nhat, khong daemon, khong dependency moi, khong socket.
**Opt-in:** `broker.enabled: false` la default — xem "Security posture" duoi.

## Design decisions (da chot voi user)

| Quyet dinh | Lua chon | Ly do |
|---|---|---|
| IPC | File spool `~/.mcgram/spool/` | Cross-platform sach, khong dependency, giu "no daemon" |
| **Kien truc** | **Owner-does-everything** | Owner goi `AskRegistry.open()` hien co; khong cham `_handle_message`. Xoa ca lop failure mode — xem "Rejected alternatives" |
| **Rollout** | **Default `false`, opt-in** | Broker ha trust boundary cua cong phe duyet — xem "Security posture" |
| Provenance | Answer mang `answered_by_pid`, non-owner cross-check PID chu lock | Bien forgery tu "ghi 1 file" thanh "phai la process giu lock" |
| Freetext routing | Khong doi | Owner dung `_pending` local nhu hien tai; ask remote **la** entry `_pending` |
| `cancel_reminder` contract | Giu `ok`, chi additive | Khong pha public MCP contract (5 consumer) |
| Scope | Ask **va** reminders | User chon mo rong ca hai |

## Security posture — doc truoc khi bat broker

Ban thao dau lap luan: *"ghi duoc spool ⟹ da doc duoc token ⟹ khong tang be mat
tan cong"*. **Lap luan nay sai ve nang luc.** Token cho phep *gui* voi danh
nghia bot; no **khong** cho phep che tao inbound update tu operator.

Hom nay answer chi phat sinh tu Telegram, qua `from_operator`
(`update_dispatcher.py:34-45`), resolve mot `asyncio.Future` **chi co trong RAM**
cua process poll. `docs/security-threat-model.md:28` ghi dung dieu do la
mitigation:

> *"`ask` requires poll ownership and returns `polling_not_owned` immediately.
> The `AskRegistry` exists only in the polling process, so no non-owner can hold
> a pending question."*

Broker xoa mitigation do. Sau plan nay, answer phat sinh tu mot file JSON ma bat
ky process **cung uid** ghi duoc. Threat model liet ke in-scope adversary
(`:17`): *"prompt injection inside Claude's context that tries to abuse the
tools"* — va Claude Code co Bash/Write. SKILL.md day rang `ask` chinh la cong
phe duyet truoc hanh dong nguy hiem.

Hau qua cu the: injected context ghi
`~/.mcgram/spool/asks/q_x.answer.json` = `{"value":"Approve",...}` → Claude tu
phe duyet deploy cua chinh no.

**Quyet dinh:** `broker.enabled` default **`false`**. User bat co y thuc, sau khi
doc trade-off. Cong voi provenance binding (duoi) de forgery de lai dau vet.

**Provenance binding** — khong chong duoc ke cung uid tuyet doi, nhung nang gia:
- Owner ghi answer kem `answered_by_pid: os.getpid()` va `update_id` cua update
  that
- Non-owner **cross-check** `answered_by_pid` == PID trong `~/.mcgram/.lock`
  (`lock._read_pid`, da co). Khong khop → tu choi answer, log warning, coi nhu
  chua co tra loi
- Audit ghi `via: broker` + `answered_by_pid` de phat hien sau

Ke tan cong gio phai la process **giu lock** moi forge duoc — tuc phai gianh
duoc quyen poll, khong chi ghi mot file.

## Rejected alternatives

### Non-owner-posts (ban thao dau — DA BAC)

Non-owner tu `sendMessage`, tu edit message; owner **chi** ghi answer file.

Bac vi cai gia cau truc, khong phai vi khong chay duoc:

| Chi phi | Chi tiet |
|---|---|
| Viet lai `_handle_message` | Union oldest-first tren local `_pending` + remote claim. Reviewer chi ra: mong manh du de plan phai canh bao `sorted` tren tuple khong dong nhat va them test rieng bat `TypeError` |
| Block event loop | `_handle_message` la ham SYNC (`ask_registry.py:105`), duoc `await` tu poll loop → scan spool cho MOI message. Batch N message → N lan scan tuan tu → vuot 25s long-poll → mat update |
| Luat split-brain | Phai quy uoc "owner chi ghi file, non-owner moi edit message". Double-edit chi duoc chan bang quy uoc, khong bang cau truc |
| Mapper index→label | Owner nhan callback cho ask remote phai doc `options` tu claim de map index → label |
| Post truoc khi dang ky | `sendMessage` xay ra TRUOC `open_claim`. Claim fail → operator dang nhin cau hoi khong ai tra loi noi, va `tg_client` **khong co** `delete_message` (da grep: 0 ket qua) nen khong don duoc |
| Leak `options` | Claim phai chua `options` de owner map callback → moi cong phe duyet dang mo tro nen liet ke duoc |
| API mismatch | Ranh gioi phase 2/3 lech: `find_by_message_id` vs `resolve_by_message_id` — `AttributeError` bi dispatcher nuot im lang |

### Owner-does-everything (DA CHON)

Owner goi `AskRegistry.open()` — ham da lam post → await future → edit message →
cleanup thanh mot khoi (`ask_registry.py:47-77`). Ask remote **tro thanh** entry
`_pending` local trong owner.

| Loi | Chi tiet |
|---|---|
| `_handle_message` / `_handle_callback` **khong cham** | Oldest-first hien co (`ask_registry.py:110-117`) da dung; khong co union, khong co `sorted` mong manh, khong scan disk trong handler |
| Khong block event loop | Owner doc request trong task nen rieng, khong trong dispatcher |
| Double-edit bat kha | Chi owner post va edit. Khong phai quy uoc — la cau truc |
| Khong can mapper | Callback resolve future local nhu thuong |
| Khong post truoc khi dang ky | Non-owner ghi request roi moi cho; owner post sau khi da co request |
| Helper giu private | Khong tach `ask_message.py`, khong doi import test (`test_ask_registry.py:11`) |
| Provenance tot hon | Answer do owner ghi — process duy nhat nhan Telegram update |

**Danh doi phai chap nhan:**

1. **Question text + options ghi ra dia.** Ban thao dau khang dinh "cau hoi khong
   ghi ra dia" — voi kien truc nay dieu do khong con dung: owner can question de
   post. Phai neu ro trong threat model, khong duoc lap lai claim cu.
2. **Them do tre ~1 poll interval** truoc khi message xuat hien trong Telegram
   (owner phai tick de doc request). 0.3s — khong dang ke so voi thoi gian nguoi go.
3. **Rate limit va `ask_options_max` ap tren counter cua owner**, khong phai
   caller. Reviewer nhan xet dieu nay "co le con dung hon" — mot bot token, mot
   ngan sach.
4. **Owner phai chong serve trung**: mot request khong duoc phuc vu hai lan.

## Architecture

```
~/.mcgram/spool/
├── asks/
│   ├── q_x.request.json   non-owner ghi: {question, options, chat_id, timeout_s,
│   │                       requester_pid, created_at, expires_at}
│   ├── q_x.taken.json     owner ghi (write_exclusive) truoc khi serve — chong trung
│   └── q_x.answer.json    owner ghi: {value, source, answered_at, update_id,
│                           answered_by_pid, expires_at}
├── heartbeat.json         owner ghi moi tick: {pid, at, protocol}
└── reminders/
    ├── r_y.rec.json       instance so huu publish
    └── r_y.cancel.json    instance khac yeu cau huy

Non-owner (ask.handle):
  1. heartbeat con tuoi? khong -> polling_not_owned (fail fast, KHONG post gi)
  2. ghi request (write_exclusive)
  3. poll answer moi 0.3s cho den timeout
  4. cross-check answered_by_pid == PID chu lock -> tra ket qua
  5. finally: don request + answer

Owner (task nen, khong trong dispatcher):
  1. moi tick: ghi heartbeat, quet request moi
  2. write_exclusive taken -> gianh quyen serve
  3. await AskRegistry.open(...)   <-- HAM HIEN CO, KHONG SUA
  4. ghi answer
```

**Phan cong ranh gioi module** (moi module moi <200 LOC theo CLAUDE.md):

| Module | Trach nhiem |
|---|---|
| `spool.py` | Atomic write/read/list/unlink JSON, TTL sweep, key validation, mode 0600/0700. Khong biet gi ve ask/reminder |
| `ask_broker.py` | Request/answer semantics + heartbeat; phia non-owner (ghi request, cho answer) |
| `ask_server.py` | Phia owner: watch request, gianh `taken`, goi `AskRegistry.open`, ghi answer |
| `reminder_broker.py` | Publish/list/cancel-request semantics cho reminder |
| `reminders.py` | Publish record khi create, thu hoi khi fire/cancel; doc cancel request |
| `tools/ask.py` | Cay quyet dinh: broker path khi khong own polling |
| `tools/list_reminders.py`, `tools/cancel_reminder.py` | Doc/ghi qua broker (giu key `ok`) |
| `config.py` | Them `BrokerConfig` |

**KHONG sua:** `ask_registry.py` (tru khong gi ca — `open()` dung nguyen ven),
`_handle_message`, `_handle_callback`, `polling.py`, `update_dispatcher.py`.

## Phases

| Phase | Name | Status |
|-------|------|--------|
| 1 | [Spool foundation](./phase-01-spool-foundation.md) | Done |
| 2 | [Ask broker core](./phase-02-ask-broker-core.md) | Done |
| 3 | [Wire ask paths](./phase-03-wire-ask-paths.md) | Done |
| 4 | [Reminder broker](./phase-04-reminder-broker.md) | Done (with deviations) |
| 5 | [Docs and verification](./phase-05-docs-and-verification.md) | Done for code, tests, architecture, threat model, README, SKILL; `mcgram doctor` spool row, `config.example.yaml`, CHANGELOG and version bump left to integration |

Deviations and the reasons for them are in
`../reports/stream-a-261109-2148-ask-reminders-report.md`. In short: with the
broker off, `ask` keeps `polling_not_owned` rather than the phase-3
`broker_unavailable` (acceptance requires identical behaviour when off). The
reminder store is always on, because durable reminders came after this plan.
So dead-owner records are adopted rather than reaped, `shutdown()` keeps
records, and `start()`/`shutdown()` stay sync. The owner supervisor lives in
`broker_wiring.py` rather than `poll_ownership.py`.

Phu thuoc tuyen tinh: 1 → 2 → 3 → 5, va 1 → 4 → 5.

## Acceptance criteria

- [ ] `ask` tra ve dung ket qua trong instance **khong** so huu poll lock, ca
      button lan freetext
- [ ] `ask` trong instance so huu di duong in-process cu, khong cham spool
- [ ] **`ask_registry.py` khong bi sua** — `open()`, `_handle_message`,
      `_handle_callback` giu nguyen; test hien co pass khong sua dong nao
- [ ] **Owner chet/khong ton tai → non-owner `ask` fail fast (<1s)**, tra
      `polling_not_owned`, KHONG post gi len Telegram, khong block het timeout
- [ ] **Owner dang 409/backoff → heartbeat cu → fail fast** (khong block)
- [ ] Mot request khong bao gio duoc serve hai lan (`taken` qua `write_exclusive`)
- [ ] Answer voi `answered_by_pid` khong khop PID chu lock bi tu choi + log warning
- [ ] Timeout van la timeout: `source: "timeout"`, request bi don
- [ ] Process chet giua chung → record mo coi bi TTL sweep don, khong ro ri file
- [ ] Sweep chay ke ca khi khong ai poll Telegram (ntfy-only / `disable_polling`)
- [ ] `list_reminders` thay reminder cua session khac, kem `owner_alive: bool`
- [ ] `cancel_reminder` huy duoc reminder cua session khac; **giu key `ok`**
- [ ] `broker.enabled: false` (DEFAULT) → hanh vi giong het truoc plan nay
- [ ] Spool khong ghi duoc **luc boot** → `polling_not_owned`
- [ ] Spool khong ghi duoc **luc runtime** → fail truoc khi post, khong de lai
      cau hoi mo coi trong chat
- [ ] Key spool duoc validate: `"../x"`, `"a/b"`, `"C:evil"` khong tao/xoa file
      ngoai namespace
- [ ] Spool dir mode `0700`, file `0600` tren POSIX; khong theo symlink
- [ ] Toan bo test hien co van pass; ruff clean
- [ ] Moi module **moi** <200 LOC; `config.py`/`server.py` khong tang dang ke

## Rui ro chinh

| Rui ro | Muc | Giam thieu |
|---|---|---|
| Forge answer file (cung uid) | **Cao** | Default off; provenance binding; audit `answered_by_pid`. Neu ro trong threat model |
| Owner chet/backoff → non-owner treo | Cao | Heartbeat + fail fast truoc khi post |
| Question text tren dia | Trung binh | Neu ro trong threat model; mode 0600; TTL ngan; sweep |
| Request serve trung | Trung binh | `taken` qua `write_exclusive` (`O_EXCL`) |
| Record mo coi (kill -9) | Trung binh | Moi record co `expires_at`; sweep xoa ca record **thieu** `expires_at` |
| Spool ngoai `~/.mcgram` (`audit.path` dieu khien `home_dir`) | Cao | Neo `spool_dir` doc lap `audit.path`; `probe()` check uid + mode + symlink |
| Path traversal qua `reminder_id` tu MCP | Trung binh | `path_for` reject key khong match regex |
| Sweep khong chay o deployment khong poll | Trung binh | Sweep task doc lap ownership, trong `_run` |
| Windows AV giu file → `os.replace` fail | Trung binh | Nuot loi, tra `False`; caller degrade |
| Answer ghi trong poll interval cuoi bi bo | Trung binh | Doc lan cuoi **sau** khi thoat vong lap |

## Unresolved questions

Khong con. Bon cau truoc day da chot:

1. **NFS/SMB share** — nhieu may cung `~/.mcgram` qua network share: `os.replace`
   khong dam bao atomic. **Chot: document la unsupported** (YAGNI).
2. **`redact_text` va spool** — **Chot: khong redact field nao trong spool, chi
   document.** Ly do: spool la *kenh truyen*, khong phai log. `value` chinh la ket
   qua non-owner doc len tra cho Claude; `question` la thu owner doc de
   `sendMessage`. Redact bat ky truong nao = vo chuc nang, khong phai "bao ve nua
   voi". Nghia vu con lai la **document trung thuc** — xem phase 5.
3. **Fake Telegram cho test 2 process** — **Chot: `http.server` trong thread rieng**
   (stdlib, khong them dependency), 5 endpoint, tro qua `Settings.api_root`. Chi
   tiet o phase 5. `aiohttp` / `pytest-rerunfailures` deu khong co trong `uv.lock`.
4. **`cli_audit.py` whitelist field** — **Da verify: khong co whitelist.**
   `_summarize` / `_fmt_rejected` / `_tail` deu dung `rec.get()` va in nguyen
   record (`cli_audit.py:113`, `:143`). Them `via` + `answered_by_pid` vao audit
   record **khong** can sua `cli_audit.py`.
