/* ── App logic ─────────────────────────────────────────────────────────
   Reads from the backend API only:
     GET {API}/api/reports/daily   → { clusters, subsystem_summary }
     GET {API}/incidents

   Schema: system → subsystem → assign_group → assignee
   Default view for Employee = tickets in their assign_group.
──────────────────────────────────────────────────────────────────────── */

// API base derives from the host that served this page — works for
// localhost AND Tailscale/LAN access (100.x.x.x / 192.168.x.x) AND
// tunneled access (trycloudflare.com / named tunnels) without hardcoding.
// When the page came over http(s) from a remote host, the API is served
// same-origin by nginx/cloudflared (which proxy /api /incidents /health)
// — no :8000 port exists on a tunnel domain. Override via localStorage
// "dash_api" / "classify_url".
const API_HOST = location.hostname || "localhost";
const API_OVERRIDE = localStorage.getItem("dash_api");
let API, CLASSIFY_URL;
if (API_OVERRIDE) {
  API = API_OVERRIDE;
  CLASSIFY_URL = localStorage.getItem("classify_url") || API_OVERRIDE;
} else if (location.protocol === "http:" || location.protocol === "https:") {
  // Same-origin: nginx/cloudflared proxy the API paths. localhost stays
  // :8000 for the dev-server case (python http.server on 8085 does NOT proxy).
  API = location.port === "8085" ? `http://${API_HOST}:8000` : location.origin;
  CLASSIFY_URL = API;
} else {
  API = `http://${API_HOST}:8000`;
  CLASSIFY_URL = API;
}
let ROLE = localStorage.getItem("dash_role") || "employee";
let FLAT = localStorage.getItem("dash_flat") === "true";
let EMP_GROUP_FILTER = localStorage.getItem("dash_group_filter") || "all";
let EMP_SEV_FILTER = localStorage.getItem("dash_sev_filter") || "";
let LEAD_TEAM_FILTER = localStorage.getItem("dash_lead_team") || "";

const TEAMS = {
  "App Support":    ["Ahmed K.", "Sara M.", "Layla R."],
  "Payments":       ["Omar T.", "Noura H."],
  "Infrastructure": ["Khalid B.", "Reem S.", "Yusuf A."],
  "Operations":     ["Faisal A.", "Mona S."],
};

const CURRENT_USER = "Ahmed K.";

let DATA = { clusters: [], individuals: [], all: [], totals: {} };

