import { fileURLToPath, URL } from 'node:url'

import { PrimeVueResolver } from '@primevue/auto-import-resolver'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { defineConfig } from 'vitest/config'

// 开发服务器只负责把同源浏览器请求转发给 API；生产流量仍由 Caddy 代理。
// 默认目标覆盖宿主上的 Playwright/本地开发，Compose 开发覆盖层通过 API_BASE_URL
// 显式改为内部服务名，避免容器内把 127.0.0.1 错当成自身。
const apiProxyTarget = process.env.API_BASE_URL ?? 'http://127.0.0.1:8000'

/**
 * 为浏览器运行与 Vitest 统一 Vue 编译、Tailwind、组件按需注册、别名及 DOM 测试环境。
 *
 * - `@tailwindcss/vite` 在构建期生成工具类，样式随产物本地打包，不引用 CDN。
 * - `unplugin-vue-components` 只解析 PrimeVue 组件（`dirs: []`），本地
 *   `src/components` 继续显式 import；Vitest 复用同一插件链，组件测试与构建一致。
 *   生成的 `components.d.ts` 只是自动导入登记表（已加入 .gitignore）；模板类型检查依赖
 *   PrimeVue 经 `primevue/config` 引入的 GlobalComponents 声明，不依赖该文件。
 * - `build.reportCompressedSize` 让构建日志输出 gzip 体积，供体积门禁与 CI 留档。
 * - `test.css.include`：Vitest 默认把未列入的样式模块（连同 `?raw` 查询）替换为空字符串；
 *   仅放行全局样式入口 `src/design/app.css`，使 `primevue.spec.ts` 能以 `?raw` 读取原文，
 *   核对其中的级联层顺序与 PrimeVue `cssLayer.order` 一致。其他样式仍按默认忽略。
 */
export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    Components({
      dirs: [],
      resolvers: [PrimeVueResolver()],
      dts: 'components.d.ts',
    }),
  ],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: false,
      },
    },
  },
  build: { reportCompressedSize: true },
  test: {
    environment: 'jsdom',
    exclude: ['**/node_modules/**', '**/e2e/**'],
    css: { include: [/\/src\/design\/app\.css(?:\?|$)/] },
  },
})
