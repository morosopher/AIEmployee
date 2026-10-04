/**
 * 共享门禁的拒绝反例只用最小合成DOM；真实库存仍挂载实际组件。
 * 聊天回归直接执行既有真实场景并检查保存的观察，证明恢复不能借旧后缀抢先通过。
 */
import '@testing-library/jest-dom/vitest'
import { cleanup, configure, getConfig } from '@testing-library/vue'
import {
  afterAll,
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'
import { assertLive, observations, observed, setScenario } from './assertions'
import { workspaceScenarios } from './workspaceScenarios'
import { TaskEventSource } from '../taskEventSource'

const message = '正在检查系统告警'
const originalTimeout = getConfig().asyncUtilTimeout
/** 最小DOM不依赖网络或动画；缩短拒绝反例的框架等待，不改变主库存的超时配置。 */
beforeEach(() => {
  configure({ asyncUtilTimeout: 100 })
  observations.clear()
  observed.clear()
  setScenario('shellAlerts')
})
afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  vi.unstubAllGlobals()
})
afterAll(() => configure({ asyncUtilTimeout: originalTimeout }))

/** @param html 仅由本文件常量构造的合成节点，禁止使用真实API内容或凭据。 */
function fixture(html: string): void {
  const container = document.createElement('div')
  container.innerHTML = html
  document.body.append(container)
}

describe('live announcement verification', () => {
  it.each([
    [
      'another role',
      `<p role="status">${message}</p><p role="alert">${message}</p>`,
    ],
    [
      'explicit live without role',
      `<p role="status">${message}</p><p aria-live="polite">${message}</p>`,
    ],
    [
      'nested alert',
      `<p role="status">${message}<span role="alert">${message}</span></p>`,
    ],
  ])('rejects a duplicate announcement in %s', async (_name, html) => {
    fixture(html)
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
    expect(observed.has('AppShell:34')).toBe(false)
  })

  it('accepts one active root beside distinct content', async () => {
    fixture(`<p role="status">${message}</p><p role="alert">另一个错误</p>`)
    await assertLive('AppShell:34', message)
    expect(observations.get('AppShell:34')?.at(-1)?.text).toEqual([message])
  })

  it('counts role plus explicit live on the same element once', async () => {
    fixture(`<p role="status" aria-live="polite">${message}</p>`)
    await assertLive('AppShell:34', message)
  })

  it('detects a duplicate implicit log announcement', async () => {
    fixture(`<p role="status">${message}</p><div role="log">${message}</div>`)
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
  })

  it('rejects an active descendant even when its text differs from the parent prefix', async () => {
    fixture(
      `<div role="status">${message}<p aria-live="assertive">另一条公告</p></div>`,
    )
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
  })

  it.each([
    'hidden',
    'aria-hidden="true"',
    'inert',
    'style="display:none"',
    'style="visibility:hidden"',
  ])('ignores duplicate roots in a %s subtree', async (attribute) => {
    fixture(
      `<p role="status">${message}</p><div ${attribute}><p role="status">${message}</p><p aria-live="polite">${message}</p></div>`,
    )
    await assertLive('AppShell:34', message)
    expect(observations.get('AppShell:34')?.at(-1)?.text).toEqual([message])
  })

  it('treats a node own off value as disabling its implicit role', async () => {
    fixture(
      `<p role="status">${message}</p><p role="status" aria-live="off">${message}</p>`,
    )
    await assertLive('AppShell:34', message)
  })

  it('does not accept an off node as the required announcement', async () => {
    fixture(`<p role="status" aria-live="off">${message}</p>`)
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
  })

  it('allows static off content and hidden roots inside an active announcement', async () => {
    fixture(
      `<div role="status">${message}<span role="alert" aria-live="off">静态说明</span><span role="alert" hidden>隐藏说明</span></div>`,
    )
    await assertLive('AppShell:34', message)
    expect(observations.get('AppShell:34')?.at(-1)?.text).toEqual([
      message + '静态说明',
    ])
  })

  it('compares perceptible text without aria-hidden decorations', async () => {
    fixture(
      `<p role="status">${message}<span aria-hidden="true">装饰</span></p><p role="alert">${message}</p>`,
    )
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
  })

  it('does not merge distinct full messages merely because they share a prefix', async () => {
    fixture(
      `<p role="status">${message}</p><p aria-live="polite">${message}（独立来源）</p>`,
    )
    await assertLive('AppShell:34', message)
  })

  it('recognizes a native implicit status through the role query', async () => {
    fixture(`<output>${message}</output>`)
    await assertLive('AppShell:34', message)
  })

  it('retains the status polite priority requirement', async () => {
    fixture(`<p role="status" aria-live="assertive">${message}</p>`)
    await expect(assertLive('AppShell:34', message)).rejects.toThrow()
  })

  it('rejects a third cross-role copy beside the registered original pair', async () => {
    const expired = '内容已到期，仅保留执行历史。'
    fixture(
      `<p role="status">${expired}</p><p role="status">${expired}</p><p role="alert">${expired}</p>`,
    )
    setScenario('actionDetail')
    await expect(assertLive('ActionDetail:85', expired, 2)).rejects.toThrow()
  })

  it('preserves the explicitly registered pair of original expired-content statuses', async () => {
    const expired = '内容已到期，仅保留执行历史。'
    fixture(`<p role="status">${expired}</p><p role="status">${expired}</p>`)
    setScenario('actionDetail')
    await assertLive('ActionDetail:85', expired, 2)
    expect(observations.get('ActionDetail:85')?.at(-1)?.count).toBe(2)
  })
})

it('records reconnecting then the complete connected message from the real chat scenario', async () => {
  configure({ asyncUtilTimeout: originalTimeout })
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  vi.stubGlobal('EventSource', TaskEventSource)
  TaskEventSource.instances = []
  setScenario('chatPage')
  await workspaceScenarios.chatPage()
  expect(
    observations.get('ChatPage:90')?.map((observation) => observation.text),
  ).toEqual([['任务：执行中 · 正在恢复实时连接'], ['任务：执行中']])
})
