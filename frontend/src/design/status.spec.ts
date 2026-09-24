import { describe, expect, it } from 'vitest'

import type {
  ApprovalStatus,
  CapabilityStatus,
  ConnectionStatus,
} from '@/api/types'
import { asTaskStatus } from '@/api/types'
import { actionStatusLabel } from '@/features/actions/presentation'

import {
  ACTION_STATUSES,
  TASK_STATUSES,
  actionStatusPresentation,
  approvalStatusPresentation,
  capabilityStatusPresentation,
  connectionStatusPresentation,
  statusPresentation,
  taskStatusPresentation,
  type StatusKind,
  type StatusPresentation,
  type StatusSeverity,
} from './status'

/** PrimeVue Tag 只接受这些 severity；映射表不得产出组件无法渲染的值。 */
const TAG_SEVERITIES: readonly StatusSeverity[] = [
  'secondary',
  'info',
  'success',
  'warn',
  'danger',
  'contrast',
]

/** 迁移前连接页 CapabilityRows.vue 显示的能力文案，迁移后必须逐字保持。 */
const CAPABILITY_LABELS: Record<CapabilityStatus, string> = {
  disabled: '已关闭',
  authorizing: '授权中',
  enabled: '已启用',
  degraded: '暂不可用',
  action_required: '需要操作',
  revoked: '已撤销',
}

/** 迁移前 ApprovalCard.vue 显示的审批文案，迁移后必须逐字保持。 */
const APPROVAL_LABELS: Record<ApprovalStatus, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '已过期',
  invalidated: '已失效',
}

/** 连接状态此前直接显示英文原值；M2.1 统一为简短中文说明。 */
const CONNECTION_LABELS: Record<ConnectionStatus, string> = {
  connecting: '连接中',
  connected: '已连接',
  degraded: '部分可用',
  expired: '授权已过期',
  disconnected: '已断开',
}

/**
 * 以类型安全的方式遍历完整映射表，避免在断言中手写不完整的状态数组。
 *
 * @param record 以状态联合为键的完整映射。
 * @returns 保留键类型的条目数组。
 */
function entriesOf<K extends string, V>(record: Record<K, V>): Array<[K, V]> {
  return Object.entries(record) as Array<[K, V]>
}

/**
 * 断言单个展示结果可直接交给 PrimeVue Tag，且文字不是原始英文状态值。
 *
 * @param presentation 待检查的展示结果。
 */
function expectRenderable(presentation: StatusPresentation): void {
  expect(TAG_SEVERITIES).toContain(presentation.severity)
  expect(presentation.label).toMatch(/^\p{Script=Han}+$/u)
}

