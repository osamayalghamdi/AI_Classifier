/* Admin console — taxonomy / credentials / status / incidents / groups / tests.
 * All /admin/* calls carry the bearer token; it's kept in localStorage so a
 * refresh doesn't re-prompt. The token itself is never rendered.
 */
"use strict";

const TOKEN_KEY = "admin_token";
let TOKEN = localStorage.getItem(TOKEN_KEY) || "";

const $ = (id) => document.getElementById(id);

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (kind || "ok");
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  if (opts.body && typeof opts.body !== "string") {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(`${res.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return data;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ── Auth ──────────────────────────────────────────────────────────────

$("authBtn").addEventListener("click", async () => {
  const tok = $("tokenInput").value.trim();
  if (!tok) return;
  TOKEN = tok;
  localStorage.setItem(TOKEN_KEY, tok);
  $("tokenInput").value = "";
  try {
    await api("/admin/status");
    $("tabs").style.display = "flex";
    $("authBtn").textContent = "✓ Unlocked";
    loadAll();
  } catch (e) {
    TOKEN = "";
    localStorage.removeItem(TOKEN_KEY);
    alert("Auth failed: " + e.message);
  }
});

// Already have a token? try to use it silently.
(async function init() {
  if (!TOKEN) return;
  try {
    await api("/admin/status");
    $("tabs").style.display = "flex";
    loadAll();
  } catch (_) {
    localStorage.removeItem(TOKEN_KEY);
    TOKEN = "";
  }
})();

// ── Tabs ──────────────────────────────────────────────────────────────

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $((btn.dataset.tab === "status" ? "statusTab" : btn.dataset.tab + "Tab")).classList.add("active");
  });
});

function loadAll() {
  loadStatus(); loadTaxonomy(); loadEnv(); loadGroups();
}

// ── Status ────────────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const d = await api("/admin/status");
    const i = d.incidents, c = d.clusters;
    const html = `
      <div class="grid">
        <div class="stat"><div class="k">Store</div><div class="v">${d.store_ready ? "ready" : "DOWN"}</div></div>
        <div class="stat"><div class="k">Embedding</div><div class="v">${d.embedding_ready ? "loaded" : "not loaded"}</div></div>
        <div class="stat"><div class="k">Incidents</div><div class="v">${i.total}</div></div>
        <div class="stat"><div class="k">OK / failed</div><div class="v">${i.ok} / ${i.failed}</div></div>
        <div class="stat"><div class="k">Active / resolved</div><div class="v">${i.active} / ${i.resolved}</div></div>
        <div class="stat"><div class="k">Clusters</div><div class="v">${c.total} <span style="font-size:12px;color:var(--muted)">(${c.active} active, ${c.proposed} proposed)</span></div></div>
        <div class="stat"><div class="k">Unassigned pool</div><div class="v">${d.unassigned_pool}</div></div>
      </div>
      <p style="margin-top:14px" class="hint">Model: <span class="mono">${esc(d.model)}</span> · server time: ${esc(d.server_time)} · version ${esc(d.version)}</p>`;
    $("statusBody").innerHTML = html;
  } catch (e) {
    $("statusBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

// ── Taxonomy ──────────────────────────────────────────────────────────

async function loadTaxonomy() {
  try {
    const d = await api("/admin/taxonomy");
    const rows = [];
    for (const sys of d.systems) {
      for (const svc of sys.services) {
        rows.push(`<tr>
          <td class="mono">${esc(sys.system)}</td>
          <td class="mono">${esc(svc.service)}</td>
          <td class="mono">${esc((svc.offerings || []).join(", "))}</td>
          <td><button class="small danger" data-del-sys="${esc(sys.system)}" data-del-svc="${esc(svc.service)}">remove</button></td>
        </tr>`);
      }
    }
    const ov = Object.keys(d.overrides).length
      ? `<p class="hint">Runtime overrides active: ${Object.keys(d.overrides).join(", ")}</p>`
      : `<p class="hint">No runtime overrides — taxonomy is the frozen base.</p>`;
    $("taxonomyBody").innerHTML =
      `<table><thead><tr><th>System</th><th>Service</th><th>Offerings</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>${ov}`;
    document.querySelectorAll("[data-del-sys]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm("Remove service '" + b.dataset.delSvc + "' from the overrides?")) return;
        try {
          await api("/admin/taxonomy/service", {
            method: "DELETE", body: { system: b.dataset.delSys, service: b.dataset.delSvc },
          });
          loadTaxonomy();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("taxonomyBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

$("txAddService").addEventListener("click", async () => {
  const system = $("txSystem").value.trim(), service = $("txService").value.trim();
  const offerings = $("txOfferings").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!system || !service) return setMsg("txMsg", "system + service required", "err");
  try {
    await api("/admin/taxonomy/service", { method: "POST", body: { system, service, offerings } });
    setMsg("txMsg", "Service added — effective immediately (new classifications + validation).");
    $("txSystem").value = $("txService").value = $("txOfferings").value = "";
    loadTaxonomy();
  } catch (e) { setMsg("txMsg", e.message, "err"); }
});

$("txAddOffering").addEventListener("click", async () => {
  const system = $("txSystem").value.trim(), service = $("txOfferService").value.trim(),
        offering = $("txOffering").value.trim();
  if (!system || !service || !offering) return setMsg("txMsg", "system + service + offering required", "err");
  try {
    await api("/admin/taxonomy/offering", { method: "POST", body: { system, service, offering } });
    setMsg("txMsg", "Offering added.");
    $("txOfferService").value = $("txOffering").value = "";
    loadTaxonomy();
  } catch (e) { setMsg("txMsg", e.message, "err"); }
});

// ── Env ───────────────────────────────────────────────────────────────

async function loadEnv() {
  try {
    const d = await api("/admin/env");
    $("envFilePath").textContent = d.file;
    const rows = d.keys.map((k) => `
      <tr>
        <td class="mono">${esc(k.key)}</td>
        <td>${k.set ? '<span class="badge ok">set</span>' : '<span class="badge bad">unset</span>'}</td>
        <td class="mono">${esc(k.masked)}</td>
        <td><input data-env-key="${esc(k.key)}" placeholder="new value" ${k.key.includes("KEY") || k.key.includes("TOKEN") ? 'type="password"' : ""}></td>
        <td><button class="small" data-env-save="${esc(k.key)}">save</button></td>
      </tr>`);
    $("envTable").querySelector("tbody").innerHTML = rows.join("");
    document.querySelectorAll("[data-env-save]").forEach((b) => {
      b.addEventListener("click", async () => {
        const key = b.dataset.envSave;
        const value = document.querySelector(`[data-env-key="${key}"]`).value;
        try {
          const r = await api("/admin/env", { method: "POST", body: { key, value } });
          alert("Saved. " + (r.note || ""));
          loadEnv();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("envTable").querySelector("tbody").innerHTML =
      `<tr><td colspan="5"><span class="badge bad">${esc(e.message)}</span></td></tr>`;
  }
}

// ── Incidents ─────────────────────────────────────────────────────────

$("incAdd").addEventListener("click", async () => {
  const title = $("incTitle").value.trim();
  const description = $("incDescription").value.trim();
  if (!title) return setMsg("incMsg", "title required", "err");
  setMsg("incMsg", '<span class="spin"></span> classifying…', "ok");
  try {
    const body = { title, description };
    if ($("incRef").value.trim()) body.source_ticket_id = $("incRef").value.trim();
    const r = await api("/admin/incidents", { method: "POST", body });
    $("incResult").style.display = "block";
    $("incResultBody").textContent = JSON.stringify(r, null, 2);
    setMsg("incMsg", "Incident classified & stored.");
    $("incTitle").value = $("incDescription").value = $("incRef").value = "";
  } catch (e) { setMsg("incMsg", e.message, "err"); }
});

// ── Groups ────────────────────────────────────────────────────────────

$("grpCreate").addEventListener("click", async () => {
  const name_ar = $("grpName").value.trim();
  if (!name_ar) return setMsg("grpMsg", "name_ar required", "err");
  const member_ids = $("grpMembers").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api("/admin/groups", { method: "POST", body: { name_ar, description: $("grpDesc").value, member_ids } });
    setMsg("grpMsg", "Group created.");
    $("grpName").value = $("grpDesc").value = $("grpMembers").value = "";
    loadGroups();
  } catch (e) { setMsg("grpMsg", e.message, "err"); }
});

async function loadGroups() {
  try {
    const d = await api("/admin/groups");
    if (!d.groups.length) { $("groupsBody").innerHTML = '<p class="hint">No groups yet.</p>'; return; }
    const rows = d.groups.map((g) => `
      <tr>
        <td class="mono">${esc(g.cluster_id)}</td>
        <td>${esc(g.name_ar)}</td>
        <td>${esc(g.description)}</td>
        <td>${esc(g.status)}</td>
        <td>${g.member_count}</td>
        <td class="mono" style="font-size:11px">${esc(g.members.map((m) => m.title).slice(0, 3).join("; "))}${g.member_count > 3 ? " …" : ""}</td>
      </tr>`);
    $("groupsBody").innerHTML =
      `<table><thead><tr><th>ID</th><th>Name (ar)</th><th>Description</th><th>Status</th><th>Members</th><th>Sample titles</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
  } catch (e) {
    $("groupsBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

// ── Tests ─────────────────────────────────────────────────────────────

function runTest(kind) {
  const out = $("testOutput");
  out.style.display = "block";
  out.textContent = "running " + kind + "…\n";
  api("/admin/tests/" + kind, { method: "POST" })
    .then((r) => {
      out.textContent = (r.output || "") + "\n\n[exit code " + r.exit_code + "]" +
        (r.truncated ? "\n[output truncated]" : "");
    })
    .catch((e) => { out.textContent = "ERROR: " + e.message; });
}
$("runSmoke").addEventListener("click", () => runTest("smoke"));
$("runPytest").addEventListener("click", () => runTest("pytest"));

// ── Danger ────────────────────────────────────────────────────────────

$("resetDb").addEventListener("click", async () => {
  if (!confirm("⚠ DELETE ALL incidents, clusters, review queue and ingestion jobs? This cannot be undone.")) return;
  if (!confirm("Really sure? Type the word in your head: this wipes the database.")) return;
  try {
    const r = await api("/admin/reset", { method: "POST" });
    setMsg("resetMsg", `Reset complete — ${r.incidents_deleted} incidents deleted.`, "ok");
    loadAll();
  } catch (e) { setMsg("resetMsg", e.message, "err"); }
});
