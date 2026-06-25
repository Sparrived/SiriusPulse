import { store } from './store.js';

const THEMES = [
  { id: 'dark', label: '暗色', icon: '🌙' },
  { id: 'midnight', label: '午夜蓝', icon: '🌊' },
  { id: 'dopamine', label: '多巴胺', icon: '🍬' },
];

const MODES = [
  { id: 'butler', label: '管家', icon: '◈' },
  { id: 'assistant', label: '助手', icon: '◆' },
];

export function getThemes() { return THEMES; }
export function getModes() { return MODES; }

export function applyTheme(id) {
  const html = document.documentElement;
  if (id === 'dark') {
    html.removeAttribute('data-theme');
  } else {
    html.setAttribute('data-theme', id);
  }
  store.theme = id;
  try { localStorage.setItem('sirius-theme', id); } catch {}
}

export function applyMode(id, originEl) {
  if (id === store.mode) return;
  triggerModeTransition(originEl);
  setTimeout(() => {
    const html = document.documentElement;
    html.setAttribute('data-mode', id);
    store.mode = id;
    try { localStorage.setItem('sirius-mode', id); } catch {}
  }, 250);
}

export function initTheme() {
  applyTheme(store.theme);
  const html = document.documentElement;
  html.setAttribute('data-mode', store.mode || 'butler');
}

function triggerModeTransition(originEl) {
  let overlay = document.getElementById('mode-transition-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'mode-transition-overlay';
    document.body.appendChild(overlay);
  }

  // 计算触发源中心坐标
  let cx = window.innerWidth / 2;
  let cy = 0;
  if (originEl) {
    const rect = originEl.getBoundingClientRect();
    cx = rect.left + rect.width / 2;
    cy = rect.top + rect.height / 2;
  }

  overlay.style.setProperty('--cx', cx + 'px');
  overlay.style.setProperty('--cy', cy + 'px');
  overlay.classList.remove('active');
  void overlay.offsetWidth; // force reflow
  overlay.classList.add('active');

  setTimeout(() => {
    overlay.classList.remove('active');
  }, 550);
}
