import { store } from './store.js';

const THEMES = [
  { id: 'dark', label: '暗色', icon: '🌙' },
  { id: 'light', label: '亮色', icon: '☀️' },
  { id: 'midnight', label: '午夜蓝', icon: '🌊' },
  { id: 'forest', label: '森林绿', icon: '🌿' },
  { id: 'sakura', label: '樱花粉', icon: '🌸' },
];

export function getThemes() { return THEMES; }

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

export function initTheme() {
  applyTheme(store.theme);
}
