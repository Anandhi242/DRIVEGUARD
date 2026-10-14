"""
DriveGuard backend (FastAPI).

Every number returned here is computed from YOUR real Backblaze files in
data/processed/. Nothing is invented. Anything that needs a trained model
(risk score, simulation, assistant) is reported as "not available yet".

Run from the DriveGuard folder:
    python -m uvicorn backend.main:app --reload
Open:  http://127.0.0.1:8000/app/
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
PROC = os.path.join(DATA, "processed")
MODELS = os.path.join(BASE, "models")
FRONT = os.path.join(BASE, "frontend")

# ---------------------------------------------------------------- SMART setup
SMART_LABELS = {
    5: "Reallocated sectors",
    9: "Power-on hours",
    187: "Reported uncorrectable errors",
    188: "Command timeout",
    194: "Temperature (C)",
    197: "Current pending sectors",
    198: "Offline uncorrectable sectors",
}
# Attributes Backblaze has publicly reported as failure-correlated.
INDICATORS = [5, 187, 188, 197, 198]
SOURCES = ["driveguard_combined.csv", "driveguard_features.csv", "driveguard_cleaned.csv"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def py(v):
    """Convert numpy/NaN values into JSON-safe python values."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return int(v)
    return v


# ------------------------------------------------------------------ data load
_state = {"df": None, "latest": None, "source": None, "cols": [], "lock": threading.Lock()}


def pick_source():
    for name in SOURCES:
        p = os.path.join(PROC, name)
        if os.path.exists(p):
            return p
    return None


def load():
    with _state["lock"]:
        if _state["df"] is not None:
            return _state
        path = pick_source()
        if path is None:
            raise HTTPException(503, "No processed CSV found in data/processed. Run preprocess.py first.")
        header = pd.read_csv(path, nrows=0).columns.tolist()
        base = [c for c in ["date", "serial_number", "model", "capacity_bytes", "failure"] if c in header]
        smart = [f"smart_{k}_raw" for k in SMART_LABELS if f"smart_{k}_raw" in header]
        df = pd.read_csv(path, usecols=base + smart, low_memory=False)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date", "serial_number"]).sort_values(["serial_number", "date"])
        df["failure"] = pd.to_numeric(df["failure"], errors="coerce").fillna(0).astype(int)
        ind_cols = [f"smart_{k}_raw" for k in INDICATORS if f"smart_{k}_raw" in df.columns]
        if ind_cols:
            df["_ind"] = (df[ind_cols].fillna(0) > 0).sum(axis=1)
        else:
            df["_ind"] = 0
        latest = df.drop_duplicates("serial_number", keep="last").copy()
        _state.update(df=df, latest=latest, source=path, cols=smart)
        return _state


# ----------------------------------------------------------------------- auth
SECRET_FILE = os.path.join(DATA, ".secret")
USERS_FILE = os.path.join(DATA, "users.json")
AUDIT_FILE = os.path.join(DATA, "audit_log.jsonl")
BASELINE_FILE = os.path.join(DATA, "integrity_baseline.json")
os.makedirs(DATA, exist_ok=True)

if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
SECRET = open(SECRET_FILE).read().strip().encode()


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(sub, role, ttl=8 * 3600):
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": sub, "role": role, "exp": int(time.time()) + ttl}).encode())
    sig = b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def read_token(tok):
    try:
        head, body, sig = tok.split(".")
        good = b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        claims = json.loads(unb64(body))
        return claims if claims["exp"] > time.time() else None
    except Exception:
        return None


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def ensure_users():
    """First run: create admin / engineer / viewer with RANDOM passwords."""
    if os.path.exists(USERS_FILE):
        return
    users, lines = {}, []
    for name, role in [("admin", "admin"), ("engineer", "engineer"), ("viewer", "viewer")]:
        pw = secrets.token_urlsafe(9)
        salt = secrets.token_hex(16)
        users[name] = {"salt": salt, "hash": hash_pw(pw, salt), "role": role}
        lines.append(f"{name} ({role}): {pw}")
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)
    with open(os.path.join(DATA, "first_run_credentials.txt"), "w") as f:
        f.write("DriveGuard first-run logins. Save these, then delete this file.\n" + "\n".join(lines) + "\n")
    print("\n=== DriveGuard first run: login credentials ===")
    print("\n".join(lines))
    print("(also saved in data/first_run_credentials.txt - delete after noting them)\n")


