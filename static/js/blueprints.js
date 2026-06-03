const API_BASE = window.location.origin;

let _blueprints = [];
let _selectedName = '';
let _stepsModalDraft = null;
let _cachedNodeTypes = [];  // from nodeTypes.js
let _cachedTools = null;    // null = not yet fetched; string[]

async function _fetchToolPool() {
  if (_cachedTools !== null) return;
  _cachedTools = [];
  try {
    const res = await fetch(`${API_BASE}/api/tools`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    _cachedTools = (data.tools || []).map(t => t.id || t).filter(Boolean).sort();
  } catch (e) {
    console.warn('blueprints: failed to load tools', e);
  }
}

const NODE_TYPE_OPTIONS = ['execute', 'reflect', 'format', 'branch'];
const QUALITY_OPTIONS = ['llm_eval', 'script', 'none'];
const EDGE_CONDITION_OPTIONS = ['default', 'on_pass', 'on_fail'];

async function _fetchNodeTypePool() {
  try {
    const res = await fetch(`${API_BASE}/api/node-types`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    _cachedNodeTypes = Array.isArray(data.node_types) ? data.node_types : [];
  } catch (e) {
    console.warn('blueprints: failed to load node types', e);
  }
}

let _cachedModels = []; // [{ endpoint_name: string, models: string[] }]

async function _fetchModels() {
  if (_cachedModels.length > 0) return;
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
    console.warn('Blueprint editor: failed to load models', e);
  }
}

function _buildModelOptionsHtml(selected) {
  let html = `<option value="">(session default)</option>`;
  for (const grp of _cachedModels) {
    html += `<optgroup label="${_esc(grp.endpoint_name)}">` ;
    for (const m of grp.models) {
      html += `<option value="${_esc(m)}"${m === selected ? ' selected' : ''}>${_esc(m)}</option>`;
    }
    html += `</optgroup>`;
  }
  const listed = _cachedModels.flatMap(g => g.models);
  if (selected && !listed.includes(selected)) {
    html += `<option value="${_esc(selected)}" selected>${_esc(selected)}</option>`;
  }
  return html;
}

function _el(id) {
  return document.getElementById(id);
}

function _showStatus(text, isError = false) {
  const el = _el('blueprint-editor-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.opacity = text ? '0.85' : '0.6';
  el.style.color = isError ? 'var(--error,#ff6b6b)' : '';
}

function _parseNodesJson() {
  const raw = (_el('blueprint-steps-json')?.value || '').trim();
  let parsed;
  try {
    parsed = JSON.parse(raw || '[]');
  } catch (e) {
    throw new Error(`Nodes JSON parse failed: ${e.message || e}`);
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('Nodes must be a non-empty JSON array');
  }
  for (let i = 0; i < parsed.length; i++) {
    const s = parsed[i] || {};
    if (typeof s !== 'object' || Array.isArray(s)) {
      throw new Error(`Node ${i + 1} must be an object`);
    }
    if (!String(s.title || '').trim()) {
      throw new Error(`Node ${i + 1} is missing title`);
    }
    if (!String(s.description || '').trim()) {
      throw new Error(`Node ${i + 1} is missing description`);
    }
    if (s.tools != null && !Array.isArray(s.tools)) {
      throw new Error(`Node ${i + 1} tools must be an array`);
    }
  }
  return parsed;
}

function _parseEdgesJson() {
  const raw = (_el('blueprint-edges-json')?.value || '').trim();
  if (!raw || raw === '[]') return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error('must be an array');
    return parsed;
  } catch (e) {
    throw new Error(`Edges JSON parse failed: ${e.message || e}`);
  }
}

function _modalStatus(text, isError = false) {
  const el = _el('blueprint-steps-modal-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--error,#ff6b6b)' : '';
  el.style.opacity = text ? '0.85' : '0.6';
}

function _setCounts() {
  const total = _blueprints.length;
  const tab = _el('blueprints-count');
  const h2 = _el('blueprints-count-h2');
  if (tab) tab.textContent = String(total);
  if (h2) h2.textContent = `${total} ${total === 1 ? 'blueprint' : 'blueprints'}`;
}

function _esc(value) {
  const div = document.createElement('div');
  div.textContent = value == null ? '' : String(value);
  return div.innerHTML;
}

function _normalizeName(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

function _newNodeId() {
  return `node_${Math.random().toString(16).slice(2, 10)}`;
}

function _normalizeNode(raw, index = 0, edges = []) {
  const source = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
  const qcSource = (source.quality_check && typeof source.quality_check === 'object' && !Array.isArray(source.quality_check))
    ? source.quality_check
    : {};
  const qcType = QUALITY_OPTIONS.includes(String(qcSource.type || '').trim()) ? String(qcSource.type).trim() : 'llm_eval';
  const nodeType = NODE_TYPE_OPTIONS.includes(String(source.node_type || '').trim()) ? String(source.node_type).trim() : 'execute';
  const nodeId = String(source.node_id || _newNodeId()).trim() || _newNodeId();
  const isBranch = nodeType === 'branch';
  // Derive on_fail routing from edge list
  let onFailTarget = '';
  let maxTraversals = 3;
  if (isBranch && edges.length > 0) {
    const onFailEdge = edges.find(e => e.from_node === nodeId && e.condition === 'on_fail');
    if (onFailEdge) {
      onFailTarget = String(onFailEdge.to_node || '');
      maxTraversals = Number(onFailEdge.max_traversals ?? 3);
    }
  }
  return {
    node_id: nodeId,
    node_type_ref: String(source.node_type_ref || '').trim() || null,
    node_type: nodeType,
    title: String(source.title || `Node ${index + 1}`).trim(),
    description: String(source.description || '').trim(),
    tools: Array.isArray(source.tools) ? source.tools.map((x) => String(x || '').trim()).filter(Boolean) : [],
    model: String(source.model || '').trim(),
    quality_check: { type: qcType, criteria: String(qcSource.criteria || '').trim() },
    _onFailTarget: onFailTarget,
    _maxTraversals: maxTraversals,
  };
}

function _nodesSummaryHtml() {
  let parsed = [];
  try {
    parsed = _parseNodesJson();
  } catch {
    return '<div class="blueprint-steps-summary-empty">Nodes JSON is invalid. Open Edit Nodes to repair.</div>';
  }
  if (!parsed.length) {
    return '<div class="blueprint-steps-summary-empty">No nodes configured yet.</div>';
  }
  const lines = parsed.map((s, i) => {
    const title = String(s?.title || `Node ${i + 1}`).trim();
    const type = String(s?.node_type || 'execute').trim();
    return `<li>${_esc(title)} <span class="orchestrator-plan-node-type">${_esc(type)}</span></li>`;
  }).join('');
  return `<div><strong>${parsed.length}</strong> node(s)</div><ol class="blueprint-steps-summary-list">${lines}</ol>`;
}

function _renderStepsSummary() {
  const box = _el('blueprint-steps-summary');
  if (!box) return;
  box.innerHTML = _nodesSummaryHtml();
}

function _buildNodeTypePickerOptions(selectedRef) {
  if (!_cachedNodeTypes.length) {
    return `<option value="">(No node types defined — create some in the Node Types tab)</option>`;
  }
  let html = `<option value="">(pick a node type)</option>`;
  const execTypes = _cachedNodeTypes.filter(n => n.classification === 'execute');
  const branchTypes = _cachedNodeTypes.filter(n => n.classification === 'branch');
  if (execTypes.length) {
    html += `<optgroup label="Execution">`;
    for (const nt of execTypes) {
      html += `<option value="${_esc(nt.type_id)}"${nt.type_id === selectedRef ? ' selected' : ''}>${_esc(nt.name)} (${_esc(nt.classification || 'execute')})</option>`;
    }
    html += `</optgroup>`;
  }
  if (branchTypes.length) {
    html += `<optgroup label="Branching">`;
    for (const nt of branchTypes) {
      html += `<option value="${_esc(nt.type_id)}"${nt.type_id === selectedRef ? ' selected' : ''}>${_esc(nt.name)}</option>`;
    }
    html += `</optgroup>`;
  }
  return html;
}

function _buildNodeCard(step, index, allNodesDraft = []) {
  const isBranch = step.node_type === 'branch';
  const branchHide = isBranch ? '' : 'display:none';

  // Node type badge info
  const nt = _cachedNodeTypes.find(n => n.type_id === step.node_type_ref) || null;
  const ntBadge = nt
    ? `<span class="orchestrator-plan-node-type" style="font-size:10px;margin-left:6px;">${_esc(nt.classification || 'execute')}</span>`
    : (step.node_type_ref ? `<span class="orchestrator-plan-node-type" style="font-size:10px;margin-left:6px;opacity:.5;">unknown type</span>` : `<span style="font-size:10px;opacity:.4;margin-left:6px;">no type selected</span>`);

  const onFailOpts = allNodesDraft
    .filter(n => n.node_id !== step.node_id)
    .map(n => {
      const sel = n.node_id === step._onFailTarget ? ' selected' : '';
      return `<option value="${_esc(n.node_id)}"${sel}>${_esc(n.title || n.node_id)}</option>`;
    }).join('');

  return `
    <div class="blueprint-step-card" data-step-index="${index}">
      <div class="blueprint-step-card-header">
        <span class="blueprint-step-card-title">Node ${index + 1}</span>
        <span class="blueprint-step-card-chip">${_esc(step.node_id || '')}</span>
        ${ntBadge}
        <div class="blueprint-step-card-actions">
          <button class="memory-toolbar-btn" type="button" data-action="move-up">Up</button>
          <button class="memory-toolbar-btn" type="button" data-action="move-down">Down</button>
          <button class="memory-toolbar-btn danger" type="button" data-action="delete">Remove</button>
        </div>
      </div>
      <div class="blueprint-step-grid" style="padding-bottom:8px;border-bottom:1px solid color-mix(in srgb,var(--border) 45%,transparent);">
        <label class="blueprint-step-label full">
          <span>Node type (from library)</span>
          <select class="blueprint-step-select" data-field="node_type_ref">
            ${_buildNodeTypePickerOptions(step.node_type_ref)}
          </select>
        </label>
      </div>
      <div class="blueprint-step-grid" style="padding-top:8px;">
        <label class="blueprint-step-label full">
          <span>Title</span>
          <input class="blueprint-step-input" data-field="title" type="text" value="${_esc(step.title || '')}" />
        </label>
        <label class="blueprint-step-label full">
          <span>Description</span>
          <textarea class="blueprint-step-textarea" data-field="description" rows="3">${_esc(step.description || '')}</textarea>
        </label>
        <label class="blueprint-step-label full bp-branch-only" style="${branchHide}">
          <span>Quality criteria (PASS / FAIL conditions)</span>
          <textarea class="blueprint-step-textarea" data-field="quality_criteria" rows="2">${_esc(step?.quality_check?.criteria || '')}</textarea>
        </label>
        <label class="blueprint-step-label bp-branch-only" style="${branchHide}">
          <span>On fail → route to</span>
          <select class="blueprint-step-select" data-field="on_fail_target">
            <option value="">(none / stop)</option>
            ${onFailOpts}
          </select>
        </label>
        <label class="blueprint-step-label bp-branch-only" style="${branchHide}">
          <span>Max traversals (0 = unlimited)</span>
          <input class="blueprint-step-input" data-field="max_traversals" type="number" min="0" max="20" value="${step._maxTraversals ?? 3}" />
        </label>
      </div>
    </div>
  `;
}

function _renderStepsCards() {
  const wrap = _el('blueprint-steps-cards');
  if (!wrap) return;
  const list = Array.isArray(_stepsModalDraft) ? _stepsModalDraft : [];
  const noTypesHint = !_cachedNodeTypes.length
    ? `<div style="font-size:12px;opacity:.6;margin-bottom:8px;padding:6px 10px;background:color-mix(in srgb,var(--accent-warning,#f59e0b) 12%,transparent);border-radius:6px;">No node types defined yet — go to the <strong>Node Types</strong> tab to create some first.</div>`
    : '';
  if (!list.length) {
    wrap.innerHTML = noTypesHint + '<div class="memory-empty">No nodes yet. Click Add Node.</div>';
    _modalStatus('No nodes configured yet');
    return;
  }
  wrap.innerHTML = noTypesHint + list.map((step, idx) => _buildNodeCard(step, idx, list)).join('');
  _modalStatus(`${list.length} node(s) loaded`);
}

async function _openStepsModal() {
  const modal = _el('blueprint-steps-modal');
  if (!modal) return;
  let parsed;
  try {
    parsed = _parseNodesJson();
  } catch (e) {
    _showStatus(e?.message || 'Invalid nodes JSON', true);
    return;
  }
  let edges = [];
  try { edges = _parseEdgesJson(); } catch { /* proceed with no edges */ }
  await _fetchNodeTypePool();
  _stepsModalDraft = parsed.map((s, i) => _normalizeNode(s, i, edges));
  modal.classList.remove('hidden');
  _renderStepsCards();
}

function _closeStepsModal() {
  const modal = _el('blueprint-steps-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  _stepsModalDraft = null;
  _modalStatus('');
}

function _selectedValues(selectEl) {
  if (!selectEl) return [];
  return Array.from(selectEl.selectedOptions || [])
    .map((o) => String(o.value || '').trim())
    .filter(Boolean);
}

function _updateToolsFromCard(card, step) {
  const select = card.querySelector('[data-field="tools_select"]');
  step.tools = _selectedValues(select);
}

function _parseModalNodesForSave() {
  const out = [];
  const steps = Array.isArray(_stepsModalDraft) ? _stepsModalDraft : [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i] || {};
    const title = String(step.title || '').trim();
    const description = String(step.description || '').trim();
    if (!title) throw new Error(`Node ${i + 1} is missing title`);
    if (!description) throw new Error(`Node ${i + 1} is missing description`);
    if (!step.node_type_ref) throw new Error(`Node ${i + 1} has no node type selected`);

    // Resolve tools/model/node_type from the node type definition
    const nt = _cachedNodeTypes.find(n => n.type_id === step.node_type_ref) || null;
    const nodeType = nt ? (nt.classification === 'branch' ? 'branch' : 'execute') : step.node_type || 'execute';
    const tools = nt ? (Array.isArray(nt.tools) ? nt.tools : []) : (step.tools || []);
    const model = nt ? (nt.model || '') : (step.model || '');
    const qualType = nt ? (nt.quality_check_type || 'llm_eval') : (step?.quality_check?.type || 'llm_eval');

    out.push({
      node_id: String(step.node_id || _newNodeId()).trim() || _newNodeId(),
      node_type_ref: step.node_type_ref,
      node_type: nodeType,
      title,
      description,
      tools,
      model,
      quality_check: {
        type: qualType,
        criteria: String(step?.quality_check?.criteria || '').trim(),
      },
    });
  }
  if (!out.length) throw new Error('At least one node is required');
  return out;
}

function _onStepsCardsInput(event) {
  const target = event.target;
  const card = target?.closest?.('.blueprint-step-card');
  if (!card || !Array.isArray(_stepsModalDraft)) return;
  const idx = Number(card.dataset.stepIndex || '-1');
  if (!Number.isInteger(idx) || idx < 0 || idx >= _stepsModalDraft.length) return;
  const step = _stepsModalDraft[idx];
  const field = target.dataset.field;
  if (!field) return;

  if (field === 'node_type_ref') {
    const nt = _cachedNodeTypes.find(n => n.type_id === target.value) || null;
    step.node_type_ref = target.value || null;
    step.node_type = nt ? (nt.classification === 'branch' ? 'branch' : 'execute') : step.node_type;
    // Show/hide branch-specific fields on this card
    const isBranch = step.node_type === 'branch';
    card.querySelectorAll('.bp-branch-only').forEach(el => { el.style.display = isBranch ? '' : 'none'; });
    // Update header badge
    const badgeEl = card.querySelector('.nt-badge');
    if (badgeEl && nt) {
      badgeEl.textContent = nt.classification;
    }
  } else if (field === 'title') {
    step.title = target.value;
  } else if (field === 'description') {
    step.description = target.value;
  } else if (field === 'quality_criteria') {
    step.quality_check = step.quality_check || {};
    step.quality_check.criteria = target.value;
  } else if (field === 'on_fail_target') {
    step._onFailTarget = target.value;
  } else if (field === 'max_traversals') {
    step._maxTraversals = Math.max(0, Number(target.value) || 0);
  }
  _modalStatus('Unsaved node changes');
}

function _onStepsCardsClick(event) {
  const btn = event.target?.closest?.('button[data-action]');
  if (!btn || !Array.isArray(_stepsModalDraft)) return;
  const card = btn.closest('.blueprint-step-card');
  if (!card) return;
  const idx = Number(card.dataset.stepIndex || '-1');
  if (!Number.isInteger(idx) || idx < 0 || idx >= _stepsModalDraft.length) return;
  const action = btn.dataset.action;

  if (action === 'delete') {
    _stepsModalDraft.splice(idx, 1);
  } else if (action === 'move-up' && idx > 0) {
    const tmp = _stepsModalDraft[idx - 1];
    _stepsModalDraft[idx - 1] = _stepsModalDraft[idx];
    _stepsModalDraft[idx] = tmp;
  } else if (action === 'move-down' && idx < _stepsModalDraft.length - 1) {
    const tmp = _stepsModalDraft[idx + 1];
    _stepsModalDraft[idx + 1] = _stepsModalDraft[idx];
    _stepsModalDraft[idx] = tmp;
  }
  _renderStepsCards();
  _modalStatus('Unsaved node changes');
}

function _addNodeCard() {
  if (!Array.isArray(_stepsModalDraft)) _stepsModalDraft = [];
  _stepsModalDraft.push(_normalizeNode({
    node_id: _newNodeId(),
    node_type_ref: null,
    node_type: 'execute',
    title: `Node ${_stepsModalDraft.length + 1}`,
    description: '',
    tools: [],
    model: '',
    quality_check: { type: 'llm_eval', criteria: '' },
  }, _stepsModalDraft.length));
  _renderStepsCards();
  _modalStatus('Added node. Fill required fields before saving.');
}

function _mergeBranchEdges(existingEdges, draftNodes) {
  const branchIds = new Set(draftNodes.filter(n => n.node_type === 'branch').map(n => n.node_id));
  const kept = existingEdges.filter(e => !(branchIds.has(e.from_node) && e.condition === 'on_fail'));
  for (const node of draftNodes) {
    if (node.node_type === 'branch' && node._onFailTarget) {
      kept.push({
        from_node: node.node_id,
        to_node: node._onFailTarget,
        condition: 'on_fail',
        max_traversals: Number(node._maxTraversals) || 3,
      });
    }
  }
  return kept;
}

function _saveNodesModal() {
  try {
    const nodes = _parseModalNodesForSave();
    const ta = _el('blueprint-steps-json');
    if (ta) ta.value = JSON.stringify(nodes, null, 2);
    let existingEdges = [];
    try { existingEdges = _parseEdgesJson(); } catch { /* start fresh */ }
    const mergedEdges = _mergeBranchEdges(existingEdges, Array.isArray(_stepsModalDraft) ? _stepsModalDraft : []);
    const edgesTa = _el('blueprint-edges-json');
    if (edgesTa) edgesTa.value = JSON.stringify(mergedEdges, null, 2);
    _renderStepsSummary();
    _showStatus(`Updated ${nodes.length} node(s) from card editor`);
    _closeStepsModal();
  } catch (e) {
    _modalStatus(e?.message || 'Unable to save nodes', true);
  }
}

function _renderList() {
  const list = _el('blueprints-list');
  if (!list) return;
  list.innerHTML = '';

  const sorted = [..._blueprints].sort((a, b) => {
    const sa = a.source === 'builtin' ? 0 : 1;
    const sb = b.source === 'builtin' ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return String(a.name || '').localeCompare(String(b.name || ''));
  });

  if (!sorted.length) {
    list.innerHTML = '<div class="memory-empty">No blueprints yet</div>';
    return;
  }

  for (const bp of sorted) {
    const row = document.createElement('div');
    row.className = 'memory-item';
    row.style.cursor = 'pointer';
    if ((_selectedName || '').toLowerCase() === String(bp.name || '').toLowerCase()) {
      row.style.borderColor = 'color-mix(in srgb, var(--red) 50%, var(--border))';
      row.style.background = 'color-mix(in srgb, var(--red) 10%, transparent)';
    }

    const sourceLabel = bp.source === 'builtin' ? 'builtin' : 'custom';
    const title = (bp.display_name || bp.name || '').trim();
    const subtitle = (bp.description || '').trim();

    row.innerHTML = `
      <div class="memory-item-content">
        <span class="memory-item-text">${_esc(title)}</span>
        <div class="memory-item-meta">
          <span class="memory-cat-badge memory-cat-fact">${_esc(sourceLabel)}</span>
          <span class="memory-item-source">${_esc(bp.name || '')}</span>
        </div>
        ${subtitle ? `<div class="memory-item-source" style="margin-top:3px;opacity:.7;white-space:normal;word-break:break-word;">${_esc(subtitle)}</div>` : ''}
      </div>
      <button type="button" class="memory-toolbar-btn blueprint-copy-json-btn" title="Copy blueprint JSON to clipboard" style="flex-shrink:0;margin-left:8px;">Copy JSON</button>
    `;

    row.querySelector('.blueprint-copy-json-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      const jsonStr = JSON.stringify(bp, null, 2);
      try {
        await navigator.clipboard.writeText(jsonStr);
      } catch {
        const ta = document.createElement('textarea');
        ta.value = jsonStr;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    });

    row.addEventListener('click', () => {
      _selectedName = String(bp.name || '');
      _loadIntoEditor(bp);
      _renderList();
    });

    list.appendChild(row);
  }
}

function _loadIntoEditor(bp) {
  _el('blueprint-name').value = bp?.name || '';
  _el('blueprint-display-name').value = bp?.display_name || '';
  _el('blueprint-description').value = bp?.description || '';
  _el('blueprint-steps-json').value = JSON.stringify(bp?.nodes || [], null, 2);
  const edgesEl = _el('blueprint-edges-json');
  if (edgesEl) edgesEl.value = JSON.stringify(bp?.edges || [], null, 2);
  _renderStepsSummary();

  const del = _el('blueprint-delete-btn');
  if (del) del.disabled = !bp || bp.source === 'builtin';

  const nameInput = _el('blueprint-name');
  if (nameInput) nameInput.disabled = !!bp && bp.source === 'builtin';

  _showStatus(bp ? `Loaded ${bp.source} blueprint: ${bp.name}` : '');
}

function _emptyBlueprint() {
  const n1 = _newNodeId();
  const n2 = _newNodeId();
  const n3 = _newNodeId();
  return {
    name: '',
    display_name: '',
    description: '',
    nodes: [
      {
        node_id: n1,
        node_type: 'execute',
        title: 'Execute core work',
        description: 'Do the key task and capture outputs.',
        tools: [],
      },
      {
        node_id: n2,
        node_type: 'reflect',
        title: 'Reflect on output',
        description: 'Evaluate quality of the previous node output.',
        tools: [],
      },
      {
        node_id: n3,
        node_type: 'format',
        title: 'Format final report',
        description: 'Synthesize all context into a polished final report.',
        tools: [],
      },
    ],
    edges: [
      { from_node: n1, to_node: n2, condition: 'default', max_traversals: 3 },
      { from_node: n2, to_node: n3, condition: 'on_pass', max_traversals: 1 },
      { from_node: n2, to_node: n1, condition: 'on_fail', max_traversals: 2 },
    ],
  };
}

function _collectEditorPayload() {
  const current = _blueprints.find((b) => String(b.name || '').toLowerCase() === String(_selectedName || '').toLowerCase());

  const rawName = _el('blueprint-name').value || '';
  const normalized = _normalizeName(rawName || current?.name || '');
  if (!normalized) throw new Error('Blueprint name is required');

  let nodes;
  try {
    nodes = _parseNodesJson();
  } catch (e) {
    throw new Error(`Invalid nodes JSON: ${e.message || e}`);
  }

  let edges = [];
  try {
    edges = _parseEdgesJson();
  } catch (e) {
    throw new Error(`Invalid edges JSON: ${e.message || e}`);
  }

  return {
    name: normalized,
    display_name: (_el('blueprint-display-name').value || normalized).trim(),
    description: (_el('blueprint-description').value || '').trim(),
    nodes,
    edges,
  };
}

async function _fetchBlueprints() {
  const res = await fetch(`${API_BASE}/api/orchestrator/blueprints`, {
    credentials: 'same-origin',
  });
  if (!res.ok) {
    throw new Error(`Failed to load blueprints (${res.status})`);
  }
  const data = await res.json();
  _blueprints = Array.isArray(data.blueprints) ? data.blueprints : [];
  _setCounts();
  _renderList();
}

export async function loadBlueprints() {
  await _fetchBlueprints();
  if (_selectedName) {
    const match = _blueprints.find((b) => String(b.name || '').toLowerCase() === String(_selectedName || '').toLowerCase());
    if (match) {
      _loadIntoEditor(match);
      return;
    }
  }
  const first = _blueprints[0];
  if (first) {
    _selectedName = first.name || '';
    _loadIntoEditor(first);
    _renderList();
  } else {
    _selectedName = '';
    _loadIntoEditor(_emptyBlueprint());
  }
}

async function _saveBlueprint() {
  try {
    const payload = _collectEditorPayload();
    const res = await fetch(`${API_BASE}/api/orchestrator/blueprints/${encodeURIComponent(payload.name)}`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Save failed (${res.status})`);
    }
    _selectedName = payload.name;
    await loadBlueprints();
    window.uiModule?.showToast?.('Blueprint saved');
  } catch (e) {
    _showStatus(e?.message || 'Save failed', true);
  }
}

async function _deleteBlueprint() {
  const name = (_selectedName || '').trim();
  if (!name) return;

  const bp = _blueprints.find((b) => String(b.name || '').toLowerCase() === name.toLowerCase());
  if (!bp || bp.source === 'builtin') {
    _showStatus('Only custom blueprints can be deleted', true);
    return;
  }

  const ok = await window.uiModule?.styledConfirm?.(`Delete blueprint "${name}"?`, { confirmText: 'Delete', danger: true });
  if (!ok) return;

  const res = await fetch(`${API_BASE}/api/orchestrator/blueprints/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    _showStatus(err.detail || 'Delete failed', true);
    return;
  }

  _selectedName = '';
  await loadBlueprints();
  window.uiModule?.showToast?.('Blueprint deleted');
}

function _newBlueprint() {
  _selectedName = '';
  _loadIntoEditor(_emptyBlueprint());
  const nameInput = _el('blueprint-name');
  if (nameInput) {
    nameInput.disabled = false;
    nameInput.focus();
  }
  _renderList();
  _showStatus('Create a new custom blueprint and click Save');
}

function _validateNodes() {
  try {
    const parsed = _parseNodesJson();
    _showStatus(`Valid JSON: ${parsed.length} node(s)`);
  } catch (e) {
    _showStatus(e?.message || 'Invalid nodes JSON', true);
  }
}

function _formatNodes() {
  try {
    const parsed = _parseNodesJson();
    const ta = _el('blueprint-steps-json');
    if (ta) ta.value = JSON.stringify(parsed, null, 2);
    _renderStepsSummary();
    _showStatus('Nodes formatted and validated');
  } catch (e) {
    _showStatus(e?.message || 'Unable to format nodes', true);
  }
}

function _cloneAsCustom() {
  const current = _blueprints.find((b) => String(b.name || '').toLowerCase() === String(_selectedName || '').toLowerCase());
  if (!current) {
    _showStatus('Select a blueprint to clone first', true);
    return;
  }
  const cloneName = _normalizeName(`${current.name || 'blueprint'}-custom`);
  _selectedName = '';
  _el('blueprint-name').value = cloneName;
  _el('blueprint-display-name').value = `${current.display_name || current.name} (Custom)`;
  _el('blueprint-description').value = current.description || '';
  _el('blueprint-steps-json').value = JSON.stringify(current.nodes || [], null, 2);
  const edgesEl = _el('blueprint-edges-json');
  if (edgesEl) edgesEl.value = JSON.stringify(current.edges || [], null, 2);
  _renderStepsSummary();
  const nameInput = _el('blueprint-name');
  if (nameInput) {
    nameInput.disabled = false;
    nameInput.focus();
    nameInput.select();
  }
  const del = _el('blueprint-delete-btn');
  if (del) del.disabled = true;
  _renderList();
  _showStatus('Cloned into editable custom blueprint. Rename if needed, then click Save.');
}

export function initBlueprintsTab() {
  const saveBtn = _el('blueprint-save-btn');
  const delBtn = _el('blueprint-delete-btn');
  const newBtn = _el('blueprint-new-btn');
  const validateBtn = _el('blueprint-validate-btn');
  const formatBtn = _el('blueprint-format-btn');
  const cloneBtn = _el('blueprint-clone-btn');
  const editStepsBtn = _el('blueprint-steps-editor-btn');
  const modalCloseBtn = _el('blueprint-steps-modal-close');
  const modalCancelBtn = _el('blueprint-steps-modal-cancel');
  const modalSaveBtn = _el('blueprint-steps-modal-save');
  const modalAddStepBtn = _el('blueprint-step-add-btn');
  const cards = _el('blueprint-steps-cards');
  const modal = _el('blueprint-steps-modal');
  const inputs = [
    _el('blueprint-name'),
    _el('blueprint-display-name'),
    _el('blueprint-description'),
    _el('blueprint-steps-json'),
    _el('blueprint-edges-json'),
  ].filter(Boolean);

  if (saveBtn && !saveBtn.dataset.bound) {
    saveBtn.dataset.bound = '1';
    saveBtn.addEventListener('click', _saveBlueprint);
  }
  if (delBtn && !delBtn.dataset.bound) {
    delBtn.dataset.bound = '1';
    delBtn.addEventListener('click', _deleteBlueprint);
  }
  if (newBtn && !newBtn.dataset.bound) {
    newBtn.dataset.bound = '1';
    newBtn.addEventListener('click', _newBlueprint);
  }
  if (validateBtn && !validateBtn.dataset.bound) {
    validateBtn.dataset.bound = '1';
    validateBtn.addEventListener('click', _validateNodes);
  }
  if (formatBtn && !formatBtn.dataset.bound) {
    formatBtn.dataset.bound = '1';
    formatBtn.addEventListener('click', _formatNodes);
  }
  if (cloneBtn && !cloneBtn.dataset.bound) {
    cloneBtn.dataset.bound = '1';
    cloneBtn.addEventListener('click', _cloneAsCustom);
  }
  if (editStepsBtn && !editStepsBtn.dataset.bound) {
    editStepsBtn.dataset.bound = '1';
    editStepsBtn.addEventListener('click', () => _openStepsModal().catch(e => _showStatus(e?.message || 'Failed to open editor', true)));
  }
  if (modalCloseBtn && !modalCloseBtn.dataset.bound) {
    modalCloseBtn.dataset.bound = '1';
    modalCloseBtn.addEventListener('click', _closeStepsModal);
  }
  if (modalCancelBtn && !modalCancelBtn.dataset.bound) {
    modalCancelBtn.dataset.bound = '1';
    modalCancelBtn.addEventListener('click', _closeStepsModal);
  }
  if (modalSaveBtn && !modalSaveBtn.dataset.bound) {
    modalSaveBtn.dataset.bound = '1';
    modalSaveBtn.addEventListener('click', _saveNodesModal);
  }
  if (modalAddStepBtn && !modalAddStepBtn.dataset.bound) {
    modalAddStepBtn.dataset.bound = '1';
    modalAddStepBtn.addEventListener('click', _addNodeCard);
  }
  if (cards && !cards.dataset.bound) {
    cards.dataset.bound = '1';
    cards.addEventListener('input', _onStepsCardsInput);
    cards.addEventListener('change', _onStepsCardsInput);
    cards.addEventListener('click', _onStepsCardsClick);
  }
  if (modal && !modal.dataset.bound) {
    modal.dataset.bound = '1';
    modal.addEventListener('click', (e) => {
      if (e.target === modal) _closeStepsModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modal && !modal.classList.contains('hidden')) {
        _closeStepsModal();
      }
    });
  }
  inputs.forEach((input) => {
    if (!input.dataset.boundBlueprintDirty) {
      input.dataset.boundBlueprintDirty = '1';
      input.addEventListener('input', () => _showStatus('Unsaved changes'));
    }
  });
}

export default {
  initBlueprintsTab,
  loadBlueprints,
};
