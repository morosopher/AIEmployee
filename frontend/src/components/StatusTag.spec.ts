/** 状态必须显示设计层冻结的中文文字，不以颜色替代业务含义。 */
import { describe, expect, it } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import StatusTag from './StatusTag.vue'

describe('StatusTag', () => {
  it('shows the task status text and updates when the server value changes', async () => {
    const { getByText, rerender } = await renderWithPlugins(StatusTag, {
      props: { kind: 'task', value: 'reconciling' },
    })
    expect(getByText('正在核对')).toBeVisible()
    await rerender({ value: 'needs_attention' })
    expect(getByText('需要人工确认')).toBeVisible()
  })

  it.each([
    { kind: 'action', value: 'editing', label: '编辑中' },
    { kind: 'capability', value: 'revoked', label: '已撤销' },
    { kind: 'approval', value: 'approved', label: '已批准' },
    { kind: 'connection', value: 'connected', label: '已连接' },
  ] as const)('shows the $kind label', async (entry) => {
    const { getByText } = await renderWithPlugins(StatusTag, { props: entry })
    expect(getByText(entry.label)).toBeVisible()
  })
})
