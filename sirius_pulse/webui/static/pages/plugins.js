import { store } from '../store.js';
import { get, post, put } from '../app.js';
import { toast, flashSuccess, DynamicConfigForm } from '../components.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

let currentModal = null;
let configForm = null;

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">插件管理</div>
          <div class="card-subtitle">管理系统插件的启用状态和配置</div>
        </div>
        <button class="btn btn-sm" id="reloadPlugins">刷新</button>
      </div>
      <div id="pluginList" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  const reloadBtn = $('reloadPlugins');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => loadPlugins());
  }
  await loadPlugins();
}

async function loadPlugins() {
  const el = $('pluginList');
  try {
    const res = await get('/plugins');
    const plugins = res.plugins || [];

    if (!plugins.length) {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-3)">暂无插件</div>';
      return;
    }

    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">${plugins.map(p => `
      <div class="card plugin-detail-btn" data-name="${p.name}" style="margin:0;cursor:pointer">
        <div style="padding:16px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
            <span class="plugin-toggle tag" data-name="${p.name}" style="font-size:11px;background:${p.enabled ? 'var(--success)' : 'var(--text-3)'};color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0" onclick="event.stopPropagation()">${p.enabled ? '已启用' : '已禁用'}</span>
            <span class="tag" style="font-size:11px;background:var(--accent);color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0">${p.version || '—'}</span>
            <span style="font-size:15px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.display_name || p.name}</span>
          </div>
          ${p.description ? `<div style="font-size:13px;color:var(--text-2);line-height:1.4">${p.description}</div>` : ''}
          <div style="display:flex;gap:12px;font-size:12px;color:var(--text-2);margin-top:12px">
            <span>命令: ${(p.commands || []).length}</span>
            <span>参数: ${(p.parameters || []).length}</span>
          </div>
        </div>
      </div>
    `).join('')}</div>`;

    el.querySelectorAll('.plugin-toggle').forEach(tag => {
      tag.addEventListener('click', (e) => {
        e.stopPropagation();
        const name = tag.dataset.name;
        const newState = tag.textContent === '已启用' ? false : true;
        tag.textContent = newState ? '已启用' : '已禁用';
        tag.style.background = newState ? 'var(--success)' : 'var(--text-3)';
        togglePlugin(name, newState, tag);
      });
    });

    el.querySelectorAll('.plugin-detail-btn').forEach(btn => {
      btn.addEventListener('click', () => openDetail(btn.dataset.name));
    });
  } catch {
    el.innerHTML = '<div style="color:var(--danger);padding:12px">插件列表加载失败</div>';
  }
}

async function togglePlugin(name, enabled, tagEl) {
  try {
    const res = await post(`/plugins/${name}/toggle`, { enabled });
    if (res.success) {
      toast(`${name} 已${enabled ? '启用' : '禁用'}`, 'success');
    } else {
      toast(res.error || '操作失败', 'error');
      if (tagEl) {
        tagEl.textContent = enabled ? '已禁用' : '已启用';
        tagEl.style.background = enabled ? 'var(--text-3)' : 'var(--success)';
      }
    }
  } catch (e) {
    toast('操作失败: ' + e.message, 'error');
    if (tagEl) {
      tagEl.textContent = enabled ? '已禁用' : '已启用';
      tagEl.style.background = enabled ? 'var(--text-3)' : 'var(--success)';
    }
  }
}

async function openDetail(name) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:720px">
      <div class="modal-header">
        <span id="modalTitle" style="font-size:16px;font-weight:600">加载中...</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body" id="modalBody">
        <div style="padding:20px;text-align:center;color:var(--text-3)">加载中...</div>
      </div>
      <div class="modal-footer" id="modalFooter"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });
  overlay.querySelector('#modalClose').addEventListener('click', closeModal);

  try {
    const detail = await get(`/plugins/${name}`);
    renderModalContent(detail);
  } catch {
    const body = $('modalBody');
    if (body) body.innerHTML = '<div style="color:var(--danger);padding:12px">加载失败</div>';
  }
}

function closeModal() {
  if (currentModal) {
    currentModal.remove();
    currentModal = null;
  }
  configForm = null;
}

