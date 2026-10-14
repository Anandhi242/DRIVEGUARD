"""
DriveGuard backend (FastAPI).

Data order at start-up: uploaded files > your local Backblaze files > generated demo data.
The dataset in use is always shown in the UI. Models, scores, simulation and assistant
answers are computed from whichever dataset is active; nothing is hard-coded.

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
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assistant as A
from . import pipeline as P

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
PROC = os.path.join(DATA, "processed")
RAW = os.path.join(DATA, "raw")
UPLOADS = os.path.join(DATA, "uploads")
MODELS = os.path.join(BASE, "models")
FRONT = os.path.join(BASE, "frontend")
MAX_UPLOAD = 3 * 1024 ** 3


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def py(v):
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


# ------------------------------------------------------------------ dataset state
S = {"df": None, "table": None, "key_cols": [], "kind": None, "name": None, "note": "",
     "result": None, "model_error": None, "fleet": None,
     "training": {"state": "starting", "message": "Starting"}}
JOB = threading.Lock()


def progress(msg):
    S["training"] = {"state": "running", "message": msg}


def csv_list(folder):
    return sorted(glob.glob(os.path.join(folder, "*.csv")))


def local_paths():
    for name in ("driveguard_combined.csv",):
        p = os.path.join(PROC, name)
        if os.path.exists(p):
            return [p]
    if csv_list(RAW):
        return csv_list(RAW)
    for name in ("driveguard_features.csv", "driveguard_cleaned.csv"):
        p = os.path.join(PROC, name)
        if os.path.exists(p):
            return [p]
    return []


def rebuild_table():
    df = S["df"]
    t = df.drop_duplicates("serial_number", keep="last").copy()
    key = [P.raw(k) for k in P.KEY if P.raw(k) in df.columns]
    ind = [P.raw(k) for k in P.INDICATORS if P.raw(k) in df.columns]
    t["_ind"] = (t[ind].fillna(0) > 0).sum(axis=1) if ind else 0
    res = S["result"]
    if res is not None:
        t = t.join(res["rk"][["score", "tier", "split"]], on="serial_number")
    else:
        t["score"], t["tier"], t["split"] = float("nan"), None, None
    S["table"], S["key_cols"] = t, key


def install(df, kind, name, note):
    S.update(df=df, kind=kind, name=name, note=note, result=None, model_error=None, fleet=None)
    rebuild_table()


def make_demo():
    os.makedirs(DATA, exist_ok=True)
    df = P.generate_demo()
    df.to_csv(os.path.join(DATA, "generated_demo.csv"), index=False)
    return df


def run_job(source):
    try:
        progress("Loading data")
        df, kind, name, note = None, None, None, ""
        order = ["uploaded", "local", "demo"] if source == "auto" else [source]
        for src in order:
            try:
                if src == "uploaded" and csv_list(UPLOADS):
                    paths = csv_list(UPLOADS)
                    df, info = P.load_csvs(paths, progress)
                    kind, name = "uploaded", f"Uploaded: {len(paths)} file(s)"
                elif src == "local" and local_paths():
                    paths = local_paths()
                    df, info = P.load_csvs(paths, progress)
                    kind, name = "local", "Local Backblaze: " + ", ".join(os.path.basename(p) for p in paths[:3]) + ("..." if len(paths) > 3 else "")
                elif src == "demo":
                    progress("Generating demo data")
                    df, info = make_demo(), {}
                    kind, name = "demo", "Generated demo data (synthetic, not Backblaze)"
                    note = "This is synthetic data in the Backblaze column layout. Upload real files to replace it."
                else:
                    if source != "auto":
                        raise ValueError(f"No {src} data found.")
                    continue
                if info.get("sampled"):
                    note = (f"Large input: kept all {info['failing_drives_in_files']} failing drives and "
                            f"{info['kept_fraction_of_other_drives'] * 100:.0f}% of other drives so it fits in memory.")
            except Exception as e:
                if source != "auto":
                    raise
                print("Skipping", src, ":", e)
                continue
            if df is None:
                continue
            install(df, kind, name, note)
            try:
                S["result"] = P.train(df, progress)
            except P.NotEnoughData as e:
                if source == "auto" and kind != "demo":
                    S["note"] = f"{name} could not be used for the model ({e}) so generated demo data is shown instead."
                    df, kind = make_demo(), "demo"
                    install(df, kind, "Generated demo data (synthetic, not Backblaze)", S["note"])
                    S["result"] = P.train(df, progress)
                else:
                    S["model_error"] = str(e)
            rebuild_table()
            S["fleet"] = None
            S["training"] = {"state": "done", "message": "Ready"}
            return
        raise ValueError("No data could be loaded.")
    except Exception as e:
        S["training"] = {"state": "error", "message": str(e)}


def start_job(source):
    with JOB:
        if S["training"]["state"] in ("running",):
            raise HTTPException(409, "Still working on the previous request. Wait a moment.")
        S["training"] = {"state": "running", "message": "Starting"}
    threading.Thread(target=run_job, args=(source,), daemon=True).start()


def loading():
    t = S["training"]
    return {"status": "loading", "message": t["message"] if t["state"] != "error" else "Error: " + t["message"]}


def unavailable():
    if S["df"] is None:
        return loading()
    if S["result"] is None:
        if S["model_error"]:
            return {"status": "unavailable", "reason": S["model_error"],
                    "hint": "Upload a Backblaze dataset with several daily files, or switch to the generated demo data."}
        return {"status": "loading", "message": S["training"]["message"]}
    return None


# ------------------------------------------------------------------------ app
app = FastAPI(title="DriveGuard")


@app.on_event("startup")
def boot():
    os.makedirs(UPLOADS, exist_ok=True)
    start_job("auto")


@app.middleware("http")
async def headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    return resp


@app.get("/")
def root():
    return RedirectResponse("/app/")


@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso(), "dataset": S["name"], "training": S["training"]}


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


ALL = ("admin", "engineer", "viewer")
WORK = ("admin", "engineer")


@app.get("/api/dataset")
def dataset(c=Depends(need(*ALL))):
    out = {"training": S["training"], "model_error": S["model_error"], "kind": S["kind"], "name": S["name"], "note": S["note"],
           "uploads": [{"name": os.path.basename(p), "mb": round(os.path.getsize(p) / 1e6, 1)} for p in csv_list(UPLOADS)],
           "local_available": bool(local_paths()), "model_ready": S["result"] is not None}
    if S["df"] is not None:
        df = S["df"]
        out.update(rows=int(len(df)), drives=int(len(S["table"])), date_from=str(df["date"].min().date()), date_to=str(df["date"].max().date()))
    return out


@app.post("/api/upload")
async def upload(request: Request, name: str, c=Depends(need(*WORK))):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))
    if not safe.lower().endswith(".csv"):
        raise HTTPException(400, "Only .csv files can be uploaded.")
    os.makedirs(UPLOADS, exist_ok=True)
    path, size = os.path.join(UPLOADS, safe), 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                os.remove(path)
                raise HTTPException(413, "File is larger than 3 GB.")
            f.write(chunk)
    try:
        P.check_header(path)
    except Exception as e:
        os.remove(path)
        raise HTTPException(400, str(e))
    audit(c["sub"], "upload", f"{safe} ({size / 1e6:.1f} MB)")
    return {"saved": safe, "mb": round(size / 1e6, 1)}


@app.post("/api/uploads/clear")
def clear_uploads(c=Depends(need(*WORK))):
    for p in csv_list(UPLOADS):
        os.remove(p)
    audit(c["sub"], "uploads_cleared")
    return {"ok": True}


@app.post("/api/dataset/use")
def use(source: str, c=Depends(need(*WORK))):
    if source not in ("uploaded", "local", "demo"):
        raise HTTPException(400, "source must be uploaded, local or demo")
    if source == "uploaded" and not csv_list(UPLOADS):
        raise HTTPException(400, "Upload at least one CSV first.")
    if source == "local" and not local_paths():
        raise HTTPException(400, "No local Backblaze files found in data/raw or data/processed.")
    start_job(source)
    audit(c["sub"], "dataset_switch", source)
    return {"started": True}


def counts():
    t = S["table"]
    op = t[t["failure"] == 0]
    return {"drives": int(len(t)), "failed": int((t["failure"] == 1).sum()),
            "high": int((op["tier"] == "High").sum()), "elevated": int((op["tier"] == "Elevated").sum()),
            "low": int((op["tier"] == "Low").sum())}


@app.get("/api/fleet")
def fleet(c=Depends(need(*ALL))):
    if S["df"] is None:
        return loading()
    df, t = S["df"], S["table"]
    if S["fleet"] is None:
        daily = df.groupby(df["date"].dt.strftime("%Y-%m-%d")).agg(drives=("serial_number", "nunique"), failures=("failure", "sum"))
        ind = {}
        for k in P.INDICATORS:
            col = P.raw(k)
            if col in t.columns:
                ind[f"SMART {k} {P.LABELS[k]}"] = int((t[col].fillna(0) > 0).sum())
        g = t.groupby("model").agg(drives=("serial_number", "count"), failures=("failure", "sum"))
        top = g.sort_values("drives", ascending=False).head(12).reset_index()
        failed = t[t["failure"] == 1]
        op = t[t["failure"] == 0]
        S["fleet"] = {
            "rows": int(len(df)), "drives": int(len(t)), "models": int(t["model"].nunique()),
            "date_from": str(df["date"].min().date()), "date_to": str(df["date"].max().date()),
            "daily": [{"date": d, "drives": int(r.drives), "failures": int(r.failures)} for d, r in daily.iterrows()],
            "failed_drives": int(len(failed)), "failed_with_indicator": int((failed["_ind"] > 0).sum()),
            "operating_with_indicator": int((op["_ind"] > 0).sum()), "indicator_counts": ind,
            "top_models": [{"model": r.model, "drives": int(r.drives), "failures": int(r.failures)} for r in top.itertuples()],
            "smart_columns_loaded": S["key_cols"]}
    out = dict(S["fleet"])
    out.update(source=S["name"], kind=S["kind"], note=S["note"], training=S["training"], model_error=S["model_error"])
    if S["result"] is not None:
        out["risk"] = counts()
        op = t[(t["failure"] == 0)].sort_values("score", ascending=False).head(8)
        out["top_risk"] = [{"serial": r.serial_number, "model": r.model, "score": float(r.score), "tier": r.tier} for r in op.itertuples()]
        out["threshold"], out["horizon"] = S["result"]["thr"], S["result"]["info"]["horizon_days"]
    return out


@app.get("/api/drives")
def drives(q: str = "", status: str = "all", page: int = 1, size: int = 25, c=Depends(need(*ALL))):
    if S["df"] is None:
        return loading()
    d = S["table"]
    if q:
        ql = q.lower()
        d = d[d["serial_number"].astype(str).str.lower().str.contains(ql, regex=False)
              | d["model"].astype(str).str.lower().str.contains(ql, regex=False)]
    if status == "failed":
        d = d[d["failure"] == 1]
    elif status == "high":
        d = d[(d["tier"] == "High") & (d["failure"] == 0)]
    elif status == "indicator":
        d = d[(d["_ind"] > 0) & (d["failure"] == 0)]
    d = d.sort_values(["failure", "score", "_ind"], ascending=[True, False, False], na_position="last")
    size = max(1, min(size, 100))
    part = d.iloc[(page - 1) * size: page * size]
    cols = S["key_cols"]
    rows = [{"serial": r["serial_number"], "model": r["model"], "date": str(r["date"].date()), "failed": int(r["failure"]),
             "indicators": int(r["_ind"]), "score": py(r.get("score")), "tier": r.get("tier") if isinstance(r.get("tier"), str) else None,
             "smart": {k: py(r.get(k)) for k in cols}} for r in part.to_dict("records")]
    return {"total": int(len(d)), "page": page, "size": size, "rows": rows, "smart_columns": cols, "has_model": S["result"] is not None}


@app.get("/api/drive/{serial}")
def drive(serial: str, c=Depends(need(*ALL))):
    if S["df"] is None:
        return loading()
    df = S["df"]
    d = df[df["serial_number"] == serial]
    if d.empty:
        raise HTTPException(404, "Drive not found.")
    audit(c["sub"], "view_drive", serial)
    cols = S["key_cols"]
    hist = [{"date": str(r["date"].date()), "failure": int(r["failure"]), "smart": {k: py(r.get(k)) for k in cols}} for r in d.to_dict("records")]
    out = {"serial": serial, "model": d.iloc[0]["model"], "capacity_bytes": py(d.iloc[0].get("capacity_bytes")),
           "history": hist, "smart_columns": cols, "risk": None, "factors": []}
    res = S["result"]
    if res is not None and serial in res["rk"].index:
        r = res["rk"].loc[serial]
        out["risk"] = {"score": float(r["score"]), "tier": r["tier"], "failed": bool(r["failed"]), "split": r["split"],
                       "threshold": res["thr"], "horizon": res["info"]["horizon_days"]}
        out["factors"] = P.explain(res, serial)
    return out


@app.get("/api/model")
def model_results(c=Depends(need(*ALL))):
    u = unavailable()
    if u:
        return u
    res = S["result"]
    return {"status": "ok", "dataset": S["name"], "kind": S["kind"], **res["info"], "risk_counts": counts()}


@app.get("/api/sim")
def sim(unplanned: float = 1000, planned: float = 200, c=Depends(need(*WORK))):
    u = unavailable()
    if u:
        return u
    res = S["result"]
    out = P.simulate(res["sim"], max(0, unplanned), max(0, planned))
    out.update(status="ok", dataset=S["name"], kind=S["kind"], horizon=res["info"]["horizon_days"], threshold=res["thr"],
               confusion={k: res["sim"][k] for k in ("tp", "fn", "fp", "tn")}, failing=res["sim"]["failing"],
               unplanned=unplanned, planned=planned)
    return out


class Ask(BaseModel):
    question: str


@app.post("/api/assistant")
def assistant(body: Ask, c=Depends(need(*WORK))):
    u = unavailable()
    if u:
        return u
    res = S["result"]
    ds = {"name": S["name"], "kind": S["kind"], "rows": len(S["df"]), "drives": len(S["table"]),
          "date_from": str(S["df"]["date"].min().date()), "date_to": str(S["df"]["date"].max().date())}
    text, sources = A.answer(body.question[:300], res, ds, counts(), P.simulate(res["sim"], 1000, 200))
    audit(c["sub"], "assistant", body.question[:80])
    return {"status": "ok", "answer": text, "sources": sources}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@app.get("/api/system")
def system(baseline: int = 0, c=Depends(need("admin"))):
    files = sorted(glob.glob(os.path.join(PROC, "*")) + glob.glob(os.path.join(MODELS, "*")) + csv_list(UPLOADS))
    base = json.load(open(BASELINE_FILE)) if os.path.exists(BASELINE_FILE) else {}
    cur, out = {}, []
    for p in files:
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, BASE).replace("\\", "/")
        digest = sha256_file(p)
        cur[rel] = digest
        status = "no baseline" if rel not in base else ("match" if base[rel] == digest else "CHANGED")
        out.append({"file": rel, "mb": round(os.path.getsize(p) / 1e6, 2), "sha256": digest[:16] + "...", "status": status})
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
