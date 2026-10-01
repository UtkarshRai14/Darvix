// Knowledge base explorer: collection stats, retrieval playground, ingestion report, records, evaluation.
const $ = id => document.getElementById(id);
let overview = [], current = null, report = null, category = null, tab = "sources";

const EXAMPLES = {
  health_in: ["Is there a waiting period for pre-existing diseases?", "Can I cover my parents?", "This is too expensive for me",
              "What is the free look period?", "What is your claim settlement ratio?"],
  life_ph: ["Hanggang kailan po ang grace period?", "Wala pa akong pera ngayon", "Paano magpalit ng beneficiary?", "What happens if my policy lapses?"],
  finance_id: ["Berapa denda kalau telat bayar?", "Bayar cicilan bisa lewat mana aja?", "Saya kena PHK, nggak bisa bayar", "Kapan BPKB bisa diambil?"],
};

function tiles(t) {
  const items = [
    [t.sources, "sources", ""], [t.active, "active records", "accent"], [t.duplicates_removed, "duplicates removed", ""],
    [t.superseded_conflicts, "conflicts superseded", "warn"], [t.quarantined, "quarantined (source errors)", "warn"],
    [t.obsolete_removed ?? 0, "obsolete removed", ""], [t.sources_failed, "extraction failures", t.sources_failed ? "bad" : ""],
    [Object.values(t.pii_entities_redacted).reduce((a, b) => a + b, 0), "PII entities redacted", "ok"]];
  $("tiles").innerHTML = items.map(([v, l, c]) => `<div class="tile ${c}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
}

async function selectCollection(name) {
  current = overview.find(c => c.collection === name);
  category = null;
  document.querySelectorAll("#collections .tab").forEach(b => b.classList.toggle("active", b.dataset.c === name));
  tiles(current.totals);
  $("examples").innerHTML = (EXAMPLES[name] || []).map(q => `<button class="chip" type="button">${esc(q)}</button>`).join("");
  $("examples").querySelectorAll("button").forEach(b => b.onclick = () => { $("q").value = b.textContent; search(); });
  $("cats").innerHTML = [["", "all"], ...Object.keys(current.categories).map(c => [c, `${c} (${current.categories[c]})`])]
    .map(([c, l]) => `<button class="chip ${c === "" ? "on" : ""}" data-c="${c}" type="button">${esc(l)}</button>`).join("");
  $("cats").querySelectorAll("button").forEach(b => b.onclick = () => {
    category = b.dataset.c || null;
    $("cats").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    if ($("q").value.trim()) search();
  });
  $("results").innerHTML = '<div class="empty">Search to see ranked records, scores, relevance-gate decisions and citations.</div>';
  $("result-count").textContent = ""; $("search-meta").textContent = `KB version v${current.kb_version} · built ${current.generated_at.replace("T", " ")}`;
  report = await api(`/api/kb/${name}/report`);
  showTab(tab);
}

async function search() {
  const q = $("q").value.trim();
  if (!q) return;
  const params = new URLSearchParams({ q, k: 5 });
  if (category) params.set("category", category);
  const res = await api(`/api/kb/${current.collection}/search?${params}`);
  $("result-count").textContent = `${res.hits.length} · ${res.latency_ms} ms`;
  $("search-meta").innerHTML = `KB v${current.kb_version} · retrieval ${res.latency_ms} ms` +
    (res.expanded_query !== q ? `<br>Expanded with market glossary: <span class="mono">${esc(res.expanded_query)}</span>` : "");
  const anyRelevant = res.hits.some(h => h.relevant);
  $("results").innerHTML = (anyRelevant ? "" : '<div class="banner warn">No record passed the relevance gate → the voice agent would say this information is unavailable and offer a human follow-up.</div>') +
    res.hits.map((h, i) => `<div class="hit ${h.relevant ? "" : "weak"}">
      <div class="h"><div class="title">#${i + 1} ${esc(h.title)}</div>${h.relevant ? '<span class="badge ok">passes gate</span>' : '<span class="badge neutral">below gate</span>'}</div>
      <div class="row wrap" style="margin-top:6px"><span class="chip cite">${esc(h.record_id)}</span><span class="chip">${esc(h.category)}</span>
        ${h.products.map(p => `<span class="chip">${esc(p)}</span>`).join("")}<span class="chip">audience: ${esc(h.audience)}</span><span class="chip">v${esc(h.version)}</span></div>
      <div class="content">${esc(h.content)}</div>
      <div class="bars">
        <div class="bar">dense <div class="track"><div class="fill" style="width:${Math.max(0, h.scores.dense) * 100}%"></div></div><span class="mono">${h.scores.dense}</span></div>
        <div class="bar">bm25 <div class="track"><div class="fill" style="width:${Math.min(100, h.scores.bm25 * 6)}%"></div></div><span class="mono">${h.scores.bm25}</span></div>
        <div class="bar">rrf <span class="mono">${h.scores.rrf}</span></div></div>
      <div class="src">${esc(h.citation)}${h.source.url ? ` · ${esc(h.source.url)}` : ""}</div></div>`).join("");
}

function statusBadge(s) {
  const tone = { ok: "ok", failed: "bad", excluded: "neutral", active: "ok", duplicate: "neutral", superseded: "warn",
                 quarantined: "bad", obsolete: "neutral" }[s] || "neutral";
  return `<span class="badge ${tone}">${esc(s)}</span>`;
}

async function showTab(name) {
  tab = name;
  document.querySelectorAll("#report-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  const body = $("report-body");
  if (name === "sources") {
    body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Source</th><th>Type</th><th>Status</th><th>Sections</th><th>Records</th>
      <th>Boilerplate removed</th><th>Notes / issues</th></tr></thead><tbody>${report.sources.map(s => `<tr>
      <td><div class="mono">${esc(s.source_id)}</div><div class="small faint">${esc(s.path)}</div></td><td>${esc(s.type)}</td><td>${statusBadge(s.status)}</td>
      <td>${s.sections}</td><td>${s.records}</td>
      <td title="${esc((s.boilerplate_examples || []).join("\n"))}">${s.boilerplate_removed}</td>
      <td class="small">${[...s.issues.map(i => `<div style="color:var(--bad)">⚠ ${esc(i)}</div>`), ...s.notes.map(n => `<div class="muted">${esc(n)}</div>`),
        s.pii_found ? `<div class="muted">PII found (not ingested): ${esc(JSON.stringify(s.pii_found))}</div>` : ""].join("")}</td></tr>`).join("")}</tbody></table></div>`;
  } else if (name === "quality") {
    const ev = report.dedup_events, fl = report.flagged_records;
    body.innerHTML = `<div class="card-title">Duplicate and conflict resolution <span class="count">${ev.length} events</span></div>
      ${ev.length ? `<div class="table-wrap"><table><thead><tr><th>Type</th><th>Kept</th><th>Dropped / superseded</th><th>Similarity</th></tr></thead><tbody>
      ${ev.map(e => `<tr><td>${statusBadge(e.type === "conflict" ? "superseded" : "duplicate")} ${esc(e.type)}</td>
        <td><span class="mono">${esc(e.kept)}</span><div class="small faint">${esc(e.kept_source)}</div></td>
        <td><span class="mono">${esc(e.dropped)}</span><div class="small faint">${esc(e.dropped_source)}</div></td>
        <td class="mono small">jaccard ${e.jaccard} · containment ${e.containment}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">None</div>'}
      <div class="card-title" style="margin-top:18px">Flagged records</div>
      ${fl.length ? `<div class="table-wrap"><table><thead><tr><th>Record</th><th>Status</th><th>Flags</th><th>Source</th></tr></thead><tbody>
      ${fl.map(f => `<tr><td class="mono">${esc(f.record_id)}</td><td>${statusBadge(f.status)}</td><td class="small">${f.flags.map(esc).join("<br>")}</td>
        <td class="small faint">${esc(f.source)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">None</div>'}
      <div class="card-title" style="margin-top:18px">PII protection</div>
      <div class="small muted">Redacted entities in ingested records: <span class="mono">${esc(JSON.stringify(report.totals.pii_entities_redacted))}</span>
        across ${report.totals.records_with_pii_redacted} record(s). Raw values are replaced by typed placeholders and never written to the index or reports (only counts by type).</div>`;
  } else if (name === "records") {
    const rows = await api(`/api/kb/${current.collection}/records`);
    body.innerHTML = `<div class="small muted" style="margin-bottom:10px">${rows.length} records (all statuses). Only <b>active</b> records are indexed.</div>
      <div class="table-wrap" style="max-height:560px;overflow:auto"><table><thead><tr><th>record_id</th><th>Title</th><th>Category</th><th>Status</th><th>Version</th><th>PII</th><th>Source</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="mono">${esc(r.record_id)}</td><td><div>${esc(r.title)}</div><div class="small faint">${esc(r.content.slice(0, 140))}${r.content.length > 140 ? "…" : ""}</div></td>
        <td>${esc(r.category)}</td><td>${statusBadge(r.status)}</td><td class="mono">${esc(r.version)}</td>
        <td>${r.pii.detected ? `<span class="badge warn">${esc(r.pii.types.join(", "))}</span>` : '<span class="faint">false</span>'}</td>
        <td class="small faint">${esc(r.source.path.replace("data/raw/", ""))}${r.source.page ? ` p.${r.source.page}` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  } else {
    const rows = (await api("/api/kb-eval")).filter(r => r.collection === current.collection);
    const all = await api("/api/kb-eval");
    const count = v => all.filter(r => r.verdict === v).length;
    body.innerHTML = `<div class="tiles" style="margin-bottom:12px"><div class="tile ok"><div class="v">${count("correct")}</div><div class="l">correct (all collections)</div></div>
      <div class="tile warn"><div class="v">${count("partially correct")}</div><div class="l">partially correct</div></div>
      <div class="tile bad"><div class="v">${count("incorrect")}</div><div class="l">incorrect</div></div></div>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>ID</th><th>Type</th><th>Question</th><th>Retrieved record</th><th>Relevance explanation</th><th>Verdict</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="mono">${r.id}</td><td>${esc(r.type)}</td><td>${esc(r.question)}</td>
        <td>${r.top_hit ? `<span class="mono">${esc(r.top_hit.record_id)}</span><div class="small faint">${esc(r.top_hit.citation)}</div>` : "-"}</td>
        <td class="small">${esc(r.explanation)}</td>
        <td><span class="badge ${{ correct: "ok", incorrect: "bad" }[r.verdict] || "warn"}">${esc(r.verdict)}</span></td></tr>`).join("")}</tbody></table></div>`
      : '<div class="empty">Run <code>python -m app.kb.evaluate</code> to generate the evaluation.</div>'}`;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("search-icon").outerHTML = ICONS.search;
  $("search-form").onsubmit = e => { e.preventDefault(); search().catch(err => toast(err.message)); };
  document.querySelectorAll("#report-tabs .tab").forEach(b => b.onclick = () => showTab(b.dataset.tab));
  overview = await api("/api/kb");
  if (!overview.length) { $("tiles").innerHTML = '<div class="empty">No knowledge base built yet. Run <code>python -m app.kb.ingest</code>.</div>'; return; }
  $("collections").innerHTML = overview.map(c => `<button class="tab" data-c="${c.collection}" title="${esc(c.name)}">${esc(c.collection)}</button>`).join("");
  document.querySelectorAll("#collections .tab").forEach(b => b.onclick = () => selectCollection(b.dataset.c));
  selectCollection(overview.find(c => c.collection === "health_in") ? "health_in" : overview[0].collection);
});
