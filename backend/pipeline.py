"""
DriveGuard pipeline: load -> future-failure labels -> features -> model -> evaluation -> simulation.

Label definition (assumption, stated in the UI too):
  A drive-day row is POSITIVE if that drive's failure is recorded 1..H days after the row.
  Rows on/after the failure day are not used. Rows of drives that never fail are NEGATIVE
  only if a full H-day window of later data exists (otherwise they are censored and dropped).
Leakage control:
  * the failure column is never a feature
  * features use only the current and PAST readings of the same drive
  * train / validation / test are split by DRIVE (a drive is never in two sets)
  * the decision threshold is chosen on validation drives, metrics are reported on test drives
"""
import os
import re

import numpy as np
import pandas as pd

KEY = [5, 9, 187, 188, 194, 197, 198]
INDICATORS = [5, 187, 188, 197, 198]
LABELS = {5: "Reallocated sectors", 9: "Power-on hours", 187: "Reported uncorrectable errors",
          188: "Command timeout", 194: "Temperature (C)", 197: "Current pending sectors",
          198: "Offline uncorrectable sectors"}
BASE = ["date", "serial_number", "model", "capacity_bytes", "failure"]
REQUIRED = ["date", "serial_number", "model", "failure"]
MAX_ROWS = 2_500_000
MAX_SMART = 24


class NotEnoughData(Exception):
    pass


def raw(k):
    return f"smart_{k}_raw"


def nice(f):
    pre = ""
    if f.startswith("chg1_"):
        pre, f = "Change since previous reading: ", f[5:]
    elif f.startswith("chg7_"):
        pre, f = "Change vs 7 readings ago: ", f[5:]
    if f == "capacity_tb":
        return "Capacity (TB)"
    m = re.match(r"smart_(\d+)_raw$", f)
    if m:
        k = int(m.group(1))
        return (pre + f"SMART {k} {LABELS.get(k, '')}").strip()
    return pre + f


def feature_id(f):
    m = re.search(r"smart_(\d+)_raw", f)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------- loading
def check_header(path):
    head = pd.read_csv(path, nrows=0).columns.tolist()
    missing = [c for c in REQUIRED if c not in head]
    if missing:
        raise ValueError(f"{os.path.basename(path)} is missing required columns: {', '.join(missing)}")
    return head


def choose_columns(path):
    head = check_header(path)
    sm = [c for c in head if c.startswith("smart_") and c.endswith("_raw")]
    fill = pd.Series(dtype=float)
    if sm:
        fill = pd.read_csv(path, nrows=50000, usecols=sm, low_memory=False).notna().mean()
    key = [raw(k) for k in KEY if raw(k) in sm]
    others = sorted([c for c in sm if c not in key and fill.get(c, 0) >= 0.2], key=lambda c: -fill[c])
    return [c for c in BASE if c in head], key + others[: MAX_SMART - len(key)]


def load_csvs(paths, progress=print):
    base, smart = choose_columns(paths[0])
    use = set(base + smart)
    progress(f"Scanning {len(paths)} file(s)")
    total, failing = 0, set()
    for p in paths:
        h = pd.read_csv(p, usecols=["serial_number", "failure"], low_memory=False)
        total += len(h)
        failing.update(h.loc[pd.to_numeric(h["failure"], errors="coerce") == 1, "serial_number"])
    frac, sampled = 1.0, False
    if total > MAX_ROWS:
        est_fail_rows = len(failing) * len(paths)
        frac = max(0.02, (MAX_ROWS - est_fail_rows) / max(1, total))
        sampled = True
    parts = []
    for i, p in enumerate(paths, 1):
        progress(f"Reading file {i} of {len(paths)}")
        d = pd.read_csv(p, usecols=lambda c: c in use, low_memory=False)
        if sampled:
            h = pd.util.hash_pandas_object(d["serial_number"], index=False) % 1000
            d = d[d["serial_number"].isin(failing) | (h < frac * 1000)]
        for c in smart + (["capacity_bytes"] if "capacity_bytes" in d.columns else []):
            d[c] = pd.to_numeric(d[c], errors="coerce").astype("float32")
        parts.append(d)
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["failure"] = pd.to_numeric(df["failure"], errors="coerce")
    df = df.dropna(subset=["date", "serial_number", "failure"])
    df = df[df["failure"].isin([0, 1])]
    df["failure"] = df["failure"].astype("int8")
    df = df.drop_duplicates(["serial_number", "date"], keep="last")
    df = df.sort_values(["serial_number", "date"]).reset_index(drop=True)
    info = {"files": len(paths), "rows_in_files": int(total), "sampled": sampled,
            "failing_drives_in_files": len(failing), "kept_fraction_of_other_drives": round(frac, 3)}
    return df, info


