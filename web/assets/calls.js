// Call log: list of recorded calls, detail view, CRM tables.
const $ = id => document.getElementById(id);
let calls = [];

function callCard(c) {
  const tone = OUTCOME_TONE[c.outcome.status] || "neutral";
  const flag = countryBadge(PROFILE_MARKET[c.profile_id]);
  const checks = c.checks_total ? `<span class="badge ${c.checks_passed === c.checks_total ? "ok" : "warn"}">checks ${c.checks_passed}/${c.checks_total}</span>` : "";
  return `<button class="call-item" data-id="${c.call_id}">
    <div class="t">${flag} ${esc(c.profile_title)}</div>
    <div class="row wrap small" style="margin-top:6px">
      <span class="badge ${tone}">${esc(c.outcome.label)}</span>
      <span class="badge ${c.source === "web" ? "info" : "accent"}">${esc(c.source)}${c.persona ? " · " + esc(c.persona) : ""}</span>${checks}
    </div>
    <div class="small faint" style="margin-top:6px">${esc(c.started_at.replace("T", " ").slice(0, 16))} · ${c.duration_s}s · ${c.metrics.agent_turns} turns${c.has_recording ? " · 🎧" : ""}</div>
  </button>`;
}

async function loadCalls() {
  calls = await api("/api/calls");
  $("call-count").textContent = calls.length;
  $("call-list").innerHTML = calls.length ? calls.map(callCard).join("")
    : '<div class="empty">No calls yet. Place a web call on the Voice Agent page or run <code>python -m app.agent.simulate</code>.</div>';
  document.querySelectorAll(".call-item").forEach(b => b.onclick = () => showCall(b.dataset.id));
  const target = location.hash.slice(1) || calls[0]?.call_id;
  if (target) showCall(target);
}

