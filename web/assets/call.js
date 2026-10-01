// Web calling interface: mic capture + VAD -> /api/calls/{id}/turn -> agent audio playback; stereo call recording.
const TARGET_SR = 16000;
let profiles = [];
let profile = null;
let call = null;              // { id, started, ended }
let phase = "idle";           // idle | connecting | listening | thinking | speaking | ended
let audio = null;             // audio graph + recorder
let timerHandle = null;
let turnCount = 0;

const $ = id => document.getElementById(id);

// ------------------------------------------------------------------------------------------ audio utils
class Downsampler {
  constructor(from, to = TARGET_SR) { this.ratio = from / to; this.pos = 0; this.rest = new Float32Array(0); }
  process(input) {
    const data = new Float32Array(this.rest.length + input.length);
    data.set(this.rest); data.set(input, this.rest.length);
    const out = [];
    let p = this.pos;
    while (p + this.ratio <= data.length) {
      const s = Math.floor(p), e = Math.max(s + 1, Math.floor(p + this.ratio));
      let sum = 0;
      for (let i = s; i < e; i++) sum += data[i];
      out.push(sum / (e - s));
      p += this.ratio;
    }
    const used = Math.floor(p);
    this.rest = data.slice(used); this.pos = p - used;
    return Float32Array.from(out);
  }
}

function toInt16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) out[i] = Math.max(-1, Math.min(1, f32[i])) * 0x7fff;
  return out;
}

function wavBlob(channels, sr = TARGET_SR) {
  const n = channels[0].length, ch = channels.length;
  const buf = new ArrayBuffer(44 + n * ch * 2), v = new DataView(buf);
  const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * ch * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, ch, true); v.setUint32(24, sr, true);
  v.setUint32(28, sr * ch * 2, true); v.setUint16(32, ch * 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, n * ch * 2, true);
  let o = 44;
  for (let i = 0; i < n; i++) for (let c = 0; c < ch; c++) { v.setInt16(o, channels[c][i], true); o += 2; }
  return new Blob([buf], { type: "audio/wav" });
}

