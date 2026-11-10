---
phase: 1
title: "Spool foundation"
status: pending
priority: P1
dependencies: []
effort: ""
---

# Phase 1: Spool foundation

## Overview

Lop IPC nen: atomic JSON write/read, liet ke, xoa, TTL sweep, **key validation**,
**file/dir mode chat**. Khong biet gi ve `ask` hay `reminder`.

Phase nay noi rong hon ban thao dau: red team chi ra bon lo bao mat o day
(spool_dir suy sai, mode 0644/0755, khong validate key, sweep giu record vinh
vien). Sua tai goc, khong de o phase docs.

## Requirements

**Functional:**
- Ghi JSON atomic: reader khong bao gio doc duoc file ghi do dang
- Doc tra `None` khi file khong ton tai / JSON hong / bi xoa giua chung
- Liet ke record theo kind, bo qua file hong
- Ghi doc quyen: 2 process cung tao 1 key → dung 1 thang thang
- Sweep record het han theo `expires_at`, **file `.tmp` mo coi**, va **record
  thieu `expires_at`** (coi la hong)
- **Reject key khong hop le truoc khi cham filesystem**
- **`probe()` xac minh spool an toan**, khong chi "ghi duoc"

**Non-functional:**
- Chay dung tren Windows lan POSIX (`os.replace` atomic ca hai)
- Moi loi I/O → tra gia tri degrade, khong raise len tool handler
- **Dir mode `0700`, file mode `0600` tren POSIX** — theo convention repo
- `spool.py` < 200 LOC

## INVARIANT: moi record PHAI co `expires_at`

Ban thao dau viet "record khong co `expires_at` khong bao gio bi sweep". Red team
chi ra day la lo ro ri vinh vien: answer file va cancel request deu khong co
`expires_at` trong ban thao dau → ton tai mai mai khi mo coi. Ket hop `q_id`
32-bit → ask moi doc answer cu → **false-approve**.

Quy tac cuoi cung:
- Moi payload ghi vao spool PHAI co `expires_at` (epoch giay)
- `sweep()` xoa record co `expires_at < now`
- `sweep()` cung xoa record **thieu** `expires_at` — coi la hong, khong phai
  vinh vien. Log warning mot lan de bat bug ghi thieu field
- Test khang dinh ca hai hanh vi

## Bao mat: bon sua tai goc

### 1. `spool_dir` KHONG duoc suy tu `audit.path`

Ban thao dau: `spool_dir = home_dir / "spool"`, va bien luan an toan dua tren
"spool nam trong `~/.mcgram/` — cung vung voi `.env`". **Sai:**
`config.py:257-259` dinh nghia `home_dir = Path(self.audit.path).parent`, va
`audit.path` do config dieu khien hoan toan.

`audit.path: /tmp/mcgram/audit.jsonl` → spool o `/tmp/mcgram/spool`. `/tmp`
world-writable → forge answer leo tu "cung uid" len "**bat ky user nao tren
may**". `tests/conftest.py:21` tro `audit.path` vao `tmp_path`, nen day la duong
mac dinh cua test, khong phai edge case.

```python
class BrokerConfig(BaseModel):
    spool_dir: str | None = None   # None -> DEFAULT_HOME/spool, KHONG theo audit.path

@property
def spool_dir(self) -> Path:
    """Neo vao DEFAULT_HOME, doc lap `audit.path`.

    KHONG dung `home_dir` (suy tu audit.path) — audit co the tro ra /tmp,
    lam spool world-writable.
    """
    if self.broker.spool_dir:
        return Path(self.broker.spool_dir).expanduser()
    return Path(DEFAULT_HOME).expanduser() / "spool"
```

### 2. Mode `0600`/`0700` — code mau ban thao dau di nguoc convention repo

`tmp.write_text(...)` → `0666 & ~umask` (thuong `0644`). `mkdir()` → `0777 &
~umask` (thuong `0755`). Repo dung `0o600` o `lock.py:86`, `audit.py:89,180,185`.

