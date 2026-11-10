---
phase: 2
title: "Ask broker core"
status: pending
priority: P1
dependencies: [1]
effort: ""
---

# Phase 2: Ask broker core

## Overview

Hai nua cua broker, **khong** sua `ask_registry.py`:

- `ask_broker.py` — phia **non-owner**: heartbeat check, ghi request, cho answer,
  cross-check provenance
- `ask_server.py` — phia **owner**: ghi heartbeat, quet request, gianh `taken`,
  goi `AskRegistry.open()` hien co, ghi answer

Phase nay chi xay hai module + test don vi. Noi vao `tools/ask.py` va `server.py`
la phase 3.

## Requirements

**Functional:**
- Non-owner: **fail fast khi khong co owner song** (truoc khi lam bat cu gi)
- Non-owner: ghi request, cho answer, dep sach du ket qua la answer hay timeout
- Non-owner: **tu choi answer co `answered_by_pid` khong khop PID chu lock**
- Owner: ghi heartbeat moi tick
- Owner: quet request, **gianh doc quyen** truoc khi serve (khong serve trung)
- Owner: goi `AskRegistry.open()` **nguyen ven** — khong tai implement post/edit
- Owner: ghi answer kem `update_id`, `answered_by_pid`, `expires_at`
- Request/answer/taken het han bi don khi sweep

**Non-functional:**
- Ca hai phia dung `asyncio.sleep`, khong block event loop
- **Owner quet request trong task nen rieng**, KHONG trong dispatcher
- `ask_broker.py` < 200 LOC, `ask_server.py` < 200 LOC
- Moi loi I/O → coi nhu khong co answer, de timeout xu ly

## Heartbeat: fail fast khi khong ai serve duoc

Ban thao dau route sang broker chi vi "spool ghi duoc", khong hoi *"co owner
song nao se doc request cua toi khong?"*. Ba duong dan den block het `timeout_s`
roi tra `source: "timeout"` sai su that:

1. **Owner dang 409/backoff** — `polling.py:52-67` sleep 10s moi 409;
   `polling.py:70-74` backoff toi 30s. Owner giu lock, `AskRegistry` van attached,
   nhung khong nhan update nao
2. **Owner chet, lock chua bi thu hoi** — `poll_ownership.py:44`
   `RETRY_INTERVAL_S = 30.0`. Trong cua so do non-owner thay spool ghi duoc va block
3. **Lech version** — user upgrade 1 session, khong upgrade session kia. Owner cu
   khong bao gio doc request

`tools/ask.py:5-9` va `docs/architecture.md:127` noi ro day la dieu **te hon mot
loi**: `source: "timeout"` bao voi Claude rang operator da phot lo cau hoi.
`tests/test_poll_ownership.py:178` va `test_multi_instance_stdio.py:191,194` dang
chot chan dieu nay.

**Heartbeat record** (`heartbeat.json`, khong theo key pattern nen dung namespace
rieng hoac kind co dinh):
```jsonc
{"pid": 31337, "at": 1785000000.0, "protocol": 1,
 "expires_at": 1785000090.0}
```

Owner ghi moi tick cua watch loop (≤ `poll_interval_s`). `heartbeat_stale_s`
default 90s — phai lon hon 25s long-poll + 10s conflict backoff + slack, de owner
dang long-poll binh thuong khong bi coi la chet.

```python
def owner_alive(self) -> tuple[bool, str]:
    """(alive, reason). Kiem tra truoc khi ghi request."""
    hb = self._spool.read_raw("heartbeat")
    if hb is None:
        return False, "khong co owner nao dang chay broker"
    if hb.get("protocol") != PROTOCOL_VERSION:
        return False, f"owner chay protocol {hb.get('protocol')}, can {PROTOCOL_VERSION}"
    age = time.time() - hb.get("at", 0)
    if age > self._heartbeat_stale_s:
        return False, f"heartbeat cu {age:.0f}s (owner pid={hb.get('pid')} co the da chet)"
    if not _pid_alive(hb["pid"]):
        return False, f"owner pid={hb['pid']} khong con chay"
    return True, ""
```