ensure_users()

# ---------------------------------------------------------------------- audit
_audit_lock = threading.Lock()


def _digest(rec):
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()


def audit(user, action, detail="", ok=True):
    with _audit_lock:
        prev = "0" * 64
        if os.path.exists(AUDIT_FILE):
            with open(AUDIT_FILE, "rb") as f:
                lines = f.read().splitlines()
            if lines:
                prev = json.loads(lines[-1])["hash"]
        rec = {"ts": now_iso(), "user": user, "action": action, "detail": detail, "ok": ok, "prev": prev}
        rec["hash"] = _digest(rec)
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")


def audit_read():
    if not os.path.exists(AUDIT_FILE):
        return [], True, None
    recs = [json.loads(x) for x in open(AUDIT_FILE).read().splitlines() if x.strip()]
    prev, ok, bad = "0" * 64, True, None
    for i, r in enumerate(recs):
        body = {k: v for k, v in r.items() if k != "hash"}
        if r["prev"] != prev or _digest(body) != r["hash"]:
            ok, bad = False, i
            break
        prev = r["hash"]
    return recs, ok, bad


_fails = {}


def current_user(request: Request):
    h = request.headers.get("authorization", "")
    claims = read_token(h[7:]) if h.startswith("Bearer ") else None
    if not claims:
        raise HTTPException(401, "Please sign in.")
    return claims


def need(*roles):
    def dep(c=Depends(current_user)):
        if c["role"] not in roles:
            audit(c["sub"], "denied", f"role {c['role']} not allowed", ok=False)
            raise HTTPException(403, "Your role cannot open this.")
        return c
    return dep


# ------------------------------------------------------------------------ app
app = FastAPI(title="DriveGuard")


@app.middleware("http")
async def headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return resp


@app.get("/")
def root():
    return {"project": "DriveGuard", "status": "running", "open": "/app/", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso(), "data_source": os.path.basename(pick_source() or "none")}


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login):
    key = body.username.lower()
    t = [x for x in _fails.get(key, []) if x > time.time() - 60]
    if len(t) >= 5:
        audit(key, "login_locked", "too many attempts", ok=False)
        raise HTTPException(429, "Too many attempts. Wait a minute.")
    users = json.load(open(USERS_FILE))
    u = users.get(key)
    if not u or not hmac.compare_digest(hash_pw(body.password, u["salt"]), u["hash"]):
        _fails[key] = t + [time.time()]
        audit(key, "login_failed", "bad credentials", ok=False)
        raise HTTPException(401, "Wrong username or password.")
    _fails.pop(key, None)
    audit(key, "login", f"role {u['role']}")
    return {"token": make_token(key, u["role"]), "user": key, "role": u["role"]}


@app.get("/api/fleet")
def fleet(c=Depends(need("admin", "engineer", "viewer"))):
    s = load()
    df, latest = s["df"], s["latest"]
    daily = df.groupby(df["date"].dt.strftime("%Y-%m-%d")).agg(
        drives=("serial_number", "nunique"), failures=("failure", "sum"))
    failed = latest[latest["failure"] == 1]
    ind_counts = {}
    for k in INDICATORS:
        col = f"smart_{k}_raw"
        if col in latest.columns:
            ind_counts[f"SMART {k} {SMART_LABELS[k]}"] = int((latest[col].fillna(0) > 0).sum())
    g = df.drop_duplicates("serial_number", keep="last").groupby("model").agg(
        drives=("serial_number", "count"), failures=("failure", "sum"))
    top = g.sort_values("drives", ascending=False).head(12).reset_index()
    healthy = latest[latest["failure"] == 0]
    return {
        "source": os.path.basename(s["source"]),
        "rows": int(len(df)),
        "drives": int(len(latest)),
        "models": int(latest["model"].nunique()),
        "date_from": str(df["date"].min().date()),
        "date_to": str(df["date"].max().date()),
        "daily": [{"date": d, "drives": int(r.drives), "failures": int(r.failures)} for d, r in daily.iterrows()],
        "failed_drives": int(len(failed)),
        "failed_with_indicator": int((failed["_ind"] > 0).sum()),
        "operating_with_indicator": int((healthy["_ind"] > 0).sum()),
        "operating": int(len(healthy)),
        "indicator_counts": ind_counts,
        "top_models": [{"model": r.model, "drives": int(r.drives), "failures": int(r.failures)}
                       for r in top.itertuples()],
        "smart_columns_loaded": s["cols"],
        "model_status": model_status(),
    }


