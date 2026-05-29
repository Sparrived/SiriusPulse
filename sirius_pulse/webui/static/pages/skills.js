import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let currentModal = null;
let historyFilter = '';

// ── Skill 自定义配置渲染器注册表 ──
// 任何 Skill 可通过在此添加条目获得专用可视化配置 UI，
// 替代默认的"参数表单 + JSON 文本域"。
// 条目格式: { render(config, meta) → html, collect(config, meta) → object, modalWidth? }
const _skillConfigRenderers = {
  github_monitor: {
    render: _renderGithubMonitorConfig,
    collect: _collectGithubMonitorConfig,
    modalWidth: '740px',
  },
};

// ── GitHub Monitor 常量与状态 ──
const GITHUB_MONITOR_EVENT_TYPES = [
  { key: 'issues', label: 'Issues', desc: '新建 / 关闭 / 重开' },
  { key: 'pulls', label: 'Pull Requests', desc: '开启 / 合并 / 关闭' },
  { key: 'releases', label: 'Releases', desc: '新版本发布' },
  { key: 'comments', label: '评论', desc: 'Issue / PR / Commit 评论' },
  { key: 'pushes', label: '推送', desc: '代码推送' },
];

let _ghRepos = [];

function _renderGithubMonitorConfig(config) {
  const repos = (config.repos || []).map((r, i) => ({ ...r, _idx: i }));
  _ghRepos = JSON.parse(JSON.stringify(repos));
  const pollSeconds = config.poll_seconds !== undefined ? config.poll_seconds : 120;
  const apiBaseUrl = config.api_base_url || 'https://api.github.com';

  return `
    <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">
      <h4 style="margin:0 0 8px;font-size:14px">⏱️ 轮询间隔</h4>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="number" id="ghPollSeconds" value="${pollSeconds}" min="30" max="3600" step="10"
          style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:100px">
        <span style="font-size:13px;color:var(--text)">秒</span>
        <span style="font-size:12px;color:var(--text-3)">(30-3600，默认120)</span>
      </div>
    </div>

    <div style="margin-top:16px">
      <h4 style="margin:0 0 8px;font-size:14px">🔗 GitHub API 地址</h4>
      <input type="text" id="ghApiBaseUrl" value="${apiBaseUrl}" placeholder="https://api.github.com"
        style="width:100%;max-width:400px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">
      <div style="font-size:11px;color:var(--text-3);margin-top:4px">
        国内可改用镜像，如 <code>https://ghproxy.com/https://api.github.com</code>
      </div>
    </div>

    <div style="margin-top:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h4 style="margin:0;font-size:14px">📦 监控仓库</h4>
        <button class="btn btn-sm btn-primary" id="ghAddRepoBtn">+ 添加仓库</button>
      </div>
      <div style="font-size:12px;color:var(--text-3);margin-bottom:12px">
        配置要监控的 GitHub 仓库，检测到新事件时将自动截屏并播报。可在下方为每个仓库独立配置事件类型和通知目标群。
      </div>
      <div id="ghRepoList">${_ghRenderRepoList()}</div>
    </div>
  `;
}

function _ghRenderRepoList() {
  if (!_ghRepos.length) {
    return '<div style="color:var(--text-3);padding:20px;text-align:center;border:1px dashed var(--border);border-radius:8px">暂无监控仓库，点击「添加仓库」开始配置</div>';
  }
  return _ghRepos.map(r => _ghRenderRepoCard(r)).join('');
}

function _ghRenderRepoCard(repo) {
  const idx = repo._idx;
  const events = repo.events || [];
  return `
    <div style="border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px;background:var(--surface-2)">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
        <div style="display:flex;gap:8px;align-items:center;flex:1">
          <input type="text" value="${repo.owner || ''}" placeholder="owner"
            style="flex:1;min-width:80px;background:var(--bg-2, var(--surface-3));color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
            data-gh-field="owner" data-gh-idx="${idx}">
          <span style="color:var(--text-3);font-size:18px">/</span>
          <input type="text" value="${repo.repo || ''}" placeholder="repo"
            style="flex:1;min-width:80px;background:var(--bg-2, var(--surface-3));color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
            data-gh-field="repo" data-gh-idx="${idx}">
        </div>
        <button class="btn btn-sm" style="color:var(--danger);margin-left:8px;flex-shrink:0" data-gh-remove="${idx}">✕ 删除</button>
      </div>

      <div style="margin-bottom:12px">
        <label style="font-size:12px;color:var(--text-3);display:block;margin-bottom:6px">监控事件</label>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          ${GITHUB_MONITOR_EVENT_TYPES.map(et => {
            const checked = events.includes(et.key) ? ' checked' : '';
            return `
              <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:13px;color:var(--text);background:var(--bg-2, var(--surface-3));border:1px solid var(--border);border-radius:6px;padding:4px 10px;white-space:nowrap">
                <input type="checkbox" data-gh-event="${et.key}" data-gh-idx="${idx}"${checked}>
                <span>${et.label}</span>
                <span style="font-size:11px;color:var(--text-3)">${et.desc}</span>
              </label>
            `;
          }).join('')}
        </div>
      </div>

      <div style="margin-bottom:12px">
        <label style="font-size:12px;color:var(--text-3);display:block;margin-bottom:4px">通知目标群（群号，多个用逗号分隔）</label>
        <input type="text" value="${(repo.groups || []).join(', ')}" placeholder="例如: 123456789, 987654321"
          style="width:100%;background:var(--bg-2, var(--surface-3));color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
          data-gh-field="groups_str" data-gh-idx="${idx}">
      </div>

      <div>
        <label style="font-size:12px;color:var(--text-3);display:block;margin-bottom:4px">GitHub Token <span style="color:var(--text-3)">（可选，避免 API 频率限制）</span></label>
        <input type="password" value="${repo.github_token || ''}" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx"
          style="width:100%;background:var(--bg-2, var(--surface-3));color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
          data-gh-field="github_token" data-gh-idx="${idx}">
        <div style="font-size:11px;color:var(--text-3);margin-top:4px">提供 Token 后 API 速率限制从 60/小时 → 5000/小时</div>
      </div>
    </div>
  `;
}

