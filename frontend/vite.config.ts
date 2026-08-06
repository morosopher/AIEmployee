import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 开发服务器只负责把同源浏览器请求转发给 API；生产流量仍由 Caddy 代理。
// 默认目标覆盖宿主上的 Playwright/本地开发，Compose 开发覆盖层通过 API_BASE_URL
// 显式改为内部服务名，避免容器内把 127.0.0.1 错当成自身。
const apiProxyTarget = process.env.API_BASE_URL ?? 'http://127.0.0.1:8000'

/** 为浏览器运行与 Vitest 统一 Vue 编译、别名及 DOM 测试环境。 */
export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: false,
      },
    },
  },
  test: { environment: 'jsdom', exclude: ['**/node_modules/**', '**/e2e/**'] },
})
