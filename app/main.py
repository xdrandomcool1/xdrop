"""xdrop — self-hosted drag & drop file sharing.

Files are stored in XDROP_STORAGE, which may be a local disk or a network share (SMB/NFS/sshfs).
The directory must contain a `.xdrop-storage` marker file; while it is missing (share not mounted,
storage machine offline) the app answers 503 instead of writing into an empty mount point.
Uploads arrive in chunks (<100 MB each, Cloudflare's
per-request limit) and are appended at an explicit offset, so an upload can resume after
network errors or even a service restart; file size is bounded only by free disk space.
Downloads are authorised here and then served by nginx via X-Accel-Redirect (Range,
sendfile, resumable).
"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

log = logging.getLogger("xdrop")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STORAGE = Path(os.environ.get("XDROP_STORAGE", "/srv/xdrop"))
DB_PATH = os.environ.get("XDROP_DB", "/var/lib/xdrop/xdrop.db")
SECRET = os.environ["XDROP_SECRET"].encode()
PASSWORD_HASH = os.environ.get("XDROP_PASSWORD_HASH", "")  # scrypt$<salt hex>$<hash hex>
# XDROP_OPEN=1: no login at all, every visitor is treated as the owner (public admin).
OPEN = os.environ.get("XDROP_OPEN") == "1"
RESERVE_BYTES = int(os.environ.get("XDROP_RESERVE_GB", "5")) * 1024**3
STALE_SECONDS = int(os.environ.get("XDROP_STALE_HOURS", "48")) * 3600
MAX_REQUEST_BODY = 96 * 1024 * 1024
WRITE_BUFFER = 4 * 1024 * 1024
SESSION_SECONDS = 30 * 86400
COOKIE = "xdrop_session"
MARKER = ".xdrop-storage"  # must exist in STORAGE, proves the share is really mounted
PARTIAL = STORAGE / ".partial"
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- auth helpers

def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str) -> bool:
    try:
        _, salt_hex, _ = PASSWORD_HASH.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt_hex)), PASSWORD_HASH)


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_SECONDS)
    return f"{exp}.{hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()}"


def is_admin(request: Request) -> bool:
    if OPEN:
        return True
    token = request.cookies.get(COOKIE)
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def check_csrf(request: Request) -> None:
    # Cross-site requests cannot set custom headers without a CORS preflight we never allow.
    if request.headers.get("x-xdrop") != "1":
        raise HTTPException(403, "Hiányzó X-XDrop fejléc")


def require_admin(request: Request, mutating: bool = False) -> None:
    if not is_admin(request):
        raise HTTPException(401, "Bejelentkezés szükséges")
    if mutating:
        check_csrf(request)


def require_uploader(request: Request, share) -> None:
    """Admin, or a friend holding this share's upload token."""
    check_csrf(request)
    if is_admin(request):
        return
    tok = request.headers.get("x-upload-token") or ""
    if share is not None and share["upload_token"] and hmac.compare_digest(tok, share["upload_token"]):
        return
    raise HTTPException(401, "Nincs jogosultság a feltöltéshez")


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


_login_attempts: dict[str, list[float]] = {}


def rate_limit_login(ip: str) -> None:
    now = time.time()
    hits = [t for t in _login_attempts.get(ip, []) if now - t < 900]
    if len(hits) >= 10:
        raise HTTPException(429, "Túl sok próbálkozás, várj 15 percet")
    hits.append(now)
    _login_attempts[ip] = hits


