/**
 * @param value Worker Markdown 或已验证 action.editor_url 的链接目标。
 * @returns 两种本地 UUID 编辑路由；query、fragment、任意相对路径均拒绝。
 */
export function focusedEditorPath(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const match =
    /^(?:\/api\/v1)?\/(mail\/drafts|calendar\/proposals)\/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.exec(
      value,
    )
  return match ? `/${match[1]}/${match[2]}` : null
}

/** @param value URL 或消息中的内容无关任务标识。 @returns 严格 UUID，避免把任意查询文本作为 API 路径。 */
export function localTaskId(value: unknown): string | null {
  return typeof value === 'string' &&
    /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)
    ? value
    : null
}
