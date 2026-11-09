---
phase: 4
title: "Reminder broker"
status: pending
priority: P2
dependencies: [1]
effort: ""
---

# Phase 4: Reminder broker

## Overview

`list_reminders` thay reminder cua moi session; `cancel_reminder` huy duoc
reminder do session khac tao. Semantics khac ask: reminder khong co "cho tra
loi" — chi la publish state + cancel mailbox.

Doc lap voi phase 2-3 sau khi phase 1 xong.

## Requirements

**Functional:**
- Instance tao reminder → publish record vao spool
- `list_reminders` tra union: local + session khac, **kem `owner_alive: bool`**
- `cancel_reminder` id local → huy ngay (duong cu)
- `cancel_reminder` id remote → ghi cancel request; owner doc va thuc thi trong
  1 poll interval
- **`cancel_reminder` giu key `ok`** — khong pha public contract
- Reminder fire hoac bi huy → record bi thu hoi
- **Record cua owner da chet bi loc ngay**, khong doi grace

**Non-functional:**
- Reminder khong bao gio fire hai lan (moi instance chi fire reminder cua chinh no)
- **Khong tao `asyncio.Task` trong `__init__`** — constructor hien thuan sync
- `reminder_broker.py` < 200 LOC; `reminders.py` van < 200 LOC (dang 136)
- Spool loi → degrade ve hanh vi per-session hien tai

## Ba sua bat buoc sau red team

### 1. `owner_alive` thay vi grace 1h (F5, ASM-F7)

Ban thao dau: `expires_at = fires_at + 3600`. Nhung `reminder_max_delay_s` default
**86400** (`config.py:80`) → record cho reminder 24h song toi **~25 gio** bat ke
process giu `asyncio.Task` con ton tai hay khong.

Nang hon: **khong co signal handler nao trong `src/`** (da grep `signal.`: 0 ket
qua). `_shutdown_phase3` nam trong `finally` cua `_run` (`server.py:129,221-223`)
→ SIGTERM mac dinh terminate → `finally` KHONG chay. Docker stop, systemd, IDE
restart deu la SIGTERM.

Ket qua: `list_reminders` bao mot reminder se khong bao gio fire, suot 25h. Hom
nay hanh vi trung thuc do cau truc — session B khong hien gi va `README.md:244`
noi ro ly do. Ban thao dau thay mot limitation trung thuc bang mot false positive.

```python
def list_all(self) -> list[dict]:
    """Record con han. Record cua owner da chet bi reap ngay, khong doi grace."""
    out = []
    for key, rec in self._spool.list("rec"):
        if not _pid_alive(rec.get("pid", 0)):
            self._spool.remove(key, "rec", "cancel")   # reap ngay
            continue
        out.append(rec)
    return out
```

`_pid_alive` da co, cross-platform (`lock.py:14-56`) — dung no, dung viet lai.
`fires_at + grace` giu lam backstop cho truong hop PID bi tai su dung.

### 2. Cancel request phai co `expires_at` (F6)

Ban thao dau: `.cancel.json` = `{"requested_by", "at"}` — khong `expires_at` →
theo invariant phase 1 (ban dau) khong bao gio bi sweep.
`take_cancel_requests(rids)` chi tra rid **nam trong** danh sach truyen vao
(`list(self._tasks)`), nen rid khong thuoc process nao dang song → khong ai lay,
khong ai xoa.

Duong ro ri la duong **binh thuong**, khong can crash: cancel toi dung luc
`_fire()` vua xong → `finally` (`reminders.py:109-111`) pop khoi `_tasks` va
retract record → tick sau `take_cancel_requests` khong thay rid → file o lai vinh
vien.

Sua: `expires_at = at + cancel_ttl_s`. Va `take_cancel_requests` cung xoa file cua
rid **het han** du khong thuoc `_tasks`.

**Bo tuyen bo "doc va xoa nguyen tu"** — read-then-unlink khong nguyen tu. Dung
la: one-shot best-effort (unlink ngay sau read, cua so trung rat nho va hau qua
la cancel duoc thuc thi hai lan — vo hai vi `cancel()` idempotent).

