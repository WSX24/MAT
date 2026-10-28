import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * 课堂前端（Vue 3 + TypeScript + Vite）。
 *
 * 构建产物 `dist/` 入库，由 FastAPI（`app.py`）以 `GET /` 与 `/assets/*` 提供：
 *  - 静态资源必须落在 `/assets/`（后端只服务这一个前缀），所以图片/视频一律
 *    走 `import` 交给 Vite 指纹化，**不放 public/**（根路径的 public 文件后端取不到）。
 *  - 深链（/app/courses/...、/app/units/.../outline）由后端 SPA 兜底返回 index.html。
 */
export default defineConfig({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    target: 'es2020',
    // 5.7MB 的首屏影片不进 base64 内联，保持独立文件便于浏览器缓存
    assetsInlineLimit: 4096,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
})
