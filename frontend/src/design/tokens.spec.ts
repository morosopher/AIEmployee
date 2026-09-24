import { Theme } from '@primeuix/themes'
import { beforeAll, describe, expect, it } from 'vitest'

import { appPreset, contrastRatio, semanticColors } from './tokens'

/**
 * 递归收集语义色板中的全部叶子值，用于证明色板只包含规范化后的 token。
 *
 * @param value 语义色板或其子分组。
 * @returns 全部颜色字面量。
 */
function colourLeaves(value: unknown): string[] {
  if (typeof value === 'string') return [value]
  if (value === null || typeof value !== 'object') return []
  return Object.values(value).flatMap(colourLeaves)
}

/**
 * 用 PrimeVue 自身的 token 解析器读取浅色方案下的最终值，确保断言覆盖的是组件
 * 实际注入的颜色，而不是对预设对象结构的猜测。
 *
 * @param path 省略 `colorScheme.light` 的点分 token 路径，如 `tag.danger.color`。
 * @returns 解析后的颜色字符串。
 * @throws Error 当 token 不存在或解析结果不是单一字符串时。
 */
function lightToken(path: string): string {
  const value: unknown = Theme.getTokenValue(`colorScheme.light.${path}`)
  if (typeof value !== 'string') throw new Error(`Unresolved token: ${path}`)
  return value
}

/**
 * 把解析出的 token 值换算为可交给 contrastRatio 的不透明十六进制颜色。
 *
 * - 引用语义 token 的组件 token（如 `select.border.color`）在解析器中表示为
 *   `light-dark(浅色, 暗色)`；应用关闭了暗色方案、只输出浅色变量，因此取第一个值。
 * - Aura 的 Message/Toast 背景为 `color-mix(in srgb, X, transparent 5%)`。叠加到比
 *   X 更亮的页面/卡片底色后只会更亮，因此取 X 作为深色文字对比度的保守下界。
 *
 * @param path 浅色方案 token 路径。
 * @returns 可交给 contrastRatio 的十六进制颜色。
 */
