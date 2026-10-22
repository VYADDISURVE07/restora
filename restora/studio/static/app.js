/* Restora Studio - plain JavaScript, no build step, works offline. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const FIELDS = ["name", "method", "url", "headers", "body", "expected_status", "expected_response"];
const LABELS = { name: "Name", method: "Method", url: "URL", headers: "Headers", body: "Body",
  expected_status: "Expected status", expected_response: "Expected response" };

const state = {
  records: [], source: null, plan: null, results: {}, live: {}, run: null, runNotes: [], selected: null,
  options: { brain: "ai", heal: "auto", strict: false, reorder: true, seed: 0,
    pace: Number(new URLSearchParams(location.search).get("pace") || 0) },
  ai: { ready: false, message: "", model: "", models: [] }, nextId: 1, planTimer: null,
};

/* ------------------------------------------------------------------ helpers */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Restora": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `The app answered ${r.status}`);
  return data;
}
let toastTimer = null;
function toast(msg, ms = 4200) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
function sendable() {
  return state.records.filter(r => FIELDS.some(f => String(r[f] || "").trim()));
}
function recordForNumber(n) { return sendable()[n - 1]; }
function numberOf(rec) { return sendable().indexOf(rec) + 1; }

/* ------------------------------------------------------------------ tracked changes */
const TOKEN = /\{\{[^{}]*\}\}|<[^<>\s]{1,60}>|\{[^{}\s"]{1,60}\}|\s+|[{}\[\]"':,=&?\/]|[^\s{}\[\]"':,=&?\/]+/g;
function diffOps(a, b) {
  const A = String(a ?? "").match(TOKEN) || [], B = String(b ?? "").match(TOKEN) || [];
  const n = A.length, m = B.length;
  if (n * m > 400000) return [{ t: "del", s: A.join("") }, { t: "ins", s: B.join("") }];
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = []; let i = 0, j = 0;
  const push = (t, s) => { const last = ops[ops.length - 1]; if (last && last.t === t) last.s += s; else ops.push({ t, s }); };
  while (i < n && j < m) {
    if (A[i] === B[j]) { push("eq", A[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push("del", A[i]); i++; }
    else { push("ins", B[j]); j++; }
  }
  while (i < n) push("del", A[i++]);
  while (j < m) push("ins", B[j++]);
  // whitespace-only edits between real edits read better merged into them
  return ops.filter(o => !(o.t !== "eq" && !o.s.trim() && o.s.length < 2));
}
function gapMark(text, gaps) {
  let html = esc(text);
  for (const g of gaps || []) {
    if (!g) continue;
    const e = esc(g);
    html = html.split(e).join(`<mark class="gap" title="A gap the brain will fill">${e}</mark>`);
  }
  return html;
}
function tracked(before, after, gaps) {
  before = String(before ?? ""); after = String(after ?? "");
  if (before === after) return gapMark(after, gaps);
  if (!before.trim()) return after ? `<ins>${esc(after)}</ins>` : "";
  return diffOps(before, after).map(o => o.t === "eq" ? gapMark(o.s, gaps) : o.t === "del" ? `<del>${esc(o.s)}</del>` : `<ins>${esc(o.s)}</ins>`)
    .join("").replace(/<\/del><ins>/g, "</del>\u200a<ins>");
}
/* a relative URL the brain completed: the added origin is quiet, the real edits stay loud */
function urlCell(typed, planned, corrected, gaps) {
  typed = String(typed ?? "");
  const origin = planned && typed && planned !== typed && planned.endsWith(typed) ? planned.slice(0, planned.length - typed.length) : "";
  if (!origin) return corrected !== undefined ? tracked(typed, corrected, gaps) : (planned && planned !== typed ? tracked(typed, planned, gaps) : gapMark(typed, gaps));
  const rest = corrected !== undefined && corrected.startsWith(origin) ? corrected.slice(origin.length) : typed;
  return `<span class="soft" title="Added by the brain">${esc(origin)}</span>` + (corrected !== undefined ? tracked(typed, rest, []) : gapMark(typed, gaps));
}

/* ------------------------------------------------------------------ loading */
function setRecords(records, source) {
  state.records = records.map(r => ({ id: state.nextId++, ...Object.fromEntries(FIELDS.map(f => [f, r[f] ?? ""])), ai: !!r.ai }));
  state.source = source; state.results = {}; state.live = {}; state.run = null; state.runNotes = []; state.selected = null;
  $("#summary").hidden = true;
  render(); schedulePlan(0);
}
async function importFile(file) {
  if (!file) return;
  const buf = await file.arrayBuffer();
  let bin = ""; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  try {
    const data = await api("/api/import", { filename: file.name, data: btoa(bin) });
    setRecords(data.records, { name: data.name, kind: data.kind });
    toast(`${data.records.length} test${data.records.length === 1 ? "" : "s"} loaded from ${data.name}` +
      (data.warnings.length ? `. ${data.warnings.length} thing(s) to know - see Brain notes.` : "."));
    state.importWarnings = data.warnings;
  } catch (e) { toast(e.message, 7000); }
}
async function loadDemo() {
  try {
    const data = await api("/api/example", { name: "demo_bank_messy" });
    setRecords(data.records, { name: data.name, kind: "example", demo: true });
    toast(`The Demo Bank is running on this computer (${data.demo_url}). This sheet has gaps, rows in the wrong order and requests the API will reject.`, 6500);
  } catch (e) { toast(e.message, 7000); }
}
async function pasteTests() {
  const text = $("#pasteText").value;
  if (!text.trim()) { toast("Paste one or more cURL commands first."); return; }
  try {
    const data = await api("/api/paste", { text });
    const append = state.records.length > 0;
    if (append) {
      state.records.push(...data.records.map(r => ({ id: state.nextId++, ...r })));
      resetRun(); render(); schedulePlan(0);
    } else setRecords(data.records, { name: "Pasted cURL", kind: data.kind });
    closeModal("#pasteModal");
    toast(`${data.records.length} test${data.records.length === 1 ? "" : "s"} added.`);
  } catch (e) { toast(e.message, 7000); }
}

/* ------------------------------------------------------------------ plan */
function schedulePlan(ms = 500) {
  clearTimeout(state.planTimer);
  state.planTimer = setTimeout(plan, ms);
}
async function plan() {
  const recs = sendable();
  if (!recs.length) { state.plan = null; render(); return; }
  try {
    state.plan = await api("/api/plan", { records: recs.map(stripRecord), options: state.options });
    render();
  } catch (e) { toast(e.message, 6000); }
}
function stripRecord(r) { return Object.fromEntries(FIELDS.map(f => [f, r[f] ?? ""])); }
function resetRun() { state.results = {}; state.live = {}; state.run = null; state.runNotes = []; $("#summary").hidden = true; }

/* ------------------------------------------------------------------ run */
async function run() {
  const recs = sendable();
  if (!recs.length || (state.run && !state.run.done)) return;
  resetRun();
  recs.forEach((_, i) => { state.live[i + 1] = "WAITING"; });
  state.run = { id: null, seq: -1, done: false };
  render(); setRunning(true);
  try {
    const { run_id } = await api("/api/run", { records: recs.map(stripRecord), options: state.options,
      source: state.source ? state.source.name : "studio.csv" });
    state.run.id = run_id;
    poll();
  } catch (e) { setRunning(false); toast(e.message, 7000); }
}
function setRunning(on) {
  const b = $("#runBtn");
  b.disabled = on || !sendable().length;
  b.textContent = on ? "Running..." : runLabel();
}
function runLabel() { const n = sendable().length; return n ? `Run ${n} test${n === 1 ? "" : "s"}` : "Run tests"; }
async function poll() {
  const r = state.run;
  if (!r || !r.id) return;
  try {
    const data = await api(`/api/run/${r.id}/events?after=${r.seq}`);
    for (const ev of data.events) { r.seq = ev.seq; handle(ev); }
    if (data.done) { r.done = true; setRunning(false); render(); return; }
  } catch (e) { /* the app may be busy; try again */ }
  setTimeout(poll, 220);
}
function handle(ev) {
  switch (ev.type) {
    case "ai":
      if (state.options.brain === "ai" && !ev.ready) toast(ev.message, 6000);
      break;
    case "plan":
      state.runOrder = ev.order; state.runInsights = ev.insights; render(); break;
    case "start":
      state.live[ev.number] = "RUNNING"; renderGrid(); break;
    case "done": {
      const rec = ev.record;
      state.results[rec.number] = rec; state.live[rec.number] = rec.result;
      addRunNotes(rec);
      renderGrid(); renderNotes();
      const tr = $(`tr[data-number="${rec.number}"]`);
      if (tr && (rec.result === "HEALED" || rec.filled.length)) { tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash"); }
      if (state.selected === rec.number) renderRowPanel();
      break;
    }
    case "finished":
      state.run.result = ev; renderSummary(ev); break;
    case "error":
      toast("The run stopped: " + ev.message, 9000); break;
  }
}
function sourceLabel(src) { return src === "ai" ? "local AI" : src === "plugin" ? "plug-in" : "rules"; }
function byline(src) { return src === "ai" || src === "plugin" ? `by the ${sourceLabel(src)}` : ""; }
function addRunNotes(rec) {
  const row = rec.number, name = rec.name;
  if (rec.filled.length) state.runNotes.push({ row, name, kind: "fill", title: `Filled ${rec.filled.length} gap${rec.filled.length === 1 ? "" : "s"}`,
    detail: rec.filled.join("\n"), source: "rules" });
  for (const a of rec.heal_attempts) state.runNotes.push({ row, name, kind: "heal",
    title: a.source === "guard"
      ? (a.status_after >= 200 && a.status_after < 300 ? `Wrong reason: with the setup fixed, the API accepted it (HTTP ${a.status_after})`
                                                      : `Refused for the wrong reason at first: setup fixed, still refused (HTTP ${a.status_after})`)
      : rec.result === "HEALED" || rec.suggestion ? `Healed: HTTP ${a.status_before} became ${a.status_after}` : `Tried a fix: HTTP ${a.status_before} became ${a.status_after}`,
    detail: a.reason, source: a.source });
  if (rec.dynamic.length) state.runNotes.push({ row, name, kind: "dynamic", title: `Accepted ${rec.dynamic.length} value${rec.dynamic.length === 1 ? "" : "s"} that change on every call`,
    detail: rec.dynamic.join("\n"), source: "rules" });
  if (rec.result === "FAILED" && rec.explanation) state.runNotes.push({ row, name, kind: "fail", title: "Failed", detail: rec.explanation,
    source: rec.explanation.includes("local AI") ? "ai" : "rules" });
}

/* ------------------------------------------------------------------ rendering */
function render() {
  const has = state.records.length > 0;
  $("#empty").hidden = has; $("#sheet").hidden = !has;
  $("#addRowBtn").disabled = !has;
  $("#source").textContent = state.source ? state.source.name : "No sheet open";
  setRunning(!!(state.run && !state.run.done));
  renderGrid(); renderNotes();
}
function displayOrder() {
  const recs = sendable();
  const order = (state.run && state.runOrder) || (state.plan && state.plan.rows.map(r => r.number));
  const nums = order && order.length === recs.length ? order : recs.map((_, i) => i + 1);
  const extra = state.records.filter(r => !recs.includes(r));
  return [...nums.map(n => ({ n, rec: recs[n - 1] })), ...extra.map(rec => ({ n: null, rec }))];
}
function planRow(n) { return state.plan ? state.plan.rows.find(r => r.number === n) : null; }
function renderGrid() {
  const tb = $("#grid tbody");
  const rows = displayOrder().map(({ n, rec }) => rowHtml(n, rec)).join("");
  tb.innerHTML = rows;
}
function rowHtml(n, rec) {
  const res = n ? state.results[n] : null;
  const pr = n ? planRow(n) : null;
  const live = n ? state.live[n] : null;
  const gaps = pr ? pr.needs.map(x => x.raw) : [];
  const cls = [state.selected === n ? "selected" : "", pr && pr.skip ? "skip" : ""].join(" ");
  let status = "";
  if (res) status = `<span class="res ${res.result}">${cap(res.result)}</span><span class="res-sub">${res.http_status ?? "no reply"}${res.result !== "SKIPPED" ? `, ${res.elapsed_ms} ms` : ""}</span>`;
  else if (live) status = `<span class="res ${live}">${live === "RUNNING" ? "Running" : "Waiting"}</span>`;
  else if (pr && pr.error) status = `<span class="res FAILED">Fix me</span><span class="res-sub">${esc(pr.error).slice(0, 60)}</span>`;
  else if (pr && pr.negative) status = `<span class="res-sub">Negative test</span>`;
  const orig = res ? res.original : (pr ? pr.original : null);
  const cell = (f) => {
    const typed = rec[f] ?? "";
    if (f === "url") {
      const corr = res && res.corrected && res.corrected.url !== undefined ? res.corrected.url : undefined;
      return urlCell(typed, pr ? pr.url : null, corr, gaps);
    }
    if (res && res.corrected && Object.keys(res.corrected).length) {
      const key = f === "expected_status" ? "expectedstatus" : f === "expected_response" ? "expectedresponse" : f;
      const before = f === "body" && orig ? orig.body : typed;
      let after = res.corrected[key] ?? typed;
      if (f === "expected_response" && !String(typed).trim() && after) after = after;   // captured sample shows as new
      return tracked(before, after, []);
    }
    if (pr && f === "method" && !String(typed).trim()) return `<ins title="Chosen by the brain">${esc(pr.method)}</ins>`;
    return gapMark(typed, gaps);
  };
  const moved = pr && pr.moved ? `<span class="moved-mark">Moved up</span>` : "";
  const ai = rec.ai ? `<span class="ai-written">Written by AI, review it</span>` : "";
  return `<tr data-number="${n ?? ""}" data-id="${rec.id}" class="${cls}">
    <td class="c-status">${status}</td>
    <td class="c-num num-cell">${n ?? ""}${moved}</td>
    <td class="c-name" data-field="name"><div class="cell">${cell("name")}</div>${ai}</td>
    <td class="c-method mono" data-field="method"><div class="cell">${cell("method")}</div></td>
    <td class="c-url mono" data-field="url"><div class="cell">${cell("url")}</div></td>
    <td class="c-headers mono" data-field="headers"><div class="cell">${cell("headers")}</div></td>
    <td class="c-body mono" data-field="body"><div class="cell">${cell("body")}</div></td>
    <td class="c-expstatus mono" data-field="expected_status"><div class="cell">${cell("expected_status")}</div></td>
    <td class="c-expected mono" data-field="expected_response"><div class="cell">${cell("expected_response")}</div></td>
  </tr>`;
}
function cap(s) { return s.charAt(0) + s.slice(1).toLowerCase(); }

function noteHtml(note, isNew) {
  return `<div class="note k-${note.kind}${isNew ? " enter" : ""}" data-row="${note.row ?? ""}">
    ${note.name ? `<div class="note-row">${esc(note.name)}${note.row ? ` (row ${note.row})` : ""}</div>` : ""}
    <div class="note-title">${esc(note.title)}</div>
    ${note.detail ? `<div class="note-detail">${esc(note.detail).replace(/\n/g, "<br>")}</div>` : ""}
    ${byline(note.source) ? `<div class="note-src">${esc(byline(note.source))}</div>` : ""}
  </div>`;
}
let shownNotes = 0;
function renderNotes() {
  const insights = (state.run && state.runInsights) || (state.plan && state.plan.insights) || [];
  const recs = sendable();
  const before = insights.map(i => {
    const n = i.row ? i.row - 1 : null;
    const rec = n ? recs[n - 1] : null;
    return { row: n, name: rec ? rec.name : "", kind: i.kind, title: i.title, detail: i.detail, source: i.source };
  });
  const parts = [];
  if (state.importWarnings && state.importWarnings.length) {
    parts.push(`<h3>About this import</h3>` + state.importWarnings.map(w => noteHtml({ kind: "warning", title: w })).join(""));
  }
  if (before.length) parts.push(`<h3>Before the run</h3>` + before.map(x => noteHtml(x)).join(""));
  if (state.runNotes.length) {
    parts.push(`<h3>During the run</h3>` + state.runNotes.map((x, i) => noteHtml(x, i >= shownNotes)).join(""));
  }
  shownNotes = state.runNotes.length;
  $("#notes").innerHTML = parts.join("") || `<p class="muted">The brain writes its notes here: what it filled in, moved, repaired or healed, and why.</p>`;
  const total = before.length + state.runNotes.length;
  $("#noteCount").textContent = total ? String(total) : "";
}
function renderSummary(ev) {
  const c = ev.counts, b = ev.brain, f = ev.files || {};
  const did = [];
  if (b.filled) did.push(`${b.filled} gap${b.filled === 1 ? "" : "s"} filled`);
  if (b.moved) did.push(`${b.moved} row${b.moved === 1 ? "" : "s"} moved`);
  if (b.repaired) did.push(`${b.repaired} input${b.repaired === 1 ? "" : "s"} repaired`);
  if (b.healed) did.push(`${b.healed} request${b.healed === 1 ? "" : "s"} healed`);
  if (b.dynamic) did.push(`${b.dynamic} changing value${b.dynamic === 1 ? "" : "s"} accepted`);
  const id = state.run.id;
  const sheet = f.fixed_xlsx ? "corrected.xlsx" : "corrected.csv";
  $("#summary").innerHTML = `
    <div class="tally"><span class="t-pass"><b>${c.PASSED}</b>passed</span><span class="t-heal"><b>${c.HEALED}</b>healed</span>
      <span class="t-fail"><b>${c.FAILED}</b>failed</span>${c.SKIPPED ? `<span class="t-skip"><b>${c.SKIPPED}</b>skipped</span>` : ""}</div>
    <div class="brainline">${did.length ? "The brain " + did.join(", ") + "." : "Nothing needed fixing."}${b.ai_calls ? ` The local AI was asked ${b.ai_calls} time${b.ai_calls === 1 ? "" : "s"}.` : ""}</div>
    <div class="actions"><a class="btn" href="/runs/${id}/${sheet}">Download corrected sheet</a><a class="btn quiet" href="/runs/${id}/report.html" target="_blank" rel="noopener">Open full report</a></div>`;
  $("#summary").hidden = false;
}
function renderRowPanel() {
  const n = state.selected, el = $("#rowPanel");
  if (!n) { el.innerHTML = `<p class="muted">Select a row to see what was sent and what came back.</p>`; return; }
  const rec = recordForNumber(n), res = state.results[n], pr = planRow(n);
  const parts = [`<h3>${esc(rec ? rec.name : "Row " + n)}</h3>`];
  parts.push(`<div class="row-actions"><button class="btn quiet" type="button" data-edit-row="${n}">Edit this row</button></div>`);
  if (!res) {
    if (pr && pr.error) parts.push(`<div class="explain">${esc(pr.error)}</div>`);
    if (pr && pr.needs.length) parts.push(`<h3>Gaps the brain will fill</h3><ul class="plain">${pr.needs.map(x => `<li><span class="mono">${esc(x.raw)}</span>${x.kind === "word" ? ` in ${esc(x.key)}` : ""}</li>`).join("")}</ul>`);
    if (pr && pr.planned.length) parts.push(`<h3>Planned</h3><ul class="plain">${pr.planned.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
    if (pr && pr.negative) parts.push(`<h3>Negative test</h3><p>Never healed into a pass, because ${esc(pr.negative)}. Restora only checks that it is refused for the reason the test is about.</p>`);
    if (!pr || (!pr.needs.length && !pr.planned.length && !pr.error)) parts.push(`<p class="muted">Run the tests to see what was sent and what came back.</p>`);
    el.innerHTML = parts.join(""); return;
  }
  if (res.result === "FAILED" && res.explanation) parts.push(`<div class="explain">${esc(res.explanation)}</div>`);
  const guarded = res.heal_attempts.some(a => a.source === "guard");
  if (res.result === "HEALED") parts.push(guarded
    ? `<div class="explain ok">Refused for the right reason. At first the API refused this request because of a setup problem the test is not about. Restora fixed only that, and the API still refused it. The fix is in the corrected sheet.</div>`
    : `<div class="explain ok">Healed: the API accepted the request after the brain fixed it. The fix is in the corrected sheet.</div>`);
  parts.push(`<dl class="kv"><dt>Result</dt><dd><span class="res ${res.result}">${cap(res.result)}</span></dd>
    <dt>HTTP status</dt><dd>${res.http_status ?? "no reply"}${res.expected_status ? ` (expected ${res.expected_status})` : ""}</dd>
    <dt>Time</dt><dd>${res.elapsed_ms} ms</dd><dt>Ran as</dt><dd>${res.position} of ${sendable().length}</dd></dl>`);
  if (res.heal_attempts.length) parts.push(`<h3>${guarded ? "Why it was refused" : "How it was healed"}</h3><ol class="steps">${res.heal_attempts.map(a =>
    `<li>${esc(a.reason)}<br><span class="muted">${a.source === "guard"
      ? `HTTP ${a.status_before} at first, then HTTP ${a.status_after} with the setup fixed`
      : `HTTP ${a.status_before} became ${a.status_after}, by the ${esc(sourceLabel(a.source))}`}</span></li>`).join("")}</ol>`);
  if (res.suggestion) parts.push(`<h3>Suggested fix</h3><p>${esc(res.suggestion)}</p>`);
  if (res.filled.length) parts.push(`<h3>Gaps filled</h3><ul class="plain">${res.filled.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
  if (res.dynamic.length) parts.push(`<h3>Accepted differences</h3><ul class="plain">${res.dynamic.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
  if (res.failures.length && res.result !== "SKIPPED") parts.push(`<h3>What went wrong</h3><ul class="plain">${res.failures.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`);
  const hdr = (h) => Object.entries(h || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  parts.push(`<h3>Request sent (secrets hidden)</h3><pre>${esc(`${res.method} ${res.url}\n${hdr(res.request_headers)}${res.request_body ? "\n\n" + res.request_body : ""}`)}</pre>`);
  parts.push(`<h3>Response received</h3><pre>${esc(`HTTP ${res.http_status ?? "-"}\n${hdr(res.response_headers)}${res.response_body ? "\n\n" + res.response_body : ""}`)}</pre>`);
  if (res.curl) parts.push(`<h3>Same call as cURL</h3><pre>${esc(res.curl)}</pre>`);
  el.innerHTML = parts.join("");
}
function selectRow(n, showDetails = true) {
  state.selected = n;
  renderGrid();
  if (showDetails) { switchTab("row"); renderRowPanel(); }
  const tr = $(`tr[data-number="${n}"]`);
  if (tr) tr.scrollIntoView({ block: "nearest" });
}
function switchTab(which) {
  const notes = which === "notes";
  $("#tabNotes").setAttribute("aria-selected", String(notes)); $("#tabRow").setAttribute("aria-selected", String(!notes));
  $("#notes").hidden = !notes; $("#rowPanel").hidden = notes;
}

/* ------------------------------------------------------------------ editing */
let editing = null;
function openEditor(rec, field) {
  editing = { rec, field };
  $("#editorTitle").textContent = `${LABELS[field]} for ${rec.name || "this row"}`;
  const ta = $("#editorText"); ta.value = rec[field] ?? "";
  $("#editor").hidden = false; ta.focus(); ta.select();
}
function saveEditor() {
  if (!editing) return;
  editing.rec[editing.field] = $("#editorText").value;
  editing = null; $("#editor").hidden = true;
  resetRun(); render(); schedulePlan(100);
}
function closeModal(sel) { $(sel).hidden = true; }

/* ------------------------------------------------------------------ AI */
async function refreshAi(recheck = false) {
  try {
    const st = await api("/api/status" + (recheck ? "?recheck=1" : ""));
    state.ai = st.ai;
    const pill = $("#aiPill");
    pill.classList.toggle("ready", st.ai.ready);
    $("#aiText").textContent = st.ai.ready ? `Local AI ready: ${st.ai.model}` : (st.ai.message.startsWith("Checking") ? st.ai.message : "Rules only: no local AI model found");
    pill.title = st.ai.message;
    $("#askHint").textContent = st.ai.ready ? `Written by ${st.ai.model}, on this computer.` : "Needs the local AI model (see Settings).";
    const sel = $("#modelSelect");
    const models = st.ai.models.length ? st.ai.models : [st.ai.model];
    sel.innerHTML = models.map(m => `<option ${m === st.ai.model ? "selected" : ""}>${esc(m)}</option>`).join("");
    $("#aiHelp").textContent = st.ai.ready ? st.ai.message :
      `${st.ai.message} To add it for free: run ai_setup.bat (Windows) or sh ai_setup.sh, then press Check again.`;
    if (st.ai.message.startsWith("Checking")) setTimeout(() => refreshAi(false), 1500);
  } catch (e) { $("#aiText").textContent = "Studio is not reachable"; }
}
async function ask() {
  const text = $("#askInput").value.trim();
  if (!text) { toast("Describe the test first, for example: check that a transfer with a negative amount is rejected."); return; }
  if (!sendable().length) { toast("Open a sheet first: the AI writes new tests for the API in your sheet."); return; }
  const btn = $("#askBtn"); btn.disabled = true; btn.textContent = "Writing...";
  try {
    const data = await api("/api/ask", { text, records: sendable().map(stripRecord) });
    if (!data.ok) { toast(data.message, 8000); return; }
    const added = data.tests.map(t => ({ id: state.nextId++, ...Object.fromEntries(FIELDS.map(f => [f, t[f] ?? ""])), ai: true }));
    state.records.push(...added);
    $("#askInput").value = "";
    resetRun(); render(); await plan();
    toast(`The AI wrote ${added.length} test${added.length === 1 ? "" : "s"} in ${data.seconds} s. Review ${added.length === 1 ? "it" : "them"}, then run.`, 6000);
    const n = numberOf(added[0]); if (n) selectRow(n);
  } catch (e) { toast(e.message, 7000); }
  finally { btn.disabled = false; btn.textContent = "Write test"; }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  for (const id of ["#fileInput", "#fileInput2"]) $(id).addEventListener("change", e => { importFile(e.target.files[0]); e.target.value = ""; });
  $("#demoBtn").addEventListener("click", loadDemo);
  $("#pasteBtn").addEventListener("click", () => { $("#pasteModal").hidden = false; $("#pasteText").focus(); });
  $$("[data-action]").forEach(b => b.addEventListener("click", () => b.dataset.action === "demo" ? loadDemo() : ($("#pasteModal").hidden = false, $("#pasteText").focus())));
  $("#pasteOk").addEventListener("click", pasteTests);
  $("#pasteCancel").addEventListener("click", () => closeModal("#pasteModal"));
  $("#runBtn").addEventListener("click", run);
  $("#addRowBtn").addEventListener("click", () => {
    const rec = { id: state.nextId++, name: "New test", method: "GET", url: "", headers: "", body: "", expected_status: "", expected_response: "" };
    state.records.push(rec); resetRun(); render(); openEditor(rec, "url");
  });
  $("#editorOk").addEventListener("click", saveEditor);
  $("#editorCancel").addEventListener("click", () => { editing = null; closeModal("#editor"); });
  $("#editorText").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveEditor(); });
  $("#askBtn").addEventListener("click", ask);
  $("#askInput").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
  $("#tabNotes").addEventListener("click", () => switchTab("notes"));
  $("#tabRow").addEventListener("click", () => { switchTab("row"); renderRowPanel(); });
  $("#grid tbody").addEventListener("click", e => {
    const tr = e.target.closest("tr"); if (!tr) return;
    const n = Number(tr.dataset.number); if (n) selectRow(n);
  });
  $("#grid tbody").addEventListener("dblclick", e => {
    const td = e.target.closest("td[data-field]"), tr = e.target.closest("tr"); if (!td || !tr) return;
    const rec = state.records.find(r => String(r.id) === tr.dataset.id); if (rec) openEditor(rec, td.dataset.field);
  });
  $("#rowPanel").addEventListener("click", e => {
    const b = e.target.closest("[data-edit-row]"); if (!b) return;
    const rec = recordForNumber(Number(b.dataset.editRow)); if (rec) openEditor(rec, "body");
  });
  $("#notes").addEventListener("click", e => {
    const note = e.target.closest(".note"); if (!note || !note.dataset.row) return;
    selectRow(Number(note.dataset.row), false);
  });
  $$(".segmented button").forEach(b => b.addEventListener("click", () => {
    const key = b.dataset.brain ? "brain" : "heal";
    state.options[key] = b.dataset[key];
    $$(`.segmented button[data-${key}]`).forEach(x => x.setAttribute("aria-checked", String(x === b)));
    if (key === "brain") schedulePlan(0);
  }));
  const settings = $("#settings");
  const toggleSettings = (show) => { settings.hidden = !show; $("#settingsBtn").setAttribute("aria-expanded", String(show)); };
  $("#settingsBtn").addEventListener("click", () => toggleSettings(settings.hidden));
  $("#aiPill").addEventListener("click", () => toggleSettings(true));
  $("#closeSettings").addEventListener("click", () => toggleSettings(false));
  $("#recheckBtn").addEventListener("click", async () => { $("#aiHelp").textContent = "Checking..."; await refreshAi(true); });
  $("#modelSelect").addEventListener("change", async e => { await api("/api/ai", { model: e.target.value }); refreshAi(false); });
  $("#strictBox").addEventListener("change", e => { state.options.strict = e.target.checked; });
  $("#orderBox").addEventListener("change", e => { state.options.reorder = !e.target.checked; schedulePlan(0); });
  $("#seedBox").addEventListener("change", e => { state.options.seed = e.target.checked ? 42 : 0; });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { ["#pasteModal", "#editor"].forEach(closeModal); toggleSettings(false); }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && $("#editor").hidden && $("#pasteModal").hidden) run();
  });
  document.addEventListener("dragover", e => e.preventDefault());
  document.addEventListener("drop", e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) importFile(f); });
}

wire();
render();
refreshAi(false);
