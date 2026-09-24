/**
 * 服务端状态到 PrimeVue `Tag` 严重度与中文文案的唯一映射。
 *
 * 这里只做展示映射，不推导或迁移任何状态：状态值来自 `src/api/` 已收窄的联合类型，
 * 映射表以 `Record<联合, …>` 声明，新增服务端状态而未补映射时类型检查直接失败。
 * 文案必须与迁移前页面逐字一致（任务/操作状态见 `features/actions/presentation.ts`，
 * 能力与审批状态见连接页和审批卡片），颜色不得作为唯一信息载体，因此每个状态都同时
 * 给出文字。危险色只留给失败、需人工确认、拒绝与撤销，避免告警疲劳。
 */
import type {
  ActionStatus,
  ApprovalStatus,
  CalendarProposalStatus,
  CapabilityStatus,
  ConnectionStatus,
  MailDraftStatus,
  TaskStatus,
} from '@/api/types'

/** PrimeVue 4 `Tag` 支持的严重度；`contrast` 预留给需要最高对比的中性标记。 */
export type StatusSeverity =
  'secondary' | 'info' | 'success' | 'warn' | 'danger' | 'contrast'

/** 单个状态的展示结果：严重度决定颜色，label 是必须同时显示的中文文字。 */
export interface StatusPresentation {
  readonly severity: StatusSeverity
  readonly label: string
}

/** 各状态族对应的服务端联合类型，供按族分发的共享状态标签使用。 */
export interface StatusValueByKind {
  task: TaskStatus
  action: ActionStatus
  capability: CapabilityStatus
  approval: ApprovalStatus
  connection: ConnectionStatus
}

/** 状态族名称：任务、操作中心条目、连接能力、审批与连接。 */
export type StatusKind = keyof StatusValueByKind

/**
 * 冻结映射表及其条目，防止某个调用方修改共享对象后影响其他组件的配色。
 *
 * @param table 以状态联合为键的完整映射。
 * @returns 深度只读的同一映射。
 */
function freezeTable<K extends string>(
  table: Record<K, StatusPresentation>,
): Readonly<Record<K, StatusPresentation>> {
  for (const presentation of Object.values<StatusPresentation>(table)) {
    Object.freeze(presentation)
  }
  return Object.freeze(table)
}

/**
 * 以类型安全的方式列出完整映射表的键；键集合由 `Record` 类型保证与联合一致。
 *
 * @param table 以状态联合为键的完整映射。
 * @returns 冻结的状态值数组。
 */
function statusKeys<K extends string>(
  table: Readonly<Record<K, StatusPresentation>>,
): readonly K[] {
  return Object.freeze(Object.keys(table) as K[])
}

/**
 * 任务状态机映射：排队与创建为中性，执行为信息，等待审批与恢复路径为警告，
 * 失败与需人工确认为危险。
 */
const taskPresentations = freezeTable<TaskStatus>({
  created: { severity: 'secondary', label: '已创建' },
  queued: { severity: 'secondary', label: '已排队' },
  running: { severity: 'info', label: '执行中' },
  waiting_approval: { severity: 'warn', label: '待审批' },
  retry_scheduled: { severity: 'warn', label: '等待安全重试' },
  reconciling: { severity: 'warn', label: '正在核对' },
  needs_attention: { severity: 'danger', label: '需要人工确认' },
  succeeded: { severity: 'success', label: '已完成' },
  failed: { severity: 'danger', label: '失败' },
  cancelled: { severity: 'secondary', label: '已取消' },
})

/** 邮件草稿与日程提案独有状态；与任务共用的状态值在操作中心沿用任务映射。 */
const localActionPresentations = freezeTable<
  Exclude<MailDraftStatus | CalendarProposalStatus, TaskStatus>
>({
  editing: { severity: 'secondary', label: '编辑中' },
  awaiting_approval: { severity: 'warn', label: '待审批' },
  executing: { severity: 'info', label: '执行中' },
  sent: { severity: 'success', label: '已发送' },
  applied: { severity: 'success', label: '已应用' },
  stale: { severity: 'warn', label: '需要重新检查' },
})

/** 操作中心联合列表的完整映射：任务状态 + 本地草稿/提案状态。 */
const actionPresentations = freezeTable<ActionStatus>({
  ...taskPresentations,
  ...localActionPresentations,
})

/** 连接能力映射；降级与需要操作提示用户处理，撤销视为危险。 */
const capabilityPresentations = freezeTable<CapabilityStatus>({
  disabled: { severity: 'secondary', label: '已关闭' },
  authorizing: { severity: 'info', label: '授权中' },
  enabled: { severity: 'success', label: '已启用' },
  degraded: { severity: 'warn', label: '暂不可用' },
  action_required: { severity: 'warn', label: '需要操作' },
  revoked: { severity: 'danger', label: '已撤销' },
})

