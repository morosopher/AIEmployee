/** 公开错误只输出安全中文映射和追踪号；服务端 detail、未知 title 与堆栈不得泄露。 */
import { fireEvent } from '@testing-library/vue'
import { describe, expect, it } from 'vitest'
import { ProblemError } from '@/api/client'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import ProblemMessage from './ProblemMessage.vue'

describe('ProblemMessage', () => {
  it('announces one safe error with a trace ID and optional recovery action', async () => {
    const problem = new ProblemError({
      type: 'about:blank',
      title: 'Conflict',
      status: 409,
      detail: '<b>unsafe detail</b>',
      instance: '',
      trace_id: 'trace-1',
      error_code: 'approval_version_conflict',
    })
    const { getAllByRole, getByRole, getByText, queryByText, emitted } =
      await renderWithPlugins(ProblemMessage, {
        props: { problem, actionLabel: '重新加载' },
      })
    expect(getAllByRole('alert')).toHaveLength(1)
    expect(getByText('审批版本已变化，请重新加载后再试。')).toBeVisible()
    expect(getByText('trace-1')).toBeVisible()
    expect(queryByText(/unsafe detail/)).toBeNull()
    expect(queryByText(/ProblemError/)).toBeNull()
    await fireEvent.click(getByRole('button', { name: '重新加载' }))
    expect(emitted().action).toHaveLength(1)
  })

  it('uses a safe fallback for unknown errors and omits an unspecified action', async () => {
    const problem = new ProblemError({
      type: 'about:blank',
      title: '<img src=x> private upstream response',
      status: 500,
      detail: 'private detail',
      instance: '',
      trace_id: 'trace-2',
      error_code: 'unknown_future_code',
    })
    const { getByText, queryByText, queryByRole } = await renderWithPlugins(
      ProblemMessage,
      { props: { problem } },
    )
    expect(getByText('操作未能完成，请稍后重试。')).toBeVisible()
    expect(queryByText(/private/)).toBeNull()
    expect(queryByRole('button')).toBeNull()
  })
})