```python
def _ensure_dir(self) -> None:
    self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

def write(self, key, kind, payload) -> bool:
    if not _valid_key(key):
        return False
    target = self.path_for(key, kind)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        self._ensure_dir()
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, target)      # atomic tren Windows + POSIX
        return True
    except OSError:
        log.debug("spool write failed for %s", target, exc_info=True)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
```

### 3. Key validation — `reminder_id` tu MCP di thang vao path

`cancel_reminder.py:26` nhan `reminder_id: str` tu MCP arg **khong validate**;
phase 4 route no vao `broker.request_cancel(rid)` → `path_for(rid, "cancel")`.
`Path('/tmp/spool') / '../../etc/evil.cancel.json'` thoat namespace.

```python
_KEY_RE = re.compile(r"^[a-z]_[0-9a-f]{8,32}$")   # q_<hex>, r_<hex>, u<digits>
_UPDATE_KEY_RE = re.compile(r"^u[0-9]{1,20}$")

def _valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key) or _UPDATE_KEY_RE.match(key))
```

Moi ham public (`write`, `write_exclusive`, `read`, `remove`, `path_for`) goi
`_valid_key` truoc khi cham filesystem. Key sai → tra `False`/`None`, log warning.

### 4. `probe()` phai xac minh AN TOAN, khong chi writability

Ban thao dau: `probe()` tra True/False theo "ghi duoc khong" → **thanh cong o
dung nhung vi tri nguy hiem nhat** (`/tmp/mcgram/spool`).

```python
def probe(self) -> tuple[bool, str]:
    """(ok, reason). Kiem tra an toan truoc kha dung.

    - khong duoc la symlink (chan TOCTOU ghi ra ngoai)
    - phai thuoc uid hien tai (POSIX)
    - mode khong duoc rong hon 0700 (POSIX)
    - phai ghi duoc
    """
```

Tra `tuple` de caller log **ly do** tat broker, khong chi "tat". Windows bo qua
uid/mode (dua vao ACL user profile), van check symlink + writability.

## Architecture

```
Spool(root: Path, namespace: str)
  ├── path_for(key, kind)   -> root/namespace/{key}.{kind}.json  | None neu key sai
  ├── write(key, kind, payload)          -> bool   # atomic, mode 0600
  ├── write_exclusive(key, kind, ...)    -> bool   # O_EXCL, False neu da ton tai
  ├── read(key, kind)                    -> dict | None
  ├── list(kind)                         -> list[tuple[key, dict]]
  ├── keys(kind)                         -> set[str]   # chi ten, khong doc noi dung
  ├── remove(key, *kinds)                -> bool   # False neu con sot
  ├── sweep(now)                         -> int    # het han + thieu expires_at + .tmp cu
  └── probe()                            -> tuple[bool, str]
```

**`remove` tra `bool`** (khong `None`): caller can biet xoa that bai de khong gia
dinh spool da sach.

**`keys(kind)`** de sweep phat hien answer mo coi ma khong `json.loads` moi file.

## Related Code Files

- Create: `src/mcgram/spool.py`
- Create: `tests/test_spool.py`
- Modify: `src/mcgram/config.py` — them `BrokerConfig` + `Settings.spool_dir`
- Modify: `tests/test_config.py` — them case cho `broker` section

## Implementation Steps

1. **`BrokerConfig` trong `config.py`**:
   ```python
   class BrokerConfig(BaseModel):
       """Cross-process broker cho `ask` va `reminders`.

       DEFAULT OFF: broker ha trust boundary cua `ask` tu "Telegram inbound +
       operator filter" xuong "bat ky code cung uid ghi duoc spool". Xem
       docs/security-threat-model.md truoc khi bat.
       """
       enabled: bool = False          # OPT-IN (quyet dinh sau red team)
       spool_dir: str | None = None   # None -> ~/.mcgram/spool (KHONG theo audit.path)
       poll_interval_s: float = Field(0.3, gt=0, le=5)
       request_ttl_pad_s: int = Field(30, ge=0)
       answer_ttl_s: int = Field(300, ge=1)
       cancel_ttl_s: int = Field(300, ge=1)
       heartbeat_stale_s: int = Field(90, ge=1)  # > 25s long-poll + slack
       sweep_interval_s: int = Field(60, ge=1)
       tmp_grace_s: int = Field(300, ge=1)
   ```
   Them `broker: BrokerConfig = Field(default_factory=BrokerConfig)` vao `Settings`.

   `config.py` hien **288 LOC** — da vuot 200 truoc plan nay. Them ~14 dong.
   Neu vuot ngan sach, tach cac `*Config` model sang `config_models.py`; ghi ly
   do vao commit. Xem phase 5 muc LOC.