/** 审批映射；过期与失效是无需处理的历史结论，拒绝为危险。 */
const approvalPresentations = freezeTable<ApprovalStatus>({
  pending: { severity: 'warn', label: '待审批' },
  approved: { severity: 'success', label: '已批准' },
  rejected: { severity: 'danger', label: '已拒绝' },
  expired: { severity: 'secondary', label: '已过期' },
  invalidated: { severity: 'secondary', label: '已失效' },
})

/** 连接映射；此前页面直接显示英文原值，M2.1 起统一使用简短中文。 */
const connectionPresentations = freezeTable<ConnectionStatus>({
  connecting: { severity: 'info', label: '连接中' },
  connected: { severity: 'success', label: '已连接' },
  degraded: { severity: 'warn', label: '部分可用' },
  expired: { severity: 'warn', label: '授权已过期' },
  disconnected: { severity: 'secondary', label: '已断开' },
})

/** 按状态族索引的全部映射，保证共享标签与专用函数读取同一份数据。 */
const presentationsByKind: {
  readonly [K in StatusKind]: Readonly<
    Record<StatusValueByKind[K], StatusPresentation>
  >
} = {
  task: taskPresentations,
  action: actionPresentations,
  capability: capabilityPresentations,
  approval: approvalPresentations,
  connection: connectionPresentations,
}

/** 服务端任务状态机的全部状态，派生自与 `TaskStatus` 联合一一对应的映射表。 */
export const TASK_STATUSES = statusKeys(taskPresentations)

/** 操作中心可能出现的全部状态（任务、邮件草稿、日程提案的并集）。 */
export const ACTION_STATUSES = statusKeys(actionPresentations)

/**
 * @param status 已由 API 层收窄的任务状态。
 * @returns 任务状态的严重度与中文文案。
 */
export function taskStatusPresentation(status: TaskStatus): StatusPresentation {
  return taskPresentations[status]
}

/**
 * @param status 操作中心条目的服务端状态。
 * @returns 与迁移前操作中心文案一致的展示结果。
 */
export function actionStatusPresentation(
  status: ActionStatus,
): StatusPresentation {
  return actionPresentations[status]
}

/**
 * @param status 连接能力状态。
 * @returns 与迁移前连接页文案一致的展示结果。
 */
export function capabilityStatusPresentation(
  status: CapabilityStatus,
): StatusPresentation {
  return capabilityPresentations[status]
}

/**
 * @param status 审批记录状态；倒计时导致的前端过期提示仍由审批卡片自行判断。
 * @returns 与迁移前审批卡片文案一致的展示结果。
 */
export function approvalStatusPresentation(
  status: ApprovalStatus,
): StatusPresentation {
  return approvalPresentations[status]
}

/**
 * @param status OAuth 连接状态。
 * @returns 连接状态的严重度与中文文案。
 */
export function connectionStatusPresentation(
  status: ConnectionStatus,
): StatusPresentation {
  return connectionPresentations[status]
}

/**
 * 共享状态标签的合法参数：状态值必须属于同一状态族。按族展开为具名元组的联合，
 * 宽泛的 `StatusKind` 与任意状态值的组合（例如 `['task', 'revoked']`）无法通过类型检查，
 * 调用方必须先把状态族收窄到具体字面量。
 */
export type StatusPresentationArgs = {
  [K in StatusKind]: [kind: K, status: StatusValueByKind[K]]
}[StatusKind]

/**
 * 在状态族映射表中查找展示结果，并在运行时再次拒绝不属于该族的值。
 *
 * 类型层面已保证组合合法；这里防御绕过类型系统的调用（例如未收窄的外部数据），
 * 并用 `Object.hasOwn` 排除 `toString` 等原型链上的键，绝不返回 `undefined`。
 *
 * @param table 某个状态族的完整映射表。
 * @param kind 状态族，仅用于错误信息。
 * @param status 待查找的状态值。
 * @returns 该状态的严重度与中文文案。
 * @throws RangeError 状态值不属于该状态族时。
 */
function presentationFrom(
  table: Readonly<Partial<Record<string, StatusPresentation>>>,
  kind: StatusKind,
  status: string,
): StatusPresentation {
  const presentation = Object.hasOwn(table, status) ? table[status] : undefined
  if (!presentation) throw new RangeError(`Unknown ${kind} status: ${status}`)
  return presentation
}

/**
 * 按状态族分发，供只接收 `kind` 与状态值的共享状态标签使用。
 *
 * @param args `[状态族, 该族的服务端状态值]`，见 {@link StatusPresentationArgs}。
 * @returns 对应的严重度与中文文案。
 * @throws RangeError 绕过类型检查传入不属于该状态族的值时。
 */
export function statusPresentation(
  ...[kind, status]: StatusPresentationArgs
): StatusPresentation {
  return presentationFrom(presentationsByKind[kind], kind, status)
}
