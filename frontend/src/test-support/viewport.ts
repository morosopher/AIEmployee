/**
 * 组件测试的视口替身：jsdom 没有实现 `window.matchMedia`，这里按给定宽度模拟媒体查询，
 * 供响应式布局测试与在 `mounted` 中直接调用 `matchMedia` 的 PrimeVue 组件（DatePicker、
 * Select 等）使用。
 *
 * 导入本模块即注册 `afterEach`，在每个测试结束后把 `matchMedia`/`innerWidth` 恢复为
 * jsdom 原始状态；项目未开启 Vitest globals，因此由模块显式注册。组件测试通常经
 * `renderWithPlugins` 使用本模块，该模块会重新导出 {@link setViewport}。
 */
import { afterEach } from 'vitest'

/** 按 16px 根字号换算 `rem`/`em`，与浏览器默认值及 Tailwind 断点定义一致。 */
const ROOT_FONT_SIZE_PX = 16

/** 可求值的宽度条件，如 `(min-width: 768px)`、`(max-width: 767.9px)`、`(min-width: 48rem)`。 */
const WIDTH_CONDITION =
  /^\(\s*(min|max)-width\s*:\s*(\d+(?:\.\d+)?|\.\d+)(px|rem|em)\s*\)$/

/**
 * 求值单个媒体条件：`all`/`screen` 媒体类型成立，其余媒体类型（如 `print`）不成立；
 * 宽度条件按当前视口比较，边界值包含在内。其他媒体特性（`prefers-reduced-motion`、
 * `orientation`、范围语法等）一律视为不成立，避免测试环境凭空声称具备某种设备能力。
 *
 * @param condition 已小写化、去除首尾空白的单个条件。
 * @param width 当前视口宽度（px）。
 * @returns 条件是否成立。
 */
function evaluateCondition(condition: string, width: number): boolean {
  if (condition === 'all' || condition === 'screen') return true
  const match = WIDTH_CONDITION.exec(condition)
  if (!match) return false
  const [, bound, value, unit] = match
  const pixels = Number(value) * (unit === 'px' ? 1 : ROOT_FONT_SIZE_PX)
  return bound === 'min' ? width >= pixels : width <= pixels
}

/**
 * 按 Media Queries 语法的受限子集求值：逗号分隔的查询任一成立即成立；单个查询可带
 * `not`/`only` 前缀，`not` 否定整个查询；以 `and` 连接的条件必须全部成立。覆盖
 * `@vueuse/core` 的 `useBreakpoints`/`useMediaQuery` 生成的小数与组合查询，例如
 * `(min-width: 768px) and (max-width: 1279.9px)`。
 *
 * @param mediaQueryList `matchMedia` 收到的原始查询字符串。
 * @param width 当前视口宽度（px）。
 * @returns 查询是否匹配；空查询按规范匹配全部视口。
 */
function evaluateMediaQueryList(mediaQueryList: string, width: number): boolean {
  if (mediaQueryList.trim() === '') return true
  return mediaQueryList.split(',').some((rawQuery) => {
    let query = rawQuery.trim().toLowerCase()
    const negated = query.startsWith('not ')
    if (negated) query = query.slice('not '.length)
    else if (query.startsWith('only ')) query = query.slice('only '.length)
    const matches = query
      .split(/\s+and\s+/)
      .every((condition) => evaluateCondition(condition.trim(), width))
    return negated ? !matches : matches
  })
}

/** 视口宽度变化时派发给媒体查询监听者的事件，结构与浏览器 `MediaQueryListEvent` 一致。 */
class ViewportChangeEvent extends Event implements MediaQueryListEvent {
  readonly media: string
  readonly matches: boolean

  /**
   * @param media 触发事件的媒体查询字符串。
   * @param matches 变化后的匹配结果。
   */
  constructor(media: string, matches: boolean) {
    super('change')
    this.media = media
    this.matches = matches
  }
}

/** lib.dom 中 `MediaQueryList` 的 `onchange`/`addListener` 回调签名。 */
type ChangeCallback = (this: MediaQueryList, event: MediaQueryListEvent) => unknown

/**
 * `window.matchMedia` 返回值的替身：继承 jsdom 的 `EventTarget`，使监听器的去重、
 * `once`/`passive` 选项与移除语义和浏览器一致；只在匹配结果真正改变时派发 `change`。
 */
class ViewportMediaQueryList extends EventTarget implements MediaQueryList {
  readonly media: string
  onchange: ChangeCallback | null = null
  #matches: boolean
  /** 旧式回调到实际 `change` 监听器的映射，保证 removeListener 能移除同一登记。 */
  readonly #legacyListeners = new Map<ChangeCallback, EventListener>()

  /**
   * @param media 原始媒体查询字符串，按浏览器行为原样保留。
   * @param width 创建时的视口宽度（px）。
   */
  constructor(media: string, width: number) {
    super()
    this.media = media
    this.#matches = evaluateMediaQueryList(media, width)
    // 与浏览器一样，`onchange` 属性处理器与 addEventListener 注册的监听器收到同一事件。
    this.addEventListener('change', (event) => {
      if (event instanceof ViewportChangeEvent) this.onchange?.call(this, event)
    })
  }

  /** 当前视口下查询是否匹配。 */
  get matches(): boolean {
    return this.#matches
  }

