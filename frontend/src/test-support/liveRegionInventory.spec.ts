/**
 * 运行迁移前每条公告的真实组件场景；源码清点仅标识出处，不代替 DOM 行为验收。
 * 末尾逐项核对实际成功执行记录，漏掉任一模板声明就失败；CSS选择器不创建空角色。
 */
import {
  afterAll,
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'
import { cleanup } from '@testing-library/vue'
import { writeFileSync } from 'node:fs'
import {
  inventory,
  additionalInventory,
  browserInventory,
  pageConfirmationInventory,
} from './live-regions/inventory'
import { observed, observations, setScenario } from './live-regions/assertions'
import { coreScenarios } from './live-regions/coreScenarios'
import { editorScenarios } from './live-regions/editorScenarios'
import { workspaceScenarios } from './live-regions/workspaceScenarios'
import { TaskEventSource } from './taskEventSource'

beforeEach(() => {
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
  // 每个场景必须显式提供自己的API；遗漏不会落到真实服务器或供应商。
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.reject(new Error('Undeclared synthetic inventory API')),
    ),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('live region migration runtime inventory', () => {
  for (const [name, run] of Object.entries({
    ...coreScenarios,
    ...editorScenarios,
    ...workspaceScenarios,
  }))
    it(name, async () => {
      setScenario(name)
      await run()
    })
})

afterAll(() => {
  const required = [
    ...inventory.filter((entry) => !entry.excluded),
    ...additionalInventory,
  ]
    .map((entry) => entry.id)
    .sort()
  expect([...observed].sort()).toEqual(required)
  if (process.env.M21_LIVE_REGION_REPORT)
    writeFileSync(
      process.env.M21_LIVE_REGION_REPORT,
      JSON.stringify(
        {
          baseline: '4ce84ae',
          lexical: { status: 43, alert: 32, dialog: 1 },
          template: { status: 43, alert: 30, dialog: 1 },
          unit: [...inventory, ...additionalInventory].map((entry) => ({
            ...entry,
            observations: observations.get(entry.id) ?? [],
          })),
          browserEvidenceRequired: browserInventory,
          pageConfirmationUnitEvidenceRequired: pageConfirmationInventory,
        },
        null,
        2,
      ),
    )
  expect(
    inventory.filter((entry) => entry.excluded).map((entry) => entry.id),
  ).toEqual(['WorkSettingsForm:240', 'ConnectionsPage:166'])
})