### 3. Khong `create_task` trong `__init__` (F11)

`ReminderScheduler.__init__` hien **thuan sync**, khong tao task nao
(`reminders.py:33-46`); task chi tao trong `create()` (`:69`) von luon goi tu
async context. Dat `create_task` vao `__init__` bien constructor thanh
loop-dependent.

12 instantiation site, trong do `tests/test_tools_reminder.py:11` (`_wire()`) la
ham **sync**, va `scripts/live-smoke.py:41` **khong** co trong file list ban thao
dau.

Nang hon: `shutdown()` la sync (`reminders.py:131`) nen khong `await` duoc task
cancel → `Task was destroyed but it is pending!`, va `_watch_cancels` co the bi cat
giua `take_cancel_requests` → cancel request mat han, reminder van fire du user da
huy.

**Quyet dinh (da chot voi user): doi `shutdown()` sang async.**

```python
# reminders.py
async def start(self) -> None:
    """Khoi dong watch-cancel task. Goi tuong minh tu `_run` sau `_wire_phase3`.

    KHONG dat trong `__init__`: constructor la sync va co 12 call site, mot so
    la ham sync (tests/test_tools_reminder.py:11) — `create_task` o day se raise
    RuntimeError khi khong co running loop.
    """
    if self._broker is not None and self._watch_task is None:
        self._watch_task = asyncio.create_task(self._watch_cancels(),
                                               name="mcgram-reminder-cancels")

async def shutdown(self) -> None:
    """Async: can await task cancel va retract record qua I/O."""
    await cancel_task(self._watch_task)      # ham da co (poll_ownership.py:176)
    self._watch_task = None
    for rid, task in list(self._tasks.items()):
        task.cancel()
        self._tasks.pop(rid, None)
        self._meta.pop(rid, None)
        if self._broker is not None:
            self._broker.retract(rid)
```

`_shutdown_phase3` (`server.py:221`) doi thanh `async def`, `await` trong
`finally` cua `_run` (`:129`). 12 call site: 10 test + `live-smoke.py` +
`server.py`. Site nao goi `shutdown()` phai them `await` — **da liet ke duoi**.

**Vong watch phai chong loi:**
```python
async def _watch_cancels(self) -> None:
    """Doc mailbox NGAY lan dau roi moi sleep (ban thao dau sleep truoc, tre
    them 1 interval). Mot loi I/O khong duoc giet task."""
    while True:
        try:
            for rid in self._broker.take_cancel_requests(list(self._tasks)):
                self.cancel(rid)
        except Exception:
            log.exception("watch_cancels tick failed")
        await asyncio.sleep(self._poll_interval_s)
```

## Contract `cancel_reminder`: giu `ok` (F9, ASM-F2)

Ban thao dau doi `{"ok": bool}` → `{"cancelled": bool}`. Day la **breaking
change** cho public MCP tool, **5 consumer**:

| Consumer | Vi tri |
|---|---|
| Schema description | `tools/cancel_reminder.py:15` — *"Returns {ok: bool}"* |
| Return | `tools/cancel_reminder.py:33` — `return {"ok": ok}` |
| Test exact-equality | `tests/test_tools_reminder.py:23` — `== {"ok": True}` |
| Test exact-equality | `tests/test_tools_reminder.py:41` — `== {"ok": False}` |
| Skill | `src/mcgram/data/skill/SKILL.md:25` |
| Docs | `README.md:107`, `docs/manual-smoke-test.md:32` |

Va success criterion cua ban thao dau lai noi "test hien co pass khong sua" —
khong the, vi 2 test assert exact dict equality.

**Quyet dinh (da chot voi user): giu `ok`, chi additive.**

```
local huy duoc            -> {"ok": true}
remote, ghi request duoc  -> {"ok": true, "via": "broker",
                              "note": "yeu cau da gui toi session so huu (pid=N)"}
khong dau ca              -> {"ok": false, "reason": "unknown_reminder"}
```

