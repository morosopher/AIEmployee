import type {
  ActionListItem,
  ActionSnapshot,
  CalendarProposal,
  MailDraft,
  UserSettings,
} from '@/api/types'

/** 合成契约固定标识；所有内容为空且不接触真实账户。 */
export const TASK_ID = '00000000-0000-0000-0000-000000000401'
export const DRAFT_ID = '00000000-0000-0000-0000-000000000301'
export const PROPOSAL_ID = '00000000-0000-0000-0000-000000000302'
export const CONNECTION_ID = '00000000-0000-0000-0000-000000000201'
export const NOW = '2030-01-01T00:00:00Z'

/** 返回完整已接受的服务端快照形状，负例仅修改被测试的一个字段。 */
export function actionSnapshot(
  overrides: Partial<ActionSnapshot> = {},
): ActionSnapshot {
  return {
    task_id: TASK_ID,
    status: 'needs_attention',
    error_code: 'provider_write_outcome_unknown',
    event_cursor: '9007199254740993',
    task_version: '9007199254740993',
    reconciliation_attempt_count: 2,
    provider_url: 'https://mail.google.com/mail/u/0/',
    action: 'mail.send',
    provider: 'google',
    created_at: NOW,
    updated_at: NOW,
    local_action: {
      id: DRAFT_ID,
      item_kind: 'mail_draft',
      status: 'needs_attention',
      version: 3,
      editor_url: `/mail/drafts/${DRAFT_ID}`,
    },
    approval: {
      id: '00000000-0000-0000-0000-000000000403',
      status: 'approved',
      version: 1,
      payload_hash: 'a'.repeat(64),
      proposal_version: 3,
      risk_level: 'high',
      expires_at: NOW,
      decided_at: NOW,
      content_status: 'available',
      preview: {
        kind: 'mail',
        provider: 'google',
        account_email: 'sender@synthetic.example.test',
        mode: 'new',
        to: [],
        cc: [],
        bcc: [],
        subject: '',
        body_text: '',
        irreversible: true,
      },
    },
    execution: {
      id: '00000000-0000-0000-0000-000000000404',
      status: 'needs_attention',
      write_attempt_count: 1,
      reconciliation_attempt_count: 2,
      error_code: 'provider_write_outcome_unknown',
      claimed_at: NOW,
      request_started_at: NOW,
      completed_at: null,
      manual_resolution: null,
    },
    timeline: [
      {
        id: '9007199254740993',
        event: 'tool.needs_attention',
        occurred_at: NOW,
        payload: { status: 'needs_attention', reconciliation_attempt_count: 2 },
      },
    ],
    ...overrides,
  }
}

/** 返回内容无关联合列表，不把编辑对象伪造为任务。 */
export function actionItems(): ActionListItem[] {
  return [
    {
      id: DRAFT_ID,
      item_kind: 'mail_draft',
      task_id: null,
      editor_url: `/mail/drafts/${DRAFT_ID}`,
      status: 'editing',
      action: 'mail.send',
      provider: 'google',
      risk_level: null,
      created_at: NOW,
      updated_at: NOW,
    },
    {
      id: PROPOSAL_ID,
      item_kind: 'calendar_proposal',
      task_id: null,
      editor_url: `/calendar/proposals/${PROPOSAL_ID}`,
      status: 'stale',
      action: 'calendar.update',
      provider: 'microsoft',
      risk_level: 'medium',
      created_at: NOW,
      updated_at: NOW,
    },
    {
      id: TASK_ID,
      item_kind: 'trusted_task',
      task_id: TASK_ID,
      editor_url: null,
      status: 'needs_attention',
      action: 'mail.send',
      provider: 'google',
      risk_level: 'high',
      created_at: NOW,
      updated_at: NOW,
    },
  ]
}

/** 草稿响应使用空正文，避免断言失败时把正文带入报告。 */
export function mailDraft(): MailDraft {
  return {
    id: DRAFT_ID,
    connection_id: CONNECTION_ID,
    mode: 'new',
    source_thread_id: null,
    source_message_id: null,
    version: 1,
    status: 'editing',
    to: [],
    cc: [],
    bcc: [],
    subject: '',
    body_text: '',
    prompt_version: null,
    model_name: null,
    retain_until: NOW,
    created_at: NOW,
    recipient_suggestions: [],
  }
}

/** 可读 shell 允许时间字段为空，但枚举和版本仍然必须存在。 */
export function calendarProposal(): CalendarProposal {
  return {
    id: PROPOSAL_ID,
    connection_id: CONNECTION_ID,
    calendar_id: 'synthetic-calendar',
    operation_kind: 'create',
    target_event_id: null,
    base_etag: null,
    before_snapshot_id: null,
    version: 1,
    status: 'editing',
    title: null,
    description: null,
    location: null,
    starts_at: null,
    ends_at: null,
    timezone: null,
    all_day: null,
    attendees: [],
    notification_policy: null,
    changed_fields: [],
    field_diffs: [],
    required_confirmations: [
      'calendar',
      'time',
      'attendees',
      'notification_policy',
    ],
    retain_until: NOW,
    availability: null,
  }
}

/** 七日工作时间遵循真实 settings schema，不省略 M2 默认值。 */
export function userSettings(): UserSettings {
  return {
    timezone: 'Asia/Shanghai',
    locale: 'zh-CN',
    brief_time: '08:00:00',
    email_body_retention_days: 30,
    source_metadata_retention_days: 90,
    workspace_history_retention_days: 365,
    default_mail_connection_id: null,
    default_calendar_connection_id: null,
    default_calendar_id: null,
    working_hours: {
      monday: [['09:00', '17:00']],
      tuesday: [],
      wednesday: [],
      thursday: [],
      friday: [],
      saturday: [],
      sunday: [],
    },
    meeting_buffer_minutes: 15,
    updated_at: NOW,
  }
}