`_pid_alive` da co san, cross-platform (`lock.py:14-56`) — import, khong viet lai.
`protocol` chan lech version: owner cu khong ghi field nay → non-owner tu choi
ngay thay vi cho vo vong.

## Provenance: chi process giu lock moi tra loi duoc

Answer mang `answered_by_pid`. Non-owner cross-check voi PID trong
`~/.mcgram/.lock` (`lock._read_pid`, da co):

```python
def _answer_trusted(self, answer: dict) -> bool:
    """Answer phai do process dang giu poll lock ghi.

    Khong chan duoc ke cung uid tuyet doi (no doc duoc lock file), nhung nang
    forgery tu "ghi mot file" thanh "phai gianh duoc quyen poll". Va de lai dau
    vet trong audit.
    """
    claimed = answer.get("answered_by_pid")
    if claimed is None:
        log.warning("answer thieu answered_by_pid — tu choi")
        return False
    holder = _read_pid(self._lock_path)
    if holder is None or claimed != holder:
        log.warning("answer tu pid=%s nhung lock thuoc pid=%s — tu choi (co the "
                    "la answer gia mao hoac ownership vua chuyen)", claimed, holder)
        return False
    return True
```

Tu choi → coi nhu chua co answer, tiep tuc cho. Neu ownership vua chuyen that
thi owner moi se serve lai request (request van con trong spool).

## Architecture

**Request payload** (`q_x.request.json`):
```jsonc
{
  "q_id": "q_a1b2c3d4e5f6g7h8",   // token_hex(8) = 64-bit
  "question": "Deploy to prod?",   // CO tren dia — owner can de post
  "options": ["Yes", "No"],        // null neu freetext-only
  "chat_id": 123456789,
  "timeout_s": 120,
  "requester_pid": 31338,
  "created_at": 1785000000.0,
  "expires_at": 1785000150.0       // created_at + timeout_s + request_ttl_pad_s
}
```

**Taken marker** (`q_x.taken.json`) — `write_exclusive`, chong serve trung:
```jsonc
{"owner_pid": 31337, "at": 1785000000.2, "expires_at": 1785000150.0}
```

**Answer payload** (`q_x.answer.json`):
```jsonc
{"value": "Yes", "source": "button", "answered_at": 1785000012.3,
 "answered_by_pid": 31337, "expires_at": 1785000312.3}
```

**API — non-owner (`ask_broker.py`):**
```python
class AskBroker:
    def __init__(self, spool: Spool, lock_path: Path, *, poll_interval_s: float,
                 ttl_pad_s: int, heartbeat_stale_s: int): ...

    def owner_alive(self) -> tuple[bool, str]
    def open_request(self, *, q_id, question, options, chat_id, timeout_s) -> bool
    async def wait_for_answer(self, q_id, *, timeout_s) -> dict | None
    def close_request(self, q_id) -> None      # xoa request + taken + answer
```

**API — owner (`ask_server.py`):**
```python
class AskServer:
    """Doc request tu spool va serve bang AskRegistry hien co.

    Chay trong task nen rieng — KHONG trong dispatcher, de khong block poll loop.
    """
    def __init__(self, spool: Spool, registry, client, settings, audit, *,
                 poll_interval_s: float, answer_ttl_s: int): ...

    async def run(self) -> None          # vong lap: heartbeat + quet + serve
    def write_heartbeat(self) -> None
    async def _serve_one(self, req: dict) -> None
    def sweep(self) -> int
```

