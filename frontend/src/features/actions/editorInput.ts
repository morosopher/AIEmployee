/** 编辑器基础输入校验只改善即时反馈，服务端仍验证精确类型与业务约束。 */
export class EditorInputError extends Error {}

/**
 * @param text 用户输入的纯邮箱列表，支持逗号、分号及换行分隔。
 * @returns 显式收件人；不查通讯录，也不猜测或补全地址。
 * @throws EditorInputError 只包含固定文案，不回显用户输入。
 */
export function recipientList(text: string): string[] {
  const addresses = text
    .split(/[,;\n，；]/)
    .map((value) => value.trim())
    .filter(Boolean)
  if (
    addresses.some(
      (address) => !/^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$/.test(address),
    )
  )
    throw new EditorInputError('请填写有效的邮箱地址，多个地址用逗号分隔。')
  return addresses
}

/** @param groups To/CC/BCC 的显式列表。 @throws EditorInputError 超过单封邮件限制或重复地址。 */
export function validateRecipientGroups(...groups: string[][]): void {
  const all = groups.flat()
  if (all.length > 50)
    throw new EditorInputError('To、CC、BCC 合计最多 50 位收件人。')
  if (new Set(all.map((address) => address.toLowerCase())).size !== all.length)
    throw new EditorInputError('收件人地址重复，请检查 To、CC、BCC。')
}
