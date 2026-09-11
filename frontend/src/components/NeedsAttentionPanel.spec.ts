import { flushPromises, mount } from '@vue/test-utils'
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
    const wrapper = mount(NeedsAttentionPanel, { props })
    expect(wrapper.text()).toContain('核对尝试：2')
    expect(wrapper.text()).toContain('provider_write_outcome_unknown')
    await wrapper.get('button[name="confirmed_not_executed"]').trigger('click')
    expect(wrapper.get('[role="dialog"]').text()).toContain(
      '不会调用供应商写接口',
    )
    expect(wrapper.get('[role="alert"]').text()).toContain('不会自动重发')
    expect(props.resolve).not.toHaveBeenCalled()
    await wrapper.get('button[name="confirm-resolution"]').trigger('click')
    expect(props.resolve).toHaveBeenCalledWith(
      props.snapshot.task_id,
      'confirmed_not_executed',
      '9007199254740993',
    )
    expect(wrapper.find('textarea').exists()).toBe(false)
  })

  it('submits the newest server version and never guesses a result from the response', async () => {
    const props = needsAttentionProps()
    const wrapper = mount(NeedsAttentionPanel, { props })
    await wrapper.get('button[name="confirmed_executed"]').trigger('click')
    await wrapper.setProps({
      snapshot: actionSnapshot({
        task_version: '9007199254740995',
        event_cursor: '9007199254740995',
      }),
    })
    await wrapper.get('button[name="confirm-resolution"]').trigger('click')
    expect(props.resolve).toHaveBeenCalledWith(
      props.snapshot.task_id,
      'confirmed_executed',
      '9007199254740995',
    )
    expect(wrapper.text()).not.toContain('已发送')
  })

  it('cancels with Escape and preserves the unresolved operation', async () => {
    const props = needsAttentionProps()
    const wrapper = mount(NeedsAttentionPanel, {
      props,
      attachTo: document.body,
    })
    await wrapper.get('button[name="confirmed_executed"]').trigger('click')
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(props.resolve).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('reconciles read-only and disables overlapping resolution controls', async () => {
    const props = {
      ...needsAttentionProps(),
      reconcile: vi.fn(() => new Promise<void>(() => undefined)),
    }
    const wrapper = mount(NeedsAttentionPanel, { props })
    await wrapper.get('button[name="reconcile"]').trigger('click')
    expect(props.reconcile).toHaveBeenCalledWith(props.snapshot.task_id)
    expect(
      wrapper.get('button[name="confirmed_executed"]').attributes('disabled'),
    ).toBeDefined()
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
    const wrapper = mount(NeedsAttentionPanel, { props })
    expect(wrapper.find('a').exists()).toBe(false)
    await wrapper.get('button[name="confirmed_executed"]').trigger('click')
    await wrapper.get('button[name="confirm-resolution"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('重新加载')
    expect(wrapper.text()).toContain('synthetic-trace')
  })
})