function concatInt16(parts) {
  const n = parts.reduce((a, p) => a + p.length, 0), out = new Int16Array(n);
  let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

// Energy VAD with a rolling-percentile noise floor; only active while the agent is listening (half duplex).
class VAD {
  constructor(onUtterance) {
    this.onUtterance = onUtterance; this.frame = 320; this.pending = new Float32Array(0);
    this.levels = []; this.pre = []; this.utt = []; this.inSpeech = false; this.run = 0;
    this.speechFrames = 0; this.silence = 0; this.enabled = false; this.level = 0;
  }
  reset() { this.pre = []; this.utt = []; this.inSpeech = false; this.run = 0; this.speechFrames = 0; this.silence = 0; }
  push(samples) {
    const data = new Float32Array(this.pending.length + samples.length);
    data.set(this.pending); data.set(samples, this.pending.length);
    let i = 0;
    for (; i + this.frame <= data.length; i += this.frame) this.onFrame(data.subarray(i, i + this.frame));
    this.pending = data.slice(i);
  }
  onFrame(f) {
    let sum = 0; for (const x of f) sum += x * x;
    const db = 10 * Math.log10(sum / f.length + 1e-12);
    this.level = Math.max(0, Math.min(1, (db + 60) / 45));
    this.levels.push(db); if (this.levels.length > 150) this.levels.shift();
    const sorted = [...this.levels].sort((a, b) => a - b);
    const floor = sorted[Math.floor(sorted.length * 0.15)] ?? -60;
    const thr = Math.max(floor + 12, -50);
    if (!this.enabled) { this.reset(); return; }
    const copy = new Float32Array(f);
    if (!this.inSpeech) {
      this.pre.push(copy); if (this.pre.length > 15) this.pre.shift();
      this.run = db > thr ? this.run + 1 : 0;
      if (this.run >= 3) { this.inSpeech = true; this.utt = [...this.pre]; this.speechFrames = this.run; this.silence = 0; }
      return;
    }
    this.utt.push(copy);
    if (db > thr) { this.speechFrames++; this.silence = 0; } else this.silence++;
    if (this.silence >= 30 || this.utt.length >= 750) {           // 600 ms of silence or 15 s max
      const enough = this.speechFrames >= 15;                     // >= 300 ms of speech
      const frames = this.utt; this.reset();
      if (enough) {
        const out = new Float32Array(frames.length * this.frame);
        frames.forEach((fr, k) => out.set(fr, k * this.frame));
        this.onUtterance(out);
      }
    }
  }
}

async function initAudio() {
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("assets/capture-worklet.js");
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const mic = ctx.createMediaStreamSource(stream);
  const merger = ctx.createChannelMerger(2);
  const agentBus = ctx.createGain();
  agentBus.connect(ctx.destination);          // agent voice -> speakers
  agentBus.connect(merger, 0, 0);             // agent voice -> recording channel 0 (left)
  mic.connect(merger, 0, 1);                  // microphone  -> recording channel 1 (right)
  const node = new AudioWorkletNode(ctx, "capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 2, channelCountMode: "explicit" });
  const mute = ctx.createGain(); mute.gain.value = 0;
  merger.connect(node); node.connect(mute); mute.connect(ctx.destination);
  const a = { ctx, stream, agentBus, node, recL: [], recR: [], dsL: new Downsampler(ctx.sampleRate), dsR: new Downsampler(ctx.sampleRate) };
  a.vad = new VAD(onUtterance);
  node.port.onmessage = e => {
    const [L, R] = e.data;
    const l16 = a.dsL.process(L), r16 = a.dsR.process(R);
    a.recL.push(toInt16(l16)); a.recR.push(toInt16(r16));
    a.vad.push(r16);
    $("orb").style.setProperty("--level", (1 + (phase === "listening" ? a.vad.level * 0.35 : 0)).toFixed(3));
  };
  return a;
}

function stopAudio() {
  if (!audio) return;
  audio.stream.getTracks().forEach(t => t.stop());
  audio.node.port.onmessage = null;
  audio.ctx.close();
}

async function playAgent(b64) {
  if (!b64) { toast("Voice service unavailable for this reply - read it in the transcript."); return; }
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const buffer = await audio.ctx.decodeAudioData(bytes.buffer);
  return new Promise(resolve => {
    const src = audio.ctx.createBufferSource();
    src.buffer = buffer; src.connect(audio.agentBus); src.onended = resolve; src.start();
  });
}

// ------------------------------------------------------------------------------------------ phases
const PHASE_TEXT = { idle: "Ready", connecting: "Connecting…", listening: "Listening", thinking: "Thinking…",
                     speaking: "Agent speaking", ended: "Call ended" };

function setPhase(p) {
  phase = p;
  const orb = $("orb");
  orb.className = "orb " + (["listening", "thinking", "speaking"].includes(p) ? p : "");
  $("call-status").textContent = PHASE_TEXT[p];
  if (audio) audio.vad.enabled = p === "listening";
  const live = ["listening", "thinking", "speaking", "connecting"].includes(p);
  $("btn-call").hidden = live; $("btn-end").hidden = !live;
  $("type-input").disabled = $("type-send").disabled = p !== "listening";
  document.querySelectorAll(".agent-option").forEach(b => b.disabled = live);
}

// ------------------------------------------------------------------------------------------ call flow
async function startCall() {
  if (!profile) return;
  if (!window.HEALTH?.groq_key_configured) { toast("Add GROQ_API_KEY to .env and restart the server to place calls."); return; }
  $("transcript").innerHTML = ""; $("knowledge").innerHTML = '<div class="empty">Retrieved records and citations appear after each answer.</div>';
  $("actions").innerHTML = ""; turnCount = 0; $("turn-count").textContent = "";
  setPhase("connecting");
  try {
    audio = await initAudio();
  } catch (e) {
    setPhase("idle"); toast("Microphone unavailable: " + e.message + ". You can still test by typing after the call starts.");
    return;
  }
  try {
    const res = await api("/api/calls", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ profile_id: profile.id }) });
    call = { id: res.call_id, started: Date.now() };
    timerHandle = setInterval(() => $("call-sub").textContent = `${fmtTime((Date.now() - call.started) / 1000)} · ${call.id}`, 500);
    addAgent(res.turn); renderState(res.state);
    setPhase("speaking");
    await playAgent(res.audio_b64);
    if (phase === "speaking") setPhase("listening");
  } catch (e) {
    toast(e.message); stopAudio(); setPhase("idle");
  }
}

