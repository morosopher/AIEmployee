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

/**
 * 校验已解析邮箱的合计数量与重复项；日程参会人复用同一即时反馈边界。
 * @param groups 经 recipientList 解析的显式 To/CC/BCC 或参会人列表，保留原始大小写。
 * @throws EditorInputError 超过数量限制或出现仅域名大小写不同的重复地址。
 */
export function validateRecipientGroups(...groups: string[][]): void {
  const all = groups.flat()
  if (all.length > 50)
    throw new EditorInputError('To、CC、BCC 合计最多 50 位收件人。')
  const keys = all.map((address) => {
    // 与服务端身份规则一致：本地部分区分大小写；规范键只用于比较，不能回写输入。
    const domainStart = address.indexOf('@') + 1
    return (
      address.slice(0, domainStart) + address.slice(domainStart).toLowerCase()
    )
  })
  if (new Set(keys).size !== all.length)
    throw new EditorInputError('收件人地址重复，请检查 To、CC、BCC。')
}
