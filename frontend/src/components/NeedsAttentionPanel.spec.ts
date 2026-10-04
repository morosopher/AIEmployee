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
    expect(wrapper.getByText(/版本或状态已变化，请重新加载后核对/)).toBeVisible()
    expect(wrapper.getByText(/synthetic-trace/)).toBeVisible()
  })

  it('shows factual errors without a live announcement and describes the controls with the no-resend warning', async () => {
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, {
      props: needsAttentionProps(),
    })
    const facts = wrapper.getByRole('note')
    // 自定义 Panel 标题必须继续命名其公开 region，不能留下悬空的 aria-labelledby。
    expect(wrapper.getByRole('region', { name: '先核实供应商中的实际结果' })).toContainElement(facts)
    expect(facts).toHaveTextContent('核对尝试：2 · 最后错误：provider_write_outcome_unknown')
    expect(facts).toHaveAttribute('aria-live', 'off')
    expect(wrapper.queryByRole('alert')).toBeNull()
    expect(wrapper.getByRole('heading', { name: '先核实供应商中的实际结果' })).toBeVisible()
    const link = wrapper.getByRole('link', { name: '在 Google 中检查结果' })
    expect(link).toHaveAttribute('href', 'https://mail.google.com/mail/u/0/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    for (const name of ['重新核对', '确认已执行', '确认未执行']) {
      expect(wrapper.getByRole('button', { name })).toHaveAccessibleDescription(
        '重新核对只读取供应商结果。确认未执行不会自动重发。',
      )
    }
  })

  /** 背景／浮层互斥呈现同一状态；移除任一禁用或将提示留在背景都会违反此约束。 */
  it.each(['confirmed_executed', 'confirmed_not_executed'] as const)(
    'keeps one pending announcement inside the %s dialog and disables every decision control',
    async (resolution) => {
      const props = {
        ...needsAttentionProps(),
        resolve: vi.fn(() => new Promise<void>(() => undefined)),
      }
      const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
      await fireEvent.click(wrapper.getByRole('button', {
        name: resolution === 'confirmed_executed' ? '确认已执行' : '确认未执行',
      }))
      const dialog = await wrapper.findByRole('dialog')
      expect(dialog).toHaveAttribute('aria-modal', 'true')
      await fireEvent.click(within(dialog).getByRole('button', { name: '确认记录结果' }))
      const status = within(dialog).getByRole('status')
      expect(status).toHaveTextContent('正在记录核对请求…')
      expect(status).toHaveAttribute('aria-live', 'polite')
      expect(wrapper.getAllByRole('status')).toHaveLength(1)
      expect(wrapper.getAllByText('正在记录核对请求…')).toHaveLength(1)
      for (const button of within(wrapper.getByRole('region', { name: '人工结果确认' })).getAllByRole('button')) {
        expect(button).toBeDisabled()
      }
      expect(within(dialog).getByRole('button', { name: '取消' })).toBeDisabled()
      expect(within(dialog).getByRole('button', { name: '确认记录结果' })).toBeDisabled()
      expect(props.resolve).toHaveBeenCalledExactlyOnceWith(props.snapshot.task_id, resolution, '9007199254740993')
      expect(props.reconcile).not.toHaveBeenCalled()
    },
  )

  it('keeps reconciliation pending in the panel and disables all three controls', async () => {
    const props = {
      ...needsAttentionProps(),
      reconcile: vi.fn(() => new Promise<void>(() => undefined)),
    }
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    await fireEvent.click(wrapper.getByRole('button', { name: '重新核对' }))
    expect(wrapper.getByRole('status')).toHaveTextContent('正在记录核对请求…')
    expect(wrapper.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(wrapper.getByRole('button', { name: '重新核对' })).toHaveAttribute('aria-busy', 'true')
    for (const button of wrapper.getAllByRole('button')) expect(button).toBeDisabled()
    expect(wrapper.queryByRole('dialog')).toBeNull()
    expect(props.reconcile).toHaveBeenCalledTimes(1)
  })

  it('moves the same recoverable error from the dialog to the panel on cancellation without duplicating it', async () => {
    const props = { ...needsAttentionProps(), reload: vi.fn() }
    props.resolve.mockRejectedValue(new ProblemError({
      type: 'about:blank', title: 'Synthetic', status: 409, detail: '', instance: '',
      error_code: 'manual_resolution_version_conflict', trace_id: 'synthetic-trace',
    }))
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    const dialog = await wrapper.findByRole('dialog')
    await fireEvent.click(within(dialog).getByRole('button', { name: '确认记录结果' }))
    const message = await within(dialog).findByText(/版本或状态已变化，请重新加载后核对/)
    expect(message.closest('[role="alert"]')).toHaveAttribute('aria-live', 'assertive')
    expect(wrapper.getAllByText(/synthetic-trace/)).toHaveLength(1)
    expect(within(dialog).getAllByRole('alert')).toHaveLength(2)
    expect(within(dialog).getByText('此决定只记录人工结果，不会调用供应商写接口。确认未执行不会自动重发；如需再次执行，必须创建新草稿或提案并重新审批。')).toBeVisible()
    expect(wrapper.queryByRole('status')).toBeNull()
    await fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(wrapper.queryByRole('dialog')).toBeNull())
    expect(wrapper.getAllByRole('alert')).toHaveLength(1)
    expect(wrapper.getByRole('alert')).toHaveTextContent('追踪编号：synthetic-trace')
    await fireEvent.click(wrapper.getByRole('button', { name: '重新加载' }))
    expect(props.reload).toHaveBeenCalledTimes(1)
    expect(props.resolve).toHaveBeenCalledTimes(1)
  })

  it('reloads the authoritative snapshot after a conflict and closes the no-longer-actionable dialog', async () => {
    const props = { ...needsAttentionProps(), reload: vi.fn() }
    props.resolve.mockRejectedValue(new ProblemError({
      type: 'about:blank', title: 'Synthetic', status: 409, detail: '', instance: '',
      error_code: 'manual_resolution_version_conflict', trace_id: 'synthetic-trace',
    }))
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    props.reload.mockImplementation(() => wrapper.rerender({ snapshot: actionSnapshot({
      status: 'succeeded', task_version: '9007199254740995', event_cursor: '9007199254740995',
    }) }))
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    const dialog = await wrapper.findByRole('dialog')
    await fireEvent.click(within(dialog).getByRole('button', { name: '确认记录结果' }))
    await fireEvent.click(await within(dialog).findByRole('button', { name: '重新加载' }))
    await waitFor(() => expect(wrapper.queryByRole('dialog')).toBeNull())
    for (const name of ['重新核对', '确认已执行', '确认未执行']) {
      expect(wrapper.getByRole('button', { name })).toBeDisabled()
    }
    expect(props.reload).toHaveBeenCalledTimes(1)
    expect(props.resolve).toHaveBeenCalledTimes(1)
  })

  /** 任务切换仍由原 generation 保护，迟到失败不能污染新对象或重开已关闭的确认。 */
  it('discards a late manual failure after switching tasks', async () => {
    let reject: (cause: unknown) => void = () => undefined
    const props = {
      ...needsAttentionProps(),
      resolve: vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail })),
    }
    const wrapper = await renderWithPlugins(NeedsAttentionPanel, { props })
    await fireEvent.click(wrapper.getByRole('button', { name: '确认已执行' }))
    await fireEvent.click(await wrapper.findByRole('button', { name: '确认记录结果' }))
    await wrapper.rerender({ snapshot: actionSnapshot({ task_id: '00000000-0000-0000-0000-000000000405' }) })
    reject(new Error('Synthetic failure'))
    await waitFor(() => expect(wrapper.queryByRole('dialog')).toBeNull())
    expect(wrapper.queryByRole('status')).toBeNull()
    expect(wrapper.queryByRole('alert')).toBeNull()
    for (const button of wrapper.getAllByRole('button')) expect(button).toBeEnabled()
  })
})
