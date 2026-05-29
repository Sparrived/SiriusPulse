import { store, setState } from './store.js';
import { get, post, put, del, setToken, clearToken, getToken } from './api.js';
export { get, post, put, del, setToken, clearToken, getToken };
import { initTheme, applyTheme, getThemes } from './theme.js';
import { wsConnect } from './ws.js';
import { toast, formatHeartbeat, $ } from './components.js';

const PAGE_META = {
  'dashboard': { title: '概览', breadcrumb: 'Dashboard', icon: '◈' },
  'global-settings': { title: '全局设置', breadcrumb: 'Configuration / Global', icon: '⚙' },
  'providers': { title: 'Provider', breadcrumb: 'Configuration / Providers', icon: '⬡' },
  'create-persona': { title: '新建人格', breadcrumb: 'Configuration / Create', icon: '＋' },
  'persona': { title: '人格配置', breadcrumb: 'Configuration / Persona', icon: '◎' },
  'orchestration': { title: '模型编排', breadcrumb: 'Configuration / Orchestration', icon: '⧉' },
  'experience': { title: '体验参数', breadcrumb: 'Configuration / Experience', icon: '◇' },
  'adapters': { title: '适配器', breadcrumb: 'Configuration / Adapters', icon: '⟐' },
  'skills': { title: 'Skills', breadcrumb: 'Extensions / Skills', icon: '⏣' },
  'token-tracker': { title: 'Token 追踪', breadcrumb: 'Analytics / Tokens', icon: '△' },
  'cognition': { title: '认知分析', breadcrumb: 'Analytics / Cognition', icon: '◎' },
  'skills-tracker': { title: 'Skill 追踪', breadcrumb: 'Analytics / Skills', icon: '⟠' },
  'conversation-history': { title: '对话分析', breadcrumb: 'Analytics / Conversations', icon: '◧' },
  'diary': { title: '日记', breadcrumb: 'Memory / Diary', icon: '◫' },
  'users': { title: '用户档案', breadcrumb: 'Memory / Users', icon: '◐' },
  'glossary': { title: '名词解释', breadcrumb: 'Memory / Glossary', icon: '◱' },
  'memory-viz': { title: '记忆可视化', breadcrumb: 'Memory / Visualization', icon: '◲' },
  'plugins': { title: '插件', breadcrumb: 'Extensions / Plugins', icon: '⬡' },
};

const NAV_GROUPS = [
  { label: '仪表盘', items: [{ page: 'dashboard', icon: '◈', label: '概览' }] },
  { label: '全局', items: [
    { page: 'global-settings', icon: '⚙', label: '全局设置' },
    { page: 'providers', icon: '⬡', label: 'Provider' },
  ]},
  { label: '人格配置', items: [
    { page: 'create-persona', icon: '＋', label: '新建人格' },
    { page: 'persona', icon: '◎', label: '人格' },
    { page: 'orchestration', icon: '⧉', label: '模型编排' },
    { page: 'experience', icon: '◇', label: '体验参数' },
    { page: 'adapters', icon: '⟐', label: 'Adapter' },
  ]},
  { label: '分析', items: [
    { page: 'token-tracker', icon: '△', label: 'Token 追踪' },
    { page: 'cognition', icon: '◎', label: '认知分析' },
    { page: 'skills-tracker', icon: '⟠', label: 'Skill 追踪' },
    { page: 'conversation-history', icon: '◧', label: '对话分析' },
  ]},
  { label: '记忆', items: [
    { page: 'diary', icon: '◫', label: '日记' },
    { page: 'users', icon: '◐', label: '用户档案' },
    { page: 'glossary', icon: '◱', label: '名词解释' },
    { page: 'memory-viz', icon: '◲', label: '记忆可视化' },
  ]},
  { label: '扩展', items: [
    { page: 'skills', icon: '⏣', label: 'Skills' },
    { page: 'plugins', icon: '⬡', label: '插件' },
  ]},
];

