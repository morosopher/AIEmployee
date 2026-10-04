import { isInaccessible, screen, waitFor } from '@testing-library/vue'
import { expect, vi } from 'vitest'
import {
  additionalInventory,
  inventory,
  type InventoryEntry,
} from './inventory'

/** 仅保存实际成功执行的项；报告记录本次DOM文字与数量，不以清单元数据冒充运行时结果。 */
export const observed = new Set<string>()
export const observations = new Map<
  string,
  Array<{ text: string[]; count: number; role: string }>
>()
let activeScenario = ''
/** @param name 正在执行的真实场景；清单误指向其他场景同样失败。 */
export function setScenario(name: string): void {
  activeScenario = name
}
const normalize = (text: string) => text.replace(/\s+/g, ' ').trim()

/** 只检查当前可感知的DOM；inert尚未被Testing Library的隐藏判断涵盖，需显式排除。 */
function perceptible(node: Element): boolean {
  return node.isConnected && !node.closest('[inert]') && !isInaccessible(node)
}

/**
 * 取未隐藏文本节点，排除aria-hidden/hidden/inert装饰和退出副本；不模拟读屏器名称计算。
 * @param node 已可感知的公告根或其文本子树。
 */
function visibleText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (node instanceof Element && !perceptible(node)) return ''
  return Array.from(node.childNodes).map(visibleText).join('')
}

/**
 * 合并ARIA默认会播报的三种role与显式live根；同节点的role/live只计一次。
 * 自身off覆盖默认role；关闭的静态父区不成为根，明确开启的子根仍单独检查。
 */
function activeRoots(): HTMLElement[] {
  const implicitRoles = ['status', 'alert', 'log'] as const
  const candidates = new Set<HTMLElement>([
    ...implicitRoles.flatMap((role) => screen.queryAllByRole(role)),
    ...document.querySelectorAll<HTMLElement>(
      '[aria-live="polite"], [aria-live="assertive"]',
    ),
  ])
  // screen以body为容器，仅查其后代；body若自己声明live role，也要参与祖先检查。
  if (implicitRoles.some((role) => document.body.getAttribute('role') === role))
    candidates.add(document.body)
  return [...candidates].filter(
    (node) => perceptible(node) && node.getAttribute('aria-live') !== 'off',
  )
}

/** 校验真实DOM语义、同文根集合及双向嵌套；不同文案不因匹配查询的相同前缀而混为一项。 */
async function verify(
  id: string,
  role: InventoryEntry['role'],
  scenario: string,
  text: string,
  count: number,
): Promise<void> {
  expect(activeScenario, id).toBe(scenario)
  let actual: string[] = []
  await waitFor(() => {
    const roots = activeRoots()
    const live = role !== 'dialog' && role !== 'alertdialog'
    const texts = new Map<HTMLElement, string>()
    const textOf = (node: HTMLElement): string => {
      const value = texts.get(node) ?? normalize(visibleText(node))
      texts.set(node, value)
      return value
    }
    const regions = screen
      .queryAllByRole(role)
      .filter(
        (node) =>
          perceptible(node) &&
          (!live || roots.includes(node)) &&
          textOf(node).includes(normalize(text)),
      )
    expect(regions, `${id}: ${text}`).toHaveLength(count)
    for (const node of regions) {
      const announcement = textOf(node)
      expect(announcement).not.toBe('')
      if (live) {
        const politeness = role === 'alert' ? 'assertive' : 'polite'
        expect(node.getAttribute('aria-live') ?? politeness).toBe(politeness)
        expect(
          roots.filter(
            (other) =>
              other !== node && (other.contains(node) || node.contains(other)),
          ),
          `${id}: nested active announcements`,
        ).toHaveLength(0)
        // 原有双实例或同字段族的已登记数量由regions保留；额外role／无role同文根不能漏算。
        expect(
          roots.filter((other) => textOf(other) === announcement),
          `${id}: duplicate announcement across roles or explicit live roots`,
        ).toHaveLength(
          regions.filter((other) => textOf(other) === announcement).length,
        )
      }
    }
    actual = regions.map(textOf)
  })
  observed.add(id)
  observations.set(id, [
    ...(observations.get(id) ?? []),
    { text: actual, count, role },
  ])
}

/** @param id 4ce84ae文件及行号。@param text 原文或带合成值的文案。@param count 原有重复实例数。 */
export async function assertLive(
  id: string,
  text: string,
  count = 1,
): Promise<void> {
  const entry = inventory.find((item) => item.id === id && !item.excluded)
  if (!entry) throw new Error(`Unknown live baseline: ${id}`)
  await verify(id, entry.role, entry.scenario, text, count)
}
/** @param id 已逐项登记的新公告。@param text 实際文字。@param count 明确来源的实例数。 */
export async function assertAdditional(
  id: string,
  text: string,
  count = 1,
): Promise<void> {
  const entry = additionalInventory.find((item) => item.id === id)
  if (!entry) throw new Error(`Unknown additional live region: ${id}`)
  await verify(id, entry.role, entry.scenario, text, count)
}

/** 合成HTTP响应保持真实解析路径，不替换Message、Dialog或行为hook。 */
export function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
/** 有界失败投影，不携带原始供应商正文。 */
export function problem(
  code = 'request_validation_failed',
  status = 409,
): Response {
  return json(
    {
      type: 'about:blank',
      title: 'Synthetic rejected',
      detail: '',
      instance: '',
      status,
      error_code: code,
      trace_id: 'synthetic-live-trace',
    },
    status,
  )
}
/** @returns 由测试显式完成的边界Promise，便于观察真实loading/busy状态。 */
export function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason: unknown) => void = () => undefined
  const promise = new Promise<T>((success, fail) => {
    resolve = success
    reject = fail
  })
  return { promise, resolve, reject }
}
/** @param handle 只接收真实URL与请求选项的合成服务端路由。 */
export function network(
  handle: (url: string, init?: RequestInit) => Response | Promise<Response>,
): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: string, init?: RequestInit) =>
      Promise.resolve(handle(input, init)),
    ),
  )
}
