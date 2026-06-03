const API_BASE = window.location.origin;

// ── State ──────────────────────────────────────────────────────────────
let _nodeTypes = [];       // NodeTypeDef[]
let _selectedId = '';      // type_id of the currently selected node type
let _cachedModels = null;  // null = not yet fetched
let _cachedTools = null;   // null = not yet fetched; string[]

const QUALITY_OPTIONS = ['llm_eval', 'script', 'none'];

// ── Helpers ────────────────────────────────────────────────────────────
function _el(id) { return document.getElementById(id); }

function _esc(val) {
  const d = document.createElement('div');
  d.textContent = val == null ? '' : String(val);
  return d.innerHTML;
}

function _showStatus(text, isError = false) {
  const el = _el('nt-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--error,#ff6b6b)' : '';
  el.style.opacity = text ? '0.85' : '0.6';
}

function _newTypeId() {
  return `nt_${Math.random().toString(16).slice(2, 10)}`;
}

// ── Model loader ───────────────────────────────────────────────────────
async function _ensureModels() {
  if (_cachedModels !== null) return;
  _cachedModels = [];
  try {
    const res = await fetch(`${API_BASE}/api/models`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    const items = (data.items || []).filter(it => (it.model_type || 'llm') === 'llm' && !it.offline);
    _cachedModels = items.map(it => ({
      endpoint_name: it.endpoint_name || it.host || 'endpoint',
      models: [...(it.models || []), ...(it.models_extra || [])].filter(Boolean),
    })).filter(g => g.models.length > 0);
  } catch (e) {
    console.warn('nodeTypes: failed to load models', e);
  }
}

// ── Tool loader ────────────────────────────────────────────────────────
async function _ensureTools() {
  if (_cachedTools !== null) return;
  _cachedTools = [];
  try {
    const res = await fetch(`${API_BASE}/api/tools`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    _cachedTools = (data.tools || []).map(t => t.id || t).filter(Boolean).sort();
  } catch (e) {
    console.warn('nodeTypes: failed to load tools', e);
  }
}

function _populateToolsSelect(selectedTools = []) {
  const toolsEl = _el('nt-tools');
  if (!toolsEl) return;
  const toolSet = new Set(selectedTools);
  const tools = _cachedTools && _cachedTools.length ? _cachedTools : selectedTools;
  // Add any selected tools not in the fetched list (preserve saved data)
  const allTools = [...new Set([...tools, ...selectedTools])].sort();
  toolsEl.innerHTML = allTools.map(t =>
    `<option value="${_esc(t)}"${toolSet.has(t) ? ' selected' : ''}>${_esc(t)}</option>`
  ).join('');
}

function _buildModelOptionsHtml(selected) {
  const models = _cachedModels || [];
  let html = `<option value="">(session default)</option>`;
  for (const grp of models) {
    html += `<optgroup label="${_esc(grp.endpoint_name)}">`;
    for (const m of grp.models) {
      html += `<option value="${_esc(m)}"${m === selected ? ' selected' : ''}>${_esc(m)}</option>`;
    }
    html += `</optgroup>`;
  }
  const listed = models.flatMap(g => g.models);
  if (selected && !listed.includes(selected)) {
    html += `<option value="${_esc(selected)}" selected>${_esc(selected)}</option>`;
  }
  return html;
}

// ── List rendering ─────────────────────────────────────────────────────
function _setCount() {
  const n = _nodeTypes.length;
  const tab = _el('node-types-count');
  if (tab) tab.textContent = String(n);
  const h2 = _el('node-types-count-h2');
  if (h2) h2.textContent = `${n} node type${n !== 1 ? 's' : ''}`;
}

function _renderList() {
  const list = _el('node-types-list');
  if (!list) return;
  list.innerHTML = '';
  if (!_nodeTypes.length) {
    list.innerHTML = '<div class="memory-empty">No node types yet. Click New.</div>';
    return;
  }
  const sorted = [..._nodeTypes].sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
  for (const nt of sorted) {
    const row = document.createElement('div');
    row.className = 'memory-item';
    row.style.cursor = 'pointer';
    if ((_selectedId || '') === String(nt.type_id || '')) {
      row.style.borderColor = 'color-mix(in srgb, var(--red) 50%, var(--border))';
      row.style.background = 'color-mix(in srgb, var(--red) 10%, transparent)';
    }
    row.innerHTML = `
      <div class="memory-item-content">
        <span class="memory-item-text">${_esc(nt.name || nt.type_id)}</span>
        <div class="memory-item-meta">
          <span class="orchestrator-plan-node-type">${_esc(nt.classification || 'execute')}</span>

        </div>
      </div>
    `;
    row.addEventListener('click', () => {
      _selectedId = String(nt.type_id || '');
      _loadIntoEditor(nt);
      _renderList();
    });
    list.appendChild(row);
  }
}

// ── Editor ─────────────────────────────────────────────────────────────
function _loadIntoEditor(nt) {
  const f = nt || {};
  const classif = f.classification === 'branch' ? 'branch' : 'execute';

  const nameEl = _el('nt-name');
  const classifEl = _el('nt-classification');
  const modelEl = _el('nt-model');
  const qualTypeEl = _el('nt-quality-type');

  if (nameEl) nameEl.value = f.name || '';
  if (classifEl) classifEl.value = classif;
  if (modelEl) modelEl.innerHTML = _buildModelOptionsHtml(f.model || '');
  if (qualTypeEl) qualTypeEl.value = f.quality_check_type || 'llm_eval';

  // Populate and select tools
  _populateToolsSelect(Array.isArray(f.tools) ? f.tools : []);

  _applyClassificationUI(classif);
  _showStatus(f.name ? `Loaded: ${f.name}` : '');

  const delBtn = _el('nt-delete-btn');
  if (delBtn) delBtn.disabled = !f.type_id;
}

function _applyClassificationUI(classif) {
  const execSection = _el('nt-exec-section');
  const branchSection = _el('nt-branch-section');
  if (execSection) execSection.style.display = classif === 'execute' ? '' : 'none';
  if (branchSection) branchSection.style.display = classif === 'branch' ? '' : 'none';
}

function _emptyNodeType() {
  return { type_id: _newTypeId(), name: '', classification: 'execute', tools: [], model: '', quality_check_type: 'llm_eval' };
}

function _collectEditorPayload() {
  const typeId = _selectedId || _newTypeId();
  const name = (_el('nt-name')?.value || '').trim();
  if (!name) throw new Error('Name is required');

  const classif = _el('nt-classification')?.value || 'execute';
  const model = _el('nt-model')?.value || '';
  const qualType = _el('nt-quality-type')?.value || 'llm_eval';
  const toolsEl = _el('nt-tools');
  const tools = classif === 'execute'
    ? Array.from(toolsEl?.selectedOptions || []).map(o => o.value).filter(Boolean)
    : [];

  return { type_id: typeId, name, classification: classif, tools, model, quality_check_type: qualType };
}

// ── API ────────────────────────────────────────────────────────────────
async function _fetchNodeTypes() {
  const res = await fetch(`${API_BASE}/api/node-types`, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`Failed to load node types (${res.status})`);
  const data = await res.json();
  _nodeTypes = Array.isArray(data.node_types) ? data.node_types : [];
  _setCount();
  _renderList();
}

export async function loadNodeTypes() {
  await Promise.all([_fetchNodeTypes(), _ensureModels(), _ensureTools()]);
  if (_selectedId) {
    const match = _nodeTypes.find(n => n.type_id === _selectedId);
    if (match) { _loadIntoEditor(match); return; }
  }
  if (_nodeTypes.length > 0) {
    _selectedId = _nodeTypes[0].type_id || '';
    _loadIntoEditor(_nodeTypes[0]);
    _renderList();
  } else {
    _selectedId = '';
    _loadIntoEditor(_emptyNodeType());
  }
}

async function _saveNodeType() {
  try {
    const payload = _collectEditorPayload();
    const res = await fetch(`${API_BASE}/api/node-types/${encodeURIComponent(payload.type_id)}`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Save failed (${res.status})`);
    }
    _selectedId = payload.type_id;
    await loadNodeTypes();
    window.uiModule?.showToast?.('Node type saved');
  } catch (e) {
    _showStatus(e?.message || 'Save failed', true);
  }
}

async function _deleteNodeType() {
  if (!_selectedId) return;
  const nt = _nodeTypes.find(n => n.type_id === _selectedId);
  if (!nt) return;
  const ok = await window.uiModule?.styledConfirm?.(`Delete node type "${nt.name || _selectedId}"?`, { confirmText: 'Delete', danger: true });
  if (!ok) return;
  const res = await fetch(`${API_BASE}/api/node-types/${encodeURIComponent(_selectedId)}`, {
    method: 'DELETE', credentials: 'same-origin',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    _showStatus(err.detail || 'Delete failed', true);
    return;
  }
  _selectedId = '';
  await loadNodeTypes();
  _loadIntoEditor(_emptyNodeType());
  window.uiModule?.showToast?.('Node type deleted');
}

function _newNodeType() {
  _selectedId = '';
  const e = _emptyNodeType();
  _loadIntoEditor(e);
  _renderList();
  _el('nt-name')?.focus();
  _showStatus('Fill in details and click Save');
}

// ── Init ───────────────────────────────────────────────────────────────
export function initNodeTypesTab() {
  const saveBtn = _el('nt-save-btn');
  const delBtn = _el('nt-delete-btn');
  const newBtn = _el('nt-new-btn');
  const classifEl = _el('nt-classification');

  if (saveBtn && !saveBtn.dataset.bound) {
    saveBtn.dataset.bound = '1';
    saveBtn.addEventListener('click', _saveNodeType);
  }
  if (delBtn && !delBtn.dataset.bound) {
    delBtn.dataset.bound = '1';
    delBtn.addEventListener('click', _deleteNodeType);
  }
  if (newBtn && !newBtn.dataset.bound) {
    newBtn.dataset.bound = '1';
    newBtn.addEventListener('click', _newNodeType);
  }
  if (classifEl && !classifEl.dataset.bound) {
    classifEl.dataset.bound = '1';
    classifEl.addEventListener('change', () => {
      _applyClassificationUI(classifEl.value);
    });
  }
  // Mark dirty on any input change
  ['nt-name', 'nt-classification', 'nt-model', 'nt-quality-type', 'nt-tools'].forEach(id => {
    const el = _el(id);
    if (el && !el.dataset.ntDirtyBound) {
      el.dataset.ntDirtyBound = '1';
      el.addEventListener('change', () => _showStatus('Unsaved changes'));
      el.addEventListener('input', () => _showStatus('Unsaved changes'));
    }
  });
}

// ── Export for use by blueprints.js ───────────────────────────────────
export function getNodeTypes() { return _nodeTypes; }

export default { initNodeTypesTab, loadNodeTypes, getNodeTypes };