**Vong lap owner:**
```python
async def run(self) -> None:
    """Heartbeat + quet request. Moi request serve trong task rieng de mot ask
    dai khong chan cac ask khac."""
    while True:
        try:
            self.write_heartbeat()
            for key, req in self._spool.list("request"):
                if self._expired(req) or key in self._serving:
                    continue
                # Gianh doc quyen TRUOC khi serve. write_exclusive dua tren
                # O_EXCL nen nguyen tu ca khi hai owner cung ton tai
                # (MCGRAM_SKIP_LOCK=1 lam duoc dieu do).
                if not self._spool.write_exclusive(key, "taken", {
                    "owner_pid": os.getpid(), "at": time.time(),
                    "expires_at": req["expires_at"],
                }):
                    continue
                self._serving.add(key)
                asyncio.create_task(self._serve_one(req), name=f"mcgram-ask-{key}")
        except Exception:
            log.exception("ask_server tick failed")   # mot loi khong duoc giet vong lap
        await asyncio.sleep(self._poll_interval_s)
```

**Serve mot request — day la cho `AskRegistry.open()` duoc dung nguyen ven:**
```python
async def _serve_one(self, req: dict) -> None:
    q_id = req["q_id"]
    try:
        if not self._rate.try_acquire("ask"):
            # Ngan sach la cua bot token, khong cua caller -> ap o owner.
            self._write_answer(q_id, "", "rate_limited")
            return
        result = await self._registry.open(
            self._client,
            chat_id=req["chat_id"],
            question=req["question"],
            options=req["options"],
            timeout_s=req["timeout_s"],
        )
        self._write_answer(q_id, result["value"], result["source"])
    except TelegramError as e:
        self._write_answer(q_id, "", "telegram_error", detail=e.description)
    except Exception:
        log.exception("serve ask %s failed", q_id)
        self._write_answer(q_id, "", "internal_error")
    finally:
        self._serving.discard(q_id)
```

`registry.open()` lo toan bo: post message, tao `_pending` entry, await future,
edit message khi tra loi/timeout, cleanup. Callback va freetext resolve qua
`_handle_callback`/`_handle_message` **khong sua**, vi ask remote gio **la** entry
`_pending` local.

**Answer luon duoc ghi**, ke ca duong loi — neu khong, non-owner cho vo vong den
timeout roi bao "operator phot lo" trong khi that ra Telegram loi.

**Wait loop — co lan doc cuoi sau vong lap:**
```python
async def wait_for_answer(self, q_id: str, *, timeout_s: int) -> dict | None:
    """Poll answer. None = timeout.

    Doc lan cuoi SAU khi thoat vong lap: neu owner ghi answer trong
    poll_interval cuoi, ban thao dau se bo mat no roi `close_request` unlink
    answer chua doc -> cu tap cua operator bi am tham nuot.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        answer = self._spool.read(q_id, "answer")
        if answer is not None and self._answer_trusted(answer):
            return answer
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(self._poll_interval_s, remaining))
    # Lan doc cuoi — dong cua so mu.
    answer = self._spool.read(q_id, "answer")
    if answer is not None and self._answer_trusted(answer):
        return answer
    return None
```

`time.monotonic()` cho deadline (khong lech khi NTP chinh gio); `time.time()` cho
`created_at`/`expires_at` (phai so sanh cross-process).

**Khong con van de clock skew.** Ban thao dau nhan ban guard `msg_ts >
posted_at` cross-process, gay bug loai claim vinh vien. Voi kien truc nay,
`posted_at` do **owner** ghi khi goi `registry.open()`, cung process so sanh voi
`msg.date` — dung nhu hom nay. Bug clock-skew san co
(`ask_registry.py:111`) khong bi nhan ban; sua no la viec rieng, ngoai scope plan.

## Related Code Files

- Create: `src/mcgram/ask_broker.py`
- Create: `src/mcgram/ask_server.py`
- Create: `tests/test_ask_broker.py`
- Create: `tests/test_ask_server.py`

## Implementation Steps

1. **`ask_broker.py`**: `owner_alive`, `open_request` (`write_exclusive`),
   `wait_for_answer` (co lan doc cuoi + `_answer_trusted`), `close_request`.
   Import `_pid_alive`, `_read_pid` tu `lock.py` — khong viet lai.

