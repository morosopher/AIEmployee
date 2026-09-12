/**
 * 声明 M2 前后端公开契约的严格判别联合。供应商 SDK 与凭据不进入这些类型；正文、预览和日程敏感字段只允许存在于短生命周期内存中。
 */
import type { TaskStatus } from './types'

/** M2 只允许两种已批准供应商和四种可信写命令。 */
export type ActionProvider = 'google' | 'microsoft'
export type ActionKind =
  'mail.send' | 'calendar.create' | 'calendar.update' | 'calendar.restore'
export type CalendarOperation = 'create' | 'update' | 'restore'
export type MailMode = 'new' | 'reply' | 'reply_all'
export type MailDraftStatus =
  | 'editing'
  | 'awaiting_approval'
  | 'executing'
  | 'sent'
  | 'needs_attention'
  | 'cancelled'
export type CalendarProposalStatus =
  | 'editing'
  | 'awaiting_approval'
  | 'executing'
  | 'applied'
  | 'stale'
  | 'needs_attention'
  | 'cancelled'
export type ToolExecutionStatus =
  | 'claimed'
  | 'executing'
  | 'succeeded'
  | 'confirmed_failed'
  | 'retryable_failed'
  | 'reconciling'
  | 'needs_attention'
export type ApprovalStatus =
  'pending' | 'approved' | 'rejected' | 'expired' | 'invalidated'
export type ManualResolution = 'confirmed_executed' | 'confirmed_not_executed'
export type ActionItemKind = 'mail_draft' | 'calendar_proposal' | 'trusted_task'
export type ActionStatus = TaskStatus | MailDraftStatus | CalendarProposalStatus

/** 冻结邮件预览仅能驻留内存，不得转为日志、查询参数或持久缓存。 */
export interface MailApprovalPreview {
  kind: 'mail'
  provider: ActionProvider
  account_email: string
  mode: MailMode
  to: string[]
  cc: string[]
  bcc: string[]
  subject: string
  body_text: string
  irreversible: true
}

/** 非重复日程的精确前后值；全天为日期，定时为显式 offset 的时刻。 */
export interface CalendarPreviewFields {
  title: string
  description: string | null
  location: string | null
  starts_at: string
  ends_at: string
  timezone: string
  all_day: boolean
  attendees: string[]
}

/** 冲突只表达区间或缺失连接，不泄漏其他日程的内容。 */
export interface CalendarConflictPreview {
  kind: 'overlap' | 'outside_working_hours' | 'partial_sources'
  starts_at: string | null
  ends_at: string | null
  missing_connection_ids: string[]
}

/** 通知策略、ETag 和供应商警告来自被冻结的服务端命令。 */
export interface CalendarApprovalPreview {
  kind: 'calendar'
  provider: ActionProvider
  account_email: string
  calendar_name: string
  operation: CalendarOperation
  before: CalendarPreviewFields | null
  after: CalendarPreviewFields
  conflicts: CalendarConflictPreview[]
  notification_policy: 'all' | 'none'
  base_etag: string | null
  compensation_available: boolean
  provider_warnings: 'google_send_updates_none_external_sync'[]
}
export type ApprovalPreview = MailApprovalPreview | CalendarApprovalPreview

/** 本地对象摘要保留自身主键，不把编辑阶段伪造为 TaskRun。 */
export type ActionLocalSummary = {
  id: string
  version: number
  editor_url: string
} & (
  | { item_kind: 'mail_draft'; status: MailDraftStatus }
  | { item_kind: 'calendar_proposal'; status: CalendarProposalStatus }
)

/** 脱敏历史与可用预览互斥，不能借空预览隐式降级有效审批。 */
export type ActionApproval = {
  id: string
  status: ApprovalStatus
  version: number
  payload_hash: string
  proposal_version: number
  risk_level: 'high' | 'medium'
  expires_at: string
  decided_at: string | null
} & (
  | { content_status: 'available'; preview: ApprovalPreview }
  | { content_status: 'redacted'; preview: null }
)

/** 执行状态仅反映服务端写入/核对事实，不把 OAuth 刷新当成成功。 */
export interface ActionExecution {
  id: string
  status: ToolExecutionStatus
  write_attempt_count: number
  reconciliation_attempt_count: number
  error_code: string | null
  claimed_at: string | null
  request_started_at: string | null
  completed_at: string | null
  manual_resolution: ManualResolution | null
}

/** 时间线仅保留可公开的状态和计数；未知字段不会进入状态层。 */
export interface ActionTimelineEvent {
  id: string
  event: string
  occurred_at: string
  payload: {
    status?: string
    error_code?: string
    write_attempt_count?: number
    reconciliation_attempt_count?: number
  }
}

/** 完整权威快照；两个版本值必须是相同规范十进制字符串。 */
export interface ActionSnapshot {
  task_id: string
  status: TaskStatus
  error_code: string | null
  event_cursor: string
  task_version: string
  reconciliation_attempt_count: number
  provider_url: string | null
  action: ActionKind | null
  provider: ActionProvider | null
  created_at: string | null
  updated_at: string | null
  local_action: ActionLocalSummary | null
  approval: ActionApproval | null
  execution: ActionExecution | null
  timeline: ActionTimelineEvent[]
}

