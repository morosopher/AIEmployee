import { EditorInputError } from '@/features/actions/editorInput'

/**
 * @param instant 带 offset 的服务端时刻。
 * @param timezone 用户明确选择的 IANA 时区。
 * @returns datetime-local 可读值；从不使用宿主机本地时区。
 * @throws EditorInputError 时区或时刻非法。
 */
export function instantToWall(instant: string, timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(new Date(instant))
    const part = (name: Intl.DateTimeFormatPartTypes): string =>
      parts.find((value) => value.type === name)?.value ?? ''
    const wall = `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`
    return part('second') === '00' ? wall : `${wall}:${part('second')}`
  } catch {
    throw new EditorInputError('请填写有效的 IANA 时区和时间。')
  }
}

/**
 * @param wall 用户输入的墙上时间。
 * @param timezone 明确选择的 IANA 时区。
 * @returns 唯一匹配的 UTC 时刻；DST 跳跃或重复时段必须由用户另选明确时间。
 * @throws EditorInputError 无效、不存在或不唯一的本地时间；不静默推断 offset。
 */
export function wallToInstant(wall: string, timezone: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(wall))
    throw new EditorInputError('请填写完整的日期和时间。')
  const normalized = wall.length === 16 ? `${wall}:00` : wall
  const naive = Date.parse(`${normalized}Z`)
  if (
    !Number.isFinite(naive) ||
    new Date(naive).toISOString().slice(0, 19) !== normalized
  )
    throw new EditorInputError('日期或时间无效。')
  const candidates = new Set<number>()
  // 从相邻时段采集该 IANA 区的真实 offset，覆盖 DST 前后；不把第一个匹配当成唯一。
  for (let hours = -36; hours <= 36; hours += 1) {
    const probe = naive + hours * 3_600_000
    const probeWall = instantToWall(new Date(probe).toISOString(), timezone)
    const offset =
      Date.parse(
        `${probeWall.length === 16 ? `${probeWall}:00` : probeWall}Z`,
      ) - probe
    const candidate = naive - offset
    const actual = instantToWall(new Date(candidate).toISOString(), timezone)
    if ((actual.length === 16 ? `${actual}:00` : actual) === normalized)
      candidates.add(candidate)
  }
  if (candidates.size !== 1)
    throw new EditorInputError(
      '该时间处于夏令时跳跃或重复时段，请选择其他时间或改用 UTC 后填写明确时刻。',
    )
  const candidate = candidates.values().next().value
  if (candidate === undefined) throw new EditorInputError('无法确定日程时间。')
  return new Date(candidate).toISOString()
}
