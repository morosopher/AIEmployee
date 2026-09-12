import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseCalendarProposal, updateCalendarProposal } from './calendar'
import {
  calendarProposal,
  CONNECTION_ID,
  PROPOSAL_ID,
} from '@/test-support/actionFixtures'
import { calendarFields } from '@/test-support/editorFixtures'

afterEach(() => vi.unstubAllGlobals())

/** 用完整合法事实作为每个负例的起点；不依赖服务端未完成的 shell 掩盖解析错误。 */
function proposalWithFacts() {
  return {
    ...calendarProposal(),
    ...calendarFields(),
    editor_facts: {
      reprepare_source: null,
      restore_source: null,
      before_status: 'not_applicable',
      before: null,
      conflict_status: 'checked',
      conflicts: [],
    },
  }
}

describe('calendar editor response boundary', () => {
  it('preserves checked facts and never turns incomplete facts into an empty result', () => {
    expect(parseCalendarProposal(proposalWithFacts())).toHaveProperty(
      'editor_facts.conflict_status',
      'checked',
    )
    const incomplete = {
      reprepare_source: null,
      restore_source: null,
      before_status: 'not_applicable',
      before: null,
      conflict_status: 'incomplete',
      conflicts: null,
    }
    expect(
      parseCalendarProposal({
        ...calendarProposal(),
        editor_facts: incomplete,
      }),
    ).toHaveProperty('editor_facts', incomplete)
    expect(
      parseCalendarProposal({ ...calendarProposal(), editor_facts: null }),
    ).toHaveProperty('editor_facts', null)
  })

  it.each([
    { conflict_status: 'checked', conflicts: null },
    { conflict_status: 'incomplete', conflicts: [] },
    { conflict_status: 'maybe' },
    { conflict_status: undefined },
    { reprepare_source: null, restore_source: null, before_status: 'available', before: null },
    {
      reprepare_source: null,
      restore_source: null,
      before_status: 'not_applicable',
      before: calendarFields(),
    },
    {
      reprepare_source: null,
      restore_source: null,
      before_status: 'unavailable',
      before: calendarFields(),
    },
    { reprepare_source: null, restore_source: null, before_status: 'missing' },
    { extra: true },
    {
      conflicts: [
        {
          kind: 'free_busy',
          starts_at: null,
          ends_at: null,
          missing_connection_ids: [],
        },
      ],
    },
  ])('rejects inconsistent editor facts without a fallback (%#)', (invalid) => {
    const value = proposalWithFacts()
    expect(() =>
      parseCalendarProposal({
        ...value,
        editor_facts: { ...value.editor_facts, ...invalid },
      }),
    ).toThrow('Invalid calendar proposal')
  })

  it('retains the original before snapshot for update and rejects offset-free times', () => {
    const value = {
      ...proposalWithFacts(),
      operation_kind: 'update',
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'available',
        before: calendarFields(),
        conflict_status: 'checked',
        conflicts: [],
      },
    }
    expect(parseCalendarProposal(value)).toHaveProperty(
      'editor_facts.before.starts_at',
      calendarFields().starts_at,
    )
    expect(() =>
      parseCalendarProposal({
        ...value,
        editor_facts: {
          ...value.editor_facts,
          before: { ...calendarFields(), starts_at: '2030-01-01T09:00:00' },
        },
      }),
    ).toThrow('Invalid calendar proposal')
  })

  it('sends one explicit confirmation and preserves the exact optimistic version', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ ...calendarProposal(), editor_facts: null }),
          { status: 200 },
        ),
      )
    vi.stubGlobal('fetch', fetch)
    await updateCalendarProposal(PROPOSAL_ID, {
      version: 3,
      confirmation: {
        kind: 'calendar',
        connection_id: CONNECTION_ID,
        calendar_id: 'synthetic-calendar',
      },
    })
    expect(fetch.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({
        version: 3,
        confirmation: {
          kind: 'calendar',
          connection_id: CONNECTION_ID,
          calendar_id: 'synthetic-calendar',
        },
      }),
    )
  })
})