def model_status():
    files = [os.path.basename(p) for p in glob.glob(os.path.join(MODELS, "*"))] if os.path.isdir(MODELS) else []
    return {"trained_model_verified": False, "files_in_models_folder": files,
            "note": "No risk scores are shown until a model is trained and evaluated (Module 3)."}


@app.get("/api/drives")
def drives(q: str = "", status: str = "all", page: int = 1, size: int = 25,
           c=Depends(need("admin", "engineer", "viewer"))):
    s = load()
    d = s["latest"]
    if q:
        ql = q.lower()
        d = d[d["serial_number"].astype(str).str.lower().str.contains(ql, regex=False)
              | d["model"].astype(str).str.lower().str.contains(ql, regex=False)]
    if status == "failed":
        d = d[d["failure"] == 1]
    elif status == "indicator":
        d = d[(d["_ind"] > 0) & (d["failure"] == 0)]
    d = d.sort_values(["failure", "_ind"], ascending=False)
    total = len(d)
    size = max(1, min(size, 100))
    part = d.iloc[(page - 1) * size: page * size]
    cols = s["cols"]
    rows = []
    for r in part.to_dict("records"):
        rows.append({"serial": r["serial_number"], "model": r["model"], "date": str(r["date"].date()),
                     "failed": int(r["failure"]), "indicators": int(r["_ind"]),
                     "smart": {k: py(r.get(k)) for k in cols}})
    return {"total": int(total), "page": page, "size": size, "rows": rows, "smart_columns": cols}


@app.get("/api/drive/{serial}")
def drive(serial: str, c=Depends(need("admin", "engineer", "viewer"))):
    s = load()
    d = s["df"][s["df"]["serial_number"] == serial]
    if d.empty:
        raise HTTPException(404, "Drive not found.")
    audit(c["sub"], "view_drive", serial)
    hist = []
    for r in d.to_dict("records"):
        hist.append({"date": str(r["date"].date()), "failure": int(r["failure"]),
                     "smart": {k: py(r.get(k)) for k in s["cols"]}})
    first = d.iloc[0]
    return {"serial": serial, "model": first["model"], "capacity_bytes": py(first.get("capacity_bytes")),
            "history": hist, "smart_columns": s["cols"],
            "risk_score": None, "risk_note": "No trained model yet, so no risk score is shown."}


@app.get("/api/sim")
def sim(c=Depends(need("admin", "engineer"))):
    s = load()
    return {"available": False,
            "dates_covered": int(s["df"]["date"].nunique()),
            "needs": ["Future-failure labels built from enough consecutive days",
                      "A trained and evaluated XGBoost model",
                      "Cost assumptions you choose (not invented here)"]}


@app.get("/api/assistant")
def assistant(c=Depends(need("admin", "engineer"))):
    return {"available": False, "needs": ["Trained model for risk output", "Explainability output from that model"]}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@app.get("/api/system")
def system(baseline: int = 0, c=Depends(need("admin"))):
    files = sorted(glob.glob(os.path.join(PROC, "*")) + glob.glob(os.path.join(MODELS, "*")))
    base = json.load(open(BASELINE_FILE)) if os.path.exists(BASELINE_FILE) else {}
    cur, out = {}, []
    for p in files:
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, BASE).replace("\\", "/")
        digest = sha256_file(p)
        cur[rel] = digest
        status = "no baseline" if rel not in base else ("match" if base[rel] == digest else "CHANGED")
        out.append({"file": rel, "mb": round(os.path.getsize(p) / 1e6, 2), "sha256": digest[:16] + "...",
                    "status": status})
    if baseline:
        json.dump(cur, open(BASELINE_FILE, "w"), indent=2)
        audit(c["sub"], "integrity_baseline", f"{len(cur)} files")
        for o in out:
            o["status"] = "match"
    report = os.path.join(PROC, "preprocessing_report.json")
    return {"files": out, "report": json.load(open(report)) if os.path.exists(report) else None}


@app.get("/api/logs")
def logs(c=Depends(need("admin"))):
    recs, ok, bad = audit_read()
    return {"count": len(recs), "chain_ok": ok, "broken_at": bad, "recent": recs[-100:][::-1]}


app.mount("/app", StaticFiles(directory=FRONT, html=True), name="app")