function _ghRefreshRepoList() {
  const el = document.getElementById('ghRepoList');
  if (el) el.innerHTML = _ghRenderRepoList();
  _ghBindRepoEvents();
}

function _ghBindRepoEvents() {
  const container = document.getElementById('ghRepoList');
  if (!container) return;

  container.querySelectorAll('[data-gh-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.ghRemove, 10);
      _ghRepos = _ghRepos.filter(r => r._idx !== idx);
      _ghRepos.forEach((r, i) => { r._idx = i; });
      _ghRefreshRepoList();
    });
  });

  container.querySelectorAll('[data-gh-field]').forEach(inp => {
    inp.addEventListener('input', () => {
      const idx = parseInt(inp.dataset.ghIdx, 10);
      const field = inp.dataset.ghField;
      const repo = _ghRepos.find(r => r._idx === idx);
      if (!repo) return;
      if (field === 'groups_str') {
        repo.groups_str = inp.value;
        repo.groups = inp.value.split(',').map(s => s.trim()).filter(Boolean);
      } else {
        repo[field] = inp.value;
      }
    });
  });

  container.querySelectorAll('[data-gh-event]').forEach(cb => {
    cb.addEventListener('change', () => {
      const idx = parseInt(cb.dataset.ghIdx, 10);
      const eventKey = cb.dataset.ghEvent;
      const repo = _ghRepos.find(r => r._idx === idx);
      if (!repo) return;
      if (!repo.events) repo.events = [];
      if (cb.checked) {
        if (!repo.events.includes(eventKey)) repo.events.push(eventKey);
      } else {
        repo.events = repo.events.filter(e => e !== eventKey);
      }
    });
  });
}

function _collectGithubMonitorConfig() {
  const repos = _ghRepos.map(r => ({
    owner: (r.owner || '').trim(),
    repo: (r.repo || '').trim(),
    events: r.events || [],
    groups: r.groups || [],
    github_token: (r.github_token || '').trim(),
  })).filter(r => r.owner && r.repo);

  const pollEl = document.getElementById('ghPollSeconds');
  const pollSeconds = pollEl ? Math.max(30, Math.min(3600, parseInt(pollEl.value, 10) || 120)) : 120;

  const apiEl = document.getElementById('ghApiBaseUrl');
  const apiBaseUrl = apiEl ? (apiEl.value || '').trim() || 'https://api.github.com' : 'https://api.github.com';

  return { repos, poll_seconds: pollSeconds, api_base_url: apiBaseUrl };
}

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
    <div class="card">
      <div class="card-header">
        <div class="card-title">执行历史</div>
        <select id="historyFilter" class="btn btn-sm">
          <option value="">全部技能</option>
        </select>
      </div>
      <div id="historyList" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  $('refreshSkills').addEventListener('click', () => loadSkills());
  $('historyFilter').addEventListener('change', (e) => {
    historyFilter = e.target.value;
    loadHistory();
  });

  await Promise.all([loadSkills(), loadHistory()]);
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
    updateHistoryFilter(skills);
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
  const renderer = _skillConfigRenderers[skillName];
  const modalWidth = (renderer && renderer.modalWidth) || '600px';

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:${modalWidth};max-height:85vh;overflow-y:auto">
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

