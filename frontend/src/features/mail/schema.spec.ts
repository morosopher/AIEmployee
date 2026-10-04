/** 邮件展示格式校验：复用原邮箱解析，数量／重复／审批规则仍由原领域边界裁决。 */
import { describe, expect, it } from 'vitest'
import { mailSchema } from './schema'

const valid = {
  to: 'CaseUser@MAIL.EXAMPLE.TEST',
  cc: '',
  bcc: '',
  subject: '合成主题',
  body_text: '  合成纯文本 <b>不会渲染</b>\n',
}

describe('mailSchema', () => {
  it('preserves original addresses, separators and body without normalization', () => {
    const input = {
      ...valid,
      to: 'One@example.test；Two@EXAMPLE.TEST\nThree@example.test，Four@example.test; Five@example.test',
    }
    expect(mailSchema.parse(input)).toEqual(input)
  })
  it.each(['to', 'cc', 'bcc'] as const)(
    'rejects a malformed item in %s without echoing it',
    (field) => {
      const result = mailSchema.safeParse({
        ...valid,
        [field]: 'synthetic-invalid',
      })
      expect(result.success).toBe(false)
      if (!result.success) {
        expect(result.error.issues[0]?.path).toEqual([field])
        expect(result.error.message).not.toContain('synthetic-invalid')
      }
    },
  )
  it.each(['to', 'subject', 'body_text'] as const)(
    'requires nonblank %s',
    (field) => {
      expect(mailSchema.safeParse({ ...valid, [field]: ' \n ' }).success).toBe(
        false,
      )
    },
  )
  it('uses the actual 255 Unicode character subject limit without truncation', () => {
    expect(
      mailSchema.parse({ ...valid, subject: '😀'.repeat(255) }).subject,
    ).toBe('😀'.repeat(255))
    expect(
      mailSchema.safeParse({ ...valid, subject: '😀'.repeat(256) }).success,
    ).toBe(false)
  })
  it('does not duplicate recipient count or cross-field identity rules', () => {
    expect(mailSchema.safeParse({ ...valid, cc: valid.to }).success).toBe(true)
    expect(
      mailSchema.safeParse({
        ...valid,
        to: Array.from({ length: 51 }, (_, i) => `p${i}@example.test`).join(
          ',',
        ),
      }).success,
    ).toBe(true)
  })
})
