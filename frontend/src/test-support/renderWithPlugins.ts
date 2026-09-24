/**
 * 组件测试的统一渲染入口：用 `@testing-library/vue` 渲染组件，并预装与 `main.ts`
 * 相同的 PrimeVue 配置（同一份 `primeVueOptions`）、ToastService、ConfirmationService、
 * 全新 Pinia 与内存路由替身，使后续组件测试只按角色、label 与可见文本断言用户结果，
 * 不依赖 PrimeVue 内部 class 或 DOM 层级。视口替身位于 `./viewport`，本模块重新导出
 * `setViewport`，组件测试统一从这里导入。
 *
 * 导入本模块即产生以下测试期副作用（只有测试文件会导入它，生产构建不包含）：
 * - 注册 `@testing-library/jest-dom` 的 Vitest 匹配器（`toBeVisible`、`toHaveFocus` 等）
 *   及其类型声明。
 * - 注册 `afterEach`：先卸载本文件渲染的全部组件，再把 `matchMedia`/`innerWidth` 恢复为
 *   jsdom 原始状态。项目未开启 Vitest globals，Testing Library 自带的自动 cleanup 检测
 *   不到全局 `afterEach` 而不会生效，因此必须在这里显式注册。
 */
import '@testing-library/jest-dom/vitest'

import {
  cleanup,
  render,
  type RenderOptions,
  type RenderResult,
} from '@testing-library/vue'
import { createPinia, type Pinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { afterEach } from 'vitest'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import { primeVueOptions } from '@/design/primevue'

import { installViewport, restoreViewport } from './viewport'

export { setViewport } from './viewport'

/** Testing Library 挂载时的全局选项（plugins、stubs、provide 等）。 */
type GlobalRenderOptions = NonNullable<RenderOptions<unknown>['global']>

/** 组件替身配置，Vue Test Utils 同时接受对象与组件名数组两种写法。 */
type StubRenderOptions = NonNullable<GlobalRenderOptions['stubs']>

/** 对象形式的组件替身：键为组件名，值为 `true`、`false` 或替身组件。 */
type StubRecord = Exclude<StubRenderOptions, string[]>

/**
 * `renderWithPlugins` 的参数：Testing Library 的常规渲染选项（props、slots、
 * `global.plugins`/`global.stubs` 等），外加渲染前的路由位置。
 *
 * 已废弃的 `store`、`routes` 选项被移除：Pinia 与路由统一由本函数创建并返回。
 */
export type RenderWithPluginsOptions<C> = Omit<
  RenderOptions<C>,
  'store' | 'routes'
> & {
  /** 渲染前内存路由导航到的完整路径（可含 query），默认 `/`。 */
  route?: string
}

/** 渲染结果：Testing Library 的查询与工具函数，加上本次安装的路由与 Pinia。 */
export type RenderWithPluginsResult = RenderResult & {
  /** 本次渲染安装的内存路由，可断言当前位置或继续 `push`。 */
  router: Router
  /** 本次渲染安装的全新 Pinia，可配合 `useXxxStore(pinia)` 读写状态。 */
  pinia: Pinia
}

/**
 * 不替换 `<transition>`/`<transition-group>`。Vue Test Utils 默认把它们换成立即渲染的
 * 替身，而 PrimeVue 4.5.5 的 Dialog/Drawer 正是在过渡钩子里完成关键行为：`onEnter`
 * 记录触发元素并绑定 document 级 Escape 监听，`onAfterEnter` 把焦点移入浮层，`onLeave`
 * 把焦点还给触发元素。替身会让这些钩子全部不执行，焦点陷阱、Esc 关闭与焦点返回的测试
 * 将形同虚设。jsdom 中真实过渡在下一动画帧结束，测试需用 `findBy*`/`waitFor` 等待。
 *
 * 另注意：PrimeVue 只识别 `event.code === 'Escape'`，测试派发的键盘事件必须同时携带
 * `code`（例如 `{ key: 'Escape', code: 'Escape' }`），只带 `key` 的事件会被静默忽略。
 */
const REAL_TRANSITIONS: StubRecord = {
  transition: false,
  'transition-group': false,
}

/**
 * 命中任意路径的空页面替身：被测组件内的 `RouterView` 不会加载真实页面及其 API 调用。
 * `RouterLink` 的 active/exact-active 仍按路径参数比较，`aria-current` 可以直接断言。
 */
const RouteStub = defineComponent({ name: 'RouteStub', render: () => null })

/**
 * @returns 每次渲染独立的内存路由，只含一条 catch-all 记录，不共享导航历史。
 */
function createStubRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: RouteStub }],
  })
}

/**
 * 把调用方的替身配置统一为对象形式，便于与 {@link REAL_TRANSITIONS} 合并。
 *
 * @param stubs 调用方传入的替身配置，可能缺省或为组件名数组。
 * @returns 对象形式的替身配置；数组中的每个组件名等价于 `true`。
 */
function toStubRecord(stubs: StubRenderOptions | undefined): StubRecord {
  if (stubs === undefined) return {}
  if (Array.isArray(stubs)) {
    return Object.fromEntries(stubs.map((name): [string, boolean] => [name, true]))
  }
  return stubs
}

/**
 * 渲染组件并安装应用级插件，供所有组件测试复用。
 *
 * 插件顺序与 `main.ts` 一致：Pinia、PrimeVue（`primeVueOptions`）、ToastService、
 * ConfirmationService、路由，调用方的 `global.plugins` 追加在其后；调用方的 `global.stubs`
 * 与 {@link REAL_TRANSITIONS} 合并且优先。PrimeVue 的 DatePicker、Select 等组件在
 * `mounted` 中直接调用 `matchMedia`，因此未调用 `setViewport` 时会按 jsdom 默认宽度
 * （1024px）安装视口替身，测试结束后自动恢复。
 *
 * @param component 被测组件。
 * @param options 常规渲染选项与可选的初始路由位置。
 * @returns 渲染结果，以及本次安装的路由与 Pinia。
 * @throws 路由导航被拒绝或组件挂载失败时，原样抛出对应错误，不静默吞掉。
 */
export async function renderWithPlugins<C>(
  component: C,
  options: RenderWithPluginsOptions<C> = {},
): Promise<RenderWithPluginsResult> {
  const { route = '/', global: callerGlobal = {}, ...renderOptions } = options
  const pinia = createPinia()
  const router = createStubRouter()
  // 先完成导航再挂载，组件首次渲染即看到目标路由，避免初始位置 `/` 的闪现干扰断言。
  await router.push(route)
  await router.isReady()
  installViewport()
  const result = render(component, {
    ...renderOptions,
    global: {
      ...callerGlobal,
      plugins: [
        pinia,
        [PrimeVue, primeVueOptions],
        ToastService,
        ConfirmationService,
        router,
        ...(callerGlobal.plugins ?? []),
      ],
      stubs: { ...REAL_TRANSITIONS, ...toStubRecord(callerGlobal.stubs) },
    },
  })
  return { ...result, router, pinia }
}

afterEach(() => {
  // 先卸载组件，让组件自行移除媒体查询与 document 监听，再恢复 window 属性；
  // `./viewport` 自身注册的恢复钩子是幂等的，与本钩子的执行先后无关。
  cleanup()
  restoreViewport()
})