function solidColour(path: string): string {
  const value = lightToken(path)
  const lightDark = /^light-dark\(\s*(#[0-9a-f]{6})\s*,/i.exec(value)
  if (lightDark?.[1]) return lightDark[1]
  const mixed = /^color-mix\(in srgb,\s*(#[0-9a-f]{6}),\s*transparent \d+%\)$/i.exec(value)
  return mixed?.[1] ?? value
}

describe('semantic colour tokens', () => {
  it('exposes every legacy hex colour as a semantic token', () => {
    expect(semanticColors.primary).toBe('#164e9c')
    expect(semanticColors.danger).toBe('#a61b1b')
    expect(semanticColors.warn.foreground).toBe('#946200')
    expect(semanticColors.warn.background).toBe('#fff5df')
    expect(Object.keys(semanticColors)).not.toContain('gray1')
  })

  it('keeps the approved M2.1 palette table and folds near-duplicate legacy colours into it', () => {
    expect(semanticColors.surface).toEqual({
      page: '#f6f8fb',
      card: '#ffffff',
      border: '#dce2ea',
    })
    expect(semanticColors.text).toEqual({
      primary: '#1e293b',
      secondary: '#485365',
      disabled: '#a8b1bf',
    })
    // M2 scoped CSS 共 15 种不透明字面量：#fff 规范化为 #ffffff，#d7dce5 与 #ddd 并入
    // surface.border；其余 12 种各自成为唯一语义 token，不产生一次性 token。半透明遮罩
    // #0f172a80 由 PrimeVue Dialog 的遮罩 token 取代，不进入语义色板。
    const leaves = colourLeaves(semanticColors)
    expect(new Set(leaves).size).toBe(leaves.length)
    expect([...leaves].sort()).toEqual(
      [
        '#164e9c',
        '#a61b1b',
        '#946200',
        '#fff5df',
        '#c6aa72',
        '#f6f8fb',
        '#ffffff',
        '#dce2ea',
        '#1e293b',
        '#485365',
        '#a8b1bf',
        '#eef5ff',
        '#fff5f5',
      ].sort(),
    )
    expect(leaves).not.toContain('#d7dce5')
    expect(leaves).not.toContain('#ddd')
  })

  it('meets WCAG AA contrast for text on surface tokens', () => {
    expect(
      contrastRatio(semanticColors.text.primary, semanticColors.surface.page),
    ).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio('#ffffff', semanticColors.primary)).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio('#ffffff', semanticColors.danger)).toBeGreaterThanOrEqual(4.5)
  })

  it('meets WCAG AA contrast for every readable foreground/background pairing', () => {
    const readablePairs: Array<[string, string]> = [
      [semanticColors.text.primary, semanticColors.surface.card],
      [semanticColors.text.secondary, semanticColors.surface.card],
      [semanticColors.text.secondary, semanticColors.surface.page],
      [semanticColors.primary, semanticColors.surface.card],
      [semanticColors.primary, semanticColors.tint.primary],
      [semanticColors.danger, semanticColors.surface.card],
      [semanticColors.danger, semanticColors.tint.danger],
      [semanticColors.warn.foreground, semanticColors.warn.background],
      [semanticColors.warn.foreground, semanticColors.surface.card],
    ]
    for (const [foreground, background] of readablePairs) {
      expect(contrastRatio(foreground, background)).toBeGreaterThanOrEqual(4.5)
    }
  })
})

describe('appPreset', () => {
  beforeAll(() => {
    Theme.setTheme({
      preset: appPreset,
      options: { prefix: 'p', darkModeSelector: false, cssLayer: false },
    })
  })

  it('wires the semantic palette into the Aura tokens read by PrimeVue and tailwindcss-primeui', () => {
    expect(lightToken('primary.color')).toBe(semanticColors.primary)
    expect(lightToken('primary.contrast.color')).toBe('#ffffff')
    expect(lightToken('highlight.background')).toBe(semanticColors.tint.primary)
    expect(lightToken('text.color')).toBe(semanticColors.text.primary)
    expect(lightToken('text.muted.color')).toBe(semanticColors.text.secondary)
    expect(lightToken('content.background')).toBe(semanticColors.surface.card)
    expect(lightToken('content.border.color')).toBe(semanticColors.surface.border)
    expect(lightToken('surface.50')).toBe(semanticColors.surface.page)
    // 禁用文字色只按名称导出：Aura 用 surface.400 渲染可交互元素，该档位必须保持
    // Aura 的 slate.400，不能被禁用色（对比度更低）取代。
    expect(lightToken('surface.400')).toBe(lightToken('slate.400'))
    expect(lightToken('surface.400')).not.toBe(semanticColors.text.disabled)
    // PrimeVue 以 red/orange/yellow 原始色板渲染 danger、error 与 warn 严重度。
    expect(lightToken('red.500')).toBe(semanticColors.danger)
    expect(lightToken('red.50')).toBe(semanticColors.tint.danger)
    expect(lightToken('orange.500')).toBe(semanticColors.warn.foreground)
    expect(lightToken('yellow.500')).toBe(semanticColors.warn.foreground)
    expect(lightToken('yellow.50')).toBe(semanticColors.warn.background)
    expect(lightToken('yellow.200')).toBe(semanticColors.warn.border)
  })

  it('keeps every PrimeVue severity rendered by Tag, Message, Toast and Button at WCAG AA', () => {
    const pairs: Array<[string, string, string]> = [
      ['body text on page', 'text.color', 'surface.50'],
      ['muted text on card', 'text.muted.color', 'content.background'],
      ['link on card', 'primary.color', 'content.background'],
      ['selected item', 'highlight.color', 'highlight.background'],
      ['input text', 'form.field.color', 'form.field.background'],
      ['input placeholder', 'form.field.placeholder.color', 'form.field.background'],
    ]
    for (const severity of ['primary', 'secondary', 'success', 'info', 'warn', 'danger', 'contrast']) {
      pairs.push([`Tag ${severity}`, `tag.${severity}.color`, `tag.${severity}.background`])
      for (const state of ['', 'hover.', 'active.']) {
        pairs.push([
          `Button ${severity} ${state || 'rest.'}`,
          `button.${severity}.${state}color`,
          `button.${severity}.${state}background`,
        ])
      }
      pairs.push([`Button outlined ${severity}`, `button.outlined.${severity}.color`, 'content.background'])
      pairs.push([`Button text ${severity}`, `button.text.${severity}.color`, 'content.background'])
    }
    for (const severity of ['info', 'success', 'warn', 'error', 'secondary', 'contrast']) {
      pairs.push([`Message ${severity}`, `message.${severity}.color`, `message.${severity}.background`])
      pairs.push([`Toast ${severity}`, `toast.${severity}.color`, `toast.${severity}.background`])
      pairs.push([`Toast ${severity} detail`, `toast.${severity}.detail.color`, `toast.${severity}.background`])
    }
    const failing = pairs
      .map(([label, foreground, background]) => ({
        label,
        ratio: Number(contrastRatio(solidColour(foreground), solidColour(background)).toFixed(2)),
      }))
      .filter(({ ratio }) => ratio < 4.5)
    expect(failing).toEqual([])
  })

  it('keeps form-field borders, field icons and switch tracks at WCAG 1.4.11 non-text contrast on card and page', () => {
    // 1.4.11 要求识别可交互控件及其状态所需的图形与背景至少 3:1；表单控件出现在卡片
    // （surface.0）与页面（surface.50）两种底色上，两者都要核对。
    const backgrounds = {
      card: solidColour('surface.0'),
      page: solidColour('surface.50'),
    }
    expect(backgrounds).toEqual({
      card: semanticColors.surface.card,
      page: semanticColors.surface.page,
    })
    const parts: Array<[string, string]> = [
      ['form field border', 'form.field.border.color'],
      ['form field hover border', 'form.field.hover.border.color'],
      ['form field icon', 'form.field.icon.color'],
      ['InputText border', 'inputtext.border.color'],
      ['InputText hover border', 'inputtext.hover.border.color'],
      ['Select border', 'select.border.color'],
      ['Select hover border', 'select.hover.border.color'],
      ['Select chevron', 'select.dropdown.color'],
      ['DatePicker input icon', 'datepicker.input.icon.color'],
      ['DatePicker dropdown icon', 'datepicker.dropdown.color'],
      ['ToggleSwitch unchecked track', 'toggleswitch.background'],
      ['ToggleSwitch unchecked hover track', 'toggleswitch.hover.background'],
    ]
    const failing = parts.flatMap(([label, path]) =>
      Object.entries(backgrounds)
        .map(([surface, background]) => ({
          label: `${label} on ${surface}`,
          ratio: Number(contrastRatio(solidColour(path), background).toFixed(2)),
        }))
        .filter(({ ratio }) => ratio < 3),
    )
    expect(failing).toEqual([])

    // 悬停描边与悬停轨道必须比常态更深，悬停反馈才可见且不会反而变浅。
    const border = solidColour('form.field.border.color')
    const hoverBorder = solidColour('form.field.hover.border.color')
    expect(hoverBorder).not.toBe(border)
    expect(contrastRatio(hoverBorder, backgrounds.card)).toBeGreaterThan(
      contrastRatio(border, backgrounds.card),
    )
    const track = solidColour('toggleswitch.background')
    const hoverTrack = solidColour('toggleswitch.hover.background')
    expect(contrastRatio(hoverTrack, backgrounds.card)).toBeGreaterThan(
      contrastRatio(track, backgrounds.card),
    )
    // 未选中时白色滑块压在轨道上，也需要与轨道保持 3:1 才能看清开关位置。
    expect(
      contrastRatio(solidColour('toggleswitch.handle.background'), track),
    ).toBeGreaterThanOrEqual(3)
  })
})

describe('contrastRatio', () => {
  it('follows the WCAG 2.1 relative luminance formula', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5)
    // WCAG 常用参考值：#767676 在白底上约为 4.54:1，恰好通过 AA 正文阈值。
    expect(contrastRatio('#767676', '#ffffff')).toBeCloseTo(4.54, 2)
  })

  it('is symmetric and accepts shorthand or upper-case hex input', () => {
    expect(contrastRatio('#FFF', '#164E9C')).toBeCloseTo(
      contrastRatio('#164e9c', '#ffffff'),
      10,
    )
  })

  it('rejects values that are not hex colours instead of guessing', () => {
    expect(() => contrastRatio('red', '#ffffff')).toThrow(RangeError)
    expect(() => contrastRatio('#12345', '#ffffff')).toThrow(RangeError)
    expect(() => contrastRatio('{primary.500}', '#ffffff')).toThrow(RangeError)
  })
})