async function onUtterance(f32) {
  if (phase !== "listening" || !call) return;
  const form = new FormData();
  form.append("audio", wavBlob([toInt16(f32)]), "utterance.wav");
  await sendTurn(form);
}

async function sendTurn(form, typed) {
  setPhase("thinking");
  const pending = addCustomer(typed ?? "…", null, true);
  try {
    const res = await api(`/api/calls/${call.id}/turn`, { method: "POST", body: form });
    if (!call || call.ended) return;   // user hung up while the agent was thinking
    const ignored = res.turn.kind === "fallback_no_speech";
    updateCustomer(pending, res.user_text || "(no speech detected)", res.asr, ignored);
    addAgent(res.turn); renderState(res.state); renderKnowledge(res.turn);
    setPhase("speaking");
    await playAgent(res.audio_b64);
    if (res.end_call) await finishCall();
    else if (phase === "speaking") setPhase("listening");
  } catch (e) {
    pending.remove(); toast(e.message);
    if (call && !call.ended) setPhase("listening");
  }
}

async function finishCall() {
  if (!call || call.ended) return;
  call.ended = true;
  clearInterval(timerHandle);
  setPhase("ended");
  try {
    const result = await api(`/api/calls/${call.id}/end`, { method: "POST" });
    renderOutcome(result.outcome, result);
    if (audio && audio.recL.length) {
      const form = new FormData();
      form.append("audio", wavBlob([concatInt16(audio.recL), concatInt16(audio.recR)]), "call.wav");
      await api(`/api/calls/${call.id}/recording`, { method: "POST", body: form });
    }
    $("actions").insertAdjacentHTML("beforeend",
      `<div class="banner ok" style="margin:10px 0 0">Saved transcript and stereo recording. <a href="calls.html#${call.id}">Open in Call Log →</a></div>`);
  } catch (e) { toast(e.message); }
  stopAudio(); audio = null;
  setPhase("ended");
}

// ------------------------------------------------------------------------------------------ rendering
function scrollTranscript() { const t = $("transcript"); t.scrollTop = t.scrollHeight; }

function latencyLine(t) {
  const x = t.timings || {};
  const parts = [["STT", x.stt_ms], ["RAG", x.retrieval_ms], ["LLM", x.llm_ms], ["TTS", x.tts_ms]]
    .filter(([, v]) => v != null).map(([k, v]) => `${k} ${v}`);
  return parts.length ? `<span class="lat">${parts.join(" · ")}${x.total_ms ? ` · total ${x.total_ms} ms` : ""}</span>` : "";
}

function addAgent(turn) {
  turnCount++; $("turn-count").textContent = `${turnCount} agent turns`;
  const badges = [];
  if (turn.answer_status === "answered_from_kb") badges.push('<span class="badge ok">grounded</span>');
  if (turn.answer_status === "info_unavailable") badges.push('<span class="badge warn">info unavailable</span>');
  if (turn.action === "escalate_to_human") badges.push('<span class="badge bad">escalated to human</span>');
  if (turn.action === "submit") badges.push('<span class="badge ok">submitted</span>');
  if (turn.action === "end_call") badges.push('<span class="badge neutral">ended</span>');
  if (turn.kind?.startsWith("fallback")) badges.push(`<span class="badge neutral">${esc(turn.kind.replace("_", " "))}</span>`);
  if (turn.reruns) badges.push('<span class="badge info" title="Draft failed validation and was regenerated">self-corrected</span>');
  const cites = (turn.citations || []).map(c => `<span class="chip cite" title="${esc(c.title + "\n" + c.citation)}">${esc(c.record_id)}</span>`);
  const div = document.createElement("div");
  div.className = "msg agent";
  div.innerHTML = `<div class="who">${esc(profile.agent_name)} · agent</div><div class="bubble">${esc(turn.text)}</div>
    <div class="meta">${badges.join("")}${cites.join("")}${latencyLine(turn)}</div>`;
  $("transcript").appendChild(div); scrollTranscript();
}