const PERSONA_PAGES = new Set([
  'persona', 'orchestration', 'experience', 'adapters', 'skills',
  'token-tracker', 'cognition', 'skills-tracker', 'conversation-history',
  'diary', 'users', 'glossary', 'memory-viz', 'create-persona',
]);

let currentPage = '';
let pageModules = {};

window.navTo = navTo;
window.selectPersona = selectPersona;

export function pApi(path) {
  const name = store.currentPersona;
  return name ? `/personas/${name}${path}` : path;
}

async function loadPageModule(page) {
  if (pageModules[page]) return pageModules[page];
  try {
    const mod = await import(`./pages/${page}.js`);
    pageModules[page] = mod;
    return mod;
  } catch {
    return null;
  }
}

export async function navTo(page, name) {
  if (name) store.currentPersona = name;

  currentPage = page;

  const nav = document.getElementById('sidebarNav');
  nav.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));

  const meta = PAGE_META[page] || {};
  const themes = getThemes();
  const ct = store.theme || 'dark';
  const ti = themes.find(t => t.id === ct);

  const header = document.getElementById('header');
  const personas = store.personas || [];
  const currentP = store.currentPersona;
  const currentPData = personas.find(p => p.name === currentP);
  const personaLabel = currentPData ? (currentPData.persona_name || currentPData.name) : '选择人格';
  const personaIcon = currentPData ? '◎' : '○';

  header.innerHTML = `
    <div class="header-left">
      <div class="persona-header-dropdown">
        <button class="persona-header-btn" id="personaHeaderBtn">${personaIcon} ${personaLabel} <span class="persona-header-arrow">▾</span></button>
        <div class="persona-header-list" id="personaHeaderList">
          ${personas.length === 0
            ? '<div class="persona-header-empty">暂无人格</div>'
            : personas.map(p => `
              <div class="persona-header-option${p.name === currentP ? ' active' : ''}" data-name="${p.name}">
                <span class="persona-header-dot${p.running ? ' running' : ''}"></span>
                <span class="persona-header-name">${p.persona_name || p.name}</span>
              </div>
            `).join('')}
        </div>
      </div>
      <h1 class="header-title">${meta.title || ''}</h1>
      <span class="header-breadcrumb">${meta.breadcrumb || ''}</span>
    </div>
    <div class="header-right">
      <div class="theme-dropdown">
        <button class="theme-btn" id="themeBtn">${ti?.icon || '🌙'} ${ti?.label || '暗色'}</button>
        <div class="theme-dropdown-list" id="themeList">
          ${themes.map(t => `<div class="theme-option${t.id === ct ? ' active' : ''}" data-theme="${t.id}">${t.icon} ${t.label}</div>`).join('')}
        </div>
      </div>
    </div>
  `;
  setupThemeDropdown();
  setupPersonaHeaderDropdown();

  const main = document.getElementById('main');
  main.innerHTML = '<div class="page-loading">加载中…</div>';
  main.classList.remove('page-enter');
  void main.offsetWidth;
  main.classList.add('page-enter');

  try {
    const res = await fetch(`/static/pages/${page}.html`);
    if (res.ok) {
      main.innerHTML = await res.text();
    } else {
      main.innerHTML = '<div class="card" id="pageContent"></div>';
    }
  } catch {
    main.innerHTML = '<div class="card" id="pageContent"></div>';
  }

  const mod = await loadPageModule(page);
  if (mod) {
    const initFn = mod.default || mod.init;
    if (initFn) await initFn(main);
  }
}

function setupThemeDropdown() {
  const btn = document.getElementById('themeBtn');
  const list = document.getElementById('themeList');
  if (!btn || !list) return;
  btn.onclick = e => { e.stopPropagation(); list.classList.toggle('open'); };
  list.querySelectorAll('.theme-option').forEach(opt => {
    opt.onclick = () => {
      applyTheme(opt.dataset.theme);
      list.classList.remove('open');
      const t = getThemes().find(t => t.id === opt.dataset.theme);
      btn.textContent = `${t?.icon || ''} ${t?.label || ''}`;
      list.querySelectorAll('.theme-option').forEach(o => o.classList.toggle('active', o.dataset.theme === opt.dataset.theme));
    };
  });
  document.addEventListener('click', () => list.classList.remove('open'));
}