async function showCall(id) {
  document.querySelectorAll(".call-item").forEach(b => b.classList.toggle("selected", b.dataset.id === id));
  history.replaceState(null, "", "#" + id);
  const c = await api(`/api/calls/${id}`);
  const m = c.metrics, tone = OUTCOME_TONE[c.outcome.status] || "";
  const test = c.test;
  const tiles = [
    [m.agent_turns, "agent turns", ""], [m.grounded_answers, "grounded answers", "ok"],
    [m.unavailable_answers, "'info unavailable'", "warn"], [m.corrective_reruns, "self-corrections", "accent"],
    [m.p50_total_ms ?? "-", "p50 turn latency (ms)", ""], [m.p95_total_ms ?? "-", "p95 turn latency (ms)", ""]];
  const transcript = c.turns.map(t => {
    if (t.role === "customer") {
      return `<div class="msg customer ${t.ignored ? "ignored" : ""}"><div class="who">Customer · ${t.t}s</div><div class="bubble">${esc(t.text || "(no speech)")}</div>
        ${t.asr ? `<div class="meta"><span class="lat">ASR ${t.asr.ms} ms</span></div>` : ""}</div>`;
    }
    const badges = [];
    if (t.answer_status === "answered_from_kb") badges.push('<span class="badge ok">grounded</span>');
    if (t.answer_status === "info_unavailable") badges.push('<span class="badge warn">info unavailable</span>');
    if (t.action && t.action !== "continue") badges.push(`<span class="badge ${t.action === "escalate_to_human" ? "bad" : "neutral"}">${esc(t.action.replaceAll("_", " "))}</span>`);
    if (t.reruns) badges.push('<span class="badge info">self-corrected</span>');
    const cites = (t.citations || []).map(x => `<span class="chip cite" title="${esc(x.title + "\n" + x.citation)}">${esc(x.record_id)}</span>`).join("");
    const tm = t.timings || {};
    const lat = [["STT", tm.stt_ms], ["RAG", tm.retrieval_ms], ["LLM", tm.llm_ms], ["TTS", tm.tts_ms]].filter(([, v]) => v != null).map(([k, v]) => `${k} ${v}`).join(" · ");
    return `<div class="msg agent"><div class="who">Agent · ${t.t}s</div><div class="bubble">${esc(t.text)}</div>
      <div class="meta">${badges.join("")}${cites}${lat ? `<span class="lat">${lat}</span>` : ""}</div></div>`;
  }).join("");
  const cites = {};
  c.turns.forEach(t => (t.citations || []).forEach(x => cites[x.record_id] = x));
  $("call-detail").innerHTML = `
    <div class="card">
      <div class="row between wrap"><div><div style="font-weight:700;font-size:16px">${esc(c.profile_title)} · ${esc(c.market)}</div>
        <div class="small faint mono">${esc(c.call_id)} · ${esc(c.source)} · ${c.duration_s}s · ended: ${esc(c.end_reason)}</div></div>
        <a class="btn" href="/recordings/${c.call_id}/transcript.md" target="_blank">Transcript (.md)</a></div>
      ${c.has_recording ? `<div style="margin-top:12px"><audio controls src="/recordings/${c.call_id}/call.wav"></audio>
        <div class="small faint">Stereo recording: agent on the left channel, customer on the right.</div></div>` : '<div class="small faint" style="margin-top:10px">No recording uploaded for this call.</div>'}
      <div class="outcome ${tone}" style="margin-top:12px"><div class="label">${esc(c.outcome.label)}</div>
        ${(c.outcome.reasons || []).length ? `<ul>${c.outcome.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        <div class="small muted" style="margin-top:8px"><b>CRM summary</b> (${esc(c.crm_summary_id)}): ${esc(c.crm_summary)}</div></div>
      <div class="tiles" style="margin-top:12px">${tiles.map(([v, l, t]) => `<div class="tile ${t}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("")}</div>
    </div>
    ${test ? `<div class="card"><div class="card-title">Test scenario · ${esc(test.persona)} <span class="count">${esc(test.coverage.join(", "))}</span></div>
      ${test.checks.map(x => `<div class="check"><span class="badge ${x.passed ? "ok" : "bad"}">${x.passed ? "PASS" : "FAIL"}</span>
        <span class="mono">${esc(x.check)}</span><span class="st small muted">expected ${esc(JSON.stringify(x.expected))} · got ${esc(JSON.stringify(x.got))}</span></div>`).join("")}
      <hr class="sep"><div class="card-title">ASR accuracy on this call <span class="count">mean WER ${test.asr_wer_mean ?? "-"}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Customer said (TTS input)</th><th>Whisper heard</th><th>WER</th></tr></thead><tbody>
      ${test.asr_pairs.map(p => `<tr><td>${esc(p.spoken)}</td><td>${esc(p.asr)}</td><td class="mono">${p.wer}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
    <div class="card"><div class="card-title">Transcript</div><div class="transcript" style="max-height:none;min-height:0">${transcript}</div></div>
    ${Object.keys(cites).length ? `<div class="card"><div class="card-title">Knowledge-base citations</div>
      ${Object.values(cites).map(x => `<div class="hit"><div class="row wrap"><span class="chip cite">${esc(x.record_id)}</span><span class="chip">${esc(x.category)}</span></div>
        <div class="title" style="margin-top:6px">${esc(x.title)}</div><div class="src">${esc(x.citation)}</div></div>`).join("")}</div>` : ""}`;
}

async function loadCrm() {
  const data = await api("/api/crm");
  const tabs = Object.keys(data);
  $("crm-tabs").innerHTML = tabs.map((t, i) => `<button class="tab ${i ? "" : "active"}" data-t="${t}">${t.replace("_", " ")} (${data[t].length})</button>`).join("");
  const show = t => {
    document.querySelectorAll("#crm-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.t === t));
    const rows = data[t];
    if (!rows.length) { $("crm-table").innerHTML = '<div class="empty">No records yet.</div>'; return; }
    $("crm-table").innerHTML = `<div class="table-wrap"><table><thead><tr><th>ID</th><th>Created</th><th>Call</th><th>Status / reason</th><th>Details</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="mono">${esc(r.id)}</td><td class="mono small">${esc(r.created_at.replace("T", " ").slice(0, 19))}</td>
        <td><a class="mono" href="#${esc(r.call_id)}" onclick="switchView('calls');setTimeout(()=>showCall('${esc(r.call_id)}'),50)">${esc(r.call_id)}</a></td>
        <td>${esc(r.status || r.outcome || r.reason || "")}${r.priority ? ` <span class="badge ${r.priority === "high" ? "bad" : "neutral"}">${esc(r.priority)}</span>` : ""}</td>
        <td class="small">${esc(r.summary || Object.entries(r.fields || {}).map(([k, v]) => `${k}: ${v}`).join(" · "))}</td></tr>`).join("")}</tbody></table></div>`;
  };
  document.querySelectorAll("#crm-tabs .tab").forEach(b => b.onclick = () => show(b.dataset.t));
  show(tabs[0]);
}

function switchView(v) {
  document.querySelectorAll(".page-head .tab").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $("view-calls").hidden = v !== "calls"; $("view-crm").hidden = v !== "crm";
  if (v === "crm") loadCrm();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".page-head .tab").forEach(b => b.onclick = () => switchView(b.dataset.view));
  loadCalls().catch(e => toast(e.message));
});
