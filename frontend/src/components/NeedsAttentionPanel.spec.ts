import { fireEvent, waitFor, within } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { describe, expect, it, vi } from 'vitest'
import { actionSnapshot } from '@/test-support/actionFixtures'
import { ProblemError } from '@/api/client'
import NeedsAttentionPanel from './NeedsAttentionPanel.vue'

/** 按真实投影传入 canonical 字符串游标；外部副作用只替换到 API callback 边界。 */
function needsAttentionProps() {
  return {
    snapshot: actionSnapshot(),
    reconcile: vi.fn().mockResolvedValue(undefined),
    resolve: vi.fn().mockResolvedValue(undefined),
  }
}

describe('NeedsAttentionPanel', () => {
  it('requires a separate confirmation and states that not executed will not resend', async () => {
    const props = needsAttentionProps()
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    expect(wrapper.container.textContent).toContain('核对尝试：2')
    expect(wrapper.container.textContent).toContain(
      'provider_write_outcome_unknown',
    )
    await fireEvent.click(wrapper.getByRole('button', { name: '确认未执行' }))
    expect((await wrapper.findByRole('dialog')).textContent).toContain(
      '不会调用供应商写接口',
    )
    expect(
      within(wrapper.getByRole('dialog')).getByRole('alert').textContent,
    ).toContain('不会自动重发')
    expect(props.resolve).not.toHaveBeenCalled()
    await fireEvent.click(wrapper.getByRole('button', { name: '确认记录结果' }))
    expect(props.resolve).toHaveBeenCalledWith(
      props.snapshot.task_id,
      'confirmed_not_executed',
      '9007199254740993',
    )
    expect(Boolean(wrapper.queryByRole('textbox'))).toBe(false)
  })

  it('submits the newest server version and never guesses a result from the response', async () => {
    const props = needsAttentionProps()
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    await wrapper.rerender({
      snapshot: actionSnapshot({
        task_version: '9007199254740995',
        event_cursor: '9007199254740995',
      }),
    })
    await fireEvent.click(wrapper.getByRole('button', { name: '确认记录结果' }))
    expect(props.resolve).toHaveBeenCalledWith(
      props.snapshot.task_id,
      'confirmed_executed',
      '9007199254740995',
    )
    expect(wrapper.container.textContent).not.toContain('已发送')
  })

  it('cancels with Escape and preserves the unresolved operation', async () => {
    const props = needsAttentionProps()
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, {
      props,
    })
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    const dialog = await wrapper.findByRole('dialog')
    await waitFor(() =>
      expect(
        within(dialog).getByRole('button', { name: '取消' }),
      ).toHaveFocus(),
    )
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(wrapper.queryByRole('dialog')).toBeNull())
    expect(props.resolve).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('reconciles read-only and disables overlapping resolution controls', async () => {
    const props = {
      ...needsAttentionProps(),
      reconcile: vi.fn(() => new Promise<void>(() => undefined)),
    }
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    await fireEvent.click(wrapper.getByRole('button', { name: '重新核对' }))
    expect(props.reconcile).toHaveBeenCalledWith(props.snapshot.task_id)
    expect(
      wrapper
        .getByRole('button', { name: '确认已执行' })
        .getAttribute('disabled'),
    ).not.toBeNull()
    expect(props.resolve).not.toHaveBeenCalled()
  })

  it('shows a recoverable conflict and never displays an untrusted provider URL', async () => {
    const props = needsAttentionProps()
    props.snapshot.provider_url = 'javascript:alert(1)'
    props.resolve.mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        detail: '',
        instance: '',
        error_code: 'manual_resolution_version_conflict',
        trace_id: 'synthetic-trace',
      }),
    )
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    expect(Boolean(wrapper.queryByRole('link'))).toBe(false)
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    await fireEvent.click(wrapper.getByRole('button', { name: '确认记录结果' }))
    await Promise.resolve()
    expect(wrapper.container.textContent).toContain('重新加载')
    expect(wrapper.container.textContent).toContain('synthetic-trace')
  })
})
