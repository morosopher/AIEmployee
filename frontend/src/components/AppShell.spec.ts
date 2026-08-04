import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

import AppShell from './AppShell.vue'
import { getSystemAlerts } from '@/api/system'

vi.mock('@/api/system', () => ({ getSystemAlerts: vi.fn().mockResolvedValue({ alerts: [{ code: 'daily_brief_overdue', severity: 'critical', local_date: '2026-08-04', diagnostic_task_id: 'diag-1' }] }) }))

describe('AppShell', () => {
  it('shows alert loading state before the first snapshot arrives', () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: AppShell }] })
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router], stubs: { RouterView: true } } })
    expect(wrapper.text()).toContain('正在检查系统告警')
  })

  it('shows a persistent overdue alert and diagnostic task link', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: AppShell }] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router], stubs: { RouterView: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('每日简报已逾期')
    expect(wrapper.find('a[href="/tasks?task_id=diag-1"]').exists()).toBe(true)
  })

  it('clears the banner when a later alert snapshot is empty', async () => {
    vi.useFakeTimers()
    vi.mocked(getSystemAlerts).mockResolvedValueOnce({ alerts: [{ code: 'daily_brief_overdue', severity: 'critical', local_date: '2026-08-04', diagnostic_task_id: 'diag-1' }] }).mockResolvedValueOnce({ alerts: [] })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: AppShell }] })
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router], stubs: { RouterView: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('每日简报已逾期')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(wrapper.text()).not.toContain('每日简报已逾期')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('does not let an older empty alert response overwrite a newer critical alert', async () => {
    vi.useFakeTimers()
    let resolveOlder: ((value: { alerts: [] }) => void) | undefined
    let resolveNewer: ((value: { alerts: [{ code: 'daily_brief_overdue'; severity: 'critical'; local_date: string; diagnostic_task_id: string }] }) => void) | undefined
    const older = new Promise<{ alerts: [] }>((resolve) => { resolveOlder = resolve })
    const newer = new Promise<{ alerts: [{ code: 'daily_brief_overdue'; severity: 'critical'; local_date: string; diagnostic_task_id: string }] }>((resolve) => { resolveNewer = resolve })
    vi.mocked(getSystemAlerts).mockReset().mockImplementationOnce(() => older).mockImplementationOnce(() => newer)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: AppShell }] })
    const wrapper = mount(AppShell, { global: { plugins: [createPinia(), router], stubs: { RouterView: true } } })
    await vi.advanceTimersByTimeAsync(60_000)
    resolveNewer?.({ alerts: [{ code: 'daily_brief_overdue', severity: 'critical', local_date: '2026-08-04', diagnostic_task_id: 'diag-1' }] })
    await flushPromises()
    resolveOlder?.({ alerts: [] })
    await flushPromises()
    expect(wrapper.text()).toContain('每日简报已逾期')
    wrapper.unmount(); vi.useRealTimers()
  })
})
