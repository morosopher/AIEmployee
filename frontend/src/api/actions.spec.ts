import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getAction,
  listActions,
  parseActionList,
  parseActionSnapshot,
  resolveAction,
} from './actions'
import {
  createMailDraft,
  generateMailDraft,
  parseMailDraft,
  submitMailDraft,
} from './mail'
import {
  createCalendarProposal,
  createRestoreProposal,
  parseCalendarProposal,
  submitCalendarProposal,
} from './calendar'
import {
  parseConnectionCapabilities,
  startMicrosoftConnection,
  syncConnection,
} from './connections'
import { parseSettings } from './settings'
import { createRequestIntent } from './requestIntent'
import {
  actionItems,
  actionSnapshot,
  calendarProposal,
  CONNECTION_ID,
  DRAFT_ID,
  mailDraft,
  NOW,
  PROPOSAL_ID,
  TASK_ID,
  userSettings,
} from '@/test-support/actionFixtures'

afterEach(() => vi.unstubAllGlobals())

describe('strict M2 response boundaries', () => {
  it('accepts all task states while preserving exact large cursor strings', () => {
    for (const status of [
      'created',
      'queued',
      'running',
      'waiting_approval',
      'retry_scheduled',
      'reconciling',
      'needs_attention',
      'succeeded',
      'failed',
      'cancelled',
    ]) {
      const result = parseActionSnapshot({ ...actionSnapshot(), status })
      expect(result.status).toBe(status)
      expect(result.task_version).toBe('9007199254740993')
    }
  })

  it('rejects an unknown manual resolution in an otherwise valid snapshot', () => {
    const value = actionSnapshot()
    expect(parseActionSnapshot(value).execution?.manual_resolution).toBeNull()
    expect(() =>
      parseActionSnapshot({
        ...value,
        execution: { ...value.execution, manual_resolution: 'maybe' },
      }),
    ).toThrow('Invalid action snapshot')
  })

  it.each([undefined, 1, '01', '-1', '1.0', ''])(
    'rejects a missing or noncanonical version (%s)',
    (task_version) => {
      expect(() =>
        parseActionSnapshot({ ...actionSnapshot(), task_version }),
      ).toThrow('Invalid action snapshot')
    },
  )

  it('rejects unequal canonical cursor and version without rounding', () => {
    expect(() =>
      parseActionSnapshot({
        ...actionSnapshot(),
        task_version: '9007199254740992',
      }),
    ).toThrow('Invalid action snapshot')
  })

  it('only accepts an expired approval with an explicitly null preview', () => {
    const value = actionSnapshot()
    expect(() =>
      parseActionSnapshot({
        ...value,
        approval: { ...value.approval, content_status: 'redacted' },
      }),
    ).toThrow('Invalid action snapshot')
    expect(
      parseActionSnapshot({
        ...value,
        approval: {
          ...value.approval,
          content_status: 'redacted',
          preview: null,
        },
      }).approval?.preview,
    ).toBeNull()
  })

  it('rejects missing enums and versions at each nested boundary', () => {
    const value = actionSnapshot()
    for (const invalid of [
      { ...value, status: undefined },
      { ...value, action: 'mail.forward' },
      { ...value, approval: { ...value.approval, status: undefined } },
      { ...value, approval: { ...value.approval, version: undefined } },
      { ...value, approval: { ...value.approval, proposal_version: 0 } },
      { ...value, execution: { ...value.execution, status: 'maybe' } },
      {
        ...value,
        execution: { ...value.execution, manual_resolution: undefined },
      },
      { ...value, local_action: { ...value.local_action, version: undefined } },
    ])
      expect(() => parseActionSnapshot(invalid)).toThrow(
        'Invalid action snapshot',
      )
  })

  it('preserves local identities and refuses mismatched task or editor bindings', () => {
    const items = actionItems()
    expect(
      parseActionList({ items, limit: 50, offset: 0 }).items[0]?.task_id,
    ).toBeNull()
    for (const item of [
      { ...items[0], task_id: TASK_ID },
      { ...items[0], editor_url: `/mail/drafts/${PROPOSAL_ID}` },
      { ...items[1], action: 'mail.send' },
      { ...items[2], task_id: DRAFT_ID },
      { ...items[0], status: undefined },
      { ...items[1], item_kind: 'calendar_grid' },
    ])
      expect(() =>
        parseActionList({ items: [item], limit: 50, offset: 0 }),
      ).toThrow('Invalid action list')
  })

  it('validates calendar previews including provider warnings and partial sources', () => {
    const value = actionSnapshot()
    const fields = {
      title: '',
      description: null,
      location: null,
      starts_at: NOW,
      ends_at: '2030-01-01T01:00:00Z',
      timezone: 'UTC',
      all_day: false,
      attendees: [],
    }
    const preview = {
      kind: 'calendar',
      provider: 'google',
      account_email: 'sender@synthetic.example.test',
      calendar_name: '',
      operation: 'update',
      before: fields,
      after: fields,
      conflicts: [
        {
          kind: 'partial_sources',
          starts_at: null,
          ends_at: null,
          missing_connection_ids: [CONNECTION_ID],
        },
      ],
      notification_policy: 'none',
      base_etag: 'synthetic-etag',
      compensation_available: true,
      provider_warnings: ['google_send_updates_none_external_sync'],
    }
    const valid = {
      ...value,
      action: 'calendar.update',
      local_action: {
        id: PROPOSAL_ID,
        item_kind: 'calendar_proposal',
        status: 'needs_attention',
        version: 3,
        editor_url: `/calendar/proposals/${PROPOSAL_ID}`,
      },
      approval: { ...value.approval, preview },
    }
    expect(parseActionSnapshot(valid).approval?.preview?.kind).toBe('calendar')
    expect(() =>
      parseActionSnapshot({
        ...valid,
        approval: {
          ...valid.approval,
          preview: { ...preview, notification_policy: undefined },
        },
      }),
    ).toThrow('Invalid action snapshot')
    expect(() =>
      parseActionSnapshot({
        ...valid,
        approval: {
          ...valid.approval,
          preview: { ...preview, provider_warnings: ['unknown'] },
        },
      }),
    ).toThrow('Invalid action snapshot')
  })

  it('validates draft and proposal variants with required versions and enums', () => {
    for (const status of [
      'editing',
      'awaiting_approval',
      'executing',
      'sent',
      'needs_attention',
      'cancelled',
    ])
      expect(parseMailDraft({ ...mailDraft(), status }).status).toBe(status)
    for (const status of [
      'editing',
      'awaiting_approval',
      'executing',
      'applied',
      'stale',
      'needs_attention',
      'cancelled',
    ])
      expect(
        parseCalendarProposal({ ...calendarProposal(), status }).status,
      ).toBe(status)
    for (const mode of ['new', 'reply', 'reply_all'])
      expect(parseMailDraft({ ...mailDraft(), mode }).mode).toBe(mode)
    for (const operation_kind of ['create', 'update', 'restore'])
      expect(
        parseCalendarProposal({ ...calendarProposal(), operation_kind })
          .operation_kind,
      ).toBe(operation_kind)
    expect(() => parseMailDraft({ ...mailDraft(), mode: undefined })).toThrow()
    expect(() =>
      parseMailDraft({ ...mailDraft(), version: undefined }),
    ).toThrow()
    expect(() =>
      parseCalendarProposal({ ...calendarProposal(), version: undefined }),
    ).toThrow()
    expect(() =>
      parseCalendarProposal({
        ...calendarProposal(),
        required_confirmations: ['unknown'],
      }),
    ).toThrow()
    expect(() =>
      parseCalendarProposal({
        ...calendarProposal(),
        availability: {
          proposal_id: PROPOSAL_ID,
          version: 1,
          candidates: [],
          completeness: 'partial',
          missing_connections: [],
          attendee_availability_checked: true,
        },
      }),
    ).toThrow()
  })

  it('validates all capability states, calendar fields and complete weekly settings', () => {
    const value = {
      connection_id: CONNECTION_ID,
      provider: 'microsoft',
      capabilities: [
        'mail.read',
        'mail.send',
        'calendar.read',
        'calendar.write',
      ].map((capability) => ({
        capability,
        status: 'enabled',
        actual_scopes: [],
        last_verified_at: null,
        last_error_code: null,
      })),
      provider_calendars: [
        {
          id: 'synthetic-calendar',
          name: '',
          timezone: 'UTC',
          is_primary: true,
          access_role: 'owner',
          can_write: true,
          provider_url: null,
        },
      ],
    }
    for (const status of [
      'disabled',
      'authorizing',
      'enabled',
      'degraded',
      'action_required',
      'revoked',
    ])
      expect(
        parseConnectionCapabilities({
          ...value,
          capabilities: value.capabilities.map((capability) => ({
            ...capability,
            status,
          })),
        }).capabilities[0]?.status,
      ).toBe(status)
    expect(() =>
      parseConnectionCapabilities({
        ...value,
        capabilities: [{ ...value.capabilities[0], status: undefined }],
      }),
    ).toThrow()
    expect(() =>
      parseConnectionCapabilities({
        ...value,
        provider_calendars: [
          { ...value.provider_calendars[0], can_write: undefined },
        ],
      }),
    ).toThrow()
    expect(parseSettings(userSettings()).working_hours.monday).toEqual([
      ['09:00', '17:00'],
    ])
    expect(() =>
      parseSettings({ ...userSettings(), default_calendar_id: undefined }),
    ).toThrow()
    expect(() =>
      parseSettings({ ...userSettings(), working_hours: { monday: [] } }),
    ).toThrow()
    expect(() =>
      parseSettings({ ...userSettings(), meeting_buffer_minutes: 1.5 }),
    ).toThrow()
  })
})

