// Live insights dashboard: WebSocket client, synchronized audio playback, nudge rendering with acknowledgements.
const $ = id => document.getElementById(id);
let sources = [], ws = null, running = false, duration = 0, audioT = 0;
let nudges = {}, segCount = 0, sigCount = 0, frustration = [];

const LABELS = {
  compliance_disclosure_missing: "recording disclosure", compliance_waiting_period: "waiting-period disclosure", risky_statement: "risky statement",
  frustration: "frustration", payment_difficulty: "payment difficulty", cross_sell: "cross-sell",
  missed_opportunity: "missed opportunity", buying_signal: "buying intent", callback_request: "callback",
};
const CHECK_NAMES = { recording_disclosure: "Call-recording disclosure", waiting_period_before_quote: "Waiting periods disclosed before quote" };

function resetView() {
  nudges = {}; segCount = 0; sigCount = 0; frustration = []; audioT = 0;
  $("transcript").innerHTML = ""; $("nudges").innerHTML = ""; $("signals").innerHTML = ""; $("insights").innerHTML = "";
  $("spark").innerHTML = ""; $("summary").hidden = true; $("topic").textContent = "";
  $("seg-count").textContent = $("nudge-count").textContent = $("sig-count").textContent = "";
  $("checklist").innerHTML = '<div class="empty">-</div>';
}

function showSource() {
  const s = sources.find(x => x.id === $("source").value);
  if (!s) return;
  duration = s.duration;
  $("clock").textContent = `00:00 / ${fmtTime(duration)}`;
  $("source-desc").textContent = s.description;
  $("expected").innerHTML = s.expected === null ? '<span class="chip">no ground truth (recorded call)</span>'
    : s.expected.length ? '<span class="small faint">Ground truth:</span>' + s.expected.map(e =>
      `<span class="chip">${esc(LABELS[e.type] || e.type)}${e.optional ? " (optional)" : ""}</span>`).join("")
    : '<span class="chip">ground truth: no nudges should fire</span>';
  $("player").src = s.audio_url;
}

function connect() {
  return new Promise((resolve, reject) => {
    if (ws && ws.readyState === WebSocket.OPEN) return resolve(ws);
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
    ws.onopen = () => resolve(ws);
    ws.onerror = () => reject(new Error("WebSocket connection failed"));
    ws.onmessage = e => handle(JSON.parse(e.data));
    ws.onclose = () => { if (running) stopped(); };
  });
}

async function start() {
  if (!window.HEALTH?.groq_key_configured) { toast("Add GROQ_API_KEY to .env and restart the server to run live analysis."); return; }
  resetView();
  const player = $("player");
  player.currentTime = 0;
  try {
    await connect();
    await new Promise(r => player.readyState >= 3 ? r() : player.addEventListener("canplaythrough", r, { once: true }));
  } catch (e) { toast(e.message); return; }
  running = true;
  $("btn-start").hidden = true; $("btn-stop").hidden = false; $("source").disabled = true;
  ws.send(JSON.stringify({ type: "start", source_id: $("source").value }));
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  stopped();
}

function stopped() {
  running = false;
  $("player").pause();
  $("btn-start").hidden = false; $("btn-stop").hidden = true; $("source").disabled = false;
}

function handle(ev) {
  switch (ev.type) {
    case "start":
      $("player").currentTime = 0; $("player").play().catch(() => {});
      renderChecklist(ev.checklist); break;
    case "progress":
      audioT = ev.t;
      $("progress").style.width = `${Math.min(100, (ev.t / duration) * 100)}%`;
      $("clock").textContent = `${fmtTime(ev.t)} / ${fmtTime(duration)}`;
      tickTtl(); break;
    case "transcript": addSegment(ev); break;
    case "nudge": addNudge(ev.nudge); break;
    case "nudge_update": updateNudge(ev.id, ev.status); break;
    case "signal": addSignal(ev); break;
    case "checklist": renderChecklist(ev.items); break;
    case "sentiment": frustration.push([ev.t, ev.frustration]); $("topic").textContent = `topic: ${ev.topic}`; drawSpark(); break;
    case "insight": $("insights").insertAdjacentHTML("afterbegin", `<div>[${fmtTime(ev.t)}] ${esc(ev.text)}</div>`); break;
    case "end": renderSummary(ev.summary); stopped(); break;
    case "stopped": stopped(); break;
    case "error": toast(ev.message); break;
  }
}

