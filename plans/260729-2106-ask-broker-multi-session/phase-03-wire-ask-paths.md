---
phase: 3
title: "Wire ask paths"
status: pending
priority: P1
dependencies: [2]
effort: ""
---

# Phase 3: Wire ask paths

## Overview

Noi `AskBroker` vao `tools/ask.py` (non-owner) va `AskServer` vao `server.py` +
`poll_ownership.py` (owner).

Phase nay **nho hon nhieu** so voi ban thao dau nho kien truc owner-does-everything:
khong tach `ask_message.py`, khong sua `ask_registry.py`, khong viet lai
`_handle_message`/`_handle_callback`, khong doi import test nao.

## Requirements

**Functional:**
- Non-owner: `ask` di duong broker khi broker bat, spool an toan, **va co owner song**
- Non-owner: khong co owner song → `polling_not_owned` **fail fast (<1s), khong
  post gi len Telegram**
- Owner: `AskServer.run()` chay khi va chi khi so huu poll lock
- Sweep chay **doc lap ownership** (ke ca deployment ntfy-only / `disable_polling`)
- Bon ly do tu choi phan biet ro: `telegram_not_configured`, `polling_disabled`,
  `broker_unavailable` (tat/spool khong an toan), `polling_not_owned` (khong owner song)

**Non-functional:**
- `ask.py` hien **182 LOC** (con 18 dong du) → duong broker o module rieng
- Duong in-process cu **khong doi mot dong logic nao**

## Architecture

**Cay quyet dinh trong `ask.handle`:**
```
transport != telegram              -> transport_unsupported     (nhu cu)
state.client is None               -> telegram_not_configured   (nhu cu)
bot.disable_polling                -> polling_disabled          (nhu cu)
state.ask_registry is not None     -> duong in-process cu       (KHONG DOI)
state.ask_broker is None           -> broker_unavailable        (tat / spool khong an toan)
not broker.owner_alive()[0]        -> polling_not_owned + reason (FAIL FAST)
con lai                            -> duong broker
```

`polling_not_owned` **giu nguyen ten** — 18 chuoi tren 7 file khong-generated dang
dua vao no (`SKILL.md:47,97`, `README.md:110,230`, `docs/architecture.md:127`,
`docs/security-threat-model.md:28`, `test_multi_instance_stdio.py:133`). Y nghia
van dung: "khong co session nao co the tra loi cau hoi nay". Chi them `reason` cu
the tu `owner_alive()`.

`broker_unavailable` la ma **moi** cho truong hop khac han: broker tat hoac spool
khong an toan. Phai them vao `_is_degraded` trong
`test_multi_instance_stdio.py:133`.

**Duong broker phia non-owner** (`ask_via_broker.py`):
```python
async def ask_via_broker(state, dest, question, options, timeout_s) -> dict:
    """Ghi request roi cho. KHONG post len Telegram — owner lam viec do.

    Thu tu quan trong: kiem tra owner + ghi request TRUOC khi bat cu gi cham
    Telegram. Ban thao dau post truoc roi moi dang ky, nen khi dang ky that bai
    thi operator dang nhin mot cau hoi khong ai tra loi noi — va `tg_client`
    khong co `delete_message` nen khong don duoc.
    """
    broker = state.ask_broker
    q_id = "q_" + secrets.token_hex(8)
    if not broker.open_request(q_id=q_id, question=question, options=options,
                               chat_id=dest.chat_id, timeout_s=timeout_s):
        return {"error": "broker_unavailable",
                "reason": "khong ghi duoc request vao spool",
                "channel": dest.name}
    try:
        answer = await broker.wait_for_answer(q_id, timeout_s=timeout_s)
    finally:
        broker.close_request(q_id)

    if answer is None:
        return {"value": "", "source": "timeout", "question_id": q_id,
                "channel": dest.name}
    src = answer["source"]
    if src in {"rate_limited", "telegram_error", "internal_error"}:
        # Owner bao loi -> KHONG duoc bao cao la timeout.
        return {"error": src, "reason": answer.get("detail", ""),
                "question_id": q_id, "channel": dest.name}
    return {"value": answer["value"], "source": src,
            "question_id": q_id, "channel": dest.name}
```

Khong co `sendMessage`, khong co `edit_message_*`, khong can `mark_broker_failed`
(ham ban thao dau bay ra ma khong dinh nghia noi — va khong the implement vi
`tg_client` khong co `delete_message`).

**Rang buoc: broker chi chay voi `operator_chat_id`**