function renderConfigModal(config, skillName) {
  const meta = config.meta || {};
  const params = meta.parameters || [];
  const extraKeys = Object.keys(config).filter(k => k !== 'enabled' && k !== 'meta' && k !== 'config');
  const skillConfig = config.config || {};
  const renderer = _skillConfigRenderers[skillName];

  let html = `
    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;margin-bottom:16px">
      <input type="checkbox" id="cfgEnabled" ${config.enabled !== false ? 'checked' : ''}>
      <span>启用技能</span>
    </label>
  `;

  if (renderer) {
    // 使用自定义渲染器
    html += renderer.render(skillConfig, meta);
  } else {
    // 通用：参数表单
    if (params.length) {
      html += `
        <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">
          <h4 style="margin:0 0 12px;font-size:14px">运行时参数</h4>
          <div style="display:grid;gap:12px">
            ${params.map(p => {
              const val = config[p.name] !== undefined ? config[p.name] : (p.default || '');
              return `
                <div class="form-group" style="margin:0">
                  <label>${p.name}${p.description ? ` <span style="color:var(--text-3);font-size:11px">${p.description}</span>` : ''}</label>
                  <input type="text" name="param_${p.name}" value="${typeof val === 'object' ? JSON.stringify(val) : val}">
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `;
    }

    // JSON 文本域：仅在无参数（唯一配置途径）或已有额外数据时显示
    const paramNames = new Set(params.map(p => p.name));
    const hasExtra = extraKeys.length > 0;
    if (paramNames.size === 0 || hasExtra) {
      const extra = {};
      extraKeys.forEach(k => { extra[k] = config[k]; });
      const extraVal = JSON.stringify(extra, null, 2);
      html += `
        <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">
          <h4 style="margin:0 0 8px;font-size:14px">额外配置 (JSON)</h4>
          <div style="font-size:12px;color:var(--text-3);margin-bottom:8px">用于配置 API Key 等 Skill 专属参数</div>
          <textarea id="cfgExtra" rows="6" style="width:100%;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:10px;font-size:12px;font-family:monospace">${extraVal}</textarea>
        </div>
      `;
    }
  }

  $('modalBody').innerHTML = html;

  // 为 github_monitor 绑定「添加仓库」按钮
  if (skillName === 'github_monitor') {
    const addBtn = document.getElementById('ghAddRepoBtn');
    if (addBtn) {
      addBtn.addEventListener('click', () => {
        const idx = _ghRepos.length;
        _ghRepos.push({
          _idx: idx, owner: '', repo: '',
          events: ['issues', 'pulls', 'releases'],
          groups: [], github_token: '', groups_str: '',
        });
        _ghRefreshRepoList();
      });
    }
    _ghBindRepoEvents();
  }

  $('modalSave').addEventListener('click', () => saveConfig(params, skillName));
}

async function saveConfig(params, skillName) {
  const name = store.currentPersona;
  const btn = $('modalSave');
  btn.disabled = true;
  btn.textContent = '保存中...';

  try {
    const payload = { enabled: $('cfgEnabled').checked };

    // 查找是否注册了自定义收集器
    const renderer = _skillConfigRenderers[skillName];

    if (renderer && renderer.collect) {
      payload.config = renderer.collect({}, {});
    } else {
      // 通用：收集参数值
      params.forEach(p => {
        const input = $(`param_${p.name}`);
        if (input) {
          const val = input.value.trim();
          try { payload[p.name] = JSON.parse(val); }
          catch { payload[p.name] = val; }
        }
      });

      // 合并额外 JSON 配置
      const extraText = $('cfgExtra')?.value?.trim();
      if (extraText) {
        try {
          const extra = JSON.parse(extraText);
          Object.assign(payload, extra);
        } catch {
          toast('额外配置 JSON 格式错误', 'error');
          btn.disabled = false;
          btn.textContent = '保存';
          return;
        }
      }
    }

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
  _ghRepos = [];
}

async function loadHistory() {
  const name = store.currentPersona;
  const el = $('historyList');
  try {
    const data = await get(`/personas/${name}/skill-history`);
    let records = data.records || data.history || [];
    if (historyFilter) {
      records = records.filter(r => r.skill_name === historyFilter);
    }
    if (!records.length) {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-3)">暂无执行记录</div>';
      return;
    }
    el.innerHTML = `<div style="display:grid;gap:8px">${records.slice(0, 50).map(r => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:13px">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="tag" style="font-size:11px">${r.skill_name || '—'}</span>
          <span style="color:${r.success ? 'var(--success)' : 'var(--danger)'}">${r.success ? '✓ 成功' : '✕ 失败'}</span>
        </div>
        <div style="display:flex;align-items:center;gap:12px;color:var(--text-3);font-size:12px">
          ${r.duration != null ? `<span>${typeof r.duration === 'number' ? r.duration.toFixed(1) + 's' : r.duration}</span>` : ''}
          <span>${r.timestamp ? new Date(r.timestamp).toLocaleString('zh-CN') : '—'}</span>
        </div>
      </div>
    `).join('')}</div>`;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger);padding:12px">加载失败: ${e.message}</div>`;
  }
}

function updateHistoryFilter(skills) {
  const sel = $('historyFilter');
  const prev = sel.value;
  sel.innerHTML = '<option value="">全部技能</option>' +
    skills.map(s => `<option value="${s.name}">${s.display_name || s.name}</option>`).join('');
  sel.value = prev;
}
