import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let adapterData = null;

export async function init(container, params) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">适配器配置</div>
        </div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在侧边栏中选择要配置的人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">适配器配置</div>
          <div class="card-subtitle">配置 ${name} 的平台适配器</div>
        </div>
        <button class="btn btn-primary" id="adapterSave" disabled>保存</button>
      </div>
      <div id="adapterContent">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  await loadAdapters(name);

  $('adapterSave').addEventListener('click', () => saveAdapters(name));
}

async function loadAdapters(name) {
  try {
    const data = await get(`/personas/${name}/adapters`);
    adapterData = data;
    renderAdapter(data.adapters?.[0] || {});
    // 加载成功后启用保存按钮
    $('adapterSave').disabled = false;
  } catch (e) {
    $('adapterContent').innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
    // 加载失败时保持保存按钮禁用
    $('adapterSave').disabled = true;
  }
}

function renderAdapter(adapter) {
  const el = $('adapterContent');
  const allowedGroups = adapter.allowed_group_ids || [];
  const allowedUsers = adapter.allowed_private_user_ids || [];

  el.innerHTML = `
    <form id="adapterForm" style="display:grid;gap:16px">
      <div style="font-size:14px;font-weight:600;color:var(--text-1);margin-bottom:4px">NapCat 适配器</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
        <div class="form-group">
          <label>启用</label>
          <div class="select-wrap">
            <select name="enabled">
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label>QQ 号</label>
          <input type="text" name="qq_number" placeholder="机器人QQ号">
        </div>
        <div class="form-group">
          <label>WebSocket URL</label>
          <input type="text" name="ws_url" placeholder="ws://localhost:3001">
        </div>
        <div class="form-group">
          <label>Token</label>
          <input type="text" name="token" placeholder="napcat_ws">
        </div>
        <div class="form-group">
          <label>根路径</label>
          <input type="text" name="root" placeholder="留空使用默认">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
        <div class="form-group">
          <label>启用群聊</label>
          <div class="select-wrap">
            <select name="enable_group_chat">
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label>启用私聊</label>
          <div class="select-wrap">
            <select name="enable_private_chat">
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </div>
        </div>
      </div>
      <div class="form-group">
        <label>允许的群组 ID</label>
        <div id="groupTags" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
        <div style="display:flex;gap:8px">
          <input type="text" id="addGroupInput" placeholder="输入群号">
          <button type="button" class="btn" id="addGroupBtn">添加</button>
        </div>
      </div>
      <div class="form-group">
        <label>允许的私聊用户 ID</label>
        <div id="userTags" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
        <div style="display:flex;gap:8px">
          <input type="text" id="addUserInput" placeholder="输入用户QQ号">
          <button type="button" class="btn" id="addUserBtn">添加</button>
        </div>
      </div>
    </form>
  `;

  const form = $('adapterForm');
  form.enabled.value = String(adapter.enabled ?? true);
  form.qq_number.value = adapter.qq_number || '';
  form.ws_url.value = adapter.ws_url || 'ws://localhost:3001';
  form.token.value = adapter.token || 'napcat_ws';
  form.root.value = adapter.root || '';
  form.enable_group_chat.value = String(adapter.enable_group_chat ?? true);
  form.enable_private_chat.value = String(adapter.enable_private_chat ?? true);

  renderTags('groupTags', allowedGroups);
  renderTags('userTags', allowedUsers);

  setupTagListeners('addGroupInput', 'addGroupBtn', 'groupTags');
  setupTagListeners('addUserInput', 'addUserBtn', 'userTags');
}

function renderTags(containerId, items) {
  const el = $(containerId);
  el.innerHTML = items.map(item => `
    <span class="tag tag-accent" data-value="${item}">
      ${item}
      <span class="tag-remove" style="cursor:pointer;margin-left:4px" data-value="${item}">×</span>
    </span>
  `).join('');

  el.querySelectorAll('.tag-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      btn.parentElement.remove();
    });
  });
}

function setupTagListeners(inputId, btnId, containerId) {
  const input = $(inputId);
  const btn = $(btnId);
  const container = $(containerId);

  function addTag() {
    const value = input.value.trim();
    if (!value) return;

    const existing = container.querySelectorAll('.tag');
    for (const tag of existing) {
      if (tag.dataset.value === value) {
        toast('该 ID 已存在', 'error');
        return;
      }
    }

    const tag = document.createElement('span');
    tag.className = 'tag tag-accent';
    tag.dataset.value = value;
    tag.innerHTML = `${value}<span class="tag-remove" style="cursor:pointer;margin-left:4px" data-value="${value}">×</span>`;
    tag.querySelector('.tag-remove').addEventListener('click', () => tag.remove());
    container.appendChild(tag);
    input.value = '';
  }

  btn.addEventListener('click', addTag);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTag();
    }
  });
}

function getTagValues(containerId) {
  return Array.from($(containerId).querySelectorAll('.tag')).map(tag => tag.dataset.value);
}

async function saveAdapters(name) {
  const form = $('adapterForm');
  if (!form) return;

  const adapter = {
    type: 'napcat',
    enabled: form.enabled.value === 'true',
    qq_number: form.qq_number.value,
    ws_url: form.ws_url.value,
    token: form.token.value,
    root: form.root.value,
    enable_group_chat: form.enable_group_chat.value === 'true',
    enable_private_chat: form.enable_private_chat.value === 'true',
    allowed_group_ids: getTagValues('groupTags'),
    allowed_private_user_ids: getTagValues('userTags'),
  };

  try {
    await post(`/personas/${name}/adapters`, { adapters: [adapter] });
    flashSuccess($('adapterSave'));
    toast('适配器配置已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