function setupPersonaHeaderDropdown() {
  const btn = document.getElementById('personaHeaderBtn');
  const list = document.getElementById('personaHeaderList');
  if (!btn || !list) return;

  btn.onclick = e => { e.stopPropagation(); list.classList.toggle('open'); };

  list.querySelectorAll('.persona-header-option').forEach(opt => {
    opt.onclick = async () => {
      const name = opt.dataset.name;
      const persona = (store.personas || []).find(p => p.name === name);
      list.classList.remove('open');

      btn.textContent = `◎ ${persona?.persona_name || name} ▾`;
      list.querySelectorAll('.persona-header-option').forEach(o => o.classList.toggle('active', o.dataset.name === name));

      await selectPersona(name);
      if (PERSONA_PAGES.has(currentPage)) {
        navTo(currentPage, name);
      }
    };
  });

  document.addEventListener('click', () => list.classList.remove('open'));
}

function renderSidebar() {
  const nav = document.getElementById('sidebarNav');
  nav.innerHTML = NAV_GROUPS.map(group => `
    <div class="nav-group">
      <div class="nav-group-label">${group.label}</div>
      ${group.items.map(item => `
        <button class="nav-item${currentPage === item.page ? ' active' : ''}" data-page="${item.page}">
          <span class="nav-icon">${item.icon}</span>
          <span>${item.label}</span>
        </button>
      `).join('')}
    </div>
  `).join('');

  nav.querySelectorAll('.nav-item').forEach(btn => {
    btn.onclick = () => navTo(btn.dataset.page);
  });
}

function renderSidebarFooter() {
  const footer = document.getElementById('sidebarFooter');
  const personas = store.personas || [];
  const running = personas.filter(p => p.running).length;
  footer.innerHTML = `
    <div class="footer-row"><span>人格</span><span class="text-mono">${personas.length}</span></div>
    <div class="footer-row"><span><span class="status-dot${running > 0 ? ' running' : ''}"></span> 运行中</span><span class="text-mono">${running}</span></div>
    <div class="footer-row"><span><span class="status-dot" id="wsDot"></span> WS</span><span id="wsStatus">—</span></div>
  `;
}

export async function selectPersona(name) {
  store.currentPersona = name;
  try { store.personaState = await get(`/personas/${name}/status`); } catch {}
  window.dispatchEvent(new CustomEvent('persona:focus', { detail: name }));
  if (PERSONA_PAGES.has(currentPage)) {
    navTo(currentPage, name);
  }
}

async function loadPersonas() {
  try {
    const res = await get('/personas');
    store.personas = res.personas || [];
    renderSidebarFooter();
    if (!store.currentPersona && store.personas.length > 0) {
      selectPersona(store.personas[0].name);
    }
  } catch {}
}

async function init() {
  initTheme();
  renderSidebar();
  renderSidebarFooter();

  window.addEventListener('auth:expired', () => {
    window.location.href = '/static/login.html';
  });
  window.addEventListener('auth:login', async () => {
    await loadPersonas();
    navTo('dashboard');
  });

  window.addEventListener('ws:connected', () => {
    const dot = document.getElementById('wsDot');
    const text = document.getElementById('wsStatus');
    if (dot) dot.className = 'status-dot running';
    if (text) text.textContent = '已连接';
  });
  window.addEventListener('ws:disconnected', () => {
    const dot = document.getElementById('wsDot');
    const text = document.getElementById('wsStatus');
    if (dot) { dot.className = 'status-dot'; dot.style.background = 'var(--warn)'; }
    if (text) text.textContent = '重连中';
  });

  const token = getToken();
  if (!token) {
    window.location.href = '/static/login.html';
    return;
  } else {
    await loadPersonas();
    navTo('dashboard');
    wsConnect();
  }

  setInterval(async () => { if (getToken()) await loadPersonas(); }, 8000);
}

init();