describe('design status presentation', () => {
  it('maps every task status to a severity and a Chinese label', () => {
    for (const status of TASK_STATUSES) {
      expect(taskStatusPresentation(status)).toMatchObject({
        severity: expect.any(String),
        label: expect.any(String),
      })
    }
    expect(taskStatusPresentation('needs_attention').severity).toBe('danger')
  })

  it('derives TASK_STATUSES from the server task state machine without inventing states', () => {
    expect(TASK_STATUSES).toHaveLength(10)
    for (const status of TASK_STATUSES) expect(asTaskStatus(status)).toBe(status)
  })

  it('keeps every action status label identical to the current action center text', () => {
    expect(ACTION_STATUSES).toHaveLength(16)
    for (const status of ACTION_STATUSES) {
      expectRenderable(actionStatusPresentation(status))
      expect(actionStatusPresentation(status).label).toBe(
        actionStatusLabel(status),
      )
    }
    for (const status of TASK_STATUSES) {
      expect(actionStatusPresentation(status)).toEqual(
        taskStatusPresentation(status),
      )
    }
  })

  it('reserves danger for failures and manual attention and success for completed writes', () => {
    expect(taskStatusPresentation('failed').severity).toBe('danger')
    expect(actionStatusPresentation('needs_attention').severity).toBe('danger')
    expect(taskStatusPresentation('succeeded').severity).toBe('success')
    expect(actionStatusPresentation('sent').severity).toBe('success')
    expect(actionStatusPresentation('applied').severity).toBe('success')
    expect(actionStatusPresentation('stale').severity).toBe('warn')
    expect(taskStatusPresentation('cancelled').severity).toBe('secondary')
  })

  it('keeps the capability labels shown on the connections page', () => {
    for (const [status, label] of entriesOf(CAPABILITY_LABELS)) {
      expectRenderable(capabilityStatusPresentation(status))
      expect(capabilityStatusPresentation(status).label).toBe(label)
    }
    expect(capabilityStatusPresentation('enabled').severity).toBe('success')
    expect(capabilityStatusPresentation('degraded').severity).toBe('warn')
    expect(capabilityStatusPresentation('revoked').severity).toBe('danger')
  })

  it('keeps the approval labels shown on approval cards', () => {
    for (const [status, label] of entriesOf(APPROVAL_LABELS)) {
      expectRenderable(approvalStatusPresentation(status))
      expect(approvalStatusPresentation(status).label).toBe(label)
    }
    expect(approvalStatusPresentation('approved').severity).toBe('success')
    expect(approvalStatusPresentation('rejected').severity).toBe('danger')
  })

  it('gives every connection status a concise Chinese label instead of the raw value', () => {
    for (const [status, label] of entriesOf(CONNECTION_LABELS)) {
      expectRenderable(connectionStatusPresentation(status))
      expect(connectionStatusPresentation(status).label).toBe(label)
    }
    expect(connectionStatusPresentation('connected').severity).toBe('success')
    expect(connectionStatusPresentation('degraded').severity).toBe('warn')
    expect(connectionStatusPresentation('disconnected').severity).toBe(
      'secondary',
    )
  })

  it('dispatches by status kind for shared status tags', () => {
    expect(statusPresentation('task', 'reconciling')).toEqual(
      taskStatusPresentation('reconciling'),
    )
    expect(statusPresentation('action', 'awaiting_approval')).toEqual(
      actionStatusPresentation('awaiting_approval'),
    )
    expect(statusPresentation('capability', 'revoked')).toEqual(
      capabilityStatusPresentation('revoked'),
    )
    expect(statusPresentation('approval', 'expired')).toEqual(
      approvalStatusPresentation('expired'),
    )
    expect(statusPresentation('connection', 'expired')).toEqual(
      connectionStatusPresentation('expired'),
    )
  })

  it('rejects a status that does not belong to the requested kind at compile time and at runtime', () => {
    // 状态族来自宽泛的 StatusKind 时（例如组件 Props），不能与任意状态值组合：task 族没有
    // revoked，旧签名却能编译并在运行时返回 undefined。
    const kinds: readonly StatusKind[] = ['capability', 'task']
    const outcomes = kinds.map((kind) => {
      try {
        // @ts-expect-error 宽泛的 StatusKind 必须先收窄到具体状态族，才能传入该族的状态值。
        return statusPresentation(kind, 'revoked').label
      } catch (error) {
        return error instanceof RangeError ? 'RangeError' : 'unexpected error'
      }
    })
    expect(outcomes).toEqual(['已撤销', 'RangeError'])
    // 映射表是普通对象，原型链上的键同样不能被当作状态值。
    // @ts-expect-error toString 不是任务状态。
    expect(() => statusPresentation('task', 'toString')).toThrow(RangeError)
  })

  it('returns immutable presentation entries so one consumer cannot recolour another', () => {
    const presentation = taskStatusPresentation('running')
    expect(Object.isFrozen(presentation)).toBe(true)
    expect(Object.isFrozen(TASK_STATUSES)).toBe(true)
    expect(Object.isFrozen(ACTION_STATUSES)).toBe(true)
  })
})
