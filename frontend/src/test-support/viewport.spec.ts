import { describe, expect, it } from 'vitest'
import { effectScope, nextTick } from 'vue'
import { breakpointsTailwind, useBreakpoints } from '@vueuse/core'

import { restoreViewport, setViewport } from './viewport'

/**
 * 任何测试运行前的 window 属性描述符。Vitest 的 jsdom 环境会把 `matchMedia` 键以
 * getter 形式挂到全局（jsdom 本身没有实现，读取结果为 `undefined`），因此“原始状态”
 * 指描述符原样恢复，而不是该键不存在。
 */
const PRISTINE_MATCH_MEDIA = Object.getOwnPropertyDescriptor(window, 'matchMedia')
const PRISTINE_INNER_WIDTH = Object.getOwnPropertyDescriptor(window, 'innerWidth')

describe('setViewport', () => {
  it('stubs matchMedia for Tailwind breakpoint queries and notifies listeners on resize', () => {
    setViewport(600)
    expect(window.innerWidth).toBe(600)
    const atLeastMd = window.matchMedia('(min-width: 768px)')
    const belowMd = window.matchMedia('(max-width: 767.9px)')
    const mdToXl = window.matchMedia('(min-width: 768px) and (max-width: 1279.9px)')
    expect([atLeastMd.matches, belowMd.matches, mdToXl.matches]).toEqual([
      false,
      true,
      false,
    ])

    const changes: boolean[] = []
    atLeastMd.addEventListener('change', (event) => changes.push(event.matches))
    setViewport(900)
    expect([atLeastMd.matches, belowMd.matches, mdToXl.matches]).toEqual([
      true,
      false,
      true,
    ])
    setViewport(1400)
    expect(mdToXl.matches).toBe(false)
    // 1400 仍满足 min-width: 768px，未变化的查询不得重复通知。
    expect(changes).toEqual([true])
  })

  it('evaluates the decimal breakpoint boundaries emitted by @vueuse/core exactly', () => {
    // useBreakpoints 的 smaller('md')、greater('md') 分别生成 767.9px 与 768.1px。
    setViewport(768)
    expect(window.matchMedia('(max-width: 767.9px)').matches).toBe(false)
    expect(window.matchMedia('(min-width: 768px)').matches).toBe(true)
    expect(window.matchMedia('(min-width: 768.1px)').matches).toBe(false)
    expect(
      window.matchMedia('(min-width: 768px) and (max-width: 1279.9px)').matches,
    ).toBe(true)
    setViewport(767.9)
    expect(window.matchMedia('(max-width: 767.9px)').matches).toBe(true)
    expect(() => setViewport(0)).toThrow(RangeError)
    expect(() => setViewport(Number.NaN)).toThrow(RangeError)
  })

  it('supports legacy listeners and stops notifying removed listeners', () => {
    setViewport(1400)
    const desktop = window.matchMedia('(min-width: 1280px)')
    const legacy: boolean[] = []
    const modern: boolean[] = []
    const onLegacyChange = (event: MediaQueryListEvent) => legacy.push(event.matches)
    const onModernChange = (event: MediaQueryListEvent) => modern.push(event.matches)
    desktop.addListener(onLegacyChange)
    desktop.addEventListener('change', onModernChange)
    setViewport(900)
    desktop.removeListener(onLegacyChange)
    desktop.removeEventListener('change', onModernChange)
    setViewport(1400)
    expect(legacy).toEqual([false])
    expect(modern).toEqual([false])
    expect(desktop.matches).toBe(true)
  })

  it('treats unsupported media features as non-matching instead of throwing', () => {
    setViewport(1024)
    expect(window.matchMedia('(prefers-reduced-motion: reduce)').matches).toBe(false)
    expect(window.matchMedia('print').matches).toBe(false)
    expect(window.matchMedia('not print and (min-width: 48rem)').matches).toBe(true)
  })

  it('drives @vueuse/core Tailwind breakpoints used by responsive layouts', async () => {
    setViewport(1400)
    const scope = effectScope()
    const layout = scope.run(() => {
      const breakpoints = useBreakpoints(breakpointsTailwind)
      return {
        mobile: breakpoints.smaller('md'),
        desktop: breakpoints.greaterOrEqual('xl'),
      }
    })
    expect(layout?.mobile.value).toBe(false)
    expect(layout?.desktop.value).toBe(true)
    setViewport(600)
    await nextTick()
    expect(layout?.mobile.value).toBe(true)
    expect(layout?.desktop.value).toBe(false)
    scope.stop()
  })

  it('restores the pristine jsdom window when the viewport stub is torn down', () => {
    // 自包含：本测试先自行改动视口，再直接调用 afterEach 所用的同一个恢复函数，
    // 不依赖其他测试的执行顺序或钩子时机。
    setViewport(600)
    expect(window.innerWidth).toBe(600)
    expect(typeof window.matchMedia).toBe('function')

    restoreViewport()
    // 恢复后既有测试看到的仍是 jsdom 原始环境：没有可调用的 matchMedia，宽度回到 1024px。
    expect(Object.getOwnPropertyDescriptor(window, 'matchMedia')).toEqual(
      PRISTINE_MATCH_MEDIA,
    )
    expect(Object.getOwnPropertyDescriptor(window, 'innerWidth')).toEqual(
      PRISTINE_INNER_WIDTH,
    )
    expect(typeof window.matchMedia).toBe('undefined')
    expect(window.innerWidth).toBe(1024)
    // 幂等：重复恢复（例如多个 afterEach 钩子各调用一次）不会抛错或改动已恢复的属性。
    expect(() => restoreViewport()).not.toThrow()
    expect(window.innerWidth).toBe(1024)
  })
})
