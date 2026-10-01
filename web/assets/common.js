// Shared helpers: top navigation, API calls, escaping, formatting, toasts.
const ICONS = {
  phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/></svg>',
  list: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>',
  book: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15z"/><path d="M20 17v5H6.5A2.5 2.5 0 0 1 4 19.5"/></svg>',
  pulse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
};

const PAGES = [
  ["index.html", "Voice Agent", "phone"],
  ["calls.html", "Call Log", "list"],
  ["knowledge.html", "Knowledge Base", "book"],
  ["insights.html", "Live Insights", "pulse"],
];

function renderTopbar() {
  const here = location.pathname.split("/").pop() || "index.html";
  const bar = document.createElement("header");
  bar.className = "topbar";
  bar.innerHTML = `
    <div class="brand">
      <svg viewBox="0 0 32 32" fill="none"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#7c6cff"/><stop offset="1" stop-color="#2dd4bf"/></linearGradient></defs>
        <rect width="32" height="32" rx="9" fill="url(#g)"/><path d="M8 17v-2M12 21V11M16 24V8M20 20v-8M24 17v-2" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>
      Callwise <small>voice AI studio</small>
    </div>
    <nav class="nav">${PAGES.map(([href, label, icon]) =>
      `<a href="${href}" class="${href === here ? "active" : ""}">${ICONS[icon]}${label}</a>`).join("")}</nav>
    <div class="spacer"></div>
    <span class="status-pill" id="health"><span class="dot"></span>checking…</span>`;
  document.body.prepend(bar);
  const toast = document.createElement("div");
  toast.className = "toast"; toast.id = "toast";
  document.body.appendChild(toast);
  api("/api/health").then(h => {
    window.HEALTH = h;
    const el = document.getElementById("health");
    el.innerHTML = h.groq_key_configured
      ? `<span class="dot ok"></span>${h.agent_model.split("/").pop()} · ${h.asr_model}`
      : `<span class="dot bad"></span>GROQ_API_KEY missing`;
    el.title = `Agent LLM: ${h.agent_model}\nFast LLM: ${h.fast_model}\nASR: ${h.asr_model}\nEmbeddings: ${h.embedding_model}`;
  }).catch(() => {});
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, ms = 6000) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}

function fmtTime(s) {
  s = Math.max(0, Math.floor(s));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function fmtValue(v) {
  if (v === true) return "yes";
  if (v === false) return "no";
  return v;
}

const OUTCOME_TONE = {
  qualified: "ok", qualified_underwriting: "ok", promise_to_pay: "ok", callback_scheduled: "ok",
  nurture: "warn", review: "warn", incomplete: "warn", unverified: "warn", paid_pending_verification: "warn", no_commitment: "warn",
  hardship: "warn", ptp_beyond_limit: "bad", retention_escalation: "bad", not_eligible: "bad", do_not_contact: "bad",
  wrong_party: "bad", verification_failed: "bad",
};

const MARKET_CODE = { India: "in", Philippines: "ph", Indonesia: "id" };
const PROFILE_MARKET = { health_lead_in: "India", life_premium_ph: "Philippines", multifinance_id: "Indonesia" };
function countryBadge(market) {
  const code = MARKET_CODE[market] || "";
  return code ? `<span class="cc ${code}" title="${esc(market)}">${code.toUpperCase()}</span>` : "";
}

document.addEventListener("DOMContentLoaded", renderTopbar);
