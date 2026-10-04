/** 日程格式校验只验证表示；冲突、账户能力与确认版本不进入 schema。 */
import { describe, expect, it, vi } from 'vitest'
import { calendarSchema, calendarTimezoneOptions } from './schema'

const valid = {
  title: 'Synthetic calendar',
  starts_at: '2030-01-01T09:00',
  ends_at: '2030-01-01T10:00',
  timezone: 'Asia/Shanghai',
  all_day: false,
  attendees: 'First@example.test, second@example.test',
}
describe('calendar format schema', () => {
  it('preserves explicit wall strings and does not impose confirmation or attendee count rules', () => {
    expect(calendarSchema.parse(valid)).toEqual(valid)
    expect(calendarSchema.safeParse({ ...valid, attendees: '' }).success).toBe(
      true,
    )
    expect(
      calendarSchema.safeParse({
        ...valid,
        attendees: 'same@example.test, same@example.test',
      }).success,
    ).toBe(true)
  })
  it.each([
    { title: ' ' },
    { timezone: 'Not/AZone' },
    { timezone: '+08:00' },
    { starts_at: '2030-02-30T09:00' },
    { ends_at: '2030-01-01T08:59' },
    { starts_at: '' },
    { attendees: 'not-an-address' },
    {
      timezone: 'America/New_York',
      starts_at: '2030-03-10T02:30',
      ends_at: '2030-03-10T04:00',
    },
    {
      timezone: 'America/New_York',
      starts_at: '2030-11-03T01:30',
      ends_at: '2030-11-03T04:00',
    },
  ])('rejects invalid formats without guessing offsets: %j', (changes) => {
    expect(calendarSchema.safeParse({ ...valid, ...changes }).success).toBe(
      false,
    )
  })
  it('validates exclusive all-day dates without timezone shifts or date overflow', () => {
    const allDay = {
      ...valid,
      all_day: true,
      starts_at: '2030-02-28',
      ends_at: '2030-03-01',
    }
    expect(calendarSchema.parse(allDay)).toEqual(allDay)
    expect(
      calendarSchema.safeParse({ ...allDay, starts_at: '2030-02-30' }).success,
    ).toBe(false)
    expect(
      calendarSchema.safeParse({ ...allDay, ends_at: '2030-02-28' }).success,
    ).toBe(false)
  })
  it('retains legal current aliases when Intl enumeration is unavailable', () => {
    const descriptor = Object.getOwnPropertyDescriptor(
      Intl,
      'supportedValuesOf',
    )
    Object.defineProperty(Intl, 'supportedValuesOf', {
      configurable: true,
      value: undefined,
    })
    try {
      expect(calendarTimezoneOptions('US/Eastern')).toEqual([
        'UTC',
        'US/Eastern',
      ])
      expect(
        calendarSchema.safeParse({ ...valid, timezone: 'US/Eastern' }).success,
      ).toBe(true)
    } finally {
      if (descriptor)
        Object.defineProperty(Intl, 'supportedValuesOf', descriptor)
      vi.restoreAllMocks()
    }
  })
})
