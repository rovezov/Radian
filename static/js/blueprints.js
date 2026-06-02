const API_BASE = window.location.origin;

let _blueprints = [];
let _selectedName = '';
let _stepsModalDraft = null;

const TOOL_OPTIONS = [
  'web_search',
  'trigger_research',
  'manage_research',
  'deep_research',
  'read_file',
  'write_file',
  'bash',
  'python',
  'manage_notes',
  'manage_memory',
  'list_sessions',
  'manage_calendar',
  'read_email',
  'list_emails',
];

const QUALITY_OPTIONS = ['llm_eval', 'script', 'none'];

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

function _parseStepsJson() {
  const raw = (_el('blueprint-steps-json')?.value || '').trim();
  let parsed;
  try {
    parsed = JSON.parse(raw || '[]');
  } catch (e) {
    throw new Error(`Steps JSON parse failed: ${e.message || e}`);
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('Steps must be a non-empty JSON array');
  }
  for (let i = 0; i < parsed.length; i++) {
    const s = parsed[i] || {};
    if (typeof s !== 'object' || Array.isArray(s)) {
      throw new Error(`Step ${i + 1} must be an object`);
    }
    if (!String(s.title || '').trim()) {
      throw new Error(`Step ${i + 1} is missing title`);
    }
    if (!String(s.description || '').trim()) {
      throw new Error(`Step ${i + 1} is missing description`);
    }
    if (s.tools != null && !Array.isArray(s.tools)) {
      throw new Error(`Step ${i + 1} tools must be an array`);
    }
  }
  return parsed;
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

function _newStepId() {
  return `step_${Math.random().toString(16).slice(2, 10)}`;
}

function _normalizeStep(raw, index = 0) {
  const source = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
  const qcSource = (source.quality_check && typeof source.quality_check === 'object' && !Array.isArray(source.quality_check))
    ? source.quality_check
    : {};
  const qcType = QUALITY_OPTIONS.includes(String(qcSource.type || '').trim()) ? String(qcSource.type).trim() : 'llm_eval';
  const retriesNum = Number.isFinite(Number(source.max_retries)) ? Number(source.max_retries) : 2;
  let inputsObj = {};
  if (source.inputs && typeof source.inputs === 'object' && !Array.isArray(source.inputs)) {
    inputsObj = source.inputs;
  }
  return {
    step_id: String(source.step_id || _newStepId()).trim() || _newStepId(),
    title: String(source.title || `Step ${index + 1}`).trim(),
    description: String(source.description || '').trim(),
    tools: Array.isArray(source.tools) ? source.tools.map((x) => String(x || '').trim()).filter(Boolean) : [],
    model: String(source.model || '').trim(),
    inputs: inputsObj,
    _inputsText: JSON.stringify(inputsObj, null, 2),
    quality_check: {
      type: qcType,
      criteria: String(qcSource.criteria || '').trim(),
    },
    max_retries: Math.max(0, Math.floor(retriesNum || 0)),
    depends_on: Array.isArray(source.depends_on)
      ? source.depends_on.map((x) => String(x || '').trim()).filter(Boolean)
      : [],
  };
}

function _stepsSummaryHtml() {
  let parsed = [];
  try {
    parsed = _parseStepsJson();
  } catch {
    return '<div class="blueprint-steps-summary-empty">Steps JSON is invalid. Open Edit Steps to repair.</div>';
  }
  if (!parsed.length) {
    return '<div class="blueprint-steps-summary-empty">No steps configured yet.</div>';
  }
  const lines = parsed.map((s, i) => {
    const title = String(s?.title || `Step ${i + 1}`).trim();
    return `<li>${_esc(title)}</li>`;
  }).join('');
  return `<div><strong>${parsed.length}</strong> step(s)</div><ol class="blueprint-steps-summary-list">${lines}</ol>`;
}

function _renderStepsSummary() {
  const box = _el('blueprint-steps-summary');
  if (!box) return;
  box.innerHTML = _stepsSummaryHtml();
}

function _buildStepCard(step, index) {
  const knownTools = new Set(TOOL_OPTIONS);
  const selectedKnown = (step.tools || []).filter((t) => knownTools.has(String(t).trim()));
  const customTools = (step.tools || []).filter((t) => !knownTools.has(String(t).trim()));
  const allToolOptions = [...TOOL_OPTIONS, ...customTools.filter(Boolean)];
  const optionsHtml = allToolOptions.map((tool) => {
    const selected = selectedKnown.includes(tool) ? ' selected' : '';
    return `<option value="${_esc(tool)}"${selected}>${_esc(tool)}</option>`;
  }).join('');

  const qualityType = QUALITY_OPTIONS.includes(step?.quality_check?.type)
    ? step.quality_check.type
    : 'llm_eval';

  return `
    <div class="blueprint-step-card" data-step-index="${index}">
      <div class="blueprint-step-card-header">
        <span class="blueprint-step-card-title">Step ${index + 1}</span>
        <span class="blueprint-step-card-chip">${_esc(step.step_id || '')}</span>
        <div class="blueprint-step-card-actions">
          <button class="memory-toolbar-btn" type="button" data-action="move-up">Up</button>
          <button class="memory-toolbar-btn" type="button" data-action="move-down">Down</button>
          <button class="memory-toolbar-btn danger" type="button" data-action="delete">Remove</button>
        </div>
      </div>
      <div class="blueprint-step-grid">
        <label class="blueprint-step-label full">
          <span>Title</span>
          <input class="blueprint-step-input" data-field="title" type="text" value="${_esc(step.title || '')}" />
        </label>
        <label class="blueprint-step-label full">
          <span>Description</span>
          <textarea class="blueprint-step-textarea" data-field="description" rows="3">${_esc(step.description || '')}</textarea>
        </label>
        <label class="blueprint-step-label">
          <span>Tools (select one or more built-ins)</span>
          <select class="blueprint-step-select" data-field="tools_select" multiple>${optionsHtml}</select>
        </label>
        <label class="blueprint-step-label">
          <span>Additional tools (comma-separated)</span>
          <input class="blueprint-step-input" data-field="tools_custom" type="text" value="${_esc(customTools.join(', '))}" placeholder="custom_tool_a, custom_tool_b" />
        </label>
        <label class="blueprint-step-label">
          <span>Quality check type</span>
          <select class="blueprint-step-select" data-field="quality_type">
            ${QUALITY_OPTIONS.map((t) => `<option value="${t}"${qualityType === t ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
        </label>
        <label class="blueprint-step-label">
          <span>Max retries</span>
          <input class="blueprint-step-input" data-field="max_retries" type="number" min="0" step="1" value="${Number(step.max_retries || 0)}" />
        </label>
        <label class="blueprint-step-label full">
          <span>Quality criteria</span>
          <textarea class="blueprint-step-textarea" data-field="quality_criteria" rows="2">${_esc(step?.quality_check?.criteria || '')}</textarea>
        </label>
        <label class="blueprint-step-label">
          <span>Model override (optional)</span>
          <input class="blueprint-step-input" data-field="model" type="text" value="${_esc(step.model || '')}" placeholder="leave blank to use default" />
        </label>
        <label class="blueprint-step-label">
          <span>Depends on step IDs (comma-separated)</span>
          <input class="blueprint-step-input" data-field="depends_on" type="text" value="${_esc((step.depends_on || []).join(', '))}" placeholder="step_abc12345" />
        </label>
        <label class="blueprint-step-label full">
          <span>Inputs JSON object</span>
          <textarea class="blueprint-step-textarea" data-field="inputs_json" rows="3">${_esc(step._inputsText || '{}')}</textarea>
        </label>
      </div>
    </div>
  `;
}

function _renderStepsCards() {
  const wrap = _el('blueprint-steps-cards');
  if (!wrap) return;
  const list = Array.isArray(_stepsModalDraft) ? _stepsModalDraft : [];
  if (!list.length) {
    wrap.innerHTML = '<div class="memory-empty">No steps yet. Click Add Step.</div>';
    _modalStatus('No steps configured yet');
    return;
  }
  wrap.innerHTML = list.map((step, idx) => _buildStepCard(step, idx)).join('');
  _modalStatus(`${list.length} step(s) loaded`);
}

function _openStepsModal() {
  const modal = _el('blueprint-steps-modal');
  if (!modal) return;
  let parsed;
  try {
    parsed = _parseStepsJson();
  } catch (e) {
    _showStatus(e?.message || 'Invalid steps JSON', true);
    return;
  }
  _stepsModalDraft = parsed.map((s, i) => _normalizeStep(s, i));
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
  const custom = card.querySelector('[data-field="tools_custom"]');
  const selected = _selectedValues(select);
  const extras = String(custom?.value || '')
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);
  const seen = new Set();
  step.tools = [...selected, ...extras].filter((t) => {
    const key = t.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function _parseModalStepsForSave() {
  const out = [];
  const steps = Array.isArray(_stepsModalDraft) ? _stepsModalDraft : [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i] || {};
    const title = String(step.title || '').trim();
    const description = String(step.description || '').trim();
    if (!title) throw new Error(`Step ${i + 1} is missing title`);
    if (!description) throw new Error(`Step ${i + 1} is missing description`);

    const qualityType = QUALITY_OPTIONS.includes(String(step?.quality_check?.type || '').trim())
      ? String(step.quality_check.type).trim()
      : 'llm_eval';
    let inputs = {};
    const rawInputs = String(step._inputsText || '{}').trim() || '{}';
    try {
      const parsed = JSON.parse(rawInputs);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        inputs = parsed;
      } else {
        throw new Error('must be an object');
      }
    } catch (e) {
      throw new Error(`Step ${i + 1} inputs JSON is invalid: ${e.message || e}`);
    }

    out.push({
      step_id: String(step.step_id || _newStepId()).trim() || _newStepId(),
      title,
      description,
      tools: Array.isArray(step.tools) ? step.tools.map((t) => String(t || '').trim()).filter(Boolean) : [],
      model: String(step.model || '').trim(),
      inputs,
      quality_check: {
        type: qualityType,
        criteria: String(step?.quality_check?.criteria || '').trim(),
      },
      max_retries: Math.max(0, Number.parseInt(step.max_retries, 10) || 0),
      depends_on: Array.isArray(step.depends_on)
        ? step.depends_on.map((x) => String(x || '').trim()).filter(Boolean)
        : [],
    });
  }
  if (!out.length) {
    throw new Error('At least one step is required');
  }
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

  if (field === 'title') {
    step.title = target.value;
  } else if (field === 'description') {
    step.description = target.value;
  } else if (field === 'tools_select' || field === 'tools_custom') {
    _updateToolsFromCard(card, step);
  } else if (field === 'quality_type') {
    step.quality_check = step.quality_check || {};
    step.quality_check.type = QUALITY_OPTIONS.includes(target.value) ? target.value : 'llm_eval';
  } else if (field === 'quality_criteria') {
    step.quality_check = step.quality_check || {};
    step.quality_check.criteria = target.value;
  } else if (field === 'max_retries') {
    step.max_retries = target.value;
  } else if (field === 'model') {
    step.model = target.value;
  } else if (field === 'depends_on') {
    step.depends_on = String(target.value || '')
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean);
  } else if (field === 'inputs_json') {
    step._inputsText = target.value;
  }
  _modalStatus('Unsaved step changes');
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
  _modalStatus('Unsaved step changes');
}

function _addStepCard() {
  if (!Array.isArray(_stepsModalDraft)) _stepsModalDraft = [];
  _stepsModalDraft.push(_normalizeStep({
    step_id: _newStepId(),
    title: `Step ${_stepsModalDraft.length + 1}`,
    description: '',
    tools: [],
    model: '',
    inputs: {},
    quality_check: { type: 'llm_eval', criteria: '' },
    max_retries: 2,
    depends_on: [],
  }, _stepsModalDraft.length));
  _renderStepsCards();
  _modalStatus('Added step. Fill required fields before saving.');
}

function _saveStepsModal() {
  try {
    const steps = _parseModalStepsForSave();
    const ta = _el('blueprint-steps-json');
    if (ta) ta.value = JSON.stringify(steps, null, 2);
    _renderStepsSummary();
    _showStatus(`Updated ${steps.length} step(s) from card editor`);
    _closeStepsModal();
  } catch (e) {
    _modalStatus(e?.message || 'Unable to save steps', true);
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
    `;

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
  _el('blueprint-keywords').value = (bp?.trigger_keywords || []).join(', ');
  _el('blueprint-steps-json').value = JSON.stringify(bp?.steps || [], null, 2);
  _renderStepsSummary();

  const del = _el('blueprint-delete-btn');
  if (del) del.disabled = !bp || bp.source === 'builtin';

  const nameInput = _el('blueprint-name');
  if (nameInput) nameInput.disabled = !!bp && bp.source === 'builtin';

  _showStatus(bp ? `Loaded ${bp.source} blueprint: ${bp.name}` : '');
}

function _emptyBlueprint() {
  return {
    name: '',
    display_name: '',
    description: '',
    trigger_keywords: [],
    steps: [
      {
        title: 'Clarify objective',
        description: 'Interpret the request and list constraints.',
        tools: [],
        max_retries: 1,
      },
      {
        title: 'Execute core work',
        description: 'Do the key task and capture outputs.',
        tools: [],
        max_retries: 2,
      },
      {
        title: 'Validate and summarize',
        description: 'Check quality and present final output.',
        tools: [],
        max_retries: 1,
      },
    ],
  };
}

function _collectEditorPayload() {
  const current = _blueprints.find((b) => String(b.name || '').toLowerCase() === String(_selectedName || '').toLowerCase());

  const rawName = _el('blueprint-name').value || '';
  const normalized = _normalizeName(rawName || current?.name || '');
  if (!normalized) throw new Error('Blueprint name is required');

  let steps;
  try {
    steps = _parseStepsJson();
  } catch (e) {
    throw new Error(`Invalid steps JSON: ${e.message || e}`);
  }

  return {
    name: normalized,
    display_name: (_el('blueprint-display-name').value || normalized).trim(),
    description: (_el('blueprint-description').value || '').trim(),
    trigger_keywords: (_el('blueprint-keywords').value || '')
      .split(',')
      .map((x) => x.trim().toLowerCase())
      .filter(Boolean),
    steps,
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

function _validateSteps() {
  try {
    const parsed = _parseStepsJson();
    _showStatus(`Valid JSON: ${parsed.length} step(s)`);
  } catch (e) {
    _showStatus(e?.message || 'Invalid steps JSON', true);
  }
}

function _formatSteps() {
  try {
    const parsed = _parseStepsJson();
    const ta = _el('blueprint-steps-json');
    if (ta) ta.value = JSON.stringify(parsed, null, 2);
    _renderStepsSummary();
    _showStatus('Steps formatted and validated');
  } catch (e) {
    _showStatus(e?.message || 'Unable to format steps', true);
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
  _el('blueprint-keywords').value = (current.trigger_keywords || []).join(', ');
  _el('blueprint-steps-json').value = JSON.stringify(current.steps || [], null, 2);
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
    _el('blueprint-keywords'),
    _el('blueprint-description'),
    _el('blueprint-steps-json'),
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
    validateBtn.addEventListener('click', _validateSteps);
  }
  if (formatBtn && !formatBtn.dataset.bound) {
    formatBtn.dataset.bound = '1';
    formatBtn.addEventListener('click', _formatSteps);
  }
  if (cloneBtn && !cloneBtn.dataset.bound) {
    cloneBtn.dataset.bound = '1';
    cloneBtn.addEventListener('click', _cloneAsCustom);
  }
  if (editStepsBtn && !editStepsBtn.dataset.bound) {
    editStepsBtn.dataset.bound = '1';
    editStepsBtn.addEventListener('click', _openStepsModal);
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
    modalSaveBtn.addEventListener('click', _saveStepsModal);
  }
  if (modalAddStepBtn && !modalAddStepBtn.dataset.bound) {
    modalAddStepBtn.dataset.bound = '1';
    modalAddStepBtn.addEventListener('click', _addStepCard);
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