2. **`ask_server.py`**: `run` (heartbeat + quet + `taken`), `_serve_one`
   (delegate `registry.open`), `_write_answer`, `sweep`.

3. **`_write_answer`** luon kem `answered_by_pid: os.getpid()` va
   `expires_at: now + answer_ttl_s`.

4. **Tests `test_ask_broker.py`**:
   - `owner_alive`: khong heartbeat → `False` + reason; heartbeat tuoi → `True`;
     heartbeat cu hon `heartbeat_stale_s` → `False`; pid khong song → `False`;
     protocol lech → `False`
   - `open_request` lan 2 cung q_id → `False`
   - `wait_for_answer` tra answer khi co
   - **answer thieu `answered_by_pid` → tu choi**, tiep tuc cho
   - **answer co pid khong khop lock → tu choi** + log warning
   - **answer ghi trong `poll_interval` cuoi van duoc tra** (test lan doc cuoi —
     quan trong: day la cu tap cua operator bi nuot)
   - timeout → `None`; `close_request` don het file
   - `close_request` goi 2 lan khong loi

5. **Tests `test_ask_server.py`** — dung `AskRegistry` that + `MagicMock` client:
   - request → serve → answer duoc ghi voi dung `value`/`source`
   - **hai `AskServer` tren cung spool: chi mot serve** (`taken` doc quyen —
     day la fix cho `MCGRAM_SKIP_LOCK=1`)
   - request het han khong duoc serve
   - `registry.open` raise `TelegramError` → answer `source: "telegram_error"`,
     khong treo non-owner
   - rate limit het → answer `source: "rate_limited"`
   - `write_heartbeat` ghi dung pid + protocol
   - mot request cham khong chan request khac (serve trong task rieng)
   - loi trong mot tick khong giet vong lap `run`

6. **Test tich hop 2 "process"** (`test_ask_server.py`): `AskBroker` +
   `AskServer` tren cung `tmp_path`; broker `open_request`, server serve, broker
   nhan answer. Test quan trong nhat cua phase.

## Success Criteria

- [ ] `ask_broker.py` < 200 LOC, `ask_server.py` < 200 LOC, ruff clean
- [ ] **`ask_registry.py` khong bi sua mot dong nao**
- [ ] Test tich hop pass: broker ghi request → server serve → broker nhan answer
- [ ] **Khong co owner song → `owner_alive` tra `False` + reason cu the**
- [ ] **Hai `AskServer` cung spool: dung mot cai serve moi request**
- [ ] **Answer khong co / sai `answered_by_pid` bi tu choi + log warning**
- [ ] **Answer ghi trong poll interval cuoi van duoc tra** (co test)
- [ ] Answer luon duoc ghi ke ca khi `registry.open` loi (khong treo non-owner)
- [ ] Rate limit ap o owner; het ngan sach → answer `rate_limited`, khong treo
- [ ] Answer co `expires_at`; record mo coi bi sweep
- [ ] Mot ask cham khong chan ask khac
- [ ] Loi mot tick khong giet vong lap `run`

## Risk Assessment

| Rui ro | Giam thieu |
|---|---|
| Owner chet giua khi dang serve | `taken` co `expires_at`; sweep don; request cung het han → non-owner timeout dung han |
| Answer gia mao | `answered_by_pid` cross-check lock; audit ghi lai |
| Hai owner (`MCGRAM_SKIP_LOCK=1`) | `taken` qua `write_exclusive` (`O_EXCL`) — nguyen tu |
| Answer ghi sau khi non-owner bo cuoc | `request_ttl_pad_s` + answer co TTL + sweep |
| Question text tren dia | Mode 0600; TTL ngan; sweep. Neu trong threat model |
| Vong lap `run` chet im lang | `try/except Exception` trong vong lap + `log.exception` |
| Task serve bi huy giua chung | `finally` discard khoi `_serving`; `taken` het han cho phep serve lai |