**Bo `already_fired`** — `ReminderScheduler.cancel` tra `bool` va
`self._tasks.pop(rid, None) is None` cho **ca** "da fire" lan "khong ton tai"
(`reminders.py:113-119`). Hai reason, mot tin hieu. Muon phan biet that thi phai
them `_fired: set[str]` co TTL vao scheduler — ngoai scope. Khong hua mot reason
khong ton tai.

**`via: broker` la "yeu cau da gui", khong phai "da huy".** Wording phai trung
thuc: reminder co the fire ngay truoc do.

## Architecture

**Record payload** (`r_y.rec.json`):
```jsonc
{
  "rid": "r_1a2b3c4d",
  "text": "check container logs",
  "fires_at": 1785003600.0,
  "channel": "oncall",
  "transport": "telegram",
  "pid": 31337,
  "expires_at": 1785007200.0   // fires_at + 1h — BACKSTOP; owner_alive la chinh
}
```

**Cancel request** (`r_y.cancel.json`):
```jsonc
{"requested_by": 31338, "at": 1785000000.0, "expires_at": 1785000300.0}
```

**API:**
```python
class ReminderBroker:
    def publish(self, rid, meta) -> bool
    def retract(self, rid) -> None
    def list_all(self) -> list[dict]                   # reap owner chet
    def request_cancel(self, rid) -> bool
    def take_cancel_requests(self, rids) -> list[str]  # + xoa rid het han
    def sweep(self) -> int
```

**`list_reminders` output** — them 3 truong:
```jsonc
{"id": "r_...", "text": "...", "fires_at": "...", "channel": "...",
 "transport": "telegram", "owner_pid": 31337, "local": false, "owner_alive": true}
```

**`reminder_max_pending`** giu per-process (`len(self._tasks)`). Dem cross-session
se lam mot session khong tao duoc reminder vi session khac da day — kho hieu. Ghi
ro trong docstring.

**Sweep**: goi trong `_watch_cancels` (task chay o **moi** instance) — dung vong
lap can, va khong phu thuoc `supervise` (von khong ton tai o deployment ntfy-only
/ `disable_polling`, `server.py:143-152`). Day la fix F7 cho phia reminder.

## Related Code Files

- Create: `src/mcgram/reminder_broker.py`
- Create: `tests/test_reminder_broker.py`
- Modify: `src/mcgram/reminders.py` — publish/retract, `start()`, `shutdown()` async, `_watch_cancels`
- Modify: `src/mcgram/tools/list_reminders.py` — union + `local`/`owner_alive`
- Modify: `src/mcgram/tools/cancel_reminder.py` — fallback broker, **giu key `ok`**
- Modify: `src/mcgram/server.py` — truyen broker, `await scheduler.start()`,
  `_shutdown_phase3` thanh async
- Modify: `scripts/live-smoke.py:41` — **khong co trong ban thao dau**; kiem
  `shutdown()` co duoc goi khong, them `await` neu co
- Modify: `tests/test_reminders.py` — 8 site (`:60,76,88,98,109,117,124,132`)
- Modify: `tests/test_poll_ownership.py:281,285` — `shutdown()` thanh `await`
- Modify: `tests/test_tools_reminder.py:11` — `_wire()` sync; giu sync duoc vi
  `start()` khong con trong `__init__`

## Implementation Steps

1. **`reminder_broker.py`** theo API tren. `Spool(settings.spool_dir, "reminders")`
   — namespace khac ask.

2. **`ReminderScheduler`** nhan `broker` (mac dinh `None` — che 10 test site +
   `live-smoke.py`):
   - `create()`: sau khi tao task, `broker.publish(rid, meta)` voi
     `expires_at = fires_at + 3600`
   - `_fire()` trong `finally`: `broker.retract(rid)`
   - `cancel()`: sau khi huy task, `broker.retract(rid)`
   - `start()` async, tuong minh — **khong** trong `__init__`
   - `shutdown()` async, `await cancel_task(self._watch_task)`, retract het
   - `_watch_cancels` doc ngay lan dau, `try/except` trong vong, goi
     `broker.sweep()` moi N tick

