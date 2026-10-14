/* DriveGuard frontend. Every value comes from the FastAPI backend; the active dataset is always shown. */
const $ = (s) => document.querySelector(s);
let session = null, charts = [], cur = "fleet", timer = null;
try { session = JSON.parse(sessionStorage.getItem("dg") || "null"); } catch (e) {}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "n/a" : Number(n).toLocaleString());
const pct = (v) => (v === null || v === undefined ? "n/a" : (v * 100).toFixed(1) + "%");

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(session ? { Authorization: "Bearer " + session.token } : {}) } });
  if (r.status === 401 && session && !path.includes("login")) { signOut(); throw new Error("Session expired"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || "Request failed");
  return j;
}

function signOut() { sessionStorage.removeItem("dg"); session = null; showLogin(); }
function showLogin() { $("#shell").hidden = true; $("#login").hidden = false; }

const NAV = { fleet: ["admin", "engineer", "viewer"], drives: ["admin", "engineer", "viewer"], model: ["admin", "engineer", "viewer"], sim: ["admin", "engineer"], assistant: ["admin", "engineer"], logs: ["admin"] };

function showApp() {
  $("#login").hidden = true; $("#shell").hidden = false;
  $("#whoName").textContent = session.user; $("#whoRole").textContent = session.role;
  document.querySelectorAll("#nav a").forEach((a) => { a.hidden = !NAV[a.dataset.v].includes(session.role); });
  go("fleet");
}

function go(v, arg) {
  cur = v; clearTimeout(timer);
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.v === (v === "detail" ? "drives" : v)));
  charts.forEach((c) => c.destroy()); charts = [];
  const view = $("#view");
  if (!view.dataset.keep) view.innerHTML = '<p class="muted">Loading...</p>';
  ({ fleet, drives, model, sim, assistant, logs, detail }[v])(view, arg).catch((e) => { view.innerHTML = `<div class="banner">${esc(e.message)}</div>`; });
}

/* Shows a message and retries when the backend is still loading/training. Returns true if the page is blocked. */
function gate(view, j, v, arg) {
  if (j.status === "loading") {
    view.innerHTML = `<h1>Please wait</h1><div class="banner info">${esc(j.message || "Working")}...<div class="prog"><i></i></div></div>`;
    timer = setTimeout(() => { if (cur === v) go(v, arg); }, 3000);
    return true;
  }
  if (j.status === "unavailable") {
    view.innerHTML = `<h1>Not available for this data</h1><div class="banner"><b>${esc(j.reason)}</b><br>${esc(j.hint || "")}</div>${dsButtons()}`;
    bindDs(view);
    return true;
  }
  return false;
}

function chart(id, type, labels, datasets, extra = {}) {
  const c = new Chart(document.getElementById(id), { type, data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: datasets.length > 1 } }, ...extra } });
  charts.push(c);
}
const kpi = (l, n, cls = "") => `<div class="card kpi"><div class="n ${cls}">${fmt(n)}</div><div class="l">${esc(l)}</div></div>`;
const tierTag = (t) => (t ? `<span class="tag ${t === "High" ? "high" : t === "Elevated" ? "elev" : "low"}">${esc(t)}</span>` : "");
const short = (c) => esc(c.replace("smart_", "S").replace("_raw", ""));

/* ---------- dataset controls (upload / switch) ---------- */
function dsButtons() {
  const canEdit = session && session.role !== "viewer";
  if (!canEdit) return "";
  return `<div class="card" style="margin:14px 0"><h2>Dataset</h2>
    <div class="bar">
      <label>Upload Backblaze CSV file(s)<input type="file" id="upl" accept=".csv" multiple></label>
      <button class="btn sm" id="dUp">Upload and analyse</button>
      <button class="btn sm alt" id="dLocal">Use my local files</button>
      <button class="btn sm alt" id="dDemo">Use generated demo data</button>
      <button class="btn sm alt" id="dClear">Remove uploads</button>
    </div><p id="dMsg" class="muted small">Needs columns date, serial_number, model, failure. For a model, upload several daily files (3 or more days).</p></div>`;
}

