"""Rule-based assistant. It answers ONLY from the computed model results; no external LLM is used."""
import re

from . import pipeline as P

ADVICE = {
    "sectors": "Back up the data now, run a full SMART self-test and schedule replacement in the next maintenance window.",
    "temp": "Check rack airflow and cooling, then re-check the temperature trend.",
    "timeout": "Check cabling, backplane and controller logs for the cause of command timeouts.",
    "age": "The drive is old. Plan lifecycle replacement and keep a spare ready.",
    "other": "Keep the drive under daily monitoring and review it again after the next readings.",
}


def _advice(feature_names):
    for f in feature_names:
        m = re.search(r"SMART (\d+)", f)
        k = int(m.group(1)) if m else None
        if k in (5, 187, 197, 198):
            return ADVICE["sectors"]
        if k == 194:
            return ADVICE["temp"]
        if k == 188:
            return ADVICE["timeout"]
        if k == 9:
            return ADVICE["age"]
    return ADVICE["other"]


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


def answer(q, res, ds, fleet_counts, sim_costs):
    ql = q.lower()
    info, rk = res["info"], res["rk"]
    for tok in re.findall(r"[\w\-\.]+", q):
        if tok in rk.index:
            r = rk.loc[tok]
            if r["failed"]:
                return (f"{tok} is recorded as failed in this dataset. Its last reading before failure had a model score of {r['score']:.2f} "
                        f"(split: {r['split']}).", ["drive record"])
            ex = P.explain(res, tok)
            lines = [f"{tok}: risk score {r['score']:.2f}, tier {r['tier']} (alert threshold {res['thr']:.2f}). "
                     f"Prediction window: failure within {info['horizon_days']} day(s). This drive was in the {r['split']} set."]
            if ex:
                lines.append("What pushes the score up:")
                lines += [f"- {e['feature']}: value {e['value']:.4g} (fleet median {e['fleet_median']:.4g})" for e in ex]
            lines.append("Suggested action: " + _advice([e["feature"] for e in ex]))
            return "\n".join(lines), ["model score", "occlusion explanation"]
    if re.search(r"top|highest|riskiest|most at risk|critical|worst|high risk", ql):
        top = rk[~rk["failed"]].sort_values("score", ascending=False).head(5)
        if top.empty:
            return "No operating drives are scored.", []
        lines = ["Highest-risk operating drives:"] + [f"- {s}: score {r.score:.2f} ({r.tier})" for s, r in top.iterrows()]
        return "\n".join(lines), ["model score"]
    if re.search(r"accura|precision|recall|f1|perform|how good|metric|evaluat", ql):
        t = info["test"]
        return (f"{info['algorithm']} on held-out test drives ({t['rows']:,} rows, {t['positives']} positive): "
                f"precision {_pct(t['precision'])}, recall {_pct(t['recall'])}, F1 {_pct(t['f1'])}, "
                f"ROC-AUC {'n/a' if t['roc_auc'] is None else round(t['roc_auc'], 3)}. "
                f"Confusion matrix: TP {t['tp']}, FP {t['fp']}, FN {t['fn']}, TN {t['tn']}."), ["test-set metrics"]
    if re.search(r"simulat|cost|saving|reactive|proactive|money", ql):
        s = sim_costs
        return (f"On {s['test_drives']:,} unseen test drives: reactive maintenance has {s['unplanned_reactive']} unplanned failures; "
                f"acting on the model's alerts leaves {s['unplanned_proactive']} unplanned, avoids {s['avoided']}, "
                f"and causes {s['unnecessary_replacements']} unnecessary replacements. "
                f"With the cost assumptions on the Simulation page, the saving is {s['saving']:,.0f}."), ["simulation"]
    if re.search(r"feature|important|why|factor|indicator|driver", ql):
        if not info["importance"]:
            return "Feature importance could not be computed for this data.", []
        return "Most influential inputs (share of importance):\n" + "\n".join(
            f"- {i['feature']}: {i['share'] * 100:.0f}%" for i in info["importance"][:6]), ["permutation importance"]
    if re.search(r"how many|count|fleet|total|summary|overview|status", ql):
        c = fleet_counts
        return (f"{c['drives']:,} drives. Model tiers for operating drives: High {c['high']}, Elevated {c['elevated']}, Low {c['low']}. "
                f"Failures recorded: {c['failed']}."), ["fleet table"]
    if re.search(r"data|source|upload|dataset", ql):
        return f"Current data: {ds['name']} ({ds['kind']}), {ds['rows']:,} rows, {ds['drives']:,} drives, {ds['date_from']} to {ds['date_to']}.", ["dataset"]
    return ("I can answer from the model results. Try: 'top risk drives', a drive serial number, 'model accuracy', "
            "'what are the important factors', 'simulation savings' or 'fleet summary'."), []
