/**
 * M2 响应解析的原子验证器：缺字段与非法枚举立即失败，不输出输入值。此模块只维护线格式和基本类型，不推断业务迁移；完整记录解析器负责字段之间的身份和版本绑定。
 */
import { asEventCursor, asJsonObject, compareEventCursors } from './types'

/** M2 解析器共享原子字段验证，不承担领域状态转换或请求行为。 */
export function record(value: unknown): Record<string, unknown> {
  const result = asJsonObject(value)
  if (!result) throw new Error('Invalid response object')
  return result
}
/**
 * @param value 不可信字段。
 * @returns 原始文本。
 * @throws Error 字段缺失或非字符串。
 */
export function text(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Invalid response string')
  return value
}
/**
 * @param value 不可信 UUID。
 * @returns 完整本地身份。
 * @throws Error 非 UUID。
 */
export function uuid(value: unknown): string {
  const result = text(value)
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(result))
    throw new Error('Invalid response identifier')
  return result
}
/**
 * @param value 可空但不可省略的字段。
 * @param parse 非空分支解析器。
 * @returns 严格可空结果。
 */
export function nullable<T>(
  value: unknown,
  parse: (value: unknown) => T,
): T | null {
  return value === null ? null : parse(value)
}
/**
 * @param value 不可信枚举。
 * @param values 接受的封闭取值。
 * @returns 已收窄枚举。
 */
export function enumeration<T extends string>(
  value: unknown,
  values: readonly T[],
): T {
  const result = values.find((candidate) => candidate === value)
  if (result === undefined) throw new Error('Invalid response enum')
  return result
}
/**
 * @param value 不可信布尔值。
 * @returns 不作 truthy 转换的布尔值。
 */
export function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new Error('Invalid response boolean')
  return value
}
/**
 * @param value 不可信计数/版本。
 * @param minimum 版本从一开始。
 * @returns 安全整数。
 */
export function integer(value: unknown, minimum = 0): number {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < minimum
  )
    throw new Error('Invalid response integer')
  return value
}
/**
 * @param value 不可信数组。
 * @param parse 元素验证器。
 * @returns 不丢弃非法元素的新数组。
 */
export function array<T>(value: unknown, parse: (value: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new Error('Invalid response array')
  return value.map(parse)
}
/**
 * @param value 不可信 RFC3339 时刻。
 * @returns 显式时区的原始字符串，避免本地时区推断。
 */
export function timestamp(value: unknown): string {
  const result = text(value)
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
      result,
    ) ||
    !Number.isFinite(Date.parse(result))
  )
    throw new Error('Invalid response timestamp')
  return result
}
/**
 * @param value 审计 ID/版本。
 * @returns PostgreSQL BIGINT 范围内的规范十进制字符串。
 */
export function cursor(value: unknown): string {
  const result = asEventCursor(value)
  if (result === null || compareEventCursors(result, '9223372036854775807') > 0)
    throw new Error('Invalid response cursor')
  return result
}
/**
 * @param value 真实分页响应。
 * @param parse 项目验证器。
 * @returns 带有明确边界的页面。
 */
export function page<T>(
  value: unknown,
  parse: (value: unknown) => T,
): { items: T[]; limit: number; offset: number } {
  const object = record(value)
  const limit = integer(object.limit, 1)
  if (limit > 100) throw new Error('Invalid response limit')
  return {
    items: array(object.items, parse),
    limit,
    offset: integer(object.offset),
  }
}
/**
 * @param value 外部响应。
 * @param parse 具体解析。
 * @param message 无内容错误。
 * @returns 已验证结果。
 */
export function boundary<T>(
  value: unknown,
  parse: (value: unknown) => T,
  message: string,
): T {
  try {
    return parse(value)
  } catch {
    throw new Error(message)
  }
}
