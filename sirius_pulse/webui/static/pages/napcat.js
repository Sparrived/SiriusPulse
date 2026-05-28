import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let napcatStatus = {};

export async function init(container) {
  container.innerHTML = `
    <div class="stat-grid" id="napcatStats"></div>
    <div class="card" style="margin-top:20px">
      <div class="card-header">
        <div class="card-title">操作</div>
      </div>
      <div id="napcatActions" style="padding:16px"></div>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="card-header">
        <div class="card-title">日志</div>
        <button class="btn btn-sm" id="refreshLogs">刷新</button>
      </div>
      <div id="napcatLogs" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  await Promise.all([loadStatus(), loadLogs()]);
  $('refreshLogs').addEventListener('click', () => loadLogs());
}

async function loadStatus() {
  try {
    const res = await get('/napcat/status');
    napcatStatus = res;
    renderStats(res);
    renderActions(res);
  } catch {
    $('napcatStats').innerHTML = '<div style="color:var(--danger);padding:12px">状态加载失败</div>';
    $('napcatActions').innerHTML = '';
  }
}

function renderStats(s) {
  const el = $('napcatStats');
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">安装状态</div>
      <div class="stat-value" style="color:${s.installed ? 'var(--success)' : 'var(--text-3)'}">${s.installed ? '已安装' : '未安装'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">运行状态</div>
      <div class="stat-value" style="color:${s.running ? 'var(--success)' : 'var(--text-3)'}">${s.running ? '运行中' : '已停止'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">QQ 客户端</div>
      <div class="stat-value" style="color:${s.qq_installed ? 'var(--success)' : 'var(--text-3)'}">${s.qq_installed ? '已安装' : '未安装'}</div>
    </div>
  `;
}

function renderActions(s) {
  const el = $('napcatActions');

  if (!s.enabled) {
    el.innerHTML = '<div style="color:var(--text-3)">NapCat 管理未启用</div>';
    return;
  }

  let buttons = '';
  if (!s.installed) {
    buttons = `<button class="btn btn-primary" id="btnInstall">📦 安装 NapCat</button>`;
  } else if (!s.running) {
    buttons = `
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="qqInput" placeholder="QQ 号码" style="max-width:200px">
        <button class="btn btn-success" id="btnStart">▶ 启动</button>
      </div>
    `;
  } else {
    buttons = `
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="qqInput" placeholder="QQ 号码" style="max-width:200px">
        <button class="btn btn-danger" id="btnStop">⏹ 停止</button>
      </div>
    `;
  }
  el.innerHTML = buttons;

  const installBtn = $('btnInstall');
  if (installBtn) {
    installBtn.addEventListener('click', () => doInstall(installBtn));
  }

  const startBtn = $('btnStart');
  if (startBtn) {
    startBtn.addEventListener('click', () => doStart(startBtn));
  }

  const stopBtn = $('btnStop');
  if (stopBtn) {
    stopBtn.addEventListener('click', () => doStop(stopBtn));
  }
}

async function doInstall(btn) {
  btn.disabled = true;
  btn.textContent = '安装中...';
  try {
    const res = await post('/napcat/install', {});
    if (res.success) {
      toast('NapCat 安装成功', 'success');
    } else {
      toast(res.message || '安装失败', 'error');
    }
  } catch (e) {
    toast('安装失败: ' + e.message, 'error');
  }
  await loadStatus();
}

async function doStart(btn) {
  const qqInput = $('qqInput');
  const qqNumber = qqInput ? qqInput.value.trim() : '';
  if (!qqNumber) {
    toast('请输入 QQ 号码', 'error');
    return;
  }
  btn.disabled = true;
  btn.textContent = '启动中...';
  try {
    const res = await post('/napcat/start', { qq_number: qqNumber });
    if (res.success) {
      toast('NapCat 已启动', 'success');
    } else {
      toast(res.message || '启动失败', 'error');
    }
  } catch (e) {
    toast('启动失败: ' + e.message, 'error');
  }
  await loadStatus();
}

async function doStop(btn) {
  const qqInput = $('qqInput');
  const qqNumber = qqInput ? qqInput.value.trim() : '';
  if (!qqNumber) {
    toast('请输入 QQ 号码', 'error');
    return;
  }
  btn.disabled = true;
  btn.textContent = '停止中...';
  try {
    const res = await post('/napcat/stop', { qq_number: qqNumber });
    if (res.success) {
      toast('NapCat 已停止', 'success');
    } else {
      toast(res.message || '停止失败', 'error');
    }
  } catch (e) {
    toast('停止失败: ' + e.message, 'error');
  }
  await loadStatus();
}

async function loadLogs() {
  const el = $('napcatLogs');
  try {
    const res = await get('/napcat/logs?lines=50');
    const logs = res.logs || [];
    if (!logs.length) {
      el.innerHTML = '<div style="color:var(--text-3);font-size:13px">暂无日志</div>';
      return;
    }
    el.innerHTML = `
      <pre style="background:var(--surface-2,#1a1a2e);padding:12px;border-radius:8px;font-size:12px;font-family:var(--font-mono);overflow-x:auto;max-height:400px;overflow-y:auto;color:var(--text-2);white-space:pre-wrap;word-break:break-all">${logs.join('\n')}</pre>
    `;
  } catch {
    el.innerHTML = '<div style="color:var(--text-3);font-size:13px">日志加载失败</div>';
  }
}
