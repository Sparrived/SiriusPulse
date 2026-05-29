import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let currentModal = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">请先选择人格</div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div>
          <div class="card-title">技能列表</div>
          <div class="card-subtitle">管理当前人格已安装的技能</div>
        </div>
        <button class="btn btn-sm" id="refreshSkills">刷新</button>
      </div>
      <div id="skillList" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  $('refreshSkills').addEventListener('click', () => loadSkills());

  await loadSkills();
}

async function loadSkills() {
  const name = store.currentPersona;
  const el = $('skillList');
  try {
    const data = await get(`/personas/${name}/skills`);
    const skills = data.skills || [];
    if (!skills.length) {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-3)">暂无技能</div>';
      return;
    }
    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">${skills.map(s => renderSkillCard(s)).join('')}</div>`;
    el.querySelectorAll('.skill-toggle').forEach(tag => {
      tag.addEventListener('click', (e) => {
        e.stopPropagation();
        const name = tag.dataset.name;
        const newState = tag.textContent === '已启用' ? false : true;
        tag.textContent = newState ? '已启用' : '已禁用';
        tag.style.background = newState ? 'var(--success)' : 'var(--text-3)';
        toggleSkill(name, newState, tag);
      });
    });
    el.querySelectorAll('.skill-config-btn').forEach(btn => {
      btn.addEventListener('click', () => openConfigModal(btn.dataset.name));
    });
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger);padding:12px">加载失败: ${e.message}</div>`;
  }
}

function renderSkillCard(s) {
  const tags = (s.tags || []).map(t => `<span class="tag">${t}</span>`).join('');
  const paramCount = (s.parameters || []).length;
  const isEnabled = s.enabled !== false;
  return `
    <div class="card skill-config-btn" data-name="${s.name}" style="margin:0;cursor:pointer">
      <div class="card-header">
        <div>
          <div style="display:flex;align-items:center;gap:12px">
            <span class="skill-toggle tag" data-name="${s.name}" style="font-size:11px;background:${isEnabled ? 'var(--success)' : 'var(--text-3)'};color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0" onclick="event.stopPropagation()">${isEnabled ? '已启用' : '已禁用'}</span>
            <span class="tag" style="font-size:11px;background:var(--accent);color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0">${s.version || '—'}</span>
            <span style="font-size:15px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.display_name || s.name}</span>
            ${s.developer_only ? '<span class="tag tag-accent" style="font-size:11px;flex-shrink:0">开发者</span>' : ''}
            ${s.silent ? '<span class="tag" style="font-size:11px;color:var(--text-3);flex-shrink:0">静默</span>' : ''}
          </div>
          ${tags ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${tags}</div>` : ''}
        </div>
      </div>
      ${s.description ? `<div style="padding:0 16px 8px;font-size:13px;color:var(--text-2);line-height:1.5">${s.description}</div>` : ''}
      <div style="padding:0 16px 16px;display:flex;gap:16px;font-size:12px;color:var(--text-3)">
        <span>参数: ${paramCount}</span>
      </div>
    </div>
  `;
}

async function toggleSkill(skillName, enabled, tagEl) {
  const name = store.currentPersona;
  try {
    await post(`/personas/${name}/skills/${skillName}/toggle`, { enabled });
    toast(`${skillName} 已${enabled ? '启用' : '禁用'}`, 'success');
  } catch (e) {
    toast('操作失败: ' + e.message, 'error');
    if (tagEl) {
      tagEl.textContent = enabled ? '已禁用' : '已启用';
      tagEl.style.background = enabled ? 'var(--text-3)' : 'var(--success)';
    }
  }
}

async function openConfigModal(skillName) {
  closeModal();
  const name = store.currentPersona;

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:600px;max-height:85vh;overflow-y:auto">
      <div class="modal-header">
        <span style="font-size:16px;font-weight:600">${skillName} 配置</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body" id="modalBody">
        <div style="padding:20px;text-align:center;color:var(--text-3)">加载中...</div>
      </div>
      <div class="modal-footer" id="modalFooter">
        <button class="btn" id="modalCancel">取消</button>
        <button class="btn btn-primary" id="modalSave">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  $('modalClose').addEventListener('click', closeModal);
  $('modalCancel').addEventListener('click', closeModal);

  try {
    const data = await get(`/personas/${name}/skills/${skillName}/config`);
    renderConfigModal(data, skillName);
  } catch (e) {
    $('modalBody').innerHTML = `<div style="color:var(--danger);padding:12px">加载失败: ${e.message}</div>`;
  }
}

async function renderConfigModal(config, skillName) {
  const meta = config.meta || {};
  const params = meta.parameters || [];
  const extraKeys = Object.keys(config).filter(k => k !== 'enabled' && k !== 'meta' && k !== 'config');
  const skillConfig = config.config || {};

  let html = `
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;margin-bottom:16px">
      <input type="checkbox" id="cfgEnabled" ${config.enabled !== false ? 'checked' : ''}>
      <span>启用技能</span>
    </label>
  `;

  // 使用 DynamicConfigForm 渲染参数表单
  if (params.length) {
    const { DynamicConfigForm } = await import('../components.js');
    const form = new DynamicConfigForm({
      containerId: 'skillConfigForm',
      parameters: params,
      settings: skillConfig,
      get: get,
    });
    await form.init();
    html += `<div id="skillConfigForm"></div>`;
    
    $('modalBody').innerHTML = html;
    form.render();
    
    // 保存时使用表单收集的值
    $('modalSave').addEventListener('click', async () => {
      const values = form.collectValues();
      await saveConfig(values, skillName);
    });
  } else {
    // 无参数时显示 JSON 编辑器
    if (extraKeys.length > 0) {
      const extra = {};
      extraKeys.forEach(k => { extra[k] = config[k]; });
      const extraVal = JSON.stringify(extra, null, 2);
      html += `
        <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">
          <h4 style="margin:0 0 8px;font-size:14px">额外配置 (JSON)</h4>
          <textarea id="cfgExtra" rows="6" style="width:100%;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:10px;font-size:12px;font-family:monospace">${extraVal}</textarea>
        </div>
      `;
    }
    
    $('modalBody').innerHTML = html;
    
    $('modalSave').addEventListener('click', async () => {
      const payload = { enabled: $('cfgEnabled').checked };
      const extraText = $('cfgExtra')?.value?.trim();
      if (extraText) {
        try {
          const extra = JSON.parse(extraText);
          Object.assign(payload, extra);
        } catch {
          toast('JSON 格式错误', 'error');
          return;
        }
      }
      await saveConfig(payload, skillName);
    });
  }
}

async function saveConfig(payload, skillName) {
  const name = store.currentPersona;
  const btn = $('modalSave');
  btn.disabled = true;
  btn.textContent = '保存中...';

  try {
    await post(`/personas/${name}/skills/${skillName}/config`, payload);
    flashSuccess(btn);
    toast('配置已保存');
    setTimeout(closeModal, 800);
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

function closeModal() {
  if (currentModal) {
    currentModal.remove();
    currentModal = null;
  }
}


