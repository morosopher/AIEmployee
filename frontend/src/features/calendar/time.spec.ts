import { describe, expect, it } from 'vitest'
import { instantToWall, wallToInstant } from './time'

describe('explicit IANA calendar inputs', () => {
  it('converts using the selected zone rather than the browser zone', () => {
    expect(instantToWall('2030-01-01T01:00:00Z', 'Asia/Shanghai')).toBe(
      '2030-01-01T09:00',
    )
    expect(wallToInstant('2030-01-01T09:00', 'Asia/Shanghai')).toBe(
      '2030-01-01T01:00:00.000Z',
    )
    expect(wallToInstant('2030-01-01T09:00', 'America/New_York')).toBe(
      '2030-01-01T14:00:00.000Z',
    )
  })
  it.each(['2030-03-10T02:30', '2030-11-03T01:30'])(
    'refuses DST gaps or overlaps instead of guessing (%s)',
    (wall) => {
      expect(() => wallToInstant(wall, 'America/New_York')).toThrow()
    },
  )
})
