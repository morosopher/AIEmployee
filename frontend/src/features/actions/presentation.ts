import type {
  ActionKind,
  ActionListItem,
  ActionProvider,
  ActionStatus,
} from '@/api/types'

/** 仅为展示分组，不在客户端执行任何状态迁移。 */
export type ActionGroupId =
  'drafts' | 'proposals' | 'approval' | 'executing' | 'attention' | 'history'
export interface ActionGroup {
  id: ActionGroupId
  label: string
  items: ActionListItem[]
}
const groupLabels: Record<ActionGroupId, string> = {
  drafts: '邮件草稿',
  proposals: '日程提案',
  approval: '待审批',
  executing: '执行或核对中',
  attention: '需要人工确认',
  history: '已完成历史',
}
const statusLabels: Record<ActionStatus, string> = {
  editing: '编辑中',
  stale: '需要重新检查',
  awaiting_approval: '待审批',
  executing: '执行中',
  sent: '已发送',
  applied: '已应用',
  created: '已创建',
  queued: '已排队',
  running: '执行中',
  waiting_approval: '待审批',
  retry_scheduled: '等待安全重试',
  reconciling: '正在核对',
  needs_attention: '需要人工确认',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const actionLabels: Record<ActionKind, string> = {
  'mail.send': '发送邮件',
  'calendar.create': '创建日程',
  'calendar.update': '修改日程',
  'calendar.restore': '恢复日程',
}
const eventLabels: Record<string, string> = {
  'task.status_changed': '任务状态已更新',
  'task.snapshot': '已恢复任务快照',
  'action.submitted': '操作已提交',
  'approval.required': '等待审批',
  'approval.resolved': '审批已有结论',
  'approval.invalidated': '审批已失效',
  'tool.claimed': '已认领执行',
  'tool.oauth_refresh_required': '需要刷新授权，尚不可重试',
  'tool.oauth_refresh_confirmed': '授权已核实，执行结果仍需确认',
  'tool.reconciling': '正在只读核对',
  'tool.needs_attention': '等待人工确认',
  'tool.manually_resolved': '人工结论已记录',
  'step.started': '步骤开始',
  'step.completed': '步骤完成',
  'step.failed': '步骤失败',
}

/**
 * @param items REST 联合列表。
 * @returns 固定六组，不添加或改变服务端对象。
 */
export function groupActions(items: ActionListItem[]): ActionGroup[] {
  return (Object.keys(groupLabels) as ActionGroupId[]).map((id) => ({
    id,
    label: groupLabels[id],
    items: items.filter((item) => groupFor(item) === id),
  }))
}
/**
 * @param status 服务端合法状态。
 * @returns 固定中文说明。
 */
export function actionStatusLabel(status: ActionStatus): string {
  return statusLabels[status]
}
/**
 * @param action 四种类型化命令或兼容 M1 的空值。
 * @returns 安全的动作名。
 */
export function actionLabel(action: ActionKind | null): string {
  return action ? actionLabels[action] : '任务操作'
}
/**
 * @param provider 已验证供应商。
 * @returns 面向用户的名称。
 */
export function providerLabel(provider: ActionProvider | null): string {
  return provider === 'google'
    ? 'Google'
    : provider === 'microsoft'
      ? 'Microsoft'
      : '未指定供应商'
}
/**
 * @param event 后端事件类型。
 * @returns 固定文案，未知事件不直接显示任意扩展内容。
 */
export function actionEventLabel(event: string): string {
  return eventLabels[event] ?? '其他状态更新'
}
/**
 * @param value 明确时区的服务端时刻。
 * @param timezone 用户 IANA 时区。
 * @returns 显式时区下的日期文本。
 */
export function formatActionTime(
  value: string | null,
  timezone: string,
): string {
  if (value === null) return '时间未知'
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: timezone,
    }).format(new Date(value))
  } catch {
    return '时间不可用'
  }
}
/**
 * 供应商检查链接只允许对应服务的 HTTPS 主机；拒绝脚本、凭据、异常端口和内容字段。
 * @param value 服务端候选链接。
 * @param provider 对应供应商。
 * @returns 可安全打开的链接，或无链接；拒绝值不进入 DOM。
 */
export function safeProviderUrl(
  value: string | null,
  provider: ActionProvider | null,
): string | null {
  if (!value || !provider || /[\s\\]/.test(value)) return null
  try {
    const url = new URL(value)
    const hosts =
      provider === 'google'
        ? ['mail.google.com', 'calendar.google.com', 'www.google.com']
        : ['outlook.office.com', 'outlook.office365.com', 'outlook.live.com']
    if (
      url.protocol !== 'https:' ||
      !hosts.includes(url.hostname) ||
      url.username ||
      url.password ||
      url.port ||
      url.hash
    )
      return null
    for (const key of url.searchParams.keys())
      if (/token|secret|password|body|subject|attendee|email/i.test(key))
        return null
    if (decodeURIComponent(value).includes('@')) return null
    return url.href
  } catch {
    return null
  }
}

/** 分类只使用服务端已给出的状态；本地 stale 提案保留在可检查的提案组。 */
function groupFor(item: ActionListItem): ActionGroupId {
  if (item.status === 'needs_attention') return 'attention'
  if (
    ['succeeded', 'failed', 'cancelled', 'sent', 'applied'].includes(
      item.status,
    )
  )
    return 'history'
  if (item.status === 'waiting_approval' || item.status === 'awaiting_approval')
    return 'approval'
  if (item.item_kind === 'trusted_task' || item.status === 'executing')
    return 'executing'
  return item.item_kind === 'mail_draft' ? 'drafts' : 'proposals'
}
