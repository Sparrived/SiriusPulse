import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

// 构建产物随 Python 包提交（sirius_pulse/webui/app），源码用户与 Docker 镜像都无需 Node。
// 开发时把 API / WebSocket / AMKR 面板反代到本地运行中的 WebUI。
const backend = process.env.SIRIUS_WEBUI_URL ?? 'http://127.0.0.1:8080';

export default defineConfig({
  base: '/app/',
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: fileURLToPath(new URL('../sirius_pulse/webui/app', import.meta.url)),
    emptyOutDir: true,
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (
            id.includes('node_modules/three') ||
            id.includes('@react-three') ||
            id.includes('postprocessing')
          ) {
            return 'world';
          }
          if (id.includes('node_modules/react') || id.includes('node_modules/scheduler')) return 'react';
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5180,
    proxy: {
      '/api': backend,
      '/amkr': backend,
      '/ws': { target: backend.replace(/^http/, 'ws'), ws: true },
    },
  },
});
