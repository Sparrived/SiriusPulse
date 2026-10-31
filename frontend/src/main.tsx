// 全局样式必须最先加载：组件样式要能覆盖 .paper 等基础类（例如 position: fixed）。
import './styles/global.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