# ------------------------------------------------------------------- demo data
def generate_demo(seed=7, n=1500, days=60):
    """Synthetic data in the Backblaze column layout. NOT real Backblaze data."""
    rng = np.random.default_rng(seed)
    models = [("ST4000DM000", 4e12, True), ("ST8000NM0055", 8e12, True), ("ST12000NM0008", 12e12, True),
              ("HGST HUH721212ALE600", 12e12, True), ("WDC WUH721414ALE6L4", 14e12, True),
              ("TOSHIBA MG07ACA14TA", 14e12, False)]
    start = pd.Timestamp("2026-04-01")
    failing = rng.choice(n, int(n * 0.07), replace=False)
    fail_day = {int(i): int(rng.integers(8, days)) for i in failing}
    degrade = {i: rng.random() < 0.65 for i in fail_day}
    cols = {k: [] for k in ["date", "serial_number", "model", "capacity_bytes", "failure",
                            "smart_5_raw", "smart_9_raw", "smart_187_raw", "smart_188_raw",
                            "smart_194_raw", "smart_197_raw", "smart_198_raw"]}
    for i in range(n):
        m = models[int(rng.integers(len(models)))]
        last = fail_day.get(i, days - 1)
        t = np.arange(last + 1)
        L = len(t)
        s5, s187, s188, s197, s198 = (np.zeros(L) for _ in range(5))
        temp = rng.normal(32, 3) + rng.normal(0, 0.8, L)
        if i not in fail_day:
            if rng.random() < 0.04:
                s5[:] = rng.integers(1, 30)
            if rng.random() < 0.03:
                s188 = np.cumsum(rng.random(L) < 0.05) * float(rng.integers(1, 4))
            if rng.random() < 0.02:
                s197[:] = rng.integers(1, 3)
        else:
            temp = temp + 2.5
            if degrade[i]:
                ramp = np.clip(t - max(0, last - int(rng.integers(5, 21))), 0, None)
                s5 = np.floor(ramp * rng.uniform(0.5, 6))
                s197 = np.floor(ramp * rng.uniform(0.3, 3))
                if rng.random() < 0.6:
                    s198 = np.floor(ramp * rng.uniform(0.2, 2))
                if m[2] and rng.random() < 0.6:
                    s187 = np.floor(ramp * rng.uniform(0.1, 1.5))
                temp = temp + ramp * 0.1
        cols["date"].append((start + pd.to_timedelta(t, unit="D")).values)
        cols["serial_number"].append(np.full(L, f"DEMO-{i:05d}"))
        cols["model"].append(np.full(L, m[0]))
        cols["capacity_bytes"].append(np.full(L, m[1]))
        fl = np.zeros(L, dtype=int)
        if i in fail_day:
            fl[-1] = 1
        cols["failure"].append(fl)
        cols["smart_5_raw"].append(s5)
        cols["smart_9_raw"].append(rng.integers(3000, 45000) + 24 * t)
        cols["smart_187_raw"].append(s187 if m[2] else np.full(L, np.nan))
        cols["smart_188_raw"].append(s188)
        cols["smart_194_raw"].append(np.round(temp))
        cols["smart_197_raw"].append(s197)
        cols["smart_198_raw"].append(s198)
    df = pd.DataFrame({k: np.concatenate(v) for k, v in cols.items()})
    for c in df.columns:
        if c.startswith("smart_") or c == "capacity_bytes":
            df[c] = df[c].astype("float32")
    df["failure"] = df["failure"].astype("int8")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["serial_number", "date"]).reset_index(drop=True)


# -------------------------------------------------------------- labels/features
def horizon(span_days):
    return max(1, min(7, span_days - 1))


def make_labels(df, H):
    fd = df.loc[df["failure"] == 1].groupby("serial_number")["date"].min()
    fdate = df["serial_number"].map(fd)
    days_to = (fdate - df["date"]).dt.days
    last = df["date"].max()
    pos = days_to.between(1, H)
    neg = (days_to > H) | (fdate.isna() & (df["date"] <= last - pd.Timedelta(days=H)))
    return pos.astype("int8").to_numpy(), (pos | neg).to_numpy(), days_to, fdate


def build_features(df, cols):
    g = df.groupby("serial_number", sort=False)
    X = df[cols].copy()
    for k in INDICATORS:
        c = raw(k)
        if c in cols:
            X["chg1_" + c] = df[c] - g[c].shift(1)
            X["chg7_" + c] = df[c] - g[c].shift(7)
    if "capacity_bytes" in df.columns:
        X["capacity_tb"] = df["capacity_bytes"] / 1e12
    return X.astype("float32")