`poll_loop` filter update ve dung 1 chat: `server.py:158` truyen
`settings.bot.operator_chat_id`, `polling.py:42` drop moi thu khac qua
`from_operator`. Nen ask tren channel khong phai operator la **khong the resolve**
— du di duong broker hay in-process.

Duong in-process co san lo nay nhung chi 1 session cham duoc. Broker lam no
reachable tu N session, va acceptance criterion khong dieu kien se chung nhan mot
hanh vi khong tong quat. Nen `ask_via_broker` tu choi som:
```python
if dest.chat_id != state.settings.bot.operator_chat_id:
    return {"error": "ask_requires_operator_chat",
            "reason": "chi operator_chat_id nhan duoc reply (poll loop filter "
                      "moi update khac qua from_operator)",
            "channel": dest.name,
            "hint": "dung send_message cho channel nay"}
```

Sua lo nay o duong in-process la change rieng, nho, ngoai scope plan — neu ra
thay vi ke thua am tham.

**Wiring owner (`poll_ownership.py`):** `AskServer.run()` chi chay khi so huu
lock. `_ensure_registry` da tao/tai dung `AskRegistry`; them `AskServer` canh no.

**Canh bao tu Scope Auditor:** `_registry` duoc **cache va tai dung** qua
demotion/re-promotion (`poll_ownership.py:126`, chot boi
`test_poll_ownership.py:109`). Neu inject broker qua `__init__` thi reference bi
dong bang o lan promote dau — broker `None` luc do se `None` mai. Va
`UpdateDispatcher` chi co `register`, khong co unregister
(`update_dispatcher.py:23`), nen handler cung khong swap duoc.

→ **Doc `state.ask_server` tai thoi diem goi**, khong dong bang trong closure.
`AskServer` tao moi moi lan promote (nhe, khong giu state ngoai `_serving`), con
`AskRegistry` van tai dung nhu hien tai.

```python
async def supervise(self, state, poll, *, retry_interval_s=None):
    ...
    self.sync(state)
    server_task = None
    if state.ask_server is not None:
        server_task = asyncio.create_task(state.ask_server.run(),
                                          name="mcgram-ask-server")
    try:
        await poll()
    finally:
        await cancel_task(server_task)      # ham da co (poll_ownership.py:176)
        self.release()
        self.sync(state)
```

**Sweep doc lap ownership:** `_start_polling` tra `(None, None)` khi
`state.client is None` (ntfy-only) hoac `bot.disable_polling`
(`server.py:143-152`) → khong co `supervise`, khong co sweeper. Nhung reminder
van hoat dong day du o ca hai truong hop (`reminders.py:92-94`), va phase 4 van
publish record.

→ Sweep task o `_run`, khong trong `supervise`.

## Related Code Files

- Create: `src/mcgram/ask_via_broker.py`
- Modify: `src/mcgram/tools/ask.py` — cay quyet dinh, goi `ask_via_broker`
- Modify: `src/mcgram/runtime.py` — them `ask_broker`, `ask_server` vao `AppState`
- Modify: `src/mcgram/server.py` — khoi tao spool/broker/server, sweep task
- Modify: `src/mcgram/poll_ownership.py` — chay `AskServer.run()` khi so huu
- Modify: `tests/test_tools_ask.py` — 5 site `AskRegistry()` (`:14,21,39,45,61`)
  van zero-arg, khong doi; them test duong broker
- Modify: `tests/test_poll_ownership.py:163` — `test_ask_fails_fast_when_not_polling`
- Modify: `tests/test_multi_instance_stdio.py:133` — `_is_degraded` them
  `broker_unavailable`
- Create: `tests/test_ask_broker_e2e.py`

**KHONG sua:** `ask_registry.py`, `tests/test_ask_registry.py` (ke ca dong `:11`
import `_build_keyboard` — helper khong bi di chuyen nua), `polling.py`,
`update_dispatcher.py`.

## Implementation Steps

1. **`AppState`** them `ask_broker` va `ask_server` (ca hai `Any | None = None`),
   kem comment giai thich lifetime: set 1 lan luc boot; `ask_server` chi hoat dong
   khi so huu lock.

2. **`server._run`** khoi tao:
   ```python
   def _build_broker_pair(settings, state):
       """(broker, server) hoac (None, None). Log ly do khi tat."""
       if not settings.broker.enabled:
           return None, None          # default off
       spool = Spool(settings.spool_dir, "asks")
       ok, reason = spool.probe()
       if not ok:
           log.warning("broker tat: spool %s khong dung duoc (%s). `ask` chi chay "
                       "trong instance so huu poll lock.", settings.spool_dir, reason)
           return None, None
       broker = AskBroker(spool, settings.lock_path, ...)
       return broker, spool           # server tao sau, can registry
   ```
   `AskServer` can `registry` nen tao trong `_ensure_registry` hoac ngay sau
   `ownership.attach`. Chot mot cho khi implement.