function addCustomer(text, asr, pending) {
  const div = document.createElement("div");
  div.className = "msg customer";
  div.innerHTML = `<div class="who">You</div><div class="bubble">${esc(text)}</div><div class="meta"></div>`;
  if (pending) div.querySelector(".bubble").classList.add("muted");
  $("transcript").appendChild(div); scrollTranscript();
  return div;
}

function updateCustomer(div, text, asr, ignored) {
  div.querySelector(".bubble").textContent = text;
  div.querySelector(".bubble").classList.remove("muted");
  if (ignored) div.classList.add("ignored");
  if (asr) div.querySelector(".meta").innerHTML =
    `${ignored ? '<span class="badge neutral">ignored: no speech</span>' : ""}<span class="lat">ASR ${asr.ms} ms · ${esc(asr.model)}</span>`;
}

function renderState(state) {
  const f = $("fields");
  f.innerHTML = profile.fields.filter(x => x.name !== "verification_last4" || state.fields[x.name] == null).map(x => {
    const v = state.fields[x.name], c = state.conflicts?.[x.name];
    const cls = c ? "conflict" : v != null ? "done" : "todo";
    const val = c ? `${esc(fmtValue(c.old))} vs ${esc(fmtValue(c.new))} - confirming` : v != null ? esc(fmtValue(v)) : "-";
    return `<div class="field ${cls}"><span class="ico">${c ? "!" : v != null ? "✓" : ""}</span>
      <div><div class="name">${esc(x.label)}${x.required ? "" : '<span class="opt">optional</span>'}</div><div class="val">${val}</div></div></div>`;
  }).join("");
  if (profile.test_account) {
    const ver = state.verified ? '<span class="badge ok">identity verified</span>'
      : `<span class="badge warn">not verified · attempts ${state.verify_attempts}/2</span>`;
    f.insertAdjacentHTML("afterbegin", `<div class="row" style="margin-bottom:4px">${ver}</div>`);
  }
  renderOutcome(state.outcome);
  $("actions").innerHTML = (state.actions || []).map(a =>
    `<div class="row small" style="margin-top:6px"><span class="badge ${a.type.includes("escalat") ? "bad" : "ok"}">${esc(a.type.replaceAll("_", " "))}</span>
     <span class="mono faint">${esc(a.record_id)}</span></div>`).join("") +
    (state.flags || []).map(fl => `<div class="small faint" style="margin-top:6px">⚑ ${esc(fl)}</div>`).join("");
}