  /**
   * 旧式监听接口，PrimeVue 与部分库仍在使用。回调按 lib.dom 签名接收
   * `MediaQueryListEvent`，因此包装为只转发 {@link ViewportChangeEvent} 的 `change`
   * 监听器登记；同一回调重复登记只生效一次，须用 `removeListener` 移除。
   *
   * @param callback 变化回调；`null` 时忽略。
   */
  addListener(callback: ChangeCallback | null): void {
    if (!callback || this.#legacyListeners.has(callback)) return
    const listener: EventListener = (event) => {
      if (event instanceof ViewportChangeEvent) callback.call(this, event)
    }
    this.#legacyListeners.set(callback, listener)
    this.addEventListener('change', listener)
  }

  /**
   * 旧式移除接口：移除 `addListener` 为该回调登记的包装监听器。
   *
   * @param callback 之前注册的回调；`null` 或未登记时忽略。
   */
  removeListener(callback: ChangeCallback | null): void {
    const listener = callback ? this.#legacyListeners.get(callback) : undefined
    if (!callback || !listener) return
    this.#legacyListeners.delete(callback)
    this.removeEventListener('change', listener)
  }

  /**
   * 按新宽度重新求值；结果未变化时不通知，与浏览器只在匹配翻转时派发事件一致。
   *
   * @param width 新的视口宽度（px）。
   */
  updateWidth(width: number): void {
    const matches = evaluateMediaQueryList(this.media, width)
    if (matches === this.#matches) return
    this.#matches = matches
    this.dispatchEvent(new ViewportChangeEvent(this.media, matches))
  }
}

/** 视口替身的运行状态，以及安装前 `window` 上的原始属性描述符（用于逐项恢复）。 */
interface ViewportState {
  width: number
  readonly originalMatchMedia: PropertyDescriptor | undefined
  readonly originalInnerWidth: PropertyDescriptor | undefined
  readonly queries: Set<ViewportMediaQueryList>
}

/** 当前测试的视口替身；`null` 表示 `window` 仍是 jsdom 原始状态。 */
let viewport: ViewportState | null = null

/**
 * 在 `window` 上安装 `matchMedia` 替身；已安装时直接复用，保证同一测试内的查询对象
 * 都能收到后续宽度变化。
 *
 * @returns 当前视口状态。
 */
function ensureViewport(): ViewportState {
  if (viewport) return viewport
  const state: ViewportState = {
    width: window.innerWidth,
    originalMatchMedia: Object.getOwnPropertyDescriptor(window, 'matchMedia'),
    originalInnerWidth: Object.getOwnPropertyDescriptor(window, 'innerWidth'),
    queries: new Set(),
  }
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList => {
      const list = new ViewportMediaQueryList(query, state.width)
      state.queries.add(list)
      return list
    },
  })
  viewport = state
  return state
}

/**
 * 按当前 `innerWidth`（jsdom 默认 1024px）安装视口替身；已安装时保持现有宽度不变。
 * `renderWithPlugins` 在每次渲染前调用，使在 `mounted` 中直接调用 `matchMedia` 的
 * PrimeVue 组件可以正常挂载。
 */
export function installViewport(): void {
  ensureViewport()
}

/**
 * 恢复 `window` 上被替换的属性：安装前存在则写回原描述符，不存在则删除。
 *
 * @param key 被替换的属性名。
 * @param descriptor 安装前的属性描述符。
 */
function restoreWindowProperty(
  key: 'matchMedia' | 'innerWidth',
  descriptor: PropertyDescriptor | undefined,
): void {
  if (descriptor) Object.defineProperty(window, key, descriptor)
  else Reflect.deleteProperty(window, key)
}

/**
 * 撤销视口替身，使下一个测试（包括不使用本模块的既有测试）看到原始 jsdom 环境。
 * 幂等：尚未安装或已恢复时直接返回，因此多个 `afterEach` 重复调用也安全。
 */
export function restoreViewport(): void {
  if (!viewport) return
  restoreWindowProperty('matchMedia', viewport.originalMatchMedia)
  restoreWindowProperty('innerWidth', viewport.originalInnerWidth)
  viewport.queries.clear()
  viewport = null
}

/**
 * 模拟视口宽度，供响应式布局测试使用：设置 `window.innerWidth`，并让 `matchMedia`
 * 按新宽度求值；此前创建的查询若匹配结果翻转，会向其 `change` 监听者（含旧式
 * `addListener` 与 `onchange`）派发事件，`@vueuse/core` 的 `useBreakpoints`/`useMediaQuery`
 * 因此随之更新。只模拟媒体查询，不派发 `resize` 事件；测试结束后自动恢复。
 *
 * 可在渲染前调用以决定首屏布局，也可在渲染后调用以模拟窗口缩放。
 *
 * @param width 视口宽度（CSS px），允许小数以覆盖 `767.9px` 这类断点边界。
 * @throws RangeError 宽度不是正的有限数时，拒绝构造不可能出现的视口。
 */
export function setViewport(width: number): void {
  if (!Number.isFinite(width) || width <= 0) {
    throw new RangeError(`Viewport width must be a positive finite number: ${width}`)
  }
  const state = ensureViewport()
  // 先更新宽度再通知，监听者在回调中读取 innerWidth 时即可看到新值。
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: width,
  })
  state.width = width
  for (const query of state.queries) query.updateWidth(width)
}

// 每个测试结束后恢复 window；直接导入本模块的测试同样受保护。
afterEach(restoreViewport)