function renderModalContent(d) {
  const title = $('modalTitle');
  if (title) title.textContent = d.display_name || d.name;

  const commands = d.commands || [];
  const parameters = d.parameters || [];
  const nlExamples = d.nl_examples || [];
  const permissions = d.permissions || {};
  const settings = d.settings || {};

  const body = $('modalBody');
  if (!body) return;
  body.innerHTML = `
    <div class="stat-grid" style="margin-bottom:16px">
      <div class="stat-card">
        <div class="stat-label">版本</div>
        <div class="stat-value" style="font-size:14px">${d.version || '—'}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">作者</div>
        <div class="stat-value" style="font-size:14px">${d.author || '—'}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">命令数</div>
        <div class="stat-value">${commands.length}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">状态</div>
        <div class="stat-value" style="font-size:14px;color:${d.enabled ? 'var(--success)' : 'var(--text-3)'}">${d.enabled ? '已启用' : '已禁用'}</div>
      </div>
    </div>

    ${commands.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">命令列表</div>
        <div style="display:grid;gap:8px">
          ${commands.map(c => `
            <div style="padding:10px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-weight:500;font-size:13px">${c.name}</span>
                <span class="tag" style="font-size:11px">${c.pattern_type || 'text'}</span>
              </div>
              ${c.description ? `<div style="font-size:12px;color:var(--text-2);margin-bottom:4px">${c.description}</div>` : ''}
              ${(c.patterns || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${c.patterns.map(pt => `<code style="font-size:11px;padding:2px 6px;background:var(--surface-3,rgba(255,255,255,0.06));border-radius:4px">${pt}</code>`).join('')}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    ${nlExamples.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">自然语言示例</div>
        <div style="display:grid;gap:6px">
          ${nlExamples.map(ex => `
            <div style="font-size:13px;padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;color:var(--text-2)">"${ex}"</div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    <div id="pluginParamsSection" style="margin-bottom:16px">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">参数配置</div>
      <form id="paramsForm" style="display:grid;gap:10px"></form>
    </div>

    <div style="margin-bottom:16px">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">权限配置</div>
      <form id="permForm" style="display:grid;gap:12px">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" name="developer_only" ${permissions.developer_only ? 'checked' : ''}>
          仅开发者可用
        </label>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" name="hidden_from_intent" ${permissions.hidden_from_intent ? 'checked' : ''}>
          意图识别中隐藏
        </label>
        <div class="form-group" style="margin:0">
          <label>频率限制 (次/分钟)</label>
          <div class="number-input-group">
            <button type="button" class="number-spin-btn" data-spin-target="rate_limit" data-spin-dir="-1">−</button>
            <input type="number" id="rate_limit" name="rate_limit" value="${permissions.rate_limit_calls_per_minute || 60}" min="1" max="1000">
            <button type="button" class="number-spin-btn" data-spin-target="rate_limit" data-spin-dir="1">+</button>
          </div>
        </div>
        <div class="form-group" style="margin:0">
          <label>群组黑名单</label>
          <input type="text" name="group_blacklist" placeholder="群号用逗号分隔" value="${(permissions.group_blacklist || []).join(',')}">
        </div>
      </form>
    </div>
  `;

  // 使用 DynamicConfigForm 组件渲染参数配置（复用参数列表）
  const section = $('pluginParamsSection');
  if (parameters.length || Object.keys(settings).length) {
    configForm = new DynamicConfigForm({
      containerId: 'pluginParamsSection',
      parameters,
      settings,
      get
    });
    configForm.init().then(() => {
      configForm.render();
      // 如果没有参数，隐藏标题
      const form = $('paramsForm');
      if (form && !form.children.length) {
        section.style.display = 'none';
      }
    });
  } else {
    section.style.display = 'none';
  }

  const footer = $('modalFooter');
  if (footer) {
    footer.innerHTML = `
      <button class="btn" id="modalCancel">取消</button>
      <button class="btn btn-primary" id="modalSave">保存配置</button>
    `;
  }

  $('modalCancel')?.addEventListener('click', closeModal);
  $('modalSave')?.addEventListener('click', () => savePluginConfig(d.name));

  // 数字调节按钮事件
  scopedPage.$('[data-spin-target]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = $(btn.dataset.spinTarget);
      if (!target) return;
      const dir = parseInt(btn.dataset.spinDir, 10);
      const step = parseFloat(target.step) || 1;
      const min = target.min !== '' ? parseFloat(target.min) : -Infinity;
      const max = target.max !== '' ? parseFloat(target.max) : Infinity;
      const cur = parseFloat(target.value) || 0;
      target.value = Math.min(max, Math.max(min, cur + step * dir));
      target.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
}

async function savePluginConfig(name) {
  const saveBtn = $('modalSave');
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = '保存中...';
  }

  try {
    const permForm = $('permForm');
    if (permForm) {
      const bl = permForm.group_blacklist.value.trim();
      const permissions = {
        developer_only: permForm.developer_only.checked,
        hidden_from_intent: permForm.hidden_from_intent.checked,
        rate_limit_calls_per_minute: parseInt(permForm.rate_limit.value, 10) || 60,
        group_blacklist: bl ? bl.split(',').map(s => s.trim()).filter(Boolean) : [],
      };
      await put(`/plugins/${name}/config`, permissions);
    }

    if (configForm) {
      const newSettings = configForm.collectValues();
      if (Object.keys(newSettings).length > 0) {
        await post(`/plugins/${name}/settings`, { settings: newSettings });
      }
    }

    flashSuccess(saveBtn);
    toast('配置已保存', 'success');
    scopedPage.timeout(closeModal, 1200);
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = '保存配置';
    }
  }
}
