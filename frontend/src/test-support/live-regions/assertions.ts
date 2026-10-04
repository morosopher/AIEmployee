import { screen, waitFor } from '@testing-library/vue'
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

/** 校验真实DOM语义根、有效播报优先级与非嵌套结构，并收集逐次观察。 */
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
    const regions = screen
      .queryAllByRole(role)
      .filter((node) =>
        normalize(node.textContent ?? '').includes(normalize(text)),
      )
    expect(regions, `${id}: ${text}`).toHaveLength(count)
    for (const node of regions) {
      expect(normalize(node.textContent ?? '')).not.toBe('')
      if (role !== 'dialog' && role !== 'alertdialog') {
        const politeness = role === 'alert' ? 'assertive' : 'polite'
        expect(node.getAttribute('aria-live') ?? politeness).toBe(politeness)
        expect(
          node.parentElement?.closest(
            '[role="status"], [role="alert"], [aria-live="polite"], [aria-live="assertive"]',
          ),
        ).toBeNull()
      }
    }
    actual = regions.map((node) => normalize(node.textContent ?? ''))
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