describe('M2 authenticated clients', () => {
  it('uses no-store requests and serializes only typed list filters', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
        ),
      )
    vi.stubGlobal('fetch', fetcher)
    await listActions({ provider: 'microsoft', status: 'needs_attention' })
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      '/api/v1/actions?provider=microsoft&status=needs_attention',
    )
    expect(fetcher.mock.calls[0]?.[1].credentials).toBe('include')
    expect(fetcher.mock.calls[0]?.[1].cache).toBe('no-store')
  })

  it('accepts minimal manual-resolution responses and sends the exact current version', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            task_id: TASK_ID,
            task_version: '9007199254740994',
          }),
        ),
      )
    vi.stubGlobal('fetch', fetcher)
    const result = await resolveAction(
      TASK_ID,
      'confirmed_not_executed',
      '9007199254740993',
    )
    expect(result.task_version).toBe('9007199254740994')
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/api/v1/actions/${TASK_ID}/manual-resolution`,
    )
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({
      resolution: 'confirmed_not_executed',
      task_version: '9007199254740993',
    })
  })

  it('rejects a snapshot for a different requested task', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(actionSnapshot({ task_id: DRAFT_ID }))),
        ),
    )
    await expect(getAction(TASK_ID)).rejects.toThrow('Invalid action snapshot')
  })

  it('keeps each creation intent key on transport failure and reuses it for retry', async () => {
    const intent = createRequestIntent()
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('Network unavailable'))
      .mockResolvedValueOnce(new Response(JSON.stringify(mailDraft())))
    vi.stubGlobal('fetch', fetcher)
    await expect(
      createMailDraft({ mode: 'new', connection_id: CONNECTION_ID }, intent),
    ).rejects.toThrow('Network unavailable')
    await createMailDraft({ mode: 'new', connection_id: CONNECTION_ID }, intent)
    const first = new Headers(fetcher.mock.calls[0]?.[1].headers).get(
      'Idempotency-Key',
    )
    expect(first).toBeTruthy()
    expect(
      new Headers(fetcher.mock.calls[1]?.[1].headers).get('Idempotency-Key'),
    ).toBe(first)
  })

  it('attaches a nonempty key to every local-object and task creation boundary', async () => {
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      expect(init.method).toBe('POST')
      return new Response(
        JSON.stringify(
          url.endsWith('/proposals') || url.endsWith('/restore-proposal')
            ? calendarProposal()
            : url.endsWith('/sync')
              ? { gmail_task_id: TASK_ID, calendar_task_id: TASK_ID }
              : { task_id: TASK_ID, status: 'queued' },
        ),
      )
    })
    vi.stubGlobal('fetch', fetcher)
    await generateMailDraft(
      DRAFT_ID,
      { version: 1, instruction: '' },
      createRequestIntent(),
    )
    await submitMailDraft(DRAFT_ID, 1, createRequestIntent())
    await createCalendarProposal(
      {
        operation_kind: 'create',
        connection_id: CONNECTION_ID,
        calendar_id: 'synthetic-calendar',
        title: '',
        starts_at: NOW,
        ends_at: '2030-01-01T01:00:00Z',
        timezone: 'UTC',
        all_day: false,
        attendees: [],
      },
      createRequestIntent(),
    )
    await submitCalendarProposal(PROPOSAL_ID, 1, createRequestIntent())
    await createRestoreProposal(
      '00000000-0000-0000-0000-000000000501',
      '00000000-0000-0000-0000-000000000502',
      createRequestIntent(),
    )
    await syncConnection(CONNECTION_ID)
    expect(fetcher).toHaveBeenCalledTimes(6)
    for (const call of fetcher.mock.calls) {
      const init = call[1]
      expect(new Headers(init.headers).get('Idempotency-Key')).toBeTruthy()
      expect(init.credentials).toBe('include')
    }
  })

  it('uses the Microsoft delegated initial read endpoint', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            authorization_url:
              'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
          }),
        ),
      )
    vi.stubGlobal('fetch', fetcher)
    await startMicrosoftConnection(['mail.read'])
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      '/api/v1/connections/microsoft/start',
    )
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({
      capabilities: ['mail.read'],
    })
  })
})
