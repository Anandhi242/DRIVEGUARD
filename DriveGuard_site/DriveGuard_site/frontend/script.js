/* DriveGuard frontend. All values come from the FastAPI backend (real Backblaze data). */
const $ = (s) => document.querySelector(s);
let session = null, charts = [];
try { session = JSON.parse(sessionStorage.getItem("dg") || "null"); } catch (e) {}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "n/a" : Number(n).toLocaleString());

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(session ? { Authorization: "Bearer " + session.token } : {}) } });
  if (r.status === 401 && session && !path.includes("login")) { signOut(); throw new Error("Session expired"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || "Request failed");
  return j;
}

function signOut() { sessionStorage.removeItem("dg"); session = null; showLogin(); }
function showLogin() { $("#shell").hidden = true; $("#login").hidden = false; }

const NAV = { fleet: ["admin", "engineer", "viewer"], drives: ["admin", "engineer", "viewer"], sim: ["admin", "engineer"], assistant: ["admin", "engineer"], logs: ["admin"] };

function showApp() {
  $("#login").hidden = true; $("#shell").hidden = false;
  $("#whoName").textContent = session.user; $("#whoRole").textContent = session.role;
  document.querySelectorAll("#nav a").forEach((a) => { a.hidden = !NAV[a.dataset.v].includes(session.role); });
  go("fleet");
}

function go(v, arg) {
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.v === v));
  charts.forEach((c) => c.destroy()); charts = [];
  const view = $("#view");
  view.innerHTML = '<p class="muted">Loading...</p>';
  ({ fleet, drives, sim, assistant, logs, detail }[v])(view, arg).catch((e) => { view.innerHTML = `<div class="banner">${esc(e.message)}</div>`; });
}

function chart(id, type, labels, datasets, extra = {}) {
  const c = new Chart(document.getElementById(id), { type, data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: datasets.length > 1 } }, ...extra } });
  charts.push(c);
}
const kpi = (l, n, cls = "") => `<div class="card kpi"><div class="n ${cls}">${fmt(n)}</div><div class="l">${esc(l)}</div></div>`;

async function fleet(view) {
  const f = await api("/api/fleet");
  const cover = f.failed_drives ? Math.round((100 * f.failed_with_indicator) / f.failed_drives) : null;
  view.innerHTML = `
    <h1>Fleet Dashboard</h1>
    <p class="muted">Source: ${esc(f.source)} &middot; ${esc(f.date_from)} to ${esc(f.date_to)} &middot; ${fmt(f.rows)} daily records</p>
    <div class="banner info"><b>Model status:</b> ${esc(f.model_status.note)} Counts below are observed directly from the Backblaze data. SMART indicator flags are a simple rule (value above 0), not a prediction.</div>
    <div class="grid g4">
      ${kpi("Distinct drives", f.drives)}${kpi("Drive models", f.models)}
      ${kpi("Failures recorded", f.failed_drives, "risk")}
      ${kpi("Operating drives with a SMART indicator", f.operating_with_indicator, "watch")}
    </div>
    <div class="grid g2">
      <div class="card"><h2>Records and failures per day</h2><canvas id="c1"></canvas></div>
      <div class="card"><h2>Drives with non-zero SMART indicator (latest reading)</h2><canvas id="c2"></canvas></div>
      <div class="card"><h2>Largest drive models and their failures</h2><canvas id="c3"></canvas></div>
      <div class="card"><h2>What the data shows</h2>
        <p>${fmt(f.failed_drives)} drives failed in this data. ${cover === null ? "" : `On the day of failure, ${fmt(f.failed_with_indicator)} of them (${cover}%) already had at least one non-zero indicator among SMART 5, 187, 188, 197, 198.`}</p>
        <p class="muted small">This is the failure-day reading, not an early warning. Early warning needs the future-failure labels and model (Module 3). SMART columns loaded: ${esc(f.smart_columns_loaded.join(", "))}.</p>
      </div>
    </div>`;
  chart("c1", "bar", f.daily.map((d) => d.date), [{ label: "Failures", data: f.daily.map((d) => d.failures), backgroundColor: "#c0392b" }]);
  chart("c2", "bar", Object.keys(f.indicator_counts), [{ label: "Drives", data: Object.values(f.indicator_counts), backgroundColor: "#c27c0e" }], { indexAxis: "y" });
  chart("c3", "bar", f.top_models.map((m) => m.model), [
    { label: "Drives", data: f.top_models.map((m) => m.drives), backgroundColor: "#2a5bd7" },
    { label: "Failures", data: f.top_models.map((m) => m.failures), backgroundColor: "#c0392b" }], { indexAxis: "y" });
}

