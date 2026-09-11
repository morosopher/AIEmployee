import type {
  ActionApproval,
  CalendarApprovalPreview,
  CalendarPreviewFields,
  Connection,
  ConnectionCapabilities,
} from '@/api/types'
import { NOW } from './actionFixtures'

/** 测试连接的 UUID 与生产 contract fixture 一致，仅包含可公开合成资料。 */
export function connection(
  provider: 'google' | 'microsoft' = 'google',
): Connection {
  return {
    id:
      provider === 'google'
        ? '00000000-0000-0000-0000-000000000201'
        : '00000000-0000-0000-0000-000000000202',
    provider,
    account_email: `${provider}@mail.example.test`,
    scopes: [],
    status: 'connected',
    last_error_code: null,
  }
}

/** 四项能力完整投影；负例只能改单一目标字段，避免掩盖解析失败原因。 */
export function connectionCapabilities(
  provider: 'google' | 'microsoft' = 'google',
): ConnectionCapabilities {
  return {
    connection_id: connection(provider).id,
    provider,
    capabilities: (
      ['mail.read', 'mail.send', 'calendar.read', 'calendar.write'] as const
    ).map((capability) => ({
      capability,
      status: 'enabled',
      actual_scopes: ['synthetic-scope'],
      last_verified_at: NOW,
      last_error_code: null,
    })),
    provider_calendars: [
      {
        id: `synthetic-${provider}-calendar`,
        name: 'Synthetic calendar',
        timezone: 'Asia/Shanghai',
        is_primary: true,
        access_role: 'owner',
        can_write: true,
        provider_url: null,
      },
    ],
  }
}

/** 合成冻结审批：只用于组件边界，不访问真实账户或供应商。 */
export function mailApproval(): ActionApproval {
  return {
    id: '00000000-0000-0000-0000-000000000403',
    status: 'pending',
    version: 2,
    payload_hash: 'a'.repeat(64),
    proposal_version: 3,
    risk_level: 'high',
    expires_at: '2030-01-01T00:10:00Z',
    decided_at: null,
    content_status: 'available',
    preview: {
      kind: 'mail',
      provider: 'google',
      account_email: 'owner@mail.example.test',
      mode: 'reply_all',
      to: ['to@mail.example.test'],
      cc: ['cc@mail.example.test'],
      bcc: ['bcc@mail.example.test'],
      subject: 'Synthetic subject',
      body_text: '<img src=x onerror=alert(1)>\n**plain text**',
      irreversible: true,
    },
  }
}

/** 精确 before/after 值使用明确 offset，避免宿主机时区影响断言。 */
export function calendarFields(): CalendarPreviewFields {
  return {
    title: 'Synthetic meeting',
    description: '',
    location: '',
    starts_at: '2030-01-01T09:00:00+08:00',
    ends_at: '2030-01-01T10:00:00+08:00',
    timezone: 'Asia/Shanghai',
    all_day: false,
    attendees: ['guest@mail.example.test'],
  }
}

/** 日历审批保留修改前后及三类真实服务端警告，前端不得计算替代结果。 */
export function calendarApproval(): ActionApproval {
  const preview: CalendarApprovalPreview = {
    kind: 'calendar',
    provider: 'google',
    account_email: 'owner@mail.example.test',
    calendar_name: 'Synthetic calendar',
    operation: 'update',
    before: calendarFields(),
    after: { ...calendarFields(), location: 'Synthetic room' },
    conflicts: [
      {
        kind: 'overlap',
        starts_at: NOW,
        ends_at: '2030-01-01T01:00:00Z',
        missing_connection_ids: [],
      },
      {
        kind: 'outside_working_hours',
        starts_at: null,
        ends_at: null,
        missing_connection_ids: [],
      },
      {
        kind: 'partial_sources',
        starts_at: null,
        ends_at: null,
        missing_connection_ids: ['00000000-0000-0000-0000-000000000202'],
      },
    ],
    notification_policy: 'none',
    base_etag: 'synthetic-etag-1',
    compensation_available: true,
    provider_warnings: ['google_send_updates_none_external_sync'],
  }
  return { ...mailApproval(), content_status: 'available', preview }
}
