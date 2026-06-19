const MEMORY_PAGES = [
  { id: 'memory-dashboard', label: '神经中枢', icon: '🧬', layer: 'Hub' },
  { id: 'evolution-chain', label: '演化链', icon: '🧫', layer: 'L0-L2' },
  { id: 'biography-view', label: '基因图谱', icon: '🧮', layer: 'L0' },
  { id: 'diary', label: '日记', icon: '◫', layer: 'L3' },
];

let navStyleInjected = false;

function injectNavStyle() {
  if (navStyleInjected) return;
  navStyleInjected = true;
  const style = document.createElement('style');
  style.textContent = `
    .mem-neural-nav {
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      align-items: center;
      gap: 0;
      padding: 8px 20px;
      background: rgba(6, 8, 13, 0.92);
      backdrop-filter: blur(16px);
      border: 1px solid rgba(0, 255, 200, 0.12);
      border-radius: 28px;
      z-index: 90;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.5), 0 0 20px rgba(0, 255, 200, 0.06);
      transition: opacity 0.4s, transform 0.4s;
    }
    .mem-neural-nav-item {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 14px;
      border-radius: 18px;
      font-size: 12px;
      color: var(--text-3);
      cursor: pointer;
      transition: all 0.3s;
      white-space: nowrap;
      position: relative;
      background: transparent;
      border: none;
      font-family: inherit;
    }
    .mem-neural-nav-item:hover {
      color: var(--text-1);
      background: rgba(0, 255, 200, 0.06);
    }
    .mem-neural-nav-item.active {
      color: #00ffc8;
      background: rgba(0, 255, 200, 0.1);
      box-shadow: 0 0 12px rgba(0, 255, 200, 0.1);
    }
    .mem-neural-nav-item .nav-icon {
      font-size: 14px;
    }
    .mem-neural-nav-item .nav-layer {
      font-size: 9px;
      opacity: 0.4;
      font-family: var(--font-mono);
    }
    .mem-neural-connector {
      width: 24px;
      height: 2px;
      position: relative;
      flex-shrink: 0;
    }
    .mem-neural-connector::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, rgba(0,255,200,0.2), transparent);
      border-radius: 1px;
    }
    .mem-neural-connector::after {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: rgba(0,255,200,0.4);
      transform: translate(-50%, -50%);
      box-shadow: 0 0 6px rgba(0,255,200,0.3);
    }
    .mem-param-hint {
      position: fixed;
      top: 80px;
      right: 24px;
      padding: 8px 16px;
      background: rgba(6, 8, 13, 0.92);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(0, 255, 200, 0.15);
      border-radius: 10px;
      font-size: 12px;
      color: #00ffc8;
      z-index: 80;
      display: flex;
      align-items: center;
      gap: 8px;
      animation: mem-hint-in 0.3s ease;
      box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }
    .mem-param-hint-close {
      background: none;
      border: none;
      color: var(--text-3);
      cursor: pointer;
      font-size: 14px;
      padding: 0 0 0 4px;
      line-height: 1;
    }
    .mem-param-hint-close:hover { color: var(--text-1); }
    @keyframes mem-hint-in {
      from { opacity: 0; transform: translateY(-8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .mem-clickable {
      cursor: pointer;
      transition: color 0.2s, text-shadow 0.2s;
    }
    .mem-clickable:hover {
      color: #00ffc8 !important;
      text-shadow: 0 0 8px rgba(0, 255, 200, 0.3);
    }
  `;
  document.head.appendChild(style);
}

export function renderNeuralNav(currentPageId) {
  injectNavStyle();
  const existing = document.querySelector('.mem-neural-nav');
  if (existing) existing.remove();

  const nav = document.createElement('div');
  nav.className = 'mem-neural-nav';

  MEMORY_PAGES.forEach((page, i) => {
    if (i > 0) {
      const connector = document.createElement('div');
      connector.className = 'mem-neural-connector';
      nav.appendChild(connector);
    }
    const btn = document.createElement('button');
    btn.className = `mem-neural-nav-item${page.id === currentPageId ? ' active' : ''}`;
    btn.innerHTML = `<span class="nav-icon">${page.icon}</span><span>${page.label}</span><span class="nav-layer">${page.layer}</span>`;
    btn.addEventListener('click', () => {
      navigateWithParams(page.id);
    });
    nav.appendChild(btn);
  });

  document.body.appendChild(nav);
}

export function removeNeuralNav() {
  const existing = document.querySelector('.mem-neural-nav');
  if (existing) existing.remove();
  const hint = document.querySelector('.mem-param-hint');
  if (hint) hint.remove();
}

const PARAM_KEY = 'mem_nav_params';

export function navigateWithParams(page, params = {}) {
  sessionStorage.setItem(PARAM_KEY, JSON.stringify({ page, params, ts: Date.now() }));
  window.location.hash = page;
}

export function consumeNavParams() {
  try {
    const raw = sessionStorage.getItem(PARAM_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (Date.now() - data.ts > 5000) {
      sessionStorage.removeItem(PARAM_KEY);
      return null;
    }
    sessionStorage.removeItem(PARAM_KEY);
    return data.params || {};
  } catch {
    return null;
  }
}

export function showParamHint(label, onClear) {
  const existing = document.querySelector('.mem-param-hint');
  if (existing) existing.remove();

  const hint = document.createElement('div');
  hint.className = 'mem-param-hint';
  hint.innerHTML = `<span>🔗 联动筛选: ${label}</span>`;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'mem-param-hint-close';
  closeBtn.textContent = '✕';
  closeBtn.addEventListener('click', () => {
    hint.remove();
    if (onClear) onClear();
  });
  hint.appendChild(closeBtn);
  document.body.appendChild(hint);
}

export function makeClickableSubject(el, subject) {
  el.classList.add('mem-clickable');
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    navigateWithParams('evolution-chain', { subject });
  });
}

export function makeClickableTopic(el, topic) {
  el.classList.add('mem-clickable');
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    navigateWithParams('diary', { search: topic });
  });
}

export function makeClickableUser(el, userId, userName) {
  el.classList.add('mem-clickable');
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    navigateWithParams('biography-view', { userId, userName });
  });
}

export function makeClickableStat(el, targetPage) {
  el.style.cursor = 'pointer';
  el.addEventListener('click', () => {
    navigateWithParams(targetPage);
  });
}

const MEMORY_PAGE_IDS = new Set(MEMORY_PAGES.map(p => p.id));

function cleanupOnNavigation() {
  window.addEventListener('hashchange', () => {
    const hash = window.location.hash.slice(1);
    if (!MEMORY_PAGE_IDS.has(hash)) {
      removeNeuralNav();
    }
  });
}

cleanupOnNavigation();