let dq = { q: "", status: "all", page: 1 };
async function drives(view) {
  const qs = new URLSearchParams({ q: dq.q, status: dq.status, page: dq.page });
  const d = await api("/api/drives?" + qs);
  const cols = d.smart_columns;
  const head = cols.map((c) => `<th>${esc(c.replace("smart_", "S").replace("_raw", ""))}</th>`).join("");
  const rows = d.rows.map((r) => `<tr class="click" data-s="${esc(r.serial)}"><td>${esc(r.serial)}</td><td>${esc(r.model)}</td><td>${esc(r.date)}</td>
    <td>${r.failed ? '<span class="tag failed">failed</span>' : r.indicators ? `<span class="tag ind">${r.indicators} indicator(s)</span>` : '<span class="tag none">none</span>'}</td>
    ${cols.map((c) => `<td>${fmt(r.smart[c])}</td>`).join("")}</tr>`).join("");
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  view.innerHTML = `
    <h1>Drive Risk Details</h1>
    <p class="muted">Latest reading per drive. Risk score appears here once a model is trained; until then rows show real SMART values and indicator flags only.</p>
    <div class="bar">
      <label>Search serial or model<input id="dq" value="${esc(dq.q)}"></label>
      <label>Show<select id="ds"><option value="all">All drives</option><option value="failed">Failed</option><option value="indicator">Operating with indicator</option></select></label>
      <button class="btn sm" id="dgo">Apply</button>
      <span class="muted">${fmt(d.total)} drives &middot; page ${d.page} of ${pages}</span>
      <button class="btn sm" id="prev" ${d.page <= 1 ? "disabled" : ""}>Prev</button>
      <button class="btn sm" id="next" ${d.page >= pages ? "disabled" : ""}>Next</button>
    </div>
    <div class="card scroll"><table><thead><tr><th>Serial</th><th>Model</th><th>Date</th><th>Status</th>${head}</tr></thead><tbody>${rows || '<tr><td colspan="9">No drives match.</td></tr>'}</tbody></table></div>`;
  $("#ds").value = dq.status;
  $("#dgo").onclick = () => { dq = { q: $("#dq").value, status: $("#ds").value, page: 1 }; go("drives"); };
  $("#prev").onclick = () => { dq.page--; go("drives"); };
  $("#next").onclick = () => { dq.page++; go("drives"); };
  view.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => go("detail", tr.dataset.s)));
}

async function detail(view, serial) {
  const d = await api("/api/drive/" + encodeURIComponent(serial));
  const cols = d.smart_columns;
  view.innerHTML = `
    <p><a href="#" id="back">&larr; Back to drives</a></p>
    <h1>${esc(d.serial)}</h1>
    <p class="muted">${esc(d.model)} &middot; capacity ${d.capacity_bytes ? (d.capacity_bytes / 1e12).toFixed(1) + " TB" : "n/a"}</p>
    <div class="banner info">${esc(d.risk_note)}</div>
    <div class="card"><h2>SMART history (raw values, ${d.history.length} reading${d.history.length === 1 ? "" : "s"})</h2>
      ${d.history.length > 1 ? '<canvas id="dc"></canvas>' : '<p class="muted">Only one daily reading exists for this drive, so no trend can be drawn.</p>'}
    </div>
    <div class="card scroll" style="margin-top:14px"><table><thead><tr><th>Date</th><th>Failure</th>${cols.map((c) => `<th>${esc(c.replace("smart_", "S").replace("_raw", ""))}</th>`).join("")}</tr></thead>
    <tbody>${d.history.map((h) => `<tr><td>${esc(h.date)}</td><td>${h.failure}</td>${cols.map((c) => `<td>${fmt(h.smart[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  $("#back").onclick = (e) => { e.preventDefault(); go("drives"); };
  if (d.history.length > 1) {
    const use = cols.filter((c) => !c.includes("_9_") && d.history.some((h) => h.smart[c]));
    const pal = ["#c0392b", "#c27c0e", "#2a5bd7", "#2f855a", "#7b4fd1", "#14213d"];
    chart("dc", "line", d.history.map((h) => h.date), (use.length ? use : cols.slice(0, 1)).map((c, i) => ({
      label: c.replace("smart_", "SMART ").replace("_raw", ""), data: d.history.map((h) => h.smart[c]), borderColor: pal[i % 6], tension: 0.2 })));
  }
}

async function sim(view) {
  const s = await api("/api/sim");
  view.innerHTML = `
    <h1>Failure Simulation</h1>
    <div class="banner"><b>Not available yet.</b> The reactive vs proactive comparison needs a trained model and cost assumptions. No simulated numbers are shown.</div>
    <div class="card"><h2>Needed before this page can show results</h2><ul>${s.needs.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
    <p class="muted">Daily snapshots currently loaded: ${fmt(s.dates_covered)}.</p></div>`;
}

async function assistant(view) {
  const a = await api("/api/assistant");
  view.innerHTML = `
    <h1>AI Assistant</h1>
    <div class="banner"><b>Not available yet.</b> The assistant explains model output, so it is switched on only after Module 3 produces real predictions.</div>
    <div class="card"><h2>Needed first</h2><ul>${a.needs.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>`;
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