function bindDs(view) {
  const msg = (t) => { const m = view.querySelector("#dMsg"); if (m) m.textContent = t; };
  const use = async (src) => {
    try { await api("/api/dataset/use?source=" + src, { method: "POST" }); view.dataset.keep = ""; go("fleet"); }
    catch (e) { msg(e.message); }
  };
  const up = view.querySelector("#dUp");
  if (!up) return;
  up.onclick = async () => {
    const files = [...view.querySelector("#upl").files];
    if (!files.length) return msg("Choose one or more CSV files first.");
    try {
      for (let i = 0; i < files.length; i++) {
        msg(`Uploading ${i + 1} of ${files.length}: ${files[i].name} ...`);
        const r = await fetch("/api/upload?name=" + encodeURIComponent(files[i].name), { method: "POST", headers: { Authorization: "Bearer " + session.token }, body: files[i] });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || "Upload failed");
      }
      await use("uploaded");
    } catch (e) { msg(e.message); }
  };
  view.querySelector("#dLocal").onclick = () => use("local");
  view.querySelector("#dDemo").onclick = () => use("demo");
  view.querySelector("#dClear").onclick = async () => { try { await api("/api/uploads/clear", { method: "POST" }); msg("Uploads removed."); } catch (e) { msg(e.message); } };
}

const badge = (f) => `<span class="chip ${f.kind === "demo" ? "demo" : "real"}">${f.kind === "demo" ? "Generated demo data" : f.kind === "uploaded" ? "Your uploaded data" : "Local Backblaze data"}</span>`;

/* ---------- pages ---------- */
async function fleet(view) {
  const f = await api("/api/fleet");
  if (gate(view, f, "fleet")) return;
  const training = f.training.state === "running";
  const cover = f.failed_drives ? Math.round((100 * f.failed_with_indicator) / f.failed_drives) : null;
  const r = f.risk;
  view.innerHTML = `
    <h1>Fleet Dashboard</h1>
    <p>${badge(f)} <span class="muted">${esc(f.source)} &middot; ${esc(f.date_from)} to ${esc(f.date_to)} &middot; ${fmt(f.rows)} daily records</span></p>
    ${f.note ? `<div class="banner">${esc(f.note)}</div>` : ""}
    ${training ? `<div class="banner info"><b>Training the model:</b> ${esc(f.training.message)}...<div class="prog"><i></i></div></div>` : ""}
    ${f.model_error ? `<div class="banner"><b>Model not trained:</b> ${esc(f.model_error)}</div>` : ""}
    ${dsButtons()}
    <div class="grid g4">
      ${kpi("Distinct drives", f.drives)}${kpi("Drive models", f.models)}
      ${kpi("Failures recorded", f.failed_drives, "risk")}
      ${r ? kpi("High-risk operating drives (model)", r.high, "risk") + kpi("Elevated-risk drives (model)", r.elevated, "watch") : kpi("Operating drives with a SMART indicator", f.operating_with_indicator, "watch")}
    </div>
    ${r ? `<div class="card" style="margin-bottom:14px"><h2>Highest-risk operating drives</h2><p class="muted small">Risk = model probability of failure within ${f.horizon} day(s). Alert threshold ${f.threshold.toFixed(2)}.</p>
      <div class="scroll"><table><thead><tr><th>Serial</th><th>Model</th><th>Score</th><th>Tier</th></tr></thead><tbody>
      ${f.top_risk.map((d) => `<tr class="click" data-s="${esc(d.serial)}"><td>${esc(d.serial)}</td><td>${esc(d.model)}</td><td>${d.score.toFixed(3)}</td><td>${tierTag(d.tier)}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
    <div class="grid g2">
      <div class="card"><h2>Failures per day</h2><canvas id="c1"></canvas></div>
      <div class="card"><h2>Drives with non-zero SMART indicator (latest reading)</h2><canvas id="c2"></canvas></div>
      <div class="card"><h2>Largest drive models and their failures</h2><canvas id="c3"></canvas></div>
      <div class="card"><h2>What the data shows</h2>
        <p>${fmt(f.failed_drives)} drives failed in this data. ${cover === null ? "" : `On the failure day, ${fmt(f.failed_with_indicator)} (${cover}%) already had a non-zero value in SMART 5, 187, 188, 197 or 198.`}</p>
        <p class="muted small">That is the failure-day reading, not an early warning; the model on the Model Results page does the early warning.</p></div>
    </div>`;
  bindDs(view);
  view.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => go("detail", tr.dataset.s)));
  chart("c1", "bar", f.daily.map((d) => d.date), [{ label: "Failures", data: f.daily.map((d) => d.failures), backgroundColor: "#c0392b" }]);
  chart("c2", "bar", Object.keys(f.indicator_counts), [{ label: "Drives", data: Object.values(f.indicator_counts), backgroundColor: "#c27c0e" }], { indexAxis: "y" });
  chart("c3", "bar", f.top_models.map((m) => m.model), [
    { label: "Drives", data: f.top_models.map((m) => m.drives), backgroundColor: "#2a5bd7" },
    { label: "Failures", data: f.top_models.map((m) => m.failures), backgroundColor: "#c0392b" }], { indexAxis: "y" });
  if (training) timer = setTimeout(() => { if (cur === "fleet") go("fleet"); }, 3000);
}

