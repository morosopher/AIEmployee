import { describe, expect, it } from 'vitest'
import { parseCalendarProposal } from './calendar'
import { calendarProposal } from '@/test-support/actionFixtures'
import { calendarFields } from '@/test-support/editorFixtures'

const EVENT_ID = '00000000-0000-0000-0000-000000abcdef'

/** 从完整可证明的 update 读取形状开始，避免负例被无关缺字段提前拒绝。 */
function withSource() {
  return {
    ...calendarProposal(),
    ...calendarFields(),
    operation_kind: 'update',
    status: 'stale',
    before_snapshot_id: '00000000-0000-0000-0000-000000000502',
    target_event_id: 'synthetic-provider-event',
    base_etag: 'synthetic-etag',
    editor_facts: {
      before_status: 'available',
      before: calendarFields(),
      conflict_status: 'checked',
      conflicts: [],
      restore_source: null,
      reprepare_source: { event_id: EVENT_ID, requires_sync: false },
    },
  }
}

describe('calendar reprepare response boundary', () => {
  it.each([true, false])(
    'preserves the exact local source and requires_sync=%s',
    (requires_sync) => {
      const value = withSource()
      value.editor_facts.reprepare_source.requires_sync = requires_sync
      expect(parseCalendarProposal(value).editor_facts).toHaveProperty(
        'reprepare_source',
        {
          event_id: EVENT_ID,
          requires_sync,
        },
      )
    },
  )

  it.each([
    undefined,
    {},
    { event_id: EVENT_ID },
    { event_id: EVENT_ID, requires_sync: 'false' },
    { event_id: 'synthetic-provider-event', requires_sync: false },
    { event_id: EVENT_ID.toUpperCase(), requires_sync: false },
    { event_id: EVENT_ID, requires_sync: false, extra: true },
  ])('rejects malformed reprepare source (%#)', (reprepare_source) => {
    const value = withSource()
    expect(parseCalendarProposal(value).editor_facts?.before_status).toBe(
      'available',
    )
    expect(() =>
      parseCalendarProposal({
        ...value,
        editor_facts: { ...value.editor_facts, reprepare_source },
      }),
    ).toThrow('Invalid calendar proposal')
  })

  it.each(['awaiting_approval', 'executing', 'needs_attention', 'applied'])(
    'rejects a source attached to %s',
    (status) => {
      const value = withSource()
      expect(parseCalendarProposal(value).status).toBe('stale')
      expect(() => parseCalendarProposal({ ...value, status })).toThrow(
        'Invalid calendar proposal',
      )
    },
  )

  it('requires available original before and the update operation', () => {
    const value = withSource()
    expect(parseCalendarProposal(value).operation_kind).toBe('update')
    expect(() =>
      parseCalendarProposal({ ...value, operation_kind: 'restore' }),
    ).toThrow('Invalid calendar proposal')
    expect(() =>
      parseCalendarProposal({
        ...value,
        editor_facts: {
          ...value.editor_facts,
          before_status: 'unavailable',
          before: null,
        },
      }),
    ).toThrow('Invalid calendar proposal')
    expect(
      parseCalendarProposal({
        ...value,
        editor_facts: { ...value.editor_facts, reprepare_source: null },
      }).editor_facts?.reprepare_source,
    ).toBeNull()
  })
})
