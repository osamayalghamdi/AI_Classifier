const API = window.location.origin + '/api';

// ── Helpers ──
const esc = s => typeof s === 'string'
  ? document.createElement('div').appendChild(document.createTextNode(s)).parentNode.innerHTML
  : s;

function byId(id) { return document.getElementById(id); }
function qa(sel) { return document.querySelectorAll(sel); }

// ── Health ──
async function checkHealth() {
  const dot = byId('healthDot');
  const text = byId('healthText');
  try {
    const d = await (await fetch(API + '/health')).json();
    dot.className = 'health-dot ok';
    text.textContent = 'OK · ' + d.model;
  } catch {
    dot.className = 'health-dot err';
    text.textContent = 'API unreachable';
  }
}

// ── Classify ──
async function classify() {
  const title = byId('title').value.trim();
  const desc = byId('description').value.trim();
  if (!title) { showError('Please enter an incident title.'); return; }
  if (title.length > 300) { showError('Title must be under 300 characters.'); return; }
  if (desc.length > 8000) { showError('Description must be under 8,000 characters.'); return; }

  const btn = byId('classifyBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Classifying…';

  try {
    const r = await fetch(API + '/classify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, description: desc, extracted_text: byId('extractedText').value }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'HTTP ' + r.status);
    const data = await r.json();
    renderResult(data);
    addToHistory(data);
    loadHistory();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg> Classify';
  }
}

function renderResult(data) {
  const c = data.classification;
  const area = byId('resultArea');
  const sevClass = 'type-severity-' + c.severity.replace(/ /g, '');
  const confClass = 'tag-confidence-' + c.confidence;
  const isFallback = c.confidence === 'low' && c.reasoning && c.reasoning.startsWith('Classification failed');

  const fallbackBanner = isFallback ? '<div class="result-fallback fade-in">⚠ Classification failed — using fallback values.</div>' : '';
  const reasoning = c.reasoning && !isFallback ? '<div class="reasoning-box"><strong>Reasoning</strong><br>' + esc(c.reasoning) + '</div>' : '';

  let relatedHTML = '';
  if (data.related_incidents && data.related_incidents.length > 0) {
    relatedHTML = '<div class="related-section"><h3>Related Incidents (' + data.related_incidents.length + ')</h3>';
    for (const rel of data.related_incidents) {
      relatedHTML +=
        '<div class="related-item fade-in">' +
          '<div class="related-item-header">' +
            '<span class="related-item-title">' + esc(rel.title) + '</span>' +
            '<span class="related-item-score">' + (rel.similarity * 100).toFixed(0) + '%</span>' +
          '</div>' +
          '<div class="related-item-tags">' +
            '<span class="related-item-tag">' + esc(rel.classification.affected_system) + '</span>' +
            '<span class="related-item-tag">' + esc(rel.classification.severity) + '</span>' +
            '<span class="related-item-tag">' + esc(rel.classification.incident_type) + '</span>' +
          '</div>' +
          (rel.reasoning ? '<div class="related-item-reasoning">' + esc(rel.reasoning) + '</div>' : '') +
        '</div>';
    }
    relatedHTML += '</div>';
  }

  area.innerHTML =
    '<div class="fade-in">' +
      fallbackBanner +
      '<div class="incident-id" style="margin-bottom:12px;">ID: ' + esc(data.incident_id || '—') + '</div>' +
      '<div class="tags-grid">' +
        '<div class="tag-item"><span class="tag-label">System</span><span class="tag-value">' + esc(c.affected_system) + '</span></div>' +
        '<div class="tag-item"><span class="tag-label">Service</span><span class="tag-value">' + esc(c.service) + '</span></div>' +
        '<div class="tag-item"><span class="tag-label">Type</span><span class="tag-value" style="color:var(--blue);background:var(--blue-bg)">' + esc(c.incident_type) + '</span></div>' +
        '<div class="tag-item"><span class="tag-label">Severity</span><span class="tag-value ' + sevClass + '">' + esc(c.severity) + '</span></div>' +
        '<div class="tag-item"><span class="tag-label">Urgency</span><span class="tag-value ' + confClass + '">' + esc(c.urgency) + '</span></div>' +
        '<div class="tag-item"><span class="tag-label">Category</span><span class="tag-value">' + esc(c.category) + '</span></div>' +
        '<div class="tag-item" style="grid-column:span 2"><span class="tag-label">Confidence</span><span class="tag-value ' + confClass + '">' + esc(c.confidence) + '</span></div>' +
      '</div>' +
      reasoning +
      relatedHTML +
    '</div>';
}

function showError(msg) {
  byId('resultArea').innerHTML = '<div class="result-error fade-in">' + esc(msg) + '</div>';
}

// ── History ──
function loadHistory() {
  const stored = localStorage.getItem('incident_history');
  const items = stored ? JSON.parse(stored) : [];
  const area = byId('historyArea');
  if (items.length === 0) {
    area.innerHTML = '<div class="history-empty fade-in">No incidents classified yet.</div>';
    return;
  }
  area.innerHTML = items.slice().reverse().map(item =>
    '<div class="history-item fade-in">' +
      '<div class="history-item-title">' + esc(item.title) + '</div>' +
      '<div class="history-item-meta">' +
        '<span class="incident-id">' + esc(item.id || '—') + '</span>' +
        '<div class="history-item-types">' +
          '<span class="type-pill type-severity-' + item.severity.replace(/ /g, '') + '">' + esc(item.severity) + '</span>' +
          '<span class="type-pill" style="background:var(--blue-bg);color:var(--blue)">' + esc(item.type) + '</span>' +
        '</div>' +
        '<span class="history-item-time">' + new Date(item.timestamp).toLocaleTimeString() + '</span>' +
      '</div>' +
    '</div>'
  ).join('');
}

function addToHistory(data) {
  const stored = localStorage.getItem('incident_history');
  const history = stored ? JSON.parse(stored) : [];
  history.push({
    id: data.incident_id, title: data.incident_title,
    severity: data.classification.severity, type: data.classification.incident_type,
    system: data.classification.affected_system, timestamp: new Date().toISOString(),
  });
  localStorage.setItem('incident_history', JSON.stringify(history));
}

// ── Reports ──
async function loadReport(period) {
  const area = byId('reportArea');
  area.innerHTML = '<div class="history-loading">Loading report…</div>';
  try {
    renderReport(await (await fetch(API + '/reports/' + period)).json());
  } catch (e) {
    area.innerHTML = '<div class="result-error fade-in">Failed to load report: ' + esc(e.message) + '</div>';
  }
}

function renderReport(data) {
  const area = byId('reportArea');
  byId('reportTotal').textContent = data.total_incidents + ' incident' + (data.total_incidents !== 1 ? 's' : '');
  if (data.clusters.length === 0) {
    area.innerHTML = '<div class="report-empty fade-in">No incidents in this period.</div>';
    return;
  }
  area.innerHTML = data.clusters.map(cl => {
    const badgeClass = 'report-badge-' + cl.worst_severity.toLowerCase();
    const cid = esc(cl.cluster_id);
    const incidents = cl.incidents.map(i =>
      '<div class="report-incident-item">' +
        '<span class="report-incident-id incident-id">' + esc(i.id) + '</span>' +
        '<span class="report-incident-title">' + esc(i.title) + '</span>' +
        '<span class="report-incident-sev type-pill type-severity-' + i.severity.replace(/ /g, '') + '">' + esc(i.severity) + '</span>' +
      '</div>'
    ).join('');
    return '<div class="report-cluster fade-in">' +
      '<div class="report-cluster-header" onclick="window.toggleIncidents(\'' + cid + '\')">' +
        '<span class="report-cluster-title">' + esc(cl.affected_system) + ' / ' + esc(cl.affected_service) + '</span>' +
        '<div class="report-cluster-badges">' +
          '<span class="report-badge ' + badgeClass + '">' + esc(cl.worst_severity) + '</span>' +
          '<span class="report-badge" style="background:var(--bg-input);color:var(--text-muted)">' + cl.count + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="report-summary">' + esc(cl.summary) + '</div>' +
      '<span class="report-expand" onclick="window.toggleIncidents(\'' + cid + '\')">Show details ▾</span>' +
      '<div class="report-incidents" id="report-incidents-' + cid + '">' + incidents + '</div>' +
    '</div>';
  }).join('');
}

window.toggleIncidents = function(clusterId) {
  const el = byId('report-incidents-' + clusterId);
  el.classList.toggle('open');
  el.previousElementSibling.textContent = el.classList.contains('open') ? 'Hide details ▴' : 'Show details ▾';
};

// ── Tabs ──
function switchTab(name) {
  qa('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  qa('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + name));
}

// ── Init ──
byId('classifyBtn').addEventListener('click', classify);
byId('clearBtn').addEventListener('click', () => {
  byId('title').value = '';
  byId('description').value = '';
  byId('resultArea').innerHTML =
    '<div class="result-empty fade-in">' +
      '<div class="result-empty-icon">🔍</div>' +
      '<div>Submit an incident to see the classification result here.</div>' +
    '</div>';
});
byId('refreshHistory').addEventListener('click', loadHistory);

// ── OCR Drop zone ──
const dropZone = byId('dropZone');
const fileInput = byId('fileInput');

dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

async function handleFile(file) {
  const status = byId('ocrStatus');
  status.hidden = false;
  status.className = 'ocr-status loading';
  status.textContent = 'Running OCR on ' + file.name + '…';

  try {
    const form = new FormData();
    form.append('file', file);
    const r = await fetch('/api/ocr', { method: 'POST', body: form });
    if (!r.ok) throw new Error('OCR failed: ' + r.status);
    const data = await r.json();
    const text = (data.text || '').trim();
    if (!text) throw new Error('No text found in file');

    // Fill the Extracted Text field
    byId('extractedText').value = text;
    byId('extractedTextGroup').style.display = 'block';

    // Show confidence warning if any words are uncertain
    let warning = '';
    if (data.has_low_confidence && data.low_confidence_words) {
      warning = ' ⚠ ' + data.low_confidence_words.length + ' word(s) may be incorrect (low confidence)';
    }

    status.className = 'ocr-status done';
    status.textContent = 'OCR complete — ' + text.split('\n').filter(l => l.trim()).length + ' lines extracted.' + warning;
  } catch (e) {
    status.className = 'ocr-status error';
    status.textContent = 'OCR error: ' + e.message;
  }
}

// ── Presets ──
qa('.preset-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    byId('title').value = chip.dataset.title;
    byId('description').value = chip.dataset.desc;
  });
});
qa('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
qa('.report-period-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    qa('.report-period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    loadReport(btn.dataset.period);
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); classify(); }
});

checkHealth();
loadHistory();
loadReport('daily');
