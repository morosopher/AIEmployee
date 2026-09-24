import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import Button from 'primevue/button'
import { defaultOptions } from 'primevue/config'
import DatePicker from 'primevue/datepicker'

import { renderWithPlugins } from '@/test-support/renderWithPlugins'

import appCss from './app.css?raw'
import { zhCNLocale } from './locale'
import { primeVueOptions, resolveCspNonce } from './primevue'
import { appPreset } from './tokens'

/** 这些键是格式或单位而非语言文字，中文 locale 可以不含汉字。 */
const NON_LINGUISTIC_KEYS = new Set([
  'fileSizeTypes',
  'firstDayOfWeek',
  'showMonthAfterYear',
  'dateFormat',
  'aria.slideNumber',
])

/**
 * 把 locale 展平为「路径 → 叶子值」，数组整体视为一个叶子以便比较长度与内容。
 *
 * @param value locale 对象或其子对象。
 * @param prefix 当前路径前缀。
 * @returns 按路径排序的叶子条目。
 */
function localeLeaves(value: object, prefix = ''): Array<[string, unknown]> {
  return Object.entries(value)
    .flatMap(([key, child]): Array<[string, unknown]> => {
      const path = prefix ? `${prefix}.${key}` : key
      return child !== null && typeof child === 'object' && !Array.isArray(child)
        ? localeLeaves(child, path)
        : [[path, child]]
    })
    .sort(([left], [right]) => left.localeCompare(right))
}

/**
 * @param text locale 文案。
 * @returns 文案中的 `{0}`、`{page}` 等运行时占位符，翻译时必须原样保留。
 */
function placeholders(text: unknown): string[] {
  return typeof text === 'string' ? (text.match(/\{[^}]+\}/g) ?? []).sort() : []
}

/**
 * 解析单条 `@layer a, b, c` 声明中的层名，忽略空白与结尾分号差异。
 *
 * @param statement 层顺序声明文本。
 * @returns 按声明顺序排列的层名；不是层声明时返回空数组。
 */
function layerNames(statement: string): string[] {
  const match = /^@layer\s+([^;{}]+);?$/.exec(statement.trim())
  return match?.[1] ? match[1].split(',').map((layer) => layer.trim()) : []
}

describe('PrimeVue global configuration', () => {
  let originalHead = ''

  beforeEach(() => {
    originalHead = document.head.innerHTML
  })

  afterEach(() => {
    document.head.innerHTML = originalHead
  })

  it('configures PrimeVue with the Aura-derived preset, zh-CN locale, no ripple, and a CSS layer below utilities', () => {
    expect(primeVueOptions.ripple).toBe(false)
    expect(primeVueOptions.locale?.today).toBe('今天')
    // PrimeVue 用 head.prepend 注入层顺序，它是文档中第一条 @layer 声明；必须列全
    // Tailwind 4 的 properties/theme/base/components/utilities 五层，否则未列出的层会被
    // 追加到 utilities 之后（例如 properties 的 @property 兜底默认值会压过工具类）。
    expect(primeVueOptions.theme.options.cssLayer).toMatchObject({
      name: 'primevue',
      order: 'properties, theme, base, primevue, components, utilities',
    })
  })

  it('stays in styled mode with the app preset, outlined inputs and no dark scheme', () => {
    expect(primeVueOptions.theme.preset).toBe(appPreset)
    expect(primeVueOptions.unstyled).toBe(false)
    expect(primeVueOptions.inputVariant).toBe('outlined')
    // Aura 默认按系统偏好切换暗色；M2.1 固定浅色主题，必须显式关闭。
    expect(primeVueOptions.theme.options.darkModeSelector).toBe(false)
  })

  it('declares the same cascade layer order in app.css before Tailwind is imported', () => {
    const statement = `@layer ${primeVueOptions.theme.options.cssLayer.order};`
    const statementIndex = appCss.indexOf(statement)
    expect(statementIndex).toBeGreaterThanOrEqual(0)
    expect(statementIndex).toBeLessThan(appCss.indexOf('@import "tailwindcss"'))
  })

  it('translates every key of the PrimeVue default locale, including aria labels', () => {
    const english = defaultOptions.locale
    if (!english) throw new Error('PrimeVue default locale is missing')
    const expected = localeLeaves(english)
    const actual = localeLeaves(zhCNLocale)
    expect(actual.map(([path]) => path)).toEqual(expected.map(([path]) => path))
    const englishByPath = new Map(expected)
    for (const [path, value] of actual) {
      const source = englishByPath.get(path)
      expect(typeof value, path).toBe(typeof source)
      expect(placeholders(value), path).toEqual(placeholders(source))
      if (Array.isArray(source)) {
        expect(Array.isArray(value) ? value.length : -1, path).toBe(source.length)
      }
      if (!NON_LINGUISTIC_KEYS.has(path)) {
        const texts = Array.isArray(value) ? value : [value]
        for (const text of texts) expect(String(text), path).toMatch(/\p{Script=Han}/u)
      }
    }
  })

  it('uses Chinese calendar conventions for date pickers', () => {
    expect(zhCNLocale).toMatchObject({
      today: '今天',
      clear: '清除',
      firstDayOfWeek: 1,
      dateFormat: 'yy-mm-dd',
    })
    expect(zhCNLocale.dayNamesMin).toEqual(['日', '一', '二', '三', '四', '五', '六'])
    expect(primeVueOptions.locale).toBe(zhCNLocale)
  })

  it('reads the CSP nonce from the meta tag when present', () => {
    document.head.innerHTML = '<meta name="csp-nonce" content="synthetic-nonce">'
    expect(resolveCspNonce(document)).toBe('synthetic-nonce')
  })

  it('leaves the CSP nonce unset when the meta tag is missing or blank', () => {
    document.head.innerHTML = ''
    expect(resolveCspNonce(document)).toBeUndefined()
    document.head.innerHTML = '<meta name="csp-nonce" content="   ">'
    expect(resolveCspNonce(document)).toBeUndefined()
  })
})

describe('PrimeVue configuration applied to rendered components', () => {
  it('lets PrimeVue inject the full cascade layer order as the first style in the document', async () => {
    await renderWithPlugins(Button, { props: { label: '保存设置' } })
    // 文档中第一条 @layer 声明决定全站层优先级；PrimeVue 把它 prepend 到 <head> 最前，
    // 且会压缩空白，因此按层名列表比较。
    const first = document.head.firstElementChild
    expect(first?.tagName).toBe('STYLE')
    expect(layerNames(first?.textContent ?? '')).toEqual(
      layerNames(`@layer ${primeVueOptions.theme.options.cssLayer.order}`),
    )
    expect(layerNames(first?.textContent ?? '')).toEqual([
      'properties',
      'theme',
      'base',
      'primevue',
      'components',
      'utilities',
    ])
  })

  it('renders PrimeVue built-in texts from the zh-CN locale', async () => {
    const { getByRole } = await renderWithPlugins(DatePicker, {
      props: { inline: true, showButtonBar: true },
    })
    expect(getByRole('button', { name: '今天' })).toBeVisible()
    expect(getByRole('button', { name: '清除' })).toBeVisible()
  })
})
