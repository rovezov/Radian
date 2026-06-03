const API_BASE = window.location.origin;

function _esc(value) {
  const div = document.createElement('div');
  div.textContent = value == null ? '' : String(value);
  return div.innerHTML;
}

function _isPlanJson(text) {
  try {
    const parsed = JSON.parse(text);
    return !!(parsed && parsed.blueprint_type && parsed.objective && Array.isArray(parsed.nodes));
  } catch (_) {
    return false;
  }
}

function _renderCard(plan) {
  const card = document.createElement('div');
  card.className = 'memory-item orchestrator-plan-card';

  const steps = (plan.nodes || []).slice(0, 8)
    .map((s, i) => {
      const tools = Array.isArray(s.tools) && s.tools.length
        ? s.tools.map((t) => `<span class="orchestrator-plan-tool">${_esc(t)}</span>`).join('')
        : '<span class="orchestrator-plan-tool orchestrator-plan-tool-muted">none</span>';
      const modelLabel = (s.model || '').trim() || 'auto';
      const nodeType = s.node_type || 'execute';
      return `
      <li class="orchestrator-plan-step">
        <div class="orchestrator-plan-step-title">${_esc(`${i + 1}. ${s.title || 'Node'}`)} <span class="orchestrator-plan-node-type">(${_esc(nodeType)})</span></div>
        <div class="orchestrator-plan-step-desc">${_esc(s.description || '')}</div>
        <div class="orchestrator-plan-step-meta">
          <span class="orchestrator-plan-model">model: ${_esc(modelLabel)}</span>
          <span class="orchestrator-plan-tools-wrap">tools: ${tools}</span>
        </div>
      </li>`;
    })
    .join('');

  card.innerHTML = `
    <div class="orchestrator-plan-header">
      <span class="orchestrator-plan-title">Execution Plan</span>
      <span class="orchestrator-plan-badge">${_esc(plan.blueprint_type)}</span>
    </div>
    <div class="orchestrator-plan-objective">${_esc(plan.objective || '')}</div>
    <div class="orchestrator-plan-label">Planned nodes</div>
    <ol class="orchestrator-plan-steps">${steps}</ol>
    <div class="orchestrator-plan-footer">
      <button type="button" class="memory-toolbar-btn active orchestrator-approve-btn">Approve and Execute</button>
      <button type="button" class="memory-toolbar-btn orchestrator-copy-btn" title="Copy plan JSON to clipboard">Copy JSON</button>
      <span class="orchestrator-status"></span>
    </div>
  `;

  const btn = card.querySelector('.orchestrator-approve-btn');
  const copyBtn = card.querySelector('.orchestrator-copy-btn');
  const status = card.querySelector('.orchestrator-status');

  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(JSON.stringify(plan, null, 2));
        const orig = copyBtn.textContent;
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = orig; }, 1500);
      } catch (e) {
        // Fallback for environments where clipboard API is restricted
        const ta = document.createElement('textarea');
        ta.value = JSON.stringify(plan, null, 2);
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        const orig = copyBtn.textContent;
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = orig; }, 1500);
      }
    });
  }

  if (btn && status) {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      status.textContent = 'Queueing…';
      try {
        const res = await fetch(`${API_BASE}/api/orchestrator/dispatch`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plan }),
        });
        if (!res.ok) throw new Error(`Dispatch failed (${res.status})`);
        const data = await res.json();
        status.textContent = `Queued: ${(data.run_id || '').slice(0, 10)}`;
        if (window.uiModule?.showToast) {
          window.uiModule.showToast('Plan queued in orchestrator');
        }
        if (window.tasksModule?.openTasksOnTab) {
          window.tasksModule.openTasksOnTab('queue');
        } else if (window.tasksModule?.openTasks) {
          window.tasksModule.openTasks();
        }
      } catch (e) {
        status.textContent = (e && e.message) ? e.message : 'Dispatch failed';
        btn.disabled = false;
      }
    });
  }

  return card;
}

export function enhanceOrchestratorPlanCards(root) {
  if (!root) return;
  const blocks = root.querySelectorAll('pre code, code');
  for (const block of blocks) {
    const pre = block.closest('pre');
    if (!pre || pre.dataset.orchestratorPlan === '1') continue;

    const txt = (block.textContent || '').trim();
    if (!txt || !_isPlanJson(txt)) continue;

    let plan;
    try {
      plan = JSON.parse(txt);
    } catch (_) {
      continue;
    }
    if (!plan || !Array.isArray(plan.nodes) || !plan.blueprint_type) continue;

    pre.dataset.orchestratorPlan = '1';
    const card = _renderCard(plan);
    pre.replaceWith(card);
  }
}