function getUserGroup(user) {
  for (const [group, members] of Object.entries(TEAMS)) {
    if (members.includes(user)) return group;
  }
  return "App Support";
}
const CURRENT_GROUP = getUserGroup(CURRENT_USER);

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s = "") => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const isAr = (s = "") => /[\u0600-\u06FF]/.test(s);
const titleHtml = (t) => (isAr(t) ? `<span lang-ar lang="ar">${esc(t)}</span>` : esc(t));
const SEV_RANK = { Critical: 4, Major: 3, Minor: 2, Cosmetic: 1 };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ── Data loading ── */
async function loadData() {
  setConn(null, "loading");
  try {
    const [rep, incs] = await Promise.all([
      fetch(`${API}/api/reports/daily`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch(`${API}/incidents`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
    ]);
    const clusters = (rep.clusters || []).map((c) => ({
      cluster_id: c.cluster_id, name: c.failure_mode_desc || c.name || c.summary?.slice(0, 60) || "Cluster",
      affected_system: c.affected_system, affected_service: c.affected_service,
      worst_severity: c.worst_severity, count: c.count, summary: c.summary,
      incidents: (c.incidents || []).map((i) => {
        const full = (Array.isArray(incs) ? incs : incs.incidents || []).find((x) => x.id === i.id) || {};
        return {
          id: i.id, title: i.title, lang: isAr(i.title) ? "ar" : "en",
          severity: i.severity || "Minor", canonical_statement: i.canonical_statement,
          similarity_pct: i.similarity_pct, description: i.description,
          assignee: full.assignee || "Unassigned", assign_group: mapTeam(full.assign_group || ""), team: mapTeam(full.assign_group || ""),
          status: full.status || "active", created_hours_ago: full.created_hours_ago,
        };
      }),
    }));
    const inClusters = new Set(clusters.flatMap((c) => c.incidents.map((i) => i.id)));
    const list = Array.isArray(incs) ? incs : incs.incidents || [];
    const individuals = list.filter((i) => !inClusters.has(i.id)).map((i) => ({
      id: i.id, title: i.title, lang: isAr(i.title) ? "ar" : "en",
      severity: safeSev(i), assignee: i.assignee || "Unassigned",
      assign_group: mapTeam(i.assign_group || ""), team: mapTeam(i.assign_group || ""), system: "—", service: "—", status: i.status || "active",
    }));
    DATA = {
      clusters, individuals,
      all: [...clusters.flatMap((c) => c.incidents), ...individuals],
      totals: { tickets: clusters.reduce((s, c) => s + c.count, 0) + individuals.length, problems: clusters.length, individuals: individuals.length },
    };
    setConn(true, "connected");
  } catch (e) {
    console.warn("API fetch failed:", e);
    setConn(false, "API unreachable");
  }
  const fp = fingerprint(DATA);
  if (fp === _lastFp) return; // nothing changed — keep the user's current view intact
  _lastFp = fp;
  render();
  restoreOpenView();
}

/* Data fingerprint — skip re-render when nothing changed so open
   incident/cluster views survive background polling. */
let _lastFp = "";
function fingerprint(d) {
  const c = (d.clusters || []).map((x) => `${x.cluster_id}:${x.count}:${x.worst_severity}`).sort().join("|");
  return `${d.totals ? d.totals.tickets : 0}|${d.totals ? d.totals.problems : 0}|${c}`;
}

/* Re-apply user view state after a data-driven re-render: open clusters,
   expanded ticket row, and cached detail content. */
let _expandedTid = null;
function restoreOpenView() {
  try {
    const savedOpen = JSON.parse(localStorage.getItem("dash_open_clusters") || "[]");
    savedOpen.forEach((cid) => {
      const el = document.getElementById(`cluster-${cid}`);
      if (el && !el.classList.contains("open")) toggleCluster(el);
    });
  } catch {}
  if (_expandedTid) {
    const row = document.querySelector(`.t-row[data-tid="${_expandedTid}"]`);
    if (row) {
      row.classList.add("expanded");
      const detail = row.nextElementSibling;
      if (detail && detail.classList.contains("t-detail")) {
        detail.innerHTML = _detailCache[_expandedTid] || '<div class="t-detail-loading">⏳ Loading incident details…</div>';
      }
    }
  }
}

function safeSev(i) {
  try { return JSON.parse(i.classification || i.classification_json || "{}").severity || "Minor"; }
  catch { return "Minor"; }
}

/* map simulator assign groups → 4 teams */
function mapTeam(g) {
  if (!g) return "App Support";
  g = g.toLowerCase();
  if (g.includes("pay") || g.includes("bill") || g.includes("visa")) return "Payments";
  if (g.includes("infra") || g.includes("net") || g.includes("gate")) return "Infrastructure";
  if (g.includes("op") || g.includes("transport") || g.includes("accom") || g.includes("health") || g.includes("field")) return "Operations";
  return "App Support";
}

function setConn(ok, label) {
  const dot = $("#connDot");
  dot.className = "live-dot" + (ok === false || ok === null ? " off" : "");
  $("#connText").textContent = label;
}

/* ── State persistence ── */
function saveState() {
  localStorage.setItem("dash_role", ROLE);
  localStorage.setItem("dash_flat", String(FLAT));
  localStorage.setItem("dash_group_filter", EMP_GROUP_FILTER);
  localStorage.setItem("dash_sev_filter", EMP_SEV_FILTER);
  localStorage.setItem("dash_lead_team", LEAD_TEAM_FILTER);
  localStorage.setItem("dash_search", $("#empSearch")?.value || "");
  const open = [...document.querySelectorAll(".cluster.open")].map((el) => el.dataset.cid).filter(Boolean);
  localStorage.setItem("dash_open_clusters", JSON.stringify(open));
}

function restoreState() {
  const roleBtn = document.querySelector(`.role-btn[data-role="${ROLE}"]`);
  if (roleBtn) {
    document.querySelectorAll(".role-btn").forEach((x) => x.classList.toggle("active", x === roleBtn));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    const v = document.getElementById(`view-${ROLE}`);
    if (v) v.classList.add("active");
  }
  document.getElementById("vtGrouped")?.classList.toggle("active", !FLAT);
  document.getElementById("vtFlat")?.classList.toggle("active", FLAT);
  const sevEl = document.getElementById("empSevFilter");
  if (sevEl) sevEl.value = EMP_SEV_FILTER;
  const groupEl = document.getElementById("empGroupFilter");
  if (groupEl) groupEl.value = EMP_GROUP_FILTER === "my" ? "my" : EMP_GROUP_FILTER;
  const teamEl = document.getElementById("leadTeamFilter");
  if (teamEl) teamEl.value = LEAD_TEAM_FILTER;
  const searchEl = document.getElementById("empSearch");
  if (searchEl) searchEl.value = localStorage.getItem("dash_search") || "";
}

/* ── Role switching ── */
$("#roleSwitch").addEventListener("click", (e) => {
  const b = e.target.closest(".role-btn");
  if (!b) return;
  ROLE = b.dataset.role;
  $$(".role-btn").forEach((x) => x.classList.toggle("active", x === b));
  $$(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${ROLE}`).classList.add("active");
  saveState();
  render();
});

$("#vtGrouped").addEventListener("click", () => { FLAT = false; syncVT(); saveState(); renderEmployee(); });
$("#vtFlat").addEventListener("click", () => { FLAT = true; syncVT(); saveState(); renderEmployee(); });
function syncVT() {
  $("#vtGrouped").classList.toggle("active", !FLAT);
  $("#vtFlat").classList.toggle("active", FLAT);
}
const _searchEl = $("#empSearch");
_searchEl.addEventListener("input", () => { saveState(); renderEmployee(); searchByIdDebounced(); });
_searchEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(_idSearchTimer); searchIncidentById(); }
  else if (e.key === "Escape") { _searchEl.value = ""; saveState(); renderEmployee(); searchIncidentById(); }
});
$("#empSevFilter").addEventListener("change", function () {
  EMP_SEV_FILTER = this.value;
  saveState();
  renderEmployee();
});
$("#empGroupFilter").addEventListener("change", function () {
  EMP_GROUP_FILTER = this.value;
  saveState();
  renderEmployee();
});
$("#leadTeamFilter").addEventListener("change", function () {
  LEAD_TEAM_FILTER = this.value;
  saveState();
  renderLead();
});

/* ── Render dispatch ── */
function render() {
  if (ROLE === "employee") renderEmployee();
  else if (ROLE === "lead") renderLead();
  else renderManager();
}

/* ═════════ EMPLOYEE ═════════ */

function activeGroup() {
  const v = EMP_GROUP_FILTER;
  if (v === "my") return CURRENT_GROUP;
  if (v === "all") return null; // no group filter
  return v;
}

/* Employee lens: filter by assign_group first (group scope), then assignee
   metadata stays on the row. "my" = CURRENT_GROUP, "all" = no group filter. */
function groupTickets() {
  const g = activeGroup();
  return g ? DATA.all.filter((t) => t.assign_group === g) : DATA.all;
}

function groupClusters() {
  const q = $("#empSearch").value.trim().toLowerCase();
  const sev = EMP_SEV_FILTER;
  const g = activeGroup();
  return DATA.clusters
    .map((c) => ({
      ...c,
      incidents: g ? c.incidents.filter((i) => i.assign_group === g) : c.incidents,
    }))
    .filter((c) => c.incidents.length > 0)
    .filter((c) => !sev || c.worst_severity === sev)
    .filter((c) => !q || c.name.toLowerCase().includes(q) || c.incidents.some((i) =>
      (i.title || "").toLowerCase().includes(q) || (i.id || "").toLowerCase().includes(q)
      || (i.description || "").toLowerCase().includes(q)))
    .sort((a, b) => (SEV_RANK[b.worst_severity] || 0) - (SEV_RANK[a.worst_severity] || 0) || b.incidents.length - a.incidents.length);
}

function renderEmployee() {
  const all = groupTickets();
  const clusters = groupClusters();
  const g = activeGroup();
  const groupName = g || "All groups";
  const gIndiv = g
    ? DATA.individuals.filter((t) => t.assign_group === g)
    : DATA.individuals;
  const inGroups = clusters.reduce((s, c) => s + c.incidents.length, 0);
  const crit = clusters.filter((c) => c.worst_severity === "Critical").length;
  const myInGroup = all.filter((t) => t.assignee === CURRENT_USER).length;

  $("#empHeadline").innerHTML =
    `<span style="font-size:12px;font-weight:600;color:var(--accent);vertical-align:middle">${esc(groupName)}</span> ` +
    `<b>${all.length}</b> tickets · <b>${clusters.length}</b> problems · ${gIndiv.length} individual`;
  $("#empSubline").innerHTML =
    `<span class="who">${CURRENT_USER} · ${groupName}</span>` +
    (crit ? `<span class="chip crit">${crit} critical cluster${crit > 1 ? "s" : ""}</span>` : "") +
    `<span>${inGroups} of ${all.length} tickets group into ${clusters.length} root causes` +
    (myInGroup && myInGroup < all.length ? ` — ${myInGroup} assigned to you` : "") +
    `</span>`;
  $("#empCount").textContent = FLAT ? `${all.length} tickets` : `${clusters.length} groups + ${gIndiv.length} individual`;

  $("#empClusters").style.display = FLAT ? "none" : "";
  $("#empFlat").style.display = FLAT ? "" : "none";

  if (FLAT) {
    const q = $("#empSearch").value.trim().toLowerCase();
    const sev = $("#empSevFilter").value;
    const rows = all
      .filter((t) => !sev || t.severity === sev)
      .filter((t) => !q || (t.title || "").toLowerCase().includes(q)
        || (t.id || "").toLowerCase().includes(q)
        || (t.description || "").toLowerCase().includes(q))
      .sort((a, b) => (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0));
    $("#empFlat").innerHTML = `<div class="flat-table">${rows.map(ticketRow).join("") || `<div class="empty">No tickets match.</div>`}</div>`;
    return;
  }

  $("#empClusters").innerHTML =
    clusters.map((c) => clusterCard(c, { mine: false })).join("") +
    (gIndiv.length
      ? `<div class="ind-section">
           <div class="ind-head">Individual tickets (${gIndiv.length}) <span class="line"></span></div>
           <div class="ind-table">${gIndiv.map(ticketRow).join("")}</div>
         </div>`
      : "") ||
    "";

  bindClusterCards($("#empClusters"));
}

/* ═════════ SHIFT LEAD ═════════ */
function renderLead() {
  const teamSel = LEAD_TEAM_FILTER;
  const clusters = DATA.clusters
    .filter((c) => !teamSel || c.incidents.some((i) => i.team === teamSel))
    .sort((a, b) => (SEV_RANK[b.worst_severity] || 0) - (SEV_RANK[a.worst_severity] || 0) || b.count - a.count);
  const crit = clusters.filter((c) => c.worst_severity === "Critical");
  const total = DATA.all.length;

  $("#leadHeadline").innerHTML = `<b>${clusters.length}</b> active clusters · <span class="hl-crit">${crit.length} critical</span>`;
  $("#leadSubline").innerHTML =
    `<span class="who">Shift lead view · all teams</span><span>${total} active tickets · ${DATA.individuals.length} ungrouped</span>` +
    (crit.length ? `<span class="chip crit">on fire: ${esc(crit[0].name)}</span>` : "");

  // needs human review: biggest cluster + any with mixed languages / low sim
  const review = [];
  clusters.forEach((c) => {
    const langs = new Set(c.incidents.map((i) => i.lang));
    const lowSim = c.incidents.filter((i) => (i.similarity_pct || 100) < 72);
    if (c.count >= 12) review.push({ q: `Split "${c.name}"?`, rs: `${c.count} tickets — may hide 2 distinct issues`, id: c.cluster_id, act: "Split" });
    else if (langs.size > 1 && c.count >= 5) review.push({ q: `Verify cross-language merge`, rs: `"${c.name}" mixes AR/EN tickets`, id: c.cluster_id, act: "Review" });
    lowSim.slice(0, 1).forEach((i) => review.push({ q: `Outlier in "${c.name}"`, rs: `${i.id} similarity only ${i.similarity_pct}%`, id: c.cluster_id, act: "Remove" }));
  });
  $("#reviewCnt").textContent = review.length;
  $("#reviewList").innerHTML = review.slice(0, 6).map((r) => `
    <div class="review-row">
      <div class="q"><div class="nm">${esc(r.q)}</div><div class="rs">${esc(r.rs)}</div></div>
      <button class="btn sm" data-review="${esc(r.act)}">${esc(r.act)}</button>
    </div>`).join("") || `<div class="empty">Nothing needs review. 🎉</div>`;
  $$("#reviewList .btn").forEach((b) => b.addEventListener("click", () => toast(`${b.dataset.review} queued — stub action`)));

  // team load
  const load = [];
  Object.entries(TEAMS).forEach(([team, members]) => {
    members.forEach((m) => {
      const n = DATA.all.filter((t) => t.assignee === m).length;
      load.push({ name: m, team, n });
    });
  });
  load.sort((a, b) => b.n - a.n);
  const max = Math.max(...load.map((l) => l.n), 1);
  const colors = ["#4f9cf9", "#7c5cfc", "#3fb950", "#e8963c", "#f85149", "#d4a72c", "#39c5cf", "#f778ba"];
  $("#teamLoad").innerHTML = load.map((l, i) => `
    <div class="team-row">
      <div class="avatar" style="background:${colors[i % colors.length]}">${esc(l.name.split(" ").map((w) => w[0]).join(""))}</div>
      <div style="min-width:86px">${esc(l.name)}<div style="font-size:10.5px;color:var(--text-faint)">${esc(l.team)}</div></div>
      <div class="bar-wrap"><div class="bar" style="width:${(l.n / max) * 100}%"></div></div>
      <div class="load-num">${l.n} tix</div>
    </div>`).join("");

  $("#leadClusters").innerHTML = clusters.map((c) => clusterCard(c, { lead: true })).join("") || `<div class="empty">No clusters for this filter.</div>`;
  bindClusterCards($("#leadClusters"));
}

/* ═════════ MANAGER ═════════ */
function renderManager() {
  const total = DATA.all.length;
  const clusterCount = DATA.clusters.length;
  const crit = DATA.clusters.filter((c) => c.worst_severity === "Critical").length;
  const deflRatio = total > 10 ? (total / Math.max(1, Math.round(total / 6))).toFixed(1) + "×" : "—";

  $(" #mgrHeadline").innerHTML = `Overview · <b>${total}</b> tickets across <b>${clusterCount}</b> clusters`;
  $(" #mgrSubline").innerHTML = `<span class="who">Manager view · cross-team trends</span><span>${crit} critical clusters</span>`;

  const stat = (lbl, val, delta, dir) => `
    <div class="stat"><div class="lbl">${lbl}</div><div class="val">${val}</div>
    <div class="delta ${dir}">${delta}</div></div>`;
  $("#mgrStats").innerHTML =
    stat("Open tickets", total, `${clusterCount} clusters`, clusterCount > 5 ? "up" : "down") +
    stat("Critical clusters", crit, `${((crit / Math.max(1, clusterCount)) * 100).toFixed(0)}% of total`, crit > 2 ? "up" : "down") +
    stat("Systems involved", [...new Set(DATA.clusters.map((c) => c.affected_system))].length, "unique systems", "flat") +
    stat("Ungrouped tickets", DATA.individuals.length, `${((DATA.individuals.length / Math.max(1, total)) * 100).toFixed(0)}% ungrouped`, DATA.individuals.length > 5 ? "up" : "down");

  // recurring: clusters by size
  const sorted = [...DATA.clusters].sort((a, b) => b.count - a.count).slice(0, 8);
  const maxCount = Math.max(...sorted.map((c) => c.count), 1);
  $("#mgrRecurring").innerHTML = sorted
    .map((x, i) => {
      const spark = Array.from({ length: 5 }, (_, j) =>
        `<i class="${j === 4 ? "cur" : ""}" style="height:${Math.max(6, (x.incidents.filter((t) => t.created_hours_ago && t.created_hours_ago < (j + 1) * 24).length / Math.max(1, x.count)) * 200)}px"></i>`
      ).join("");
      return `<div class="lb-row">
        <div class="lb-rank ${i === 0 ? "hot" : ""}">${i + 1}</div>
        <div class="lb-name">${esc(x.name)}<span class="sub">${esc(x.affected_system)} · ${x.count} tickets</span></div>
        <div class="spark">${spark}</div>
        <div class="lb-num">${x.count}<small>${x.incidents.filter((t) => t.created_hours_ago && t.created_hours_ago < 24).length} today</small></div>
      </div>`;
    }).join("");

  // systems
  const sysCounts = {};
  DATA.clusters.forEach((c) => { sysCounts[c.affected_system] = (sysCounts[c.affected_system] || 0) + c.count; });
  const sysArr = Object.entries(sysCounts).map(([name, count]) => ({ name, count }));
  sysArr.sort((a, b) => b.count - a.count);
  const sysMax = Math.max(...sysArr.map((s) => s.count), 1);
  $("#mgrSystems").innerHTML = sysArr.map((s) => `
    <div class="sysbar-row">
      <div class="nm">${esc(s.name)}</div>
      <div class="bar-wrap"><div class="bar" style="width:${(s.count / sysMax) * 100}%"></div></div>
      <div class="n">${s.count}</div>
    </div>`).join("") || `<div class="empty">No system data.</div>`;

  // resolution histogram (mock distribution)
  const buckets = [
    { bucket: "< 4h", pct: 31 }, { bucket: "4–12h", pct: 27 },
    { bucket: "12–24h", pct: 22 }, { bucket: "1–3d", pct: 14 }, { bucket: "> 3d", pct: 6 },
  ];
  $("#mgrResolution").innerHTML =
    `<div style="display:flex;align-items:flex-end;gap:10px;height:110px;padding:6px 4px 0">` +
    buckets.map((r) => `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;height:100%;justify-content:flex-end">
        <div style="font-size:11px;font-weight:700">${r.pct}%</div>
        <div style="width:100%;background:var(--accent);opacity:.75;border-radius:4px 4px 0 0;height:${r.pct * 2.4}px"></div>
        <div style="font-size:10.5px;color:var(--text-faint)">${r.bucket}</div>
      </div>`).join("") + `</div>`;

  // deflection
  const estReplies = Math.max(1, Math.round(clusterCount * 2.5));
  $("#mgrDeflection").innerHTML = `
    <div style="text-align:center;padding:14px 0 8px">
      <div style="font-size:44px;font-weight:800;letter-spacing:-.03em;color:var(--accent)">${deflRatio}</div>
      <div style="color:var(--text-dim);font-size:12.5px;margin-top:4px">
        <b style="color:var(--text)">${total}</b> tickets grouped into
        <b style="color:var(--text)">${clusterCount}</b> clusters
      </div>
      <div style="color:var(--text-faint);font-size:11.5px;margin-top:10px">
        ~${estReplies} replies needed vs ${total} individual — est. ${Math.round((total - estReplies) * 3 / 60)} agent-hours
      </div>
    </div>`;
}

/* ═════════ shared components ═════════ */

// Dynamic status color mapping — extend as new statuses appear
const STATUS_COLORS = {
  active:      { border: "var(--crit)",  bg: "var(--crit-bg)",  emoji: "🔴" },
  escalated:   { border: "var(--major)", bg: "var(--major-bg)", emoji: "🟡" },
  resolved:    { border: "var(--minor)", bg: "var(--minor-bg)", emoji: "🟢" },
  pending:     { border: "#4f9cf9",      bg: "rgba(79,156,249,.13)", emoji: "🔵" },
  third_party: { border: "#7c5cfc",      bg: "rgba(124,92,252,.13)", emoji: "🟣" },
  verify:      { border: "#39c5cf",      bg: "rgba(57,197,207,.13)", emoji: "⏳" },
  failed:      { border: "#f85149",      bg: "rgba(248,81,73,.13)",  emoji: "❌" },
  cancelled:   { border: "#8b949e",      bg: "rgba(139,148,158,.12)", emoji: "⏹" },
};
const STATUS_DEFAULT = { border: "#8b949e", bg: "rgba(139,148,158,.12)", emoji: "◈" };

function statusLookup(s) {
  return STATUS_COLORS[s] || STATUS_DEFAULT;
}

function statusSection(status, tickets, opts) {
  if (!tickets.length) return "";
  const c = statusLookup(status);
  const label = `${c.emoji} ${status.charAt(0).toUpperCase() + status.slice(1)}`;
  return `<div style="border-left:3px solid ${c.border};margin:0 0 0 13px;padding-left:3px">
    <div class="panel-title" style="margin-bottom:0;padding:6px 16px 2px;background:${c.bg};border-top:1px solid var(--border);border-radius:0">${esc(label)} · ${tickets.length}</div>
    <div class="t-rows" style="border-top:none">${tickets.map((t) => ticketRow(t, opts)).join("")}</div>
  </div>`;
}

function clusterCard(c, opts = {}) {
  const open = c.worst_severity === "Critical" && opts.mine ? "open" : "";
  const shown = c.incidents;
  const mineCount = opts.mine ? shown.length : null;

  // Group tickets by status dynamically
  const byStatus = {};
  shown.forEach((t) => {
    const s = t.status || "active";
    if (!byStatus[s]) byStatus[s] = [];
    byStatus[s].push(t);
  });
  const statusKeys = Object.keys(byStatus).sort((a, b) => {
    const order = ["active", "escalated", "pending", "third_party", "verify", "resolved", "failed", "cancelled"];
    return (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b));
  });
  const growth = c.incidents.find((i) => i.growth)?.growth;
  const tags = [
    opts.mine ? `<span class="tag sys">${mineCount} of your tickets</span>` : `<span class="tag sys">${c.count} tickets</span>`,
    `<span class="tag">${esc(c.affected_system)}</span>`,
    growth ? `<span class="tag growth">▲ ${esc(growth)}</span>` : "",
    c.shared_with_teams ? `<span class="tag" style="color:var(--gold);border-color:var(--gold)">shared: ${esc(c.shared_with_teams.join(" + "))}</span>` : "",
  ].join("");

  const actions = opts.mine
    ? `<button class="btn primary" data-act="reply">✉ Reply to all ${shown.length} with template</button>
       <button class="btn" data-act="link">🔗 Link to parent incident</button>`
    : `<button class="btn primary" data-act="assign">Reassign cluster</button>
       <button class="btn" data-act="merge">Merge with…</button>
       <button class="btn" data-act="split">Split</button>
       <button class="btn danger" data-act="resolve">Resolve all ${c.count}</button>`;

  return `
  <div class="cluster ${open}" data-cid="${esc(c.cluster_id)}" data-state="${open ? "open" : "closed"}" id="cluster-${esc(c.cluster_id)}">
    <div class="cluster-head" role="button" aria-expanded="${open ? "true" : "false"}" tabindex="0">
      <div class="chev">▶</div>
      <div class="c-title">
        <span class="c-name">${esc(c.name)}</span>
        <span class="c-tags">${tags}</span>
      </div>
      <div class="c-right"><span class="sev ${esc(c.worst_severity)}">${esc(c.worst_severity)}</span></div>
    </div>
    <div class="c-body">
      <div class="c-summary">${esc(c.summary || "")}</div>
      <div class="c-actions">${actions}</div>
      ${statusKeys.map((s) => statusSection(s, byStatus[s])).join("")}
    </div>
  </div>`;
}

function ticketRow(t) {
  const sim = t.similarity_pct ? `<span class="sim">${t.similarity_pct}%</span>` : "";
  const x = t.similarity_pct ? `<button class="x-btn" data-remove="${esc(t.id)}" title="Not the same issue — remove from group">✕</button>` : "";
  const st = statusLookup(t.status || "active");
  const statusBadge = `<span class="mini-status" style="color:${st.border};border:1px solid ${st.border};background:${st.bg};border-radius:10px;padding:2px 8px;font-size:11.5px;white-space:nowrap">${st.emoji} ${esc(t.status || "active")}</span>`;
  const meta = [t.assignee && t.assignee !== CURRENT_USER ? esc(t.assignee) : null, t.created_hours_ago != null ? `${t.created_hours_ago}h ago` : null, t.assign_group && t.assign_group !== CURRENT_GROUP ? esc(t.assign_group) : null].filter(Boolean).join(" · ");
  return `
  <div class="t-row" data-tid="${esc(t.id)}">
    <span class="t-id">${esc(t.id)}</span>
    <div class="t-main">
      <div class="t-title">${titleHtml(t.title)}</div>
      ${meta ? `<div class="t-meta">${meta}</div>` : ""}
    </div>
    <div class="t-right">${statusBadge}${sim}<span class="mini-sev ${esc(t.severity || "Minor")}"></span>${x}</div>
  </div>
  <div class="t-detail" data-tid="${esc(t.id)}"><div class="t-detail-loading">⏳ Loading incident details…</div></div>`;
}

function toggleCluster(el) {
  const open = el.classList.toggle("open");
  el.dataset.state = open ? "open" : "closed";
  el.querySelector(".cluster-head").setAttribute("aria-expanded", String(open));
  saveState();
}

function bindClusterCards(root) {
  $$(".cluster-head", root).forEach((h) => {
    h.addEventListener("click", () => toggleCluster(h.closest(".cluster")));
    h.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleCluster(h.closest(".cluster")); }
    });
  });
  $$("[data-act]", root).forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const act = b.dataset.act;
      const cid = b.closest(".cluster").dataset.cid;
      const msgs = {
        reply: "Bulk reply composer would open here (template + AR/EN auto-translate)",
        link: "Link picker — attach group to parent INC",
        assign: "Reassign dialog — stub",
        merge: "Merge picker — stub",
        split: "Split flow — select tickets to move out",
        resolve: "Resolve-all requires confirmation in real build",
      };
      toast(`${act} on ${cid}: ${msgs[act] || "stub"}`);
    })
  );
  $$("[data-remove]", root).forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      toast(`${b.dataset.remove} flagged for removal — correction logged (stub)`);
    })
  );
  // Ticket row click — expand/collapse detail panel
  $$(".t-row", root).forEach((row) => {
    row.addEventListener("click", (e) => {
      // Don't toggle if clicking on the remove button
      if (e.target.closest("[data-remove]")) return;
      const tid = row.dataset.tid;
      const detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains("t-detail")) return;
      const wasExpanded = row.classList.contains("expanded");
      // Collapse all others
      row.closest(".t-rows")?.querySelectorAll(".t-row.expanded").forEach((r) => {
        if (r !== row) r.classList.remove("expanded");
      });
      if (wasExpanded) {
        row.classList.remove("expanded");
        if (_expandedTid === tid) _expandedTid = null;
        return;
      }
      row.classList.add("expanded");
      _expandedTid = tid;
      fetchIncidentDetail(tid, detail);
    });
  });
}

/* ── Fetch incident detail and render ── */
let _detailCache = {};
async function fetchIncidentDetail(tid, detailEl) {
  if (_detailCache[tid]) {
    detailEl.innerHTML = _detailCache[tid];
    return;
  }
  detailEl.innerHTML = '<div class="t-detail-loading">⏳ Loading incident details…</div>';
  try {
    const resp = await fetch(`${API}/incidents/${tid}`);
    if (!resp.ok) throw new Error(resp.status);
    const inc = await resp.json();
    const html = renderIncidentDetail(inc);
    _detailCache[tid] = html;
    detailEl.innerHTML = html;
  } catch (e) {
    detailEl.innerHTML = `<div class="t-detail-loading" style="color:var(--crit)">❌ Failed to load: ${e.message}</div>`;
  }
}

/* ── Search UX: incident-ID lookup + feedback ──────────────────────
   - exact 12-hex ID  -> server-side GET /incidents/{id}, full detail panel
   - 6-11 hex prefix  -> live hint + client-side match count (IDs are 12 chars)
   - Enter            -> run the lookup immediately, scroll to the result
   - Esc              -> clear the search
   - close × on the panel clears the search too                            */
let _idSearchTimer = null;
let _lastIdQuery = "";
function searchByIdDebounced() {
  clearTimeout(_idSearchTimer);
  _idSearchTimer = setTimeout(searchIncidentById, 250);
}
function ensureIdResultBox() {
  let box = document.getElementById("idSearchResult");
  if (box) return box;
  box = document.createElement("div");
  box.id = "idSearchResult";
  box.style.cssText = "background:var(--surface);border:1px solid var(--accent);border-radius:var(--radius);margin:14px 0;display:none";
  const view = document.querySelector(".wrap .view") || document.querySelector(".wrap");
  view.parentNode.insertBefore(box, view);
  return box;
}
function idResultHead(labelHtml) {
  return '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);font-size:12px;font-weight:700;color:var(--accent)">' + labelHtml +
    '<button id="idSearchClose" title="Clear search" style="background:transparent;border:none;color:var(--text-faint);font-size:16px;line-height:1;padding:2px 6px;cursor:pointer">×</button></div>';
}
function hookIdSearchClose(box) {
  const btn = box.querySelector("#idSearchClose");
  if (btn) btn.addEventListener("click", () => {
    const s = document.getElementById("empSearch");
    if (s) s.value = "";
    box.style.display = "none";
    _lastIdQuery = "";
    saveState();
    renderEmployee();
    if (s) s.focus();
  });
}
async function searchIncidentById() {
  const q = ($("#empSearch").value || "").trim().toLowerCase();
  const box = ensureIdResultBox();
  const exact = /^[0-9a-f]{12}$/.test(q);
  const prefix = /^[0-9a-f]{6,11}$/.test(q);
  if (!exact && !prefix) { box.style.display = "none"; _lastIdQuery = ""; return; }
  if (q === _lastIdQuery) return;
  _lastIdQuery = q;
  box.style.display = "block";
  if (prefix && !exact) {
    const ids = (DATA.all || []).map((t) => (t.id || "").toLowerCase());
    const n = ids.filter((i) => i.startsWith(q)).length;
    box.innerHTML = idResultHead("Incident ID lookup") +
      '<div style="padding:12px 16px;font-size:13px;color:var(--text-dim)">' +
      (n ? "<b>" + n + "</b> ticket" + (n > 1 ? "s" : "") + " start with <code>" + esc(q) + "</code> — keep typing, IDs are 12 characters."
         : "No tickets start with <code>" + esc(q) + "</code> — IDs are 12 characters.") +
      "</div>";
    hookIdSearchClose(box);
    return;
  }
  box.innerHTML = idResultHead("Incident <code>" + esc(q) + "</code>") +
    '<div class="t-detail-loading" style="padding:12px 16px">⏳ Looking up…</div>';
  try {
    const resp = await fetch(`${API}/incidents/${encodeURIComponent(q)}`);
    if (!resp.ok) {
      box.innerHTML = idResultHead("Incident <code>" + esc(q) + "</code>") +
        '<div style="padding:12px 16px;font-size:13px;color:var(--text-dim)">No incident found with this ID — check the ID shown after filing, or use the list below.</div>';
      hookIdSearchClose(box);
      return;
    }
    const inc = await resp.json();
    box.innerHTML = idResultHead("Incident <code>" + esc(q) + "</code>") +
      '<div style="padding:6px 16px 12px">' + renderIncidentDetail(inc) + "</div>";
    hookIdSearchClose(box);
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    box.innerHTML = idResultHead("Incident <code>" + esc(q) + "</code>") +
      '<div style="padding:12px 16px;font-size:13px;color:var(--crit)">❌ Lookup failed: ' + esc(e.message || e) + "</div>";
    hookIdSearchClose(box);
  }
}

function renderIncidentDetail(inc) {
  let cls = inc.classification || inc.classification_json || {};
  if (typeof cls === "string") try { cls = JSON.parse(cls); } catch {}
  const fm = cls.failure_mode || "—";
  const conf = cls.confidence || "—";
  const confClass = conf === "high" ? "conf-high" : conf === "medium" ? "conf-medium" : conf === "low" ? "conf-low" : "";
  const severity = cls.severity || "—";
  const sevBadge = `<span class="sev ${esc(severity)}" style="font-size:11px;display:inline-block">${esc(severity)}</span>`;
  const urgency = cls.urgency || "—";
  const category = cls.category || "—";
  const incidentType = cls.incident_type || "—";
  const system = cls.affected_system || inc.affected_system || "—";
  const rawService = cls.service || "—";
  // Split dot-path: "Service.Offering" → separate rows
  const svcParts = rawService.split(".");
  const serviceName = esc(svcParts[0]);
  const offeringName = svcParts.length > 1 ? esc(svcParts.slice(1).join(".")) : null;
  const signature = esc(cls.signature || "—");
  const canonical = esc(cls.canonical_statement || "—");
  const reasoning = esc(cls.reasoning || "—");
  const desc = esc(inc.description || "—");
  const assignee = esc(inc.assignee || "—");
  const assignGroup = esc(inc.assign_group || "—");
  const priority = esc(inc.priority || "—");
  const status = inc.status || "—";
  const createdAt = inc.created_at ? new Date(inc.created_at).toLocaleString() : "—";
  const firstSeen = inc.first_seen ? new Date(inc.first_seen).toLocaleString() : "—";
  const lastSeen = inc.last_seen ? new Date(inc.last_seen).toLocaleString() : "—";
  const docCount = (inc.documents || []).length;
  const tickets = (inc.source_ticket_ids || []).join(", ") || "—";
  const occCount = inc.occurrence_count || 1;
  const notes = esc(inc.notes || "—");
  const contentHash = inc.content_hash ? `<code>${esc(inc.content_hash)}</code>` : "—";
  const extText = esc(inc.extracted_text || "");
  const discHist = (inc.discussion_history || []).length;
  const escInfo = esc(inc.escalation_info || "—");
  const compCode = esc(inc.completion_code || "—");

  const offeringRow = offeringName
    ? `<div class="dd-row"><span class="dd-label">Offering (Sub-Service)</span><span class="dd-value"><code>${offeringName}</code></span></div>`
    : "";

  const extRow = extText
    ? `<div class="dd-row"><span class="dd-label">Extracted Text (OCR)</span><span class="dd-value" style="font-size:11px;max-height:80px;overflow-y:auto">${extText}</span></div>`
    : "";

  return `<div class="t-detail-inner">
    <div class="dd-row"><span class="dd-label">Failure Mode</span><span class="dd-value"><span class="fm-badge">${esc(fm)}</span> ${sevBadge}</span></div>
    <div class="dd-row"><span class="dd-label">Confidence</span><span class="dd-value"><span class="${confClass}">${esc(conf)}</span></span></div>
    <div class="dd-row"><span class="dd-label">System</span><span class="dd-value">${esc(system)}</span></div>
    <div class="dd-row"><span class="dd-label">Service</span><span class="dd-value">${serviceName}</span></div>
    ${offeringRow}
    <div class="dd-row"><span class="dd-label">Incident Type</span><span class="dd-value">${esc(incidentType)}</span></div>
    <div class="dd-row"><span class="dd-label">Urgency</span><span class="dd-value">${esc(urgency)}</span></div>
    <div class="dd-row"><span class="dd-label">Category (Root Cause)</span><span class="dd-value">${esc(category)}</span></div>
    <div class="dd-row"><span class="dd-label">Signature</span><span class="dd-value">${signature}</span></div>
    <div class="dd-row"><span class="dd-label">Canonical Statement</span><span class="dd-value">${canonical}</span></div>
    <div class="dd-row"><span class="dd-label">Reasoning</span><span class="dd-value" style="font-size:11.5px;color:var(--text-dim)">${reasoning}</span></div>
    <div class="dd-row"><span class="dd-label">Description</span><span class="dd-value" style="max-height:100px;overflow-y:auto;font-size:11.5px">${desc}</span></div>
    ${extRow}
    <div class="dd-row" style="border-top:1px solid var(--border);margin-top:4px;padding-top:6px"><span class="dd-label">Assignee</span><span class="dd-value">${assignee} <span style="color:var(--text-faint)">(${assignGroup})</span></span></div>
    <div class="dd-row"><span class="dd-label">Priority</span><span class="dd-value">${esc(priority)}</span></div>
    <div class="dd-row"><span class="dd-label">Status</span><span class="dd-value">${esc(status)}</span></div>
    <div class="dd-row"><span class="dd-label">Occurrence Count</span><span class="dd-value">${occCount}×${occCount > 1 ? ` <span style="color:var(--text-faint);font-size:11px">(seen by dedupe gate)</span>` : ""}</span></div>
    <div class="dd-row"><span class="dd-label">Created</span><span class="dd-value">${createdAt}</span></div>
    <div class="dd-row"><span class="dd-label">First Seen</span><span class="dd-value">${firstSeen}</span></div>
    <div class="dd-row"><span class="dd-label">Last Seen</span><span class="dd-value">${lastSeen}</span></div>
    <div class="dd-row"><span class="dd-label">Content Hash</span><span class="dd-value">${contentHash}</span></div>
    <div class="dd-row"><span class="dd-label">Source Ticket IDs</span><span class="dd-value"><code>${tickets}</code></span></div>
    <div class="dd-row"><span class="dd-label">Documents</span><span class="dd-value">${docCount} file(s)</span></div>
    <div class="dd-row"><span class="dd-label">Discussion History</span><span class="dd-value">${discHist} message(s)</span></div>
    <div class="dd-row"><span class="dd-label">Escalation Info</span><span class="dd-value">${escInfo}</span></div>
    <div class="dd-row"><span class="dd-label">Completion Code</span><span class="dd-value">${compCode}</span></div>
    <div class="dd-row"><span class="dd-label">Notes</span><span class="dd-value">${notes}</span></div>
  </div>`;
}

/* ── boot ── */
restoreState();

const classifyLink = $("classifyLink");
if (classifyLink) classifyLink.href = CLASSIFY_URL;
// Set the group filter dropdown label dynamically
const groupOpt = $("#empGroupFilter option[value='my']");
if (groupOpt) groupOpt.textContent = `My Group (${CURRENT_GROUP})`;

// Poll the API — re-render only when the data fingerprint actually changed,
// so open incident/cluster views are never wiped by background refreshes.
let lastRun = Date.now();

function updateTimer() {
  const elapsed = Math.floor((Date.now() - lastRun) / 1000);
  const fmt = (s) => `${Math.floor(s / 60)}m ${s % 60}s`;
  $("#lastRunTimer").textContent = `🤖 clustering: ${fmt(elapsed)} ago`;
}

setInterval(updateTimer, 1000);

setInterval(async () => {
  lastRun = Date.now();
  const before = _lastFp;
  await loadData();
  if (_lastFp && _lastFp !== before) toast("🔄 Clustering cycle refreshed");
}, 60000);

loadData().then(() => {
  // Restore open clusters from saved state
  try {
    const savedOpen = JSON.parse(localStorage.getItem("dash_open_clusters") || "[]");
    savedOpen.forEach((cid) => {
      const el = document.getElementById(`cluster-${cid}`);
      if (el && !el.classList.contains("open")) toggleCluster(el);
    });
  } catch {}
  // deep link: #cluster=G-102 opens that cluster and scrolls to it
  const m = location.hash.match(/cluster=([\w-]+)/);
  if (m) {
    const el = document.getElementById(`cluster-${m[1]}`);
    if (el) {
      if (!el.classList.contains("open")) toggleCluster(el);
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  // deep link: #incident=61e416f9dea6 runs the ID search for that incident
  const im = location.hash.match(/incident=([0-9a-f]{12})/);
  if (im) {
    const s = document.getElementById("empSearch");
    if (s) { s.value = im[1]; searchIncidentById(); }
  }
});