/** 内容无关列表共同字段。 */
interface ActionListBase {
  id: string
  provider: ActionProvider
  risk_level: 'high' | 'medium' | null
  created_at: string
  updated_at: string
}
/** 每个变体严格绑定其动作、状态、导航与原始身份。 */
export type ActionListItem = ActionListBase &
  (
    | {
        item_kind: 'mail_draft'
        task_id: null
        editor_url: string
        status: MailDraftStatus
        action: 'mail.send'
      }
    | {
        item_kind: 'calendar_proposal'
        task_id: null
        editor_url: string
        status: CalendarProposalStatus
        action: Exclude<ActionKind, 'mail.send'>
      }
    | {
        item_kind: 'trusted_task'
        task_id: string
        editor_url: null
        status: TaskStatus
        action: ActionKind
      }
  )
export interface ActionListPage {
  items: ActionListItem[]
  limit: number
  offset: number
}
/** 只允许内容无关的服务端筛选字段进入 URL。 */
export interface ActionFilters {
  provider?: ActionProvider
  status?: ActionStatus
  item_kind?: ActionItemKind
  action?: ActionKind
  limit?: number
  offset?: number
}

/** 当前草稿版本响应；正文和收件人只在内存驻留。 */
export interface MailDraft {
  id: string
  connection_id: string
  mode: MailMode
  source_thread_id: string | null
  source_message_id: string | null
  version: number
  status: MailDraftStatus
  to: string[]
  cc: string[]
  bcc: string[]
  subject: string
  body_text: string
  prompt_version: string | null
  model_name: string | null
  retain_until: string | null
  created_at: string | null
  recipient_suggestions: string[]
}
export type CalendarEditableField =
  | 'title'
  | 'description'
  | 'location'
  | 'starts_at'
  | 'ends_at'
  | 'timezone'
  | 'all_day'
  | 'attendees'
  | 'notification_policy'
export type CalendarConfirmation =
  'calendar' | 'time' | 'attendees' | 'notification_policy'
/** 本人日历候选结果固定声明未检查参会人可用性。 */
export interface CalendarAvailability {
  proposal_id: string
  version: number
  candidates: Array<{ starts_at: string; ends_at: string }>
  completeness: 'complete' | 'partial'
  missing_connections: string[]
  attendee_availability_checked: false
}
/** 原始修改前快照与本次有界冲突读取分别声明完整性，空列表只代表已执行检查。 */
export type CalendarEditorFacts = {
  restore_source: { event_id: string; snapshot_id: string } | null
  /** 仅服务端证明的本人本地 update 来源；旧缓存必须先同步并显式重读。 */
  reprepare_source: { event_id: string; requires_sync: boolean } | null
} & (
  | { before_status: 'available'; before: CalendarPreviewFields }
  | { before_status: 'not_applicable' | 'unavailable'; before: null }
) &
  (
    | { conflict_status: 'incomplete'; conflicts: null }
    | { conflict_status: 'checked'; conflicts: CalendarConflictPreview[] }
  )
/** 未确认 shell 的内容可空，但版本、状态与待确认枚举仍必须存在。 */
export interface CalendarProposal {
  id: string
  connection_id: string
  calendar_id: string
  operation_kind: CalendarOperation
  target_event_id: string | null
  base_etag: string | null
  before_snapshot_id: string | null
  version: number
  status: CalendarProposalStatus
  title: string | null
  description: string | null
  location: string | null
  starts_at: string | null
  ends_at: string | null
  timezone: string | null
  all_day: boolean | null
  attendees: string[]
  notification_policy: 'all' | 'none' | null
  changed_fields: CalendarEditableField[]
  field_diffs: Array<{ field: CalendarEditableField; changed: true }>
  required_confirmations: CalendarConfirmation[]
  retain_until: string
  availability: CalendarAvailability | null
  editor_facts: CalendarEditorFacts | null
}
export interface AcceptedActionTask {
  task_id: string
  status: 'queued'
}
export type CapabilityName =
  'mail.read' | 'mail.send' | 'calendar.read' | 'calendar.write'
export type CapabilityStatus =
  | 'disabled'
  | 'authorizing'
  | 'enabled'
  | 'degraded'
  | 'action_required'
  | 'revoked'
export type ConnectionStatus =
  'connecting' | 'connected' | 'degraded' | 'expired' | 'disconnected'
/** 连接公开身份；不包含 OAuth token 或凭据。 */
export interface Connection {
  id: string
  provider: ActionProvider
  account_email: string
  scopes: string[]
  status: ConnectionStatus
  last_error_code: string | null
}
export interface ConnectionCapability {
  capability: CapabilityName
  status: CapabilityStatus
  actual_scopes: string[]
  last_verified_at: string | null
  last_error_code: string | null
}
export interface ProviderCalendar {
  id: string
  name: string
  timezone: string
  is_primary: boolean
  access_role: string
  can_write: boolean
  provider_url: string | null
}
export interface ConnectionCapabilities {
  connection_id: string
  provider: ActionProvider
  capabilities: ConnectionCapability[]
  provider_calendars: ProviderCalendar[]
}
export type Weekday =
  | 'monday'
  | 'tuesday'
  | 'wednesday'
  | 'thursday'
  | 'friday'
  | 'saturday'
  | 'sunday'
export type WorkingHours = Record<Weekday, Array<[string, string]>>
/** 用户完整工作设置，PATCH 可独立清空默认连接/日历。 */
export interface UserSettings {
  timezone: string
  locale: string
  brief_time: string
  email_body_retention_days: number
  source_metadata_retention_days: number
  workspace_history_retention_days: number
  default_mail_connection_id: string | null
  default_calendar_connection_id: string | null
  default_calendar_id: string | null
  working_hours: WorkingHours
  meeting_buffer_minutes: number
  updated_at: string
}