let dq = { q: "", status: "all", page: 1 };
async function drives(view) {
  const d = await api("/api/drives?" + new URLSearchParams({ q: dq.q, status: dq.status, page: dq.page }));
  if (gate(view, d, "drives")) return;
  const cols = d.smart_columns;
  const rows = d.rows.map((r) => `<tr class="click" data-s="${esc(r.serial)}"><td>${esc(r.serial)}</td><td>${esc(r.model)}</td><td>${esc(r.date)}</td>
    <td>${r.failed ? '<span class="tag failed">failed</span>' : r.indicators ? `<span class="tag ind">${r.indicators} indicator(s)</span>` : '<span class="tag none">none</span>'}</td>
    <td>${r.score === null ? "n/a" : r.score.toFixed(3)} ${r.failed ? "" : tierTag(r.tier)}</td>
    ${cols.map((c) => `<td>${fmt(r.smart[c])}</td>`).join("")}</tr>`).join("");
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  view.innerHTML = `
    <h1>Drive Risk Details</h1>
    <p class="muted">Latest reading per drive. ${d.has_model ? "Risk is the model's probability of failure in the prediction window." : "Risk score appears when the model finishes training."}</p>
    <div class="bar">
      <label>Search serial or model<input id="dq" value="${esc(dq.q)}"></label>
      <label>Show<select id="ds"><option value="all">All drives</option><option value="high">High risk (model)</option><option value="failed">Failed</option><option value="indicator">Operating with SMART indicator</option></select></label>
      <button class="btn sm" id="dgo">Apply</button>
      <span class="muted">${fmt(d.total)} drives &middot; page ${d.page} of ${pages}</span>
      <button class="btn sm" id="prev" ${d.page <= 1 ? "disabled" : ""}>Prev</button>
      <button class="btn sm" id="next" ${d.page >= pages ? "disabled" : ""}>Next</button>
    </div>
    <div class="card scroll"><table><thead><tr><th>Serial</th><th>Model</th><th>Date</th><th>Status</th><th>Risk</th>${cols.map((c) => `<th>${short(c)}</th>`).join("")}</tr></thead><tbody>${rows || '<tr><td colspan="9">No drives match.</td></tr>'}</tbody></table></div>`;
  $("#ds").value = dq.status;
  $("#dgo").onclick = () => { dq = { q: $("#dq").value, status: $("#ds").value, page: 1 }; go("drives"); };
  $("#prev").onclick = () => { dq.page--; go("drives"); };
  $("#next").onclick = () => { dq.page++; go("drives"); };
  view.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => go("detail", tr.dataset.s)));
}