function addSegment(s) {
  segCount++; $("seg-count").textContent = `${segCount} segments`;
  const box = $("transcript");
  const div = document.createElement("div");
  div.className = `seg ${s.speaker} ${s.low_confidence ? "low" : ""}`;
  div.dataset.start = s.start;
  div.innerHTML = `<div class="ts">${fmtTime(s.start)}</div><div><div class="sp">${s.speaker}</div>
    <div class="tx">${esc(s.text || "…")}</div>
    <div class="m">ASR ${s.asr_ms} ms · end-point ${s.endpoint_ms} ms · RTF ${s.rtf}${s.low_confidence ? ` · filtered: ${esc(s.low_confidence)}` : ""}</div></div>`;
  const after = [...box.children].find(c => parseFloat(c.dataset.start) > s.start);
  box.insertBefore(div, after || null);
  box.scrollTop = box.scrollHeight;
}

function addNudge(n) {
  const box = $("nudges");
  box.querySelector(".empty")?.remove();
  const div = document.createElement("div");
  div.className = `nudge p${n.priority}`;
  div.id = `nudge-${n.id}`;
  const t = n.timings || {};
  div.innerHTML = `<div class="nt"><span>${esc(n.title)}</span></div>
    <div class="nx">${esc(n.text)}</div>
    ${n.evidence && n.type !== "compliance_disclosure_missing" ? `<div class="ev">“${esc(n.evidence)}”</div>` : ""}
    <div class="nm"><span class="badge ${n.priority === 1 ? "bad" : n.priority === 2 ? "warn" : "accent"}">P${n.priority} · ${esc(n.category)}</span>
      <span class="chip">${esc(n.source)} · conf ${n.confidence}</span>
      <span class="lat">at ${fmtTime(n.emitted_audio_t)} · e2e ${t.e2e_emit_ms} ms${t.llm_ms ? ` · LLM ${t.llm_ms}` : ""} · ASR ${t.asr_ms ?? "-"}</span></div>
    <div class="ttl" style="width:100%"></div>`;
  box.prepend(div);
  nudges[n.id] = { ...n, el: div };
  $("nudge-count").textContent = `${Object.keys(nudges).length} delivered`;
  requestAnimationFrame(() => ws?.send(JSON.stringify({ type: "ack", id: n.id })));   // rendered -> delivery latency
}

function updateNudge(id, status) {
  const n = nudges[id];
  if (!n) return;
  n.status = status;
  n.el.classList.remove("active"); n.el.classList.add(status);
  n.el.querySelector(".ttl").style.width = "0";
}

function tickTtl() {
  for (const n of Object.values(nudges)) {
    if (n.status && n.status !== "active") continue;
    const total = n.expires_t - n.emitted_audio_t, left = Math.max(0, n.expires_t - audioT);
    n.el.querySelector(".ttl").style.width = `${total > 0 ? (left / total) * 100 : 0}%`;
  }
}

function addSignal(s) {
  sigCount++; $("sig-count").textContent = `${sigCount} raw`;
  const box = $("signals");
  box.querySelector(".empty")?.remove();
  box.insertAdjacentHTML("afterbegin", `<div class="sig ${s.suppressed ? "sup" : ""}">
    <span class="mono faint">${fmtTime(s.t)}</span><span class="ty">${esc(LABELS[s.type] || s.type)}</span>
    <span class="faint">${s.confidence}</span><span class="why">${s.suppressed ? "suppressed: " + esc(s.suppressed) : "→ nudge"}</span></div>`);
}

function renderChecklist(items) {
  if (!items) return;
  $("checklist").innerHTML = Object.entries(items).map(([k, v]) => `<div class="check"><span>${esc(CHECK_NAMES[k] || k)}</span>
    <span class="st badge ${v === "done" ? "ok" : v === "missed" ? "bad" : "neutral"}">${v}</span></div>`).join("");
}

