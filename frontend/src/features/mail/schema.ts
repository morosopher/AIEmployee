/** 邮件展示格式校验不改变输入；收件人规模、重复身份及能力／版本仍由原 hook 与服务端裁决。 */
import { z } from 'zod'
import { zodResolver } from '@primevue/forms/resolvers/zod'
import { recipientList } from '@/features/actions/editorInput'

/** @param required To 必须非空；CC/BCC 可空。复用既有解析以保留分隔符和地址大小写规则。 */
function recipients(required: boolean) {
  return z.string().superRefine((value, context) => {
    try {
      if (required && recipientList(value).length === 0) {
        context.addIssue({ code: 'custom', message: '请至少填写一位收件人。' })
      } else recipientList(value)
    } catch {
      context.addIssue({
        code: 'custom',
        message: '请填写有效的邮箱地址，多个地址用逗号分隔。',
      })
    }
  })
}

/** 不使用 trim/transform 回写输入；主题按 Unicode 字符计数，与服务端 255 字符契约一致。 */
export const mailSchema = z.object({
  to: recipients(true),
  cc: recipients(false),
  bcc: recipients(false),
  subject: z
    .string()
    .refine((value) => value.trim().length > 0, '请填写主题。')
    .refine(
      (value) => Array.from(value).length <= 255,
      '主题最多 255 个字符。',
    ),
  body_text: z
    .string()
    .refine((value) => value.trim().length > 0, '请填写纯文本正文。'),
})

/** 真实 Form 使用同一 resolver；仅格式有效时才允许显式保存或提交已审阅版本。 */
export const mailResolver = zodResolver(mailSchema)