3. **`ask_via_broker.py`** theo doan code tren, gom check `operator_chat_id`.

4. **`tools/ask.py`**: cay quyet dinh moi. `_polling_error_detail` them nhanh
   `broker_unavailable`, va nhanh `polling_not_owned` gio kem `reason` tu
   `owner_alive()`:
   ```python
   alive, reason = broker.owner_alive()
   # reason: "heartbeat cu 47s (owner pid=1234 co the da chet)"
   ```
   Giu `poll_owner_pid` trong payload nhu hien tai.

5. **Audit**: duong broker ghi `via: broker` + `answered_by_pid`. **Da verify:
   `cli_audit.py` khong whitelist field** — `_summarize` / `_fmt_rejected` /
   `_tail` deu dung `rec.get()` va in nguyen record (`cli_audit.py:113`, `:143`).
   Khong can sua `cli_audit.py`.

6. **Rate limit**: ap o **owner** (`AskServer._serve_one`), khong o non-owner —
   ngan sach thuoc bot token. Non-owner nhan `source: "rate_limited"` va tra loi
   ro. Ghi ro trong docstring de nguoi doc khong tuong bi bo sot.

7. **Sweep task** trong `_run`, doc lap ownership, `try/except` trong vong lap.

8. **Tests:**
   - `test_tools_ask.py`: test hien co pass **khong sua**; them: broker path tra
     dung gia tri; `broker_unavailable` khi `state.ask_broker is None`
   - `test_poll_ownership.py:163`: giu khang dinh `elapsed < 1.0`; doi assert
     sang `broker_unavailable` khi khong co broker
   - `test_ask_broker_e2e.py`:
     - `AskBroker` + `AskServer` + `AskRegistry` that: button resolve qua
       `_handle_callback` **khong sua**
     - freetext resolve qua `_handle_message` **khong sua**
     - **khong co heartbeat → `ask` fail fast <1s, `send_message` khong duoc goi**
       (chot chan quan trong nhat: khong post cau hoi mo coi)
     - **heartbeat cu → fail fast** tuong tu
     - **owner pid chet → fail fast**
     - answer voi pid sai → bi tu choi, ask tiep tuc cho roi timeout
     - `broker.enabled: false` (default) → `broker_unavailable`
     - `probe()` sai → `broker_unavailable` kem reason trong log
     - `ask` tren channel khong phai operator → `ask_requires_operator_chat`
   - `test_multi_instance_stdio.py`: `_is_degraded` nhan ca hai ma

## Success Criteria

- [ ] `ask` chay duoc trong non-owner: button va freetext deu tra dung gia tri
- [ ] **`ask_registry.py` khong bi sua; `tests/test_ask_registry.py` khong bi sua**
- [ ] **Khong co owner song → fail fast <1s, KHONG goi `send_message`** (co test)
- [ ] Duong in-process cu khong doi mot dong logic nao
- [ ] `polling_not_owned` giu nguyen ten (18 chuoi tren 7 file van dung)
- [ ] `broker_unavailable` duoc them vao `_is_degraded`
- [ ] Answer loi (`rate_limited`/`telegram_error`) khong bi bao cao la `timeout`
- [ ] `ask` tren channel khong phai operator bi tu choi som, co ma rieng
- [ ] `AskServer` doc `state.ask_server` tai thoi diem goi (khong dong bang)
- [ ] Sweep task chay ke ca khi khong ai poll
- [ ] `ask.py` khong vuot 200 LOC
- [ ] Audit phan biet `via: broker`

## Risk Assessment

| Rui ro | Giam thieu |
|---|---|
| Post cau hoi ma khong ai tra loi noi | Khong post o non-owner. Owner chi post sau khi da co request |
| Broker reference dong bang qua re-promotion | Doc `state.ask_server` tai thoi diem goi |
| `AskServer.run()` con chay sau khi mat lock | `cancel_task` trong `finally` cua `supervise` |
| Loi owner bi bao cao thanh timeout | `_serve_one` luon ghi answer kem `source` phan biet; non-owner map sang error |
| `cli_audit` khong tolerate field moi | Buoc 5 kiem truoc; them neu can |
| `ask.py` phinh qua 200 LOC (dang 182) | Duong broker o module rieng ngay tu dau |
| Sweep khong chay o deployment khong poll | Sweep task trong `_run`, doc lap ownership |