function drawSpark() {
  if (!frustration.length) return;
  const W = 300, H = 70, pts = frustration.map(([t, f]) => [(t / duration) * W, H - 6 - f * (H - 12)]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const th = H - 6 - 0.6 * (H - 12);
  $("spark").innerHTML = `<line x1="0" x2="${W}" y1="${th}" y2="${th}" stroke="rgba(251,191,36,.35)" stroke-dasharray="4 4"/>
    <path d="${line}" fill="none" stroke="url(#sg)" stroke-width="2.2" stroke-linejoin="round"/>
    <defs><linearGradient id="sg" x1="0" y1="1" x2="0" y2="0"><stop stop-color="#2dd4bf"/><stop offset="1" stop-color="#f87171"/></linearGradient></defs>
    ${pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="2.6" fill="#e7eaf2"/>`).join("")}`;
}

function renderSummary(s) {
  const ev = s.evaluation;
  const lat = s.latency;
  const rows = [["endpoint_ms", "End-pointing"], ["asr_queue_ms", "ASR queue"], ["asr_ms", "ASR"], ["rules_ms", "Rules"],
    ["llm_ms", "LLM"], ["nudge_ms", "Nudge control"], ["delivery_ms", "Delivery (WS round trip)"],
    ["e2e_emit_ms", "Speech end → emitted"], ["e2e_display_ms", "Speech end → displayed"]];
  const sup = Object.entries(s.suppressed_by_reason).map(([k, v]) => `<span class="chip">${esc(k)}: ${v}</span>`).join("") || '<span class="faint">none</span>';
  $("summary").hidden = false;
  $("summary").innerHTML = `<div class="card-title">Session summary · ${esc(s.title)}</div>
    <div class="tiles">
      <div class="tile"><div class="v">${s.segments}</div><div class="l">ASR segments (${s.segments_filtered_low_confidence} filtered)</div></div>
      <div class="tile"><div class="v">${s.raw_signals}</div><div class="l">raw signals</div></div>
      <div class="tile accent"><div class="v">${s.nudges_delivered}</div><div class="l">nudges delivered</div></div>
      ${ev ? `<div class="tile ok"><div class="v">${ev.true_positives}</div><div class="l">true positives</div></div>
      <div class="tile ${ev.false_positives.length ? "bad" : ""}"><div class="v">${ev.false_positives.length}</div><div class="l">false positives</div></div>
      <div class="tile ${ev.false_negatives.length ? "warn" : ""}"><div class="v">${ev.false_negatives.length}</div><div class="l">missed (FN)</div></div>` : ""}
      <div class="tile"><div class="v">${s.asr_per_segment.p50_ms ?? "-"}</div><div class="l">ASR p50 per chunk (ms)</div></div>
    </div>
    <div class="row wrap" style="margin-top:12px"><span class="small muted">Suppressed:</span>${sup}</div>
    ${ev && (ev.false_positives.length || ev.false_negatives.length) ? `<div class="small" style="margin-top:10px">
      ${ev.false_positives.map(f => `<div style="color:var(--bad)">FP ${esc(f.type)} at ${f.t}s: “${esc(f.evidence)}”</div>`).join("")}
      ${ev.false_negatives.map(f => `<div style="color:var(--warn)">FN ${esc(f.type)} (turn ${f.turn})</div>`).join("")}</div>` : ""}
    <div class="table-wrap lat-table" style="margin-top:14px"><table><thead><tr><th>Component</th><th>P50 (ms)</th><th>P95 (ms)</th><th>n</th></tr></thead><tbody>
      ${rows.map(([k, l]) => `<tr><td>${l}</td><td class="mono">${lat[k]?.p50 ?? "-"}</td><td class="mono">${lat[k]?.p95 ?? "-"}</td><td class="mono">${lat[k]?.n ?? 0}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="small faint" style="margin-top:8px">Saved to reports/live_sessions/. Run <code>python -m app.live.evaluate --report</code> to aggregate.</div>`;
}

document.addEventListener("DOMContentLoaded", async () => {
  $("btn-start").innerHTML = `${ICONS.play} Start live analysis`;
  $("btn-stop").innerHTML = `${ICONS.stop} Stop`;
  $("btn-start").onclick = start; $("btn-stop").onclick = stop;
  sources = await api("/api/live/sources");
  if (!sources.length) { $("source-desc").innerHTML = 'No call audio found. Run <code>python -m app.live.build_scenarios</code>.'; return; }
  $("source").innerHTML = sources.map(s => `<option value="${esc(s.id)}">${s.kind === "scenario" ? "Scenario" : "Recording"} · ${esc(s.title)} (${s.duration}s)</option>`).join("");
  $("source").onchange = showSource;
  showSource();
});