async def body_json(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Érvénytelen JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Érvénytelen JSON")
    return data


# ---------------------------------------------------------------- database

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS shares(
        id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL,
        expires INTEGER, deleted INTEGER NOT NULL DEFAULT 0, upload_token TEXT);
    CREATE TABLE IF NOT EXISTS files(
        id TEXT PRIMARY KEY, share_id TEXT NOT NULL REFERENCES shares(id), name TEXT NOT NULL,
        size INTEGER NOT NULL, received INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
        error TEXT, created INTEGER NOT NULL, updated INTEGER, done_at INTEGER,
        by_friend INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS files_share ON files(share_id);
    CREATE UNIQUE INDEX IF NOT EXISTS shares_upload_token ON shares(upload_token);
    """
)


def share_row(share_id: str):
    row = db.execute("SELECT * FROM shares WHERE id=? AND deleted=0", (share_id,)).fetchone()
    if not row or (row["expires"] and row["expires"] < time.time()):
        return None
    return row


def file_row(fid: str):
    return db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()


# ---------------------------------------------------------------- storage

# The storage may be a network share. When the machine behind it is off,
# filesystem calls can block for seconds, so availability is probed in the background on
# a dedicated thread and request handlers only read the cached flag.
_probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage-probe")
_storage_up = False


def storage_ok() -> bool:
    return _storage_up


async def storage_probe_loop() -> None:
    global _storage_up
    loop = asyncio.get_running_loop()
    pending = None
    while True:
        if pending is None or pending.done():
            pending = loop.run_in_executor(_probe_pool, (STORAGE / MARKER).is_file)
        try:
            up = bool(await asyncio.wait_for(asyncio.shield(pending), 4))
        except Exception:  # timeout or I/O error: treat as offline, keep the hung probe
            up = False
        if up != _storage_up:
            log.warning("storage %s", "ONLINE" if up else "OFFLINE")
            if up:
                await asyncio.to_thread(PARTIAL.mkdir, exist_ok=True)
        _storage_up = up
        await asyncio.sleep(5 if up else 3)


def require_storage() -> None:
    if not storage_ok():
        raise HTTPException(503, "A tárhely most nem elérhető")


def free_bytes() -> int:
    st = os.statvfs(STORAGE)
    return st.f_bavail * st.f_frsize


def reserved_bytes() -> int:
    """Bytes promised to uploads that are still in progress."""
    r = db.execute("SELECT COALESCE(SUM(size - received), 0) FROM files WHERE status='uploading'").fetchone()
    return int(r[0])


def partial_path(fid: str) -> Path:
    return PARTIAL / fid


def final_path(share_id: str, name: str) -> Path:
    # Pure string check (no filesystem access: the storage may be an offline network share).
    base = os.path.normpath(str(STORAGE / share_id))
    p = os.path.normpath(os.path.join(base, name))
    if not p.startswith(base + os.sep):
        raise HTTPException(400, "Érvénytelen útvonal")
    return Path(p)


WIN_BAD = set('<>:"|?*')
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean_name(raw: str) -> str:
    """Sanitise a client path so it is valid on the Windows (NTFS/SMB) storage."""
    parts = []
    for p in str(raw).replace("\\", "/").split("/"):
        p = "".join("_" if c in WIN_BAD else c for c in p if c.isprintable())
        p = p.strip().rstrip(". ")[:180].rstrip(". ")
        if not p or p in (".", ".."):
            continue
        if p.split(".", 1)[0].upper() in WIN_RESERVED:
            p = "_" + p
        parts.append(p)
    if not parts or len(parts) > 12:
        raise HTTPException(400, "Érvénytelen fájlnév")
    return "/".join(parts)


def unique_name(share_id: str, name: str) -> str:
    # NTFS is case-insensitive: compare lowercased.
    taken = {r["name"].lower() for r in db.execute(
        "SELECT name FROM files WHERE share_id=? AND status!='failed'", (share_id,))}
    if name.lower() not in taken:
        return name
    head, _, last = name.rpartition("/")
    stem, dot, ext = last.rpartition(".")
    if not stem:
        stem, dot, ext = last, "", ""
    n = 1
    while True:
        cand = f"{stem} ({n}){dot}{ext}"
        full = f"{head}/{cand}" if head else cand
        if full.lower() not in taken:
            return full
        n += 1


def _finish(fid: str, share_id: str, name: str) -> None:
    dst = final_path(share_id, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial_path(fid), dst)


async def finish_upload(fid: str) -> None:
    r = file_row(fid)
    await asyncio.to_thread(_finish, fid, r["share_id"], r["name"])
    now = int(time.time())
    db.execute("UPDATE files SET status='done', received=size, done_at=?, updated=? WHERE id=?", (now, now, fid))
    log.info("upload %s done: %s/%s (%d bytes)", fid, r["share_id"], r["name"], r["size"])


def _rm(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


async def purge_share(share_id: str) -> None:
    db.execute("UPDATE shares SET deleted=1, upload_token=NULL WHERE id=?", (share_id,))
    for r in db.execute("SELECT id FROM files WHERE share_id=? AND status='uploading'", (share_id,)).fetchall():
        await asyncio.to_thread(_rm, partial_path(r["id"]))
    db.execute("UPDATE files SET status='failed', error='Megosztás törölve' WHERE share_id=? AND status='uploading'",
               (share_id,))
    if storage_ok():
        await asyncio.to_thread(_rm, STORAGE / share_id)


async def housekeeping() -> None:
    while True:
        try:
            now = int(time.time())
            if storage_ok():
                for row in db.execute("SELECT id FROM shares WHERE deleted=0 AND expires IS NOT NULL "
                                      "AND expires < ?", (now,)).fetchall():
                    log.info("share %s expired, deleting", row["id"])
                    await purge_share(row["id"])
                for row in db.execute("SELECT id FROM files WHERE status='uploading' AND "
                                      "COALESCE(updated, created) < ?", (now - STALE_SECONDS,)).fetchall():
                    await asyncio.to_thread(_rm, partial_path(row["id"]))
                    db.execute("UPDATE files SET status='failed', error='Abbahagyott feltöltés' WHERE id=?",
                               (row["id"],))
        except Exception:  # keep the loop alive whatever happens
            log.exception("housekeeping")
        await asyncio.sleep(300)


# ---------------------------------------------------------------- app

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_locks: dict[str, asyncio.Lock] = {}


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(storage_probe_loop())
    asyncio.create_task(housekeeping())


NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/s/{share_id}")
async def share_page(share_id: str):
    return FileResponse(STATIC / "share.html", headers=NO_CACHE)


@app.get("/u/{token}")
async def upload_page(token: str):
    return FileResponse(STATIC / "upload.html", headers=NO_CACHE)


@app.get("/healthz")
async def healthz():
    ok = storage_ok()
    return JSONResponse({"ok": ok, "storage": ok}, status_code=200 if ok else 503)


@app.get("/api/me")
async def me(request: Request):
    admin = is_admin(request)
    out: dict = {"admin": admin, "open": OPEN}
    if admin and storage_ok():
        try:
            free = await asyncio.wait_for(asyncio.to_thread(free_bytes), 4)
            out["free"] = max(0, free - reserved_bytes())
        except Exception:
            pass
    return out


@app.post("/api/login")
async def login(request: Request):
    rate_limit_login(client_ip(request))
    body = await body_json(request)
    if not check_password(str(body.get("password", ""))):
        raise HTTPException(401, "Hibás jelszó")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, make_session(), max_age=SESSION_SECONDS, httponly=True,
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
                    samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


def file_json(r) -> dict:
    return {"id": r["id"], "name": r["name"], "size": r["size"], "received": r["received"],
            "status": r["status"], "error": r["error"], "by_friend": bool(r["by_friend"])}


@app.get("/api/shares")
async def list_shares(request: Request):
    require_admin(request)
    out = []
    for s in db.execute("SELECT * FROM shares WHERE deleted=0 ORDER BY created DESC").fetchall():
        files = db.execute("SELECT * FROM files WHERE share_id=? AND status!='failed' ORDER BY created",
                           (s["id"],)).fetchall()
        out.append({"id": s["id"], "note": s["note"], "created": s["created"], "expires": s["expires"],
                    "upload_token": s["upload_token"], "files": [file_json(f) for f in files]})
    return out


def expiry_from(days) -> int | None:
    try:
        return int(time.time() + float(days) * 86400) if days else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Érvénytelen lejárat")


@app.post("/api/shares")
async def create_share(request: Request):
    require_admin(request, mutating=True)
    body = await body_json(request)
    sid = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(16) if body.get("allow_upload") else None
    expires = expiry_from(body.get("expires_days"))
    db.execute("INSERT INTO shares(id, note, created, expires, upload_token) VALUES(?,?,?,?,?)",
               (sid, str(body.get("note", ""))[:200], int(time.time()), expires, token))
    return {"id": sid, "expires": expires, "upload_token": token}


@app.patch("/api/shares/{share_id}")
async def update_share(share_id: str, request: Request):
    require_admin(request, mutating=True)
    s = share_row(share_id)
    if not s:
        raise HTTPException(404, "Nincs ilyen megosztás")
    body = await body_json(request)
    if "note" in body:
        db.execute("UPDATE shares SET note=? WHERE id=?", (str(body["note"])[:200], share_id))
    if "expires_days" in body:
        db.execute("UPDATE shares SET expires=? WHERE id=?", (expiry_from(body["expires_days"]), share_id))
    if "allow_upload" in body:
        token = (s["upload_token"] or secrets.token_urlsafe(16)) if body["allow_upload"] else None
        db.execute("UPDATE shares SET upload_token=? WHERE id=?", (token, share_id))
    s = share_row(share_id)
    return {"ok": True, "upload_token": s["upload_token"], "expires": s["expires"]}


@app.delete("/api/shares/{share_id}")
async def delete_share(share_id: str, request: Request):
    require_admin(request, mutating=True)
    await purge_share(share_id)
    return {"ok": True}


@app.get("/api/upload/{token}")
async def upload_info(token: str):
    s = db.execute("SELECT * FROM shares WHERE upload_token=? AND deleted=0", (token,)).fetchone()
    if not s or (s["expires"] and s["expires"] < time.time()):
        raise HTTPException(404, "Ez a feltöltési link nem érvényes vagy lejárt")
    return {"share_id": s["id"], "note": s["note"], "expires": s["expires"], "storage": storage_ok()}


@app.post("/api/shares/{share_id}/files")
async def create_file(share_id: str, request: Request):
    s = share_row(share_id)
    require_uploader(request, s)
    if not s:
        raise HTTPException(404, "Nincs ilyen megosztás")
    require_storage()
    body = await body_json(request)
    try:
        size = int(body.get("size", -1))
    except (TypeError, ValueError):
        size = -1
    if size < 0:
        raise HTTPException(400, "Hiányzó méret")
    try:
        free = await asyncio.wait_for(asyncio.to_thread(free_bytes), 4)
    except Exception:
        raise HTTPException(503, "A tárhely most nem elérhető")
    available = free - reserved_bytes() - RESERVE_BYTES
    if size > available:
        raise HTTPException(507, f"Nincs elég hely a tárhelyen (szabad: {max(0, available) // 1024**2} MB)")
    name = unique_name(share_id, clean_name(body.get("name", "")))
    final_path(share_id, name)  # validates the path
    fid = secrets.token_urlsafe(12)
    now = int(time.time())
    db.execute("INSERT INTO files(id, share_id, name, size, status, created, updated, by_friend) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (fid, share_id, name, size, "uploading", now, now, 0 if is_admin(request) else 1))
    await asyncio.to_thread(PARTIAL.mkdir, exist_ok=True)
    await asyncio.to_thread(partial_path(fid).touch)
    if size == 0:
        await finish_upload(fid)
    return {"id": fid, "name": name, "received": 0}


def file_for_uploader(fid: str, request: Request):
    r = file_row(fid)
    if not r:
        raise HTTPException(404, "Nincs ilyen fájl")
    s = db.execute("SELECT * FROM shares WHERE id=?", (r["share_id"],)).fetchone()
    require_uploader(request, s)
    return r


def current_size(fid: str) -> int:
    try:
        return partial_path(fid).stat().st_size
    except FileNotFoundError:
        return 0


@app.get("/api/files/{fid}")
async def file_status(fid: str, request: Request):
    r = file_for_uploader(fid, request)
    out = file_json(r)
    if r["status"] == "uploading":
        out["received"] = await asyncio.to_thread(current_size, fid)
    return out


@app.put("/api/files/{fid}")
async def put_chunk(fid: str, offset: int, request: Request):
    r = file_for_uploader(fid, request)
    if r["status"] != "uploading":
        return JSONResponse({"received": r["received"], "status": r["status"], "error": r["error"]},
                            status_code=410 if r["status"] == "failed" else 200)
    require_storage()
    if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BODY:
        raise HTTPException(413, "Túl nagy darab")
    size = r["size"]
    lock = _locks.setdefault(fid, asyncio.Lock())
    async with lock:
        cur = await asyncio.to_thread(current_size, fid)
        if offset != cur:
            # Client out of sync (e.g. an earlier request died mid-body): tell it where we are.
            return JSONResponse({"received": cur, "status": "uploading"}, status_code=409)
        f = await asyncio.to_thread(open, partial_path(fid), "ab")
        buf = bytearray()
        try:
            async for chunk in request.stream():
                if cur + len(buf) + len(chunk) > size:
                    raise HTTPException(400, "Több adat érkezett, mint a megadott méret")
                buf += chunk
                if len(buf) >= WRITE_BUFFER:
                    await asyncio.to_thread(f.write, bytes(buf))
                    cur += len(buf)
                    buf.clear()
        except ClientDisconnect:
            pass  # keep what arrived; the client resumes from `received`
        finally:
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
                cur += len(buf)
            await asyncio.to_thread(f.close)
        db.execute("UPDATE files SET received=?, updated=? WHERE id=?", (cur, int(time.time()), fid))
        status = "uploading"
        if cur == size:
            await finish_upload(fid)
            status = "done"
            _locks.pop(fid, None)
    return {"received": cur, "status": status}


@app.delete("/api/files/{fid}")
async def delete_file(fid: str, request: Request):
    r = file_for_uploader(fid, request)
    if not is_admin(request) and r["status"] != "uploading":
        raise HTTPException(403, "Kész fájlt csak a tulajdonos törölhet")
    if r["status"] == "uploading":
        await asyncio.to_thread(_rm, partial_path(fid))
    elif r["status"] == "done" and storage_ok():
        await asyncio.to_thread(_rm, final_path(r["share_id"], r["name"]))
    db.execute("DELETE FROM files WHERE id=?", (fid,))
    return {"ok": True}


@app.get("/api/public/shares/{share_id}")
async def public_share(share_id: str):
    s = share_row(share_id)
    if not s:
        raise HTTPException(404, "Ez a megosztás nem létezik vagy lejárt")
    files = db.execute("SELECT name, size, status FROM files WHERE share_id=? AND status IN "
                       "('done','uploading') ORDER BY name", (share_id,)).fetchall()
    return {"id": s["id"], "note": s["note"], "created": s["created"], "expires": s["expires"],
            "files": [{"name": f["name"], "size": f["size"], "ready": f["status"] == "done"} for f in files]}


@app.get("/dl/{share_id}/{name:path}")
async def download(share_id: str, name: str, dl: int = 0):
    if not share_row(share_id):
        raise HTTPException(404, "Ez a megosztás nem létezik vagy lejárt")
    r = db.execute("SELECT name FROM files WHERE share_id=? AND name=? AND status='done'",
                   (share_id, name)).fetchone()
    if not r:
        raise HTTPException(404, "Nincs ilyen fájl")
    require_storage()
    headers = {"X-Accel-Redirect": "/_files/" + quote(f"{share_id}/{name}", safe="/"),
               "Cache-Control": "private, no-store"}
    if dl:
        base = name.rsplit("/", 1)[-1]
        ascii_name = base.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base)}"
    return Response(headers=headers)
