export function toast(msg, type = 'success') {
  const container = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

export function animateNumber(el, target, duration = 600) {
  if (!el) return;
  const start = parseInt(el.textContent.replace(/,/g, '') || '0', 10) || 0;
  if (start === target) return;
  const startTime = performance.now();
  function tick(now) {
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(start + (target - start) * eased).toLocaleString();
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

export function flashSuccess(btn) {
  if (!btn) return;
  const prev = btn.textContent;
  btn.classList.add('btn-success-flash');
  btn.textContent = '✓ ' + prev;
  btn.disabled = true;
  setTimeout(() => { btn.classList.remove('btn-success-flash'); btn.textContent = prev; btn.disabled = false; }, 1200);
}

export function applyStagger(container, childSelector) {
  if (!container) return;
  container.classList.add('animate-stagger');
  const children = childSelector ? container.querySelectorAll(childSelector) : container.children;
  Array.from(children).forEach((child, i) => child.style.setProperty('--i', String(i)));
}

export function showLoginOverlay() {
  const overlay = document.getElementById('loginOverlay');
  overlay.style.display = 'flex';
  overlay.innerHTML = `
    <div class="login-card">
      <div style="font-size:28px;margin-bottom:8px">✦</div>
      <h2 style="font-family:var(--font-display);font-size:22px;margin-bottom:4px;color:var(--text-1)">Sirius Pulse</h2>
      <p style="font-size:13px;color:var(--text-2);margin-bottom:24px">请输入管理员密码以访问控制台</p>
      <div class="form-group">
        <label>密码</label>
        <input id="loginPassword" type="password" placeholder="输入密码" autofocus>
      </div>
      <div id="loginError" style="color:var(--danger);font-size:12px;margin-bottom:12px;display:none"></div>
      <button id="loginBtn" class="btn btn-primary" style="width:100%">登录</button>
    </div>
  `;
  const pwInput = document.getElementById('loginPassword');
  const loginBtn = document.getElementById('loginBtn');
  
  async function doLogin() {
    const password = pwInput.value;
    const errEl = document.getElementById('loginError');
    if (!password) { errEl.textContent = '请输入密码'; errEl.style.display = ''; return; }
    try {
      const { post, setToken } = await import('./api.js');
      const data = await post('/auth/login', { username: 'admin', password });
      if (data.success && data.token) {
        setToken(data.token);
        overlay.style.display = 'none';
        toast('登录成功');
        window.dispatchEvent(new CustomEvent('auth:login'));
      } else {
        errEl.textContent = data.error || '登录失败';
        errEl.style.display = '';
      }
    } catch (e) {
      errEl.textContent = '网络错误';
      errEl.style.display = '';
    }
  }
  
  loginBtn.onclick = doLogin;
  pwInput.onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
  setTimeout(() => pwInput.focus(), 100);
}

export function hideLoginOverlay() {
  document.getElementById('loginOverlay').style.display = 'none';
}

export function formatHeartbeat(ts) {
  if (!ts) return '—';
  const diff = (Date.now() - new Date(ts)) / 1000;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  return new Date(ts).toLocaleString('zh-CN');
}

export function statCard(label, value, detail = '', icon = '') {
  return `
    <div class="stat-card">
      <div class="stat-label">${icon ? `<span>${icon}</span>` : ''}${label}</div>
      <div class="stat-value">${value}</div>
      ${detail ? `<div class="stat-detail">${detail}</div>` : ''}
    </div>
  `;
}

export const $ = (id) => document.getElementById(id);
export const $$ = (sel, root = document) => root.querySelectorAll(sel);