3. **`server.py`**: `await state.reminders.start()` sau `_wire_phase3`;
   `_shutdown_phase3` thanh `async def`, `await` trong `finally`.

4. **`tools/list_reminders.py`** — union, danh dau `local`:
   ```python
   local = state.reminders.list()
   local_ids = {r["id"] for r in local}
   remote = [r for r in broker.list_all() if r["rid"] not in local_ids]
   ```
   Local thang khi trung id (phong thu).

5. **`tools/cancel_reminder.py`** — giu key `ok` theo bang tren. **Khong** sua
   schema description (`:15`) vi contract khong doi. Them `via`/`note` khi qua broker.

6. **Kiem `scripts/live-smoke.py`** co goi `shutdown()` khong — neu co, them
   `await` (file nay khong co trong ban thao dau, red team chi ra).

7. **Tests:**
   - `test_reminder_broker.py`: publish → `list_all` thay; retract → khong con;
     **`list_all` reap record co pid chet**; request_cancel + take round-trip;
     `take` xoa file (goi lan 2 rong); `take` chi tra rid trong danh sach; **`take`
     xoa file cua rid het han du khong thuoc `_tasks`** (fix ro ri F6); sweep don
     record qua han
   - `test_reminders.py`: scheduler co broker → create publish, fire retract,
     `shutdown()` retract het; **`__init__` khong tao task** (khang dinh goi
     `ReminderScheduler(...)` ngoai async context khong raise); `start()` tao task;
     `_watch_cancels` khong chet khi broker raise
   - `test_tools_reminder.py`: test hien co pass **khong sua assert** (`ok` giu
     nguyen); them: `list_reminders` thay reminder cua "session khac" kem
     `owner_alive`; `cancel_reminder` remote tra `{"ok": true, "via": "broker", ...}`
     va scheduler kia nhan duoc trong 1 interval
   - Cac site goi `shutdown()` doi thanh `await` — 3 file

## Success Criteria

- [ ] `list_reminders` thay reminder session khac, co `local`, `owner_pid`, `owner_alive`
- [ ] **Record cua owner chet bi reap ngay** (khong bao 25h nhu ban thao dau)
- [ ] `cancel_reminder` remote → session so huu huy trong 1 poll interval
- [ ] **`cancel_reminder` giu key `ok`; 2 test exact-equality pass khong sua**
- [ ] **Khong hua `already_fired`** (khong dan xuat duoc tu API hien co)
- [ ] **`__init__` khong tao task** — construct ngoai async context khong raise
- [ ] `start()`/`shutdown()` async; moi call site da `await`
- [ ] **Cancel request het han bi xoa du khong thuoc `_tasks`** (khong ro ri)
- [ ] `_watch_cancels` doc ngay lan dau; loi khong giet task
- [ ] Reminder fire dung 1 lan, boi dung instance tao ra no
- [ ] Sweep chay trong `_watch_cancels` — khong phu thuoc `supervise`
- [ ] `broker.enabled: false` (default) → hanh vi per-session giong hien tai
- [ ] Test reminder hien co pass khong sua logic (chi them `await`)
- [ ] `reminder_broker.py` < 200 LOC; `reminders.py` < 200 LOC

## Risk Assessment

| Rui ro | Giam thieu |
|---|---|
| Reminder fire hai lan | Chi instance so huu co task; instance khac khong fire tu record |
| Cancel request thuc thi lai | `take` unlink ngay sau read; `cancel()` idempotent nen vo hai |
| Record ma sau SIGTERM/kill -9 | `_pid_alive` reap ngay; `fires_at + 1h` la backstop |
| `shutdown()` async pha 12 call site | Da liet ke het; 10 la test, deu async tru `_wire()` (khong goi shutdown) |
| `reminders.py` phinh qua 200 LOC (dang 136) | `start`/`_watch_cancels` ~20 LOC; theo doi, tach neu vuot |
| Nguoi dung tuong `via: broker` = da huy | Wording "yeu cau da gui", khong khang dinh ket qua |
| PID tai su dung → reap oan record song | `fires_at + 1h` backstop; rui ro rat thap, chap nhan |