function renderOutcome(o, result) {
  const tone = OUTCOME_TONE[o.status] || "";
  const plan = o.plan_recommendation?.length ? `<div class="small" style="margin-top:6px">Suggested plan: <b>${esc(o.plan_recommendation.join(" + "))}</b></div>` : "";
  const reasons = (o.reasons || []).length ? `<ul>${o.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  const crm = result ? `<div class="small muted" style="margin-top:8px"><b>CRM summary</b> (${esc(result.crm_summary_id)}): ${esc(result.crm_summary)}</div>` : "";
  $("outcome").innerHTML = `<div class="outcome ${tone}"><div class="label">${esc(o.label || o.status)}</div>${plan}${reasons}${crm}</div>`;
}

function renderKnowledge(turn) {
  const cited = new Set((turn.citations || []).map(c => c.record_id));
  if (!turn.retrieved?.length) { $("knowledge").innerHTML = '<div class="empty">No retrieval for this turn.</div>'; return; }
  $("knowledge").innerHTML = `<div class="small faint" style="margin-bottom:8px">Query: “${esc(turn.query || "")}”</div>` +
    turn.retrieved.map(h => `<div class="hit ${cited.has(h.record_id) ? "cited" : ""} ${h.relevant ? "" : "weak"}">
      <div class="h"><div class="title">${esc(h.title)}</div>
        ${cited.has(h.record_id) ? '<span class="badge accent">cited</span>' : h.relevant ? '<span class="badge info">relevant</span>' : '<span class="badge neutral">weak</span>'}</div>
      <div class="row wrap" style="margin-top:6px"><span class="chip cite">${esc(h.record_id)}</span><span class="chip">${esc(h.category)}</span>
        <span class="lat">dense ${h.scores.dense} · bm25 ${h.scores.bm25}</span></div></div>`).join("");
}

// ------------------------------------------------------------------------------------------ setup
function selectProfile(p) {
  profile = p;
  document.querySelectorAll(".agent-option").forEach(b => b.classList.toggle("selected", b.dataset.id === p.id));
  $("selected-agent").innerHTML = `${countryBadge(p.market)} ${esc(p.agent_name)} · ${esc(p.title)}`;
  const acc = p.test_account;
  $("test-account-card").hidden = !acc;
  if (acc) {
    const rows = [["Name", acc.customer_name], ["Reference", acc.reference], ["Last 4 digits", `<b>${esc(acc.verify_last4)}</b>`],
      [acc.amount_label, acc.amount], ["Due date", `${acc.due_date} (${acc.days_overdue} days ago)`],
      acc.grace_period_end ? ["Grace period ends", acc.grace_period_end] : ["Penalty so far", acc.penalty_so_far]];
    $("test-account").innerHTML = `<div class="test-account">Answer as <b>${esc(acc.customer_name)}</b>. The agent must verify you before sharing any account details.
      <div class="kv">${rows.map(([k, v]) => `<span class="k">${esc(k)}</span><span>${String(v).startsWith("<b>") ? v : esc(v)}</span>`).join("")}</div></div>`;
  }
  renderState({ fields: {}, conflicts: {}, outcome: { status: "incomplete", label: "Not started", reasons: [] }, actions: [], verify_attempts: 0 });
  $("outcome").innerHTML = '<div class="empty">No call yet</div>';
}

async function init() {
  $("btn-call").innerHTML = `${ICONS.phone} Start web call`;
  $("type-send").innerHTML = ICONS.send;
  $("orb-icon").innerHTML = ICONS.mic;
  $("btn-call").onclick = startCall;
  $("btn-end").onclick = finishCall;
  $("type-form").onsubmit = e => {
    e.preventDefault();
    const text = $("type-input").value.trim();
    if (!text || phase !== "listening") return;
    $("type-input").value = "";
    const form = new FormData(); form.append("text", text);
    sendTurn(form, text);
  };
  try {
    profiles = await api("/api/profiles");
  } catch (e) { toast(e.message); return; }
  $("agents").innerHTML = profiles.map(p => `<button class="agent-option" data-id="${p.id}">
      <div class="t">${countryBadge(p.market)}${esc(p.title)}</div>
      <div class="s">${esc(p.agent_name)} · ${esc(p.company)}</div>
      <div class="row wrap" style="margin-top:7px"><span class="chip">${esc(p.language_label)}</span><span class="chip">ASR: ${esc(p.asr.language)}</span>
        <span class="chip">KB: ${esc(p.kb_collection)}</span></div></button>`).join("");
  document.querySelectorAll(".agent-option").forEach(b => b.onclick = () => selectProfile(profiles.find(p => p.id === b.dataset.id)));
  selectProfile(profiles[0]);
  setPhase("idle");
  setTimeout(() => {
    if (window.HEALTH && !window.HEALTH.groq_key_configured)
      $("banner").innerHTML = '<div class="banner warn">GROQ_API_KEY is not configured. Copy <code>.env.example</code> to <code>.env</code>, add a free key from console.groq.com, and restart the server.</div>';
  }, 800);
}

document.addEventListener("DOMContentLoaded", init);