def _best_threshold(y, s):
    from sklearn.metrics import precision_recall_curve
    if y.sum() == 0:
        return 0.5
    p, r, t = precision_recall_curve(y, s)
    if len(t) == 0:
        return 0.5
    f = 2 * p[:-1] * r[:-1] / np.maximum(p[:-1] + r[:-1], 1e-9)
    return float(t[int(np.nanargmax(f))])


def _make_model(spw):
    try:
        from xgboost import XGBClassifier
        return "XGBoost", XGBClassifier(n_estimators=250, max_depth=4, learning_rate=0.08, subsample=0.8,
                                        colsample_bytree=0.8, scale_pos_weight=spw, tree_method="hist",
                                        eval_metric="aucpr", n_jobs=-1, random_state=42)
    except Exception:
        from sklearn.ensemble import HistGradientBoostingClassifier
        return "HistGradientBoosting (XGBoost not installed)", HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.08, max_iter=250, random_state=42)


def train(df, progress=print):
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import average_precision_score, roc_auc_score

    df = df.sort_values(["serial_number", "date"]).reset_index(drop=True)
    span = (df["date"].max() - df["date"].min()).days
    if span < 2:
        raise NotEnoughData(f"This data covers only {span + 1} day(s). Future-failure labels need at least 3 days of readings.")
    H = horizon(span)
    progress("Building future-failure labels")
    y, keep, days_to, fdate = make_labels(df, H)
    if y[keep].sum() < 20:
        raise NotEnoughData(f"Only {int(y[keep].sum())} drive-days fall within {H} day(s) before a failure; need at least 20 to train.")
    smart = [c for c in df.columns if c.startswith("smart_")]
    progress("Building features")
    X = build_features(df, smart)

    ever = df.groupby("serial_number")["failure"].max()
    rng = np.random.default_rng(42)
    parts = []
    for cls in (0, 1):
        s = rng.permutation(ever.index[ever.values == cls].to_numpy())
        n = len(s)
        idx = np.arange(n)
        parts.append(pd.Series(np.where(idx < int(n * .6), "train", np.where(idx < int(n * .8), "val", "test")), index=s))
    split = pd.concat(parts)
    rs = df["serial_number"].map(split).to_numpy()
    tr, va, te = (keep & (rs == k) for k in ("train", "val", "test"))
    if y[tr].sum() < 5:
        raise NotEnoughData("Too few failing drives in the training split (need at least 5 positive drive-days).")

    progress("Training model")
    i_tr = np.flatnonzero(tr)
    pos_i, neg_i = i_tr[y[i_tr] == 1], i_tr[y[i_tr] == 0]
    if len(neg_i) > 400_000:
        neg_i = rng.choice(neg_i, 400_000, replace=False)
    fit_i = np.concatenate([pos_i, neg_i])
    spw = float(min(200, len(neg_i) / max(1, len(pos_i))))
    algo, model = _make_model(spw)
    if algo.startswith("XGBoost"):
        model.fit(X.iloc[fit_i], y[fit_i])
    else:
        model.fit(X.iloc[fit_i], y[fit_i], sample_weight=np.where(y[fit_i] == 1, spw, 1.0))
    progress("Scoring and evaluating")
    score = model.predict_proba(X)[:, 1].astype("float32")
    thr = _best_threshold(y[va], score[va])

    pred = score[te] >= thr
    yt = y[te]
    tp, fp = int((pred & (yt == 1)).sum()), int((pred & (yt == 0)).sum())
    fn, tn = int((~pred & (yt == 1)).sum()), int((~pred & (yt == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    both = len(np.unique(yt)) == 2
    test = {"rows": int(te.sum()), "positives": int(yt.sum()), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": prec, "recall": rec, "f1": f1,
            "roc_auc": float(roc_auc_score(yt, score[te])) if both else None,
            "pr_auc": float(average_precision_score(yt, score[te])) if both else None,
            "base_rate": float(yt.mean()) if len(yt) else None}

    progress("Computing feature importance")
    imp = []
    for mask in (te, va, tr):
        if y[mask].sum() >= 1:
            ids = np.flatnonzero(mask)
            p_, n_ = ids[y[ids] == 1], ids[y[ids] == 0]
            if len(n_) > 15000:
                n_ = rng.choice(n_, 15000, replace=False)
            ids = np.concatenate([p_, n_])
            pi = permutation_importance(model, X.iloc[ids], y[ids], scoring="average_precision",
                                        n_repeats=3, random_state=0, n_jobs=1)
            v = np.clip(pi.importances_mean, 0, None)
            tot = v.sum()
            order = np.argsort(-v)[:12]
            imp = [{"feature": nice(X.columns[j]), "share": float(v[j] / tot) if tot else 0.0} for j in order if v[j] > 0]
            break

    # one scoring row per drive: last non-failure reading, else the failure row
    nonfail = df["failure"] == 0
    last_nf = df.loc[nonfail].groupby("serial_number").tail(1)
    last_all = df.groupby("serial_number").tail(1)
    rows = pd.concat([last_nf, last_all[~last_all["serial_number"].isin(last_nf["serial_number"])]])
    rk = pd.DataFrame({"score": score[rows.index], "failed": ever.reindex(rows["serial_number"]).to_numpy().astype(bool),
                       "split": split.reindex(rows["serial_number"]).to_numpy()}, index=rows["serial_number"].to_numpy())
    rk["tier"] = np.where(rk["score"] >= thr, "High", np.where(rk["score"] >= thr / 2, "Elevated", "Low"))
    Xrk = X.loc[rows.index].copy()
    Xrk.index = rows["serial_number"].to_numpy()
    med = X.iloc[fit_i].median()

    # drive-level result on TEST drives (for the simulation)
    d = pd.DataFrame({"s": df["serial_number"], "date": df["date"], "score": score, "dt": days_to.to_numpy(), "sp": rs})
    t = d[(d["sp"] == "test") & (d["dt"].isna() | (d["dt"] >= 1))]
    first_flag = t[t["score"] >= thr].groupby("s")["date"].min()
    test_drives = split.index[split.values == "test"]
    fail_d = ever.reindex(test_drives).to_numpy().astype(bool)
    flagged = np.isin(test_drives, first_flag.index)
    fd_map = fdate.groupby(df["serial_number"]).first()
    lead = [(fd_map[s] - first_flag[s]).days for s in test_drives[fail_d & flagged]]
    sim = {"test_drives": int(len(test_drives)), "failing": int(fail_d.sum()),
           "tp": int((fail_d & flagged).sum()), "fn": int((fail_d & ~flagged).sum()),
           "fp": int((~fail_d & flagged).sum()), "tn": int((~fail_d & ~flagged).sum()),
           "median_lead_days": float(np.median(lead)) if lead else None}

    split_counts = {k: {"drives": int((split == k).sum()), "failing": int(ever[split[split == k].index].sum())}
                    for k in ("train", "val", "test")}
    info = {"algorithm": algo, "horizon_days": H, "threshold": thr, "days_covered": span + 1,
            "labelled_rows": int(keep.sum()), "positive_rows": int(y[keep].sum()),
            "features": [nice(c) for c in X.columns], "split": split_counts, "test": test, "importance": imp,
            "assumptions": [
                f"A row is positive if the drive fails within the next {H} day(s). The horizon is shortened automatically when the data covers few days.",
                "Rows on or after the failure day are not used. Healthy drives' last readings are dropped if a full horizon of later data does not exist.",
                "Features use only current and past readings of the same drive. The failure column is never a feature.",
                "Drives are split 60/20/20 into train/validation/test, so no drive appears in two sets. The threshold is tuned on validation drives; metrics are on test drives.",
            ]}
    if test["positives"] < 30:
        info["assumptions"].append(f"Caution: only {test['positives']} positive test rows, so these metrics are indicative, not reliable.")
    progress("Done")
    return {"model": model, "med": med, "Xrk": Xrk, "rk": rk, "sim": sim, "info": info, "thr": thr}


def explain(res, serial, top=5):
    """Occlusion: how much the risk score falls when a feature is set to the fleet median."""
    Xrk, model, med = res["Xrk"], res["model"], res["med"]
    if serial not in Xrk.index:
        return []
    x = Xrk.loc[[serial]]
    base = float(model.predict_proba(x)[0, 1])
    batch = pd.concat([x] * len(Xrk.columns), ignore_index=True)
    for j, c in enumerate(Xrk.columns):
        batch.iloc[j, j] = med[c]
    drop = base - model.predict_proba(batch)[:, 1]
    out = []
    for j in np.argsort(-drop)[:top]:
        if drop[j] > 0.001:
            c = Xrk.columns[j]
            v = x.iloc[0, j]
            out.append({"feature": nice(c), "value": None if pd.isna(v) else float(v),
                        "fleet_median": None if pd.isna(med[c]) else float(med[c]), "effect": float(drop[j])})
    return out


def simulate(sim, unplanned, planned):
    f, tp, fn, fp = sim["failing"], sim["tp"], sim["fn"], sim["fp"]
    reactive = f * unplanned
    proactive = tp * planned + fn * unplanned + fp * planned
    return {"reactive_cost": reactive, "proactive_cost": proactive, "saving": reactive - proactive,
            "unplanned_reactive": f, "unplanned_proactive": fn, "avoided": tp, "unnecessary_replacements": fp,
            "test_drives": sim["test_drives"], "median_lead_days": sim["median_lead_days"]}
