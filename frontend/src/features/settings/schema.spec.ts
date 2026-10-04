/** 设置 schema 只验证格式；完整七日 tuple 与服务端快照均用合成值。 */
import { describe, expect, it, vi } from 'vitest'
import { userSettings } from '@/test-support/actionFixtures'
import {
  settingsSchema,
  settingsResolver,
  timezoneOptions,
  normalizeBriefTime,
} from './schema'

const days = [
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
] as const

describe('settings format schema', () => {
  it('accepts complete snapshots, zero-second GET precision and multiple intervals or empty days', () => {
    const value = userSettings()
    value.brief_time = '08:00:00'
    value.working_hours.monday = [
      ['09:00', '12:00'],
      ['13:00', '17:00'],
    ]
    expect(settingsSchema.safeParse(value).success).toBe(true)
    expect(normalizeBriefTime(value.brief_time)).toBe('08:00')
    expect(settingsResolver).toBeTypeOf('function')
    for (const day of days) value.working_hours[day] = []
    expect(settingsSchema.safeParse(value).success).toBe(true)
  })

  it.each(days)(
    'rejects each reversed/equal interval on %s without changing tuples',
    (day) => {
      for (const pair of [
        ['17:00', '17:00'],
        ['18:00', '17:00'],
      ] as Array<[string, string]>) {
        for (const index of [0, 1]) {
          const value = userSettings()
          value.working_hours[day] = [
            ['09:00', '12:00'],
            ['13:00', '17:00'],
          ]
          value.working_hours[day][index] = pair
          const result = settingsSchema.safeParse(value)
          expect(result.success).toBe(false)
          if (!result.success)
            expect(result.error.issues[0]?.message).toContain(
              '结束时间必须晚于开始时间',
            )
          expect(value.working_hours[day][index]).toEqual(pair)
        }
      }
    },
  )

  it.each([
    '',
    '24:00',
    '08:60',
    '8:00',
    '08:00:01',
    '08:00:00.001',
    '08:00:00.000',
  ])(
    'rejects unsupported brief precision or syntax %j without truncating',
    (brief_time) => {
      expect(
        settingsSchema.safeParse({ ...userSettings(), brief_time }).success,
      ).toBe(false)
      expect(normalizeBriefTime(brief_time)).toBe(brief_time)
    },
  )

  it('validates locale, integer retention and bounded buffer without duplicating overlap or permissions', () => {
    for (const patch of [
      { locale: '' },
      { locale: '<html>' },
      { meeting_buffer_minutes: 121 },
      { meeting_buffer_minutes: 0.5 },
      { email_body_retention_days: 0 },
      { source_metadata_retention_days: 1.5 },
      { workspace_history_retention_days: 3651 },
      { timezone: 'Mars/Olympus' },
    ]) {
      expect(
        settingsSchema.safeParse({ ...userSettings(), ...patch }).success,
      ).toBe(false)
    }
    const value = userSettings()
    value.working_hours.monday = [
      ['09:00', '17:00'],
      ['10:00', '12:00'],
    ]
    expect(settingsSchema.safeParse(value).success).toBe(true)
    expect(
      settingsSchema.safeParse({ ...userSettings(), meeting_buffer_minutes: 0 })
        .success,
    ).toBe(true)
  })

  it('offers local Intl zones plus UTC and retains current or explicitly legal IANA outside a finite fallback', () => {
    const spy = vi
      .spyOn(Intl, 'supportedValuesOf')
      .mockReturnValue(['Asia/Shanghai'])
    expect(timezoneOptions('Pacific/Chatham')).toEqual([
      'UTC',
      'Asia/Shanghai',
      'Pacific/Chatham',
    ])
    expect(
      settingsSchema.safeParse({
        ...userSettings(),
        timezone: 'Pacific/Chatham',
      }).success,
    ).toBe(true)
    spy.mockRestore()
    const original = Intl.supportedValuesOf
    Object.defineProperty(Intl, 'supportedValuesOf', {
      configurable: true,
      value: undefined,
    })
    try {
      expect(timezoneOptions('Pacific/Chatham')).toEqual([
        'UTC',
        'Pacific/Chatham',
      ])
      expect(
        settingsSchema.safeParse({
          ...userSettings(),
          timezone: 'Pacific/Chatham',
        }).success,
      ).toBe(true)
    } finally {
      Object.defineProperty(Intl, 'supportedValuesOf', {
        configurable: true,
        value: original,
      })
    }
  })
})