async function detail(view, serial) {
  const d = await api("/api/drive/" + encodeURIComponent(serial));
  if (gate(view, d, "detail", serial)) return;
  const cols = d.smart_columns, rk = d.risk;
  view.innerHTML = `
    <p><a href="#" id="back">&larr; Back to drives</a></p>
    <h1>${esc(d.serial)}</h1>
    <p class="muted">${esc(d.model)} &middot; capacity ${d.capacity_bytes ? (d.capacity_bytes / 1e12).toFixed(1) + " TB" : "n/a"}</p>
    ${rk ? `<div class="grid g2"><div class="card kpi"><div class="n ${rk.tier === "High" ? "risk" : rk.tier === "Elevated" ? "watch" : "ok"}">${rk.score.toFixed(3)}</div>
        <div class="l">${rk.failed ? "Score on the last reading before failure" : `Risk of failure within ${rk.horizon} day(s)`} &middot; ${tierTag(rk.tier)} &middot; alert threshold ${rk.threshold.toFixed(2)} &middot; ${esc(rk.split)} drive</div></div>
      <div class="card"><h2>Why this score</h2>${d.factors.length ? `<ul>${d.factors.map((f) => `<li><b>${esc(f.feature)}</b>: ${f.value === null ? "n/a" : f.value.toLocaleString()} (fleet median ${f.fleet_median === null ? "n/a" : f.fleet_median.toLocaleString()}) raises the score by ${f.effect.toFixed(3)}</li>`).join("")}</ul>` : '<p class="muted">No single input pushes this score up noticeably.</p>'}
      <p class="muted small">Effect = drop in score when that input is replaced by the fleet median.</p></div></div>` : '<div class="banner info">No risk score for this drive yet (model not available).</div>'}
    <div class="card"><h2>SMART history (raw values, ${d.history.length} reading${d.history.length === 1 ? "" : "s"})</h2>
      ${d.history.length > 1 ? '<canvas id="dc"></canvas>' : '<p class="muted">Only one daily reading exists for this drive, so no trend can be drawn.</p>'}</div>
    <div class="card scroll" style="margin-top:14px"><table><thead><tr><th>Date</th><th>Failure</th>${cols.map((c) => `<th>${short(c)}</th>`).join("")}</tr></thead>
    <tbody>${d.history.slice(-60).map((h) => `<tr><td>${esc(h.date)}</td><td>${h.failure}</td>${cols.map((c) => `<td>${fmt(h.smart[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  $("#back").onclick = (e) => { e.preventDefault(); go("drives"); };
  if (d.history.length > 1) {
    const use = cols.filter((c) => !c.includes("_9_") && !c.includes("_194_") && d.history.some((h) => h.smart[c]));
    const pal = ["#c0392b", "#c27c0e", "#2a5bd7", "#2f855a", "#7b4fd1", "#14213d"];
    chart("dc", "line", d.history.map((h) => h.date), (use.length ? use : cols.slice(0, 1)).map((c, i) => ({
      label: c.replace("smart_", "SMART ").replace("_raw", ""), data: d.history.map((h) => h.smart[c]), borderColor: pal[i % 6], tension: 0.2 })));
  }
}

async function model(view) {
  const m = await api("/api/model");
  if (gate(view, m, "model")) return;
  const t = m.test;
  view.innerHTML = `
    <h1>Model Results</h1>
    <p>${badge(m)} <span class="muted">${esc(m.dataset)}</span></p>
    <div class="banner info">Task: predict whether a drive fails within the next <b>${m.horizon_days} day(s)</b>. Algorithm: <b>${esc(m.algorithm)}</b>. Alert threshold ${m.threshold.toFixed(3)} (chosen on validation drives). All numbers below are on held-out <b>test drives</b>.</div>
    ${t.positives < 30 ? `<div class="banner">Only ${t.positives} positive test rows, so treat these metrics as indicative.</div>` : ""}
    <div class="grid g4">
      ${[["Precision", pct(t.precision)], ["Recall", pct(t.recall)], ["F1-score", pct(t.f1)], ["ROC-AUC", t.roc_auc === null ? "n/a" : t.roc_auc.toFixed(3)], ["PR-AUC", t.pr_auc === null ? "n/a" : t.pr_auc.toFixed(3)]].map(([l, v]) => `<div class="card kpi"><div class="n">${v}</div><div class="l">${l}</div></div>`).join("")}
    </div>
    <div class="grid g2">
      <div class="card"><h2>Confusion matrix (test rows)</h2>
        <div class="cm"><div class="h"></div><div class="h">Predicted fail</div><div class="h">Predicted OK</div>
        <div class="h">Actually fails</div><div>TP<br><b>${fmt(t.tp)}</b></div><div>FN<br><b>${fmt(t.fn)}</b></div>
        <div class="h">Actually OK</div><div>FP<br><b>${fmt(t.fp)}</b></div><div>TN<br><b>${fmt(t.tn)}</b></div></div></div>
      <div class="card"><h2>Most influential inputs</h2>${m.importance.length ? '<canvas id="imp"></canvas>' : '<p class="muted">Not computed for this data.</p>'}</div>
      <div class="card"><h2>Data split (by drive)</h2><table><thead><tr><th>Set</th><th>Drives</th><th>Failing drives</th></tr></thead><tbody>
        ${["train", "val", "test"].map((k) => `<tr><td>${k}</td><td>${fmt(m.split[k].drives)}</td><td>${fmt(m.split[k].failing)}</td></tr>`).join("")}</tbody></table>
        <p class="muted small">${fmt(m.labelled_rows)} labelled drive-days, ${fmt(m.positive_rows)} positive, ${m.days_covered} days covered.</p></div>
      <div class="card"><h2>How labels were built</h2><ul>${m.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>
    </div>`;
  if (m.importance.length) chart("imp", "bar", m.importance.map((i) => i.feature), [{ label: "Share", data: m.importance.map((i) => +(i.share * 100).toFixed(1)), backgroundColor: "#2a5bd7" }], { indexAxis: "y" });
}

let costs = { unplanned: 1000, planned: 200 };
async function sim(view) {
  const s = await api(`/api/sim?unplanned=${costs.unplanned}&planned=${costs.planned}`);
  if (gate(view, s, "sim")) return;
  const c = s.confusion;
  view.innerHTML = `
    <h1>Failure Simulation</h1>
    <p>${badge(s)} <span class="muted">${esc(s.dataset)}</span></p>
    <div class="banner info">Reactive = replace a drive only after it fails. Proactive = replace a drive when the model's score reaches the alert threshold (${s.threshold.toFixed(2)}). Simulated on <b>${fmt(s.test_drives)} unseen test drives</b> (${fmt(s.failing)} of them fail). Costs are <b>your assumptions</b>: edit them below.</div>
    <div class="bar"><label>Cost of an unplanned failure<input id="cu" type="number" min="0" value="${s.unplanned}"></label>
      <label>Cost of a planned replacement<input id="cp" type="number" min="0" value="${s.planned}"></label><button class="btn sm" id="cgo">Recalculate</button></div>
    <div class="grid g4">
      ${kpi("Reactive total cost", s.reactive_cost)}${kpi("Proactive total cost", s.proactive_cost)}
      ${kpi("Saving from proactive", s.saving, s.saving >= 0 ? "ok" : "risk")}${kpi("Failures caught early", s.avoided, "ok")}
    </div>
    <div class="grid g2">
      <div class="card"><h2>Unplanned failures</h2><canvas id="s1"></canvas></div>
      <div class="card"><h2>Drive-level outcome of the alerts</h2>
        <ul><li>Caught before failing: <b>${c.tp}</b></li><li>Missed (still failed unplanned): <b>${c.fn}</b></li>
        <li>Unnecessary replacements (false alarms): <b>${c.fp}</b></li><li>Healthy and not flagged: <b>${fmt(c.tn)}</b></li>
        <li>Median warning time for caught drives: <b>${s.median_lead_days === null ? "n/a" : s.median_lead_days + " day(s)"}</b></li></ul>
        <p class="muted small">A drive counts as flagged if any of its readings before failure reached the threshold. Drives that fail on their first recorded day cannot be flagged.</p></div>
    </div>`;
  $("#cgo").onclick = () => { costs = { unplanned: +$("#cu").value || 0, planned: +$("#cp").value || 0 }; go("sim"); };
  chart("s1", "bar", ["Reactive", "Proactive (model)"], [{ label: "Unplanned failures", data: [s.unplanned_reactive, s.unplanned_proactive], backgroundColor: ["#c0392b", "#2f855a"] }]);
}

let chat = [];
async function assistant(view) {
  const probe = await api("/api/assistant", { method: "POST", body: JSON.stringify({ question: "fleet summary" }) });
  if (gate(view, probe, "assistant")) return;
  if (!chat.length) chat = [{ me: false, t: "Hello. I answer from the model results only. Ask about a drive serial number, the top risk drives, model accuracy, important factors, the simulation or the fleet." }];
  const draw = () => {
    $("#chat").innerHTML = chat.map((m) => `<div class="msg ${m.me ? "me" : "bot"}">${esc(m.t)}</div>`).join("");
    $("#chat").scrollTop = 1e9;
  };
  view.innerHTML = `
    <h1>AI Assistant</h1>
    <div class="banner info">Rule-based assistant over the trained model's output. It uses no outside AI service and never invents numbers.</div>
    <div class="card"><div id="chat" class="chat"></div>
      <div>${["Top risk drives", "Model accuracy", "What are the important factors", "Simulation savings", "Fleet summary"].map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <form id="ask" class="row" style="margin-top:10px"><input id="q" placeholder="Ask a question or type a drive serial..." autocomplete="off"><button class="btn">Ask</button></form></div>`;
  draw();
  const send = async (q) => {
    if (!q.trim()) return;
    chat.push({ me: true, t: q }); draw();
    try { const r = await api("/api/assistant", { method: "POST", body: JSON.stringify({ question: q }) });
      chat.push({ me: false, t: r.answer + (r.sources && r.sources.length ? "\n\nBased on: " + r.sources.join(", ") : "") });
    } catch (e) { chat.push({ me: false, t: e.message }); }
    draw();
  };
  $("#ask").onsubmit = (e) => { e.preventDefault(); const q = $("#q").value; $("#q").value = ""; send(q); };
  view.querySelectorAll(".chip[data-q]").forEach((b) => (b.onclick = () => send(b.dataset.q)));
}

async function logs(view) {
  const [l, s] = await Promise.all([api("/api/logs"), api("/api/system")]);
  const rep = s.report;
  view.innerHTML = `
    <h1>System Logs</h1>
    <div class="grid g2">
      <div class="card"><h2>Audit log integrity</h2>
        <p class="${l.chain_ok ? "ok-t" : "bad-t"}"><b>${l.chain_ok ? "Hash chain verified" : "Chain BROKEN at entry " + l.broken_at}</b> &middot; ${fmt(l.count)} entries</p>
        <p class="muted small">Each entry stores the SHA-256 of itself and the previous entry, so editing an old line is detected.</p></div>
      <div class="card"><h2>Preprocessing report</h2>${rep ? `<p>${fmt(rep.original_rows)} rows, ${fmt(rep.original_columns)} columns in; ${fmt(rep.cleaned_rows)} rows, ${fmt(rep.cleaned_columns)} columns out.<br>Duplicates removed: ${fmt(rep.duplicate_rows_removed)}. Date range: ${esc((rep.date_range || {}).start)} to ${esc((rep.date_range || {}).end)}.</p>` : '<p class="muted">preprocessing_report.json not found.</p>'}</div>
    </div>
    <div class="card scroll"><div class="bar"><h2 style="margin:0">File integrity</h2><button class="btn sm" id="base">Record current hashes as baseline</button></div>
      <table><thead><tr><th>File</th><th>MB</th><th>SHA-256</th><th>Status</th></tr></thead><tbody>
      ${s.files.map((f) => `<tr><td>${esc(f.file)}</td><td>${f.mb}</td><td>${esc(f.sha256)}</td><td class="${f.status === "CHANGED" ? "bad-t" : f.status === "match" ? "ok-t" : ""}">${esc(f.status)}</td></tr>`).join("")}</tbody></table></div>
    <div class="card scroll" style="margin-top:14px"><h2>Recent activity</h2><table><thead><tr><th>Time (UTC)</th><th>User</th><th>Action</th><th>Detail</th><th>OK</th></tr></thead><tbody>
      ${l.recent.map((r) => `<tr><td>${esc(r.ts)}</td><td>${esc(r.user)}</td><td>${esc(r.action)}</td><td>${esc(r.detail)}</td><td class="${r.ok ? "ok-t" : "bad-t"}">${r.ok ? "yes" : "no"}</td></tr>`).join("")}</tbody></table></div>`;
  $("#base").onclick = async () => { await api("/api/system?baseline=1"); go("logs"); };
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try {
    session = await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("#u").value, password: $("#p").value }) });
    try { sessionStorage.setItem("dg", JSON.stringify(session)); } catch (x) {}
    $("#p").value = ""; showApp();
  } catch (err) { $("#loginErr").textContent = err.message; }
});
$("#out").onclick = signOut;
document.querySelectorAll("#nav a").forEach((a) => (a.onclick = () => go(a.dataset.v)));
if (session) showApp(); else showLogin();
