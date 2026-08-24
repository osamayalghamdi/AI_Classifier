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
    startStatusAutoRefresh();
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
    startStatusAutoRefresh();
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
  loadStatus(); loadTaxonomy(); loadSystems(); loadEnv(); loadModels(); loadGroups(); loadAssGroups();
  loadAssGroupDropdown(); loadEndpoints();
}

// ── Status (auto-refresh every 15s) ───────────────────────────────────

let _statusTimer = null;

function startStatusAutoRefresh() {
  if (_statusTimer) clearInterval(_statusTimer);
  _statusTimer = setInterval(() => { if (TOKEN) loadStatus(); }, 15000);
}

$("statusRefresh").addEventListener("click", () => { if (TOKEN) loadStatus(); });

async function loadStatus() {
  if (!TOKEN) {
    $("statusBody").innerHTML = '<span class="hint">Enter your bearer token above and click Unlock.</span>';
    return;
  }
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
    $("statusUpdated").textContent = "updated " + new Date().toLocaleTimeString();
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

// ── Taxonomy JSON import / export ─────────────────────────────────────

$("txImportJson").addEventListener("click", async () => {
  const raw = $("txJson").value.trim();
  if (!raw) return setMsg("txJsonMsg", "paste JSON first", "err");
  let payload;
  try { payload = JSON.parse(raw); } catch (e) { return setMsg("txJsonMsg", "invalid JSON: " + e.message, "err"); }
  if (typeof payload !== "object" || Array.isArray(payload) || !Object.keys(payload).length) {
    return setMsg("txJsonMsg", 'expected an object like {"System": {"Service": ["Offering"]}}', "err");
  }
  try {
    const r = await api("/admin/taxonomy/import", { method: "POST", body: payload });
    setMsg("txJsonMsg", `Imported ${r.services_added} service(s), ${r.offerings_added} offering(s) — effective immediately.`);
    $("txJson").value = "";
    loadTaxonomy();
  } catch (e) { setMsg("txJsonMsg", e.message, "err"); }
});

$("txExportJson").addEventListener("click", async () => {
  try {
    const d = await api("/admin/taxonomy");
    const out = {};
    for (const sys of d.systems) {
      out[sys.system] = {};
      for (const svc of sys.services) {
        out[sys.system][svc.service] = svc.offerings || [];
      }
    }
    $("txJson").value = JSON.stringify(out, null, 2);
    setMsg("txJsonMsg", "Current effective taxonomy loaded into the box — edit and Import to add more.");
    navigator.clipboard && navigator.clipboard.writeText(JSON.stringify(out));
  } catch (e) { setMsg("txJsonMsg", e.message, "err"); }
});

// ── System activation (call-centre-covered systems) ───────────────────

async function loadSystems() {
  try {
    const d = await api("/admin/systems");
    const rows = d.systems.map((s) => `
      <tr>
        <td>${esc(s.system)}</td>
        <td>${s.active ? '<span class="badge ok">AI selects</span>' : '<span class="badge warn">deactivated (call centre)</span>'}</td>
        <td>${esc(s.note)}</td>
        <td>
          ${s.system === "Other"
            ? '<span class="hint">always active (fallback)</span>'
            : `<button class="small" data-sys-toggle="${esc(s.system)}" data-sys-active="${s.active}">${s.active ? "deactivate" : "activate"}</button>`}
        </td>
      </tr>`);
    $("systemsBody").innerHTML =
      `<table><thead><tr><th>System</th><th>Status</th><th>Note</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
    document.querySelectorAll("[data-sys-toggle]").forEach((b) => {
      b.addEventListener("click", async () => {
        const nextActive = b.dataset.sysActive !== "true";
        const note = nextActive ? "" : prompt(`Deactivate "${b.dataset.sysToggle}"?\nReason (e.g. covered by call centre):`) || "";
        try {
          await api("/admin/systems/" + encodeURIComponent(b.dataset.sysToggle), {
            method: "PATCH", body: { active: nextActive, note },
          });
          loadSystems(); loadTaxonomy();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("systemsBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

// ── Endpoints reference ───────────────────────────────────────────────

async function loadEndpoints() {
  try {
    const d = await api("/admin/endpoints");
    $("endpointCount").textContent = `${d.count} endpoints.`;
    const methodColor = (m) => ({
      GET: "#1E9E6A", POST: "#1E6FD9", PUT: "#F2900B", PATCH: "#6D4AFF", DELETE: "#D64545",
    }[m] || "#5E6E8C");
    const rows = d.endpoints.map((e) => `
      <tr>
        <td><span class="badge" style="background:${methodColor(e.method)}22;color:${methodColor(e.method)}">${e.method}</span></td>
        <td class="mono">${esc(e.path)}</td>
        <td>${esc(e.summary || "—")}</td>
        <td>${e.auth === "bearer" ? '<span class="badge warn">bearer</span>' : '<span class="badge ok">open</span>'}</td>
      </tr>`);
    $("endpointsTable").querySelector("tbody").innerHTML = rows.join("");
  } catch (e) {
    $("endpointsTable").querySelector("tbody").innerHTML =
      `<tr><td colspan="4"><span class="badge bad">${esc(e.message)}</span></td></tr>`;
  }
}

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

// ── Model registry (enable / disable) ─────────────────────────────────

async function loadModels() {
  try {
    const d = await api("/admin/models");
    const active = d.active || {};
    const rows = d.models.map((m) => {
      const isActive = active[m.role] === m.name;
      return `
      <tr>
        <td class="mono">${esc(m.name)}</td>
        <td>${esc(m.role)}</td>
        <td>${m.enabled ? '<span class="badge ok">enabled</span>' : '<span class="badge bad">disabled</span>'}${isActive ? ' <span class="badge ok">● active</span>' : ""}</td>
        <td class="mono">${esc(m.model_id)}</td>
        <td class="mono">${esc(m.api_base || "(inherit)")}</td>
        <td class="mono">${m.key_set ? esc(m.key_masked) : '<span class="hint">no key</span>'}</td>
        <td>${m.enabled
          ? `<button class="small" data-model-disable="${esc(m.name)}">disable</button>`
          : `<button class="small" data-model-enable="${esc(m.name)}">enable</button>`}</td>
      </tr>`;
    });
    const act = Object.entries(active).map(([r, n]) => n ? `${r}=${n}` : `${r}=<i>none</i>`).join(" · ");
    $("modelsBody").innerHTML =
      `<p class="hint">Active: ${act}</p>
       <table><thead><tr><th>Model</th><th>Role</th><th>State</th><th>Model id</th><th>API base</th><th>Key</th><th></th></tr></thead>
       <tbody>${rows.join("")}</tbody></table>`;
    document.querySelectorAll("[data-model-enable]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          const r = await api(`/admin/models/${b.dataset.modelEnable}/enable`, { method: "POST", body: {} });
          alert("Enabled. " + (r.note || ""));
          loadModels();
        } catch (e) { alert(e.message); }
      });
    });
    document.querySelectorAll("[data-model-disable]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          const r = await api(`/admin/models/${b.dataset.modelDisable}/disable`, { method: "POST", body: {} });
          alert("Disabled. " + (r.note || ""));
          loadModels();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("modelsBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

// ── Assignment groups (teams incidents are routed to) ─────────────────

async function loadAssGroupDropdown() {
  try {
    const d = await api("/admin/assignment-groups");
    const sel = $("incGroup");
    sel.innerHTML = '<option value="">— none —</option>' + d.groups
      .filter((g) => g.active)
      .map((g) => `<option value="${esc(g.name)}">${esc(g.name)}</option>`)
      .join("");
  } catch (_) { /* dropdown is best-effort; incident still works without it */ }
}

$("agAdd").addEventListener("click", async () => {
  const name = $("agName").value.trim();
  if (!name) return setMsg("agMsg", "name required", "err");
  try {
    await api("/admin/assignment-groups", { method: "POST", body: {
      name, description: $("agDesc").value.trim(), sort_order: parseInt($("agOrder").value || "10", 10),
    }});
    setMsg("agMsg", "Assignment group added.");
    $("agName").value = $("agDesc").value = "";
    loadAssGroups(); loadAssGroupDropdown();
  } catch (e) { setMsg("agMsg", e.message, "err"); }
});

async function loadAssGroups() {
  try {
    const d = await api("/admin/assignment-groups");
    if (!d.groups.length) { $("assGroupsBody").innerHTML = '<p class="hint">No groups yet — add one above.</p>'; return; }
    const rows = d.groups.map((g) => `
      <tr>
        <td class="mono">${g.id}</td>
        <td>${esc(g.name)}</td>
        <td>${esc(g.description)}</td>
        <td>${g.sort_order}</td>
        <td>${g.active ? '<span class="badge ok">active</span>' : '<span class="badge bad">inactive</span>'}</td>
        <td>
          <button class="small" data-ag-toggle="${g.id}" data-ag-active="${g.active}" data-ag-name="${esc(g.name)}">${g.active ? "deactivate" : "activate"}</button>
          <button class="small danger" data-ag-del="${g.id}" data-ag-name="${esc(g.name)}">delete</button>
        </td>
      </tr>`);
    $("assGroupsBody").innerHTML =
      `<table><thead><tr><th>ID</th><th>Name</th><th>Description</th><th>Order</th><th>Status</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
    document.querySelectorAll("[data-ag-toggle]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await api("/admin/assignment-groups/" + b.dataset.agToggle, { method: "PATCH", body: { active: b.dataset.agActive !== "true" } });
          loadAssGroups(); loadAssGroupDropdown();
        } catch (e) { alert(e.message); }
      });
    });
    document.querySelectorAll("[data-ag-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm(`Delete assignment group "${b.dataset.agName}"? Incidents already carrying this name keep it.`)) return;
        try {
          await api("/admin/assignment-groups/" + b.dataset.agDel, { method: "DELETE" });
          loadAssGroups(); loadAssGroupDropdown();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("assGroupsBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
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
    if ($("incGroup").value) body.assign_group = $("incGroup").value;
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
        <td><button class="small danger" data-del-group="${esc(g.cluster_id)}" data-del-name="${esc(g.name_ar)}">delete</button></td>
      </tr>`);
    $("groupsBody").innerHTML =
      `<table><thead><tr><th>ID</th><th>Name (ar)</th><th>Description</th><th>Status</th><th>Members</th><th>Sample titles</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
    document.querySelectorAll("[data-del-group]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm(`Delete group "${b.dataset.delName}" and its ${b.closest("tr").children[4].textContent} member(s)? Members return to the unassigned pool.`)) return;
        try {
          const r = await api("/admin/groups/" + b.dataset.delGroup, { method: "DELETE" });
          loadGroups();
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    $("groupsBody").innerHTML = `<span class="badge bad">${esc(e.message)}</span>`;
  }
}

// ── Tests ─────────────────────────────────────────────────────────────

function runTest(kind, btn) {
  const out = $("testOutput");
  out.style.display = "block";
  const orig = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> running…'; }
  out.textContent = "running " + (kind === "pytest" ? "full pytest suite (takes a few minutes)…" : "smoke_test.sh…") + "\n";
  api("/admin/tests/" + kind, { method: "POST" })
    .then((r) => {
      out.textContent = (r.output || "") + "\n\n[exit code " + r.exit_code + "]" +
        (r.truncated ? "\n[output truncated]" : "");
    })
    .catch((e) => { out.textContent = "ERROR: " + e.message; })
    .finally(() => { if (btn) { btn.disabled = false; btn.innerHTML = orig; } });
}
$("runSmoke").addEventListener("click", (e) => runTest("smoke", e.target));
$("runPytest").addEventListener("click", (e) => runTest("pytest", e.target));

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
