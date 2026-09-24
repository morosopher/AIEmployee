/**
 * PrimeVue 全局配置：styled 模式 + 定制 Aura 预设、完整 zh-CN locale、关闭 ripple，
 * 并把组件样式放进位于 Tailwind `utilities` 之前的 `primevue` 级联层。
 *
 * 组件经 unplugin-vue-components 按需注册，这里不做任何 `app.component` 全局注册。
 */
import type { PrimeVueConfiguration } from 'primevue/config'

import { zhCNLocale } from './locale'
import { appPreset } from './tokens'

/**
 * 全站级联层顺序，与 `app.css` 顶部的 `@layer` 声明逐字一致。
 *
 * PrimeVue 首个组件挂载时用 `document.head.prepend` 注入本顺序，它因此成为文档中的
 * 第一条层声明并决定最终优先级：Tailwind 的 `@property` 兜底默认值（properties）<
 * 设计变量（theme）< 预检（base）< PrimeVue 组件样式 < 自定义组件层 < 工具类。
 * 未列出的层会按首次出现追加到 `utilities` 之后：省略 `components` 会让它压过工具类，
 * 省略 `properties` 会让旧浏览器兜底的 `--tw-*` 初始值覆盖工具类设置的变量。
 * tailwindcss-primeui 只含 `@keyframes` 的 `keyframes` 层不参与声明优先级，保持追加在最后。
 */
const CASCADE_LAYER_ORDER =
  'properties, theme, base, primevue, components, utilities'

/**
 * 读取服务端下发的 CSP nonce，供 styled 模式注入的 `<style>` 使用。
 *
 * M2.1 不配置 CSP，也不在页面中放置该 meta；M2.2 若启用 CSP，由服务端在
 * `<meta name="csp-nonce">` 写入每次响应的 nonce，PrimeVue 注入的主题样式随之放行，
 * 不得改用 `unsafe-inline`。nonce 只转交给 PrimeVue，不记录、不持久化。
 *
 * @param root 要查询的文档或节点，测试可注入 jsdom 文档。
 * @returns 去除首尾空白后的 nonce；meta 缺失或内容为空时返回 `undefined`，
 *   以免 PrimeVue 写出空的 `nonce` 属性。
 */
export function resolveCspNonce(root: ParentNode): string | undefined {
  const content = root
    .querySelector('meta[name="csp-nonce"]')
    ?.getAttribute('content')
    ?.trim()
  return content ? content : undefined
}

/**
 * 应用唯一的 PrimeVue 安装参数，由 `main.ts` 与测试辅助 `renderWithPlugins` 共用。
 *
 * - `theme.options.darkModeSelector: false`：Aura 默认跟随系统切换暗色，M2.1 固定
 *   浅色主题，关闭后只输出浅色变量（PrimeVue 同时声明 `color-scheme: light`）。
 * - `cssLayer`：见 {@link CASCADE_LAYER_ORDER}。
 * - `csp.nonce`：模块加载时读取一次 meta；SPA 生命周期内 nonce 不变。
 */
export const primeVueOptions = {
  theme: {
    preset: appPreset,
    options: {
      prefix: 'p',
      darkModeSelector: false,
      cssLayer: {
        name: 'primevue',
        order: CASCADE_LAYER_ORDER,
      },
    },
  },
  locale: zhCNLocale,
  ripple: false,
  inputVariant: 'outlined',
  unstyled: false,
  csp: { nonce: resolveCspNonce(document) },
} satisfies PrimeVueConfiguration