2. **`Settings.spool_dir`** theo doan code o muc bao mat #1 — **khong** dung
   `home_dir`.

3. **`spool.py`** — implement theo API tren, gom ca 4 sua bao mat.
   - `list(kind)` bo qua file JSON hong thay vi raise
   - `sweep(now)` xoa: (a) `expires_at < now`, (b) record **thieu**
     `expires_at` + log warning, (c) `*.tmp` mtime cu hon `tmp_grace_s`
   - `remove` tra `False` neu unlink that bai vi ly do khac `FileNotFoundError`

4. **Tests** (`tests/test_spool.py`):
   - write → read round-trip
   - `write_exclusive` lan 2 tra `False`, khong de len noi dung cu
   - read file JSON hong → `None`, khong raise
   - `list` bo qua file hong
   - `sweep` xoa record het han
   - **`sweep` xoa record thieu `expires_at`** (doi nguoc ban thao dau)
   - `sweep` xoa `.tmp` cu hon grace, giu `.tmp` moi
   - `remove` tra `True` khi xoa het, `False` khi con sot
   - `keys(kind)` dung tap key, khong doc noi dung
   - **key validation**: `"../x"`, `"a/b"`, `"C:evil"`, `"q_XYZ"`, `""` deu bi
     reject; **khang dinh khong file nao duoc tao/xoa ngoai namespace**
   - **mode (POSIX only, skip Windows)**: dir `0700`, file `0600`
   - **`probe()` reject symlink**; reject dir mode `0777`; tra reason cu the
   - `probe()` tra `False` khi root khong ghi duoc
   - namespace tach biet: `Spool(root, "asks")` khong thay record `"reminders"`
   - **`spool_dir` khong doi khi `audit.path` tro noi khac** (chot chan cho sua #1)

## Success Criteria

- [ ] `spool.py` < 200 LOC, ruff clean
- [ ] Moi ham I/O nuot `OSError`, tra gia tri degrade — khong raise len caller
- [ ] **`sweep` xoa record thieu `expires_at`** (khong giu vinh vien)
- [ ] **Key sai khong tao/xoa duoc file ngoai namespace** (co test)
- [ ] **Dir `0700`, file `0600` tren POSIX** (co test, skip Windows)
- [ ] **`probe()` reject symlink va mode rong; tra reason**
- [ ] **`spool_dir` doc lap `audit.path`** (co test)
- [ ] `broker.enabled` default **`false`**
- [ ] `remove` tra `bool` phan anh dung ket qua
- [ ] `test_spool.py` pass tren Windows CI
- [ ] Test config hien co van pass
- [ ] `config.py` khong tang dang ke — hoac da tach model sang module rieng

## Risk Assessment

| Rui ro | Giam thieu |
|---|---|
| `os.replace` that bai (Windows AV giu file) | Nuot loi, tra `False`; caller degrade. Khong retry loop |
| Tmp file ro ri khi process chet giua write | Tmp mang PID; sweep don `.tmp` cu hon grace |
| Config them field pha config cu | Toan field co default; test config hien co la chot chan |
| `config.py` phinh them (da 288 LOC) | Tach `*Config` sang module rieng neu vuot; ghi ly do |
| Caller bo qua `remove` tra `False` | Phase 2 phai xu ly: khong xoa duoc answer → khong tai dung key |
| Symlink dat sau `probe()` (TOCTOU) | `probe()` giam cua so, khong dong hoan toan; mode 0700 lam buoc chan thu hai |
