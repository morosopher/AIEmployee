import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { getCurrentUser, getTask, ProblemError } from '@/api/client'
import { requestSourceCacheDeletion } from '@/api/privacy'
import { createMemoryHistory, createRouter } from 'vue-router'
import SettingsPage from './SettingsPage.vue'

vi.mock('@/api/privacy', () => ({
  requestSourceCacheDeletion: vi.fn().mockResolvedValue({ task_id: 'source-task', status: 'queued' }),
  requestAllDataDeletion: vi.fn().mockResolvedValue({ task_id: 'all-task', status: 'queued' }),
}))
vi.mock('@/api/auth', () => ({ listSessions: vi.fn().mockResolvedValue([]), revokeSession: vi.fn() }))
vi.mock('@/api/settings', () => ({ getSettings: vi.fn().mockResolvedValue({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365, updated_at: '2026-08-04T00:00:00Z' }), updateSettings: vi.fn() }))
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, getTask: vi.fn().mockResolvedValue({ id: 'source-task', kind: 'privacy.source_cache_deletion', status: 'queued', retry_of_task_id: null, error_code: null, event_cursor: '0', steps: [] }), getCurrentUser: vi.fn() }
})

describe('SettingsPage', () => {
  it('renders retention controls and saves settings', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365, updated_at: '2026-08-04T00:00:00Z' }) }))
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a :href="to.path + \'?task_id=\' + to.query.task_id"><slot /></a>', props: ['to'] } } } })
    expect(wrapper.text()).toContain('邮件正文保留天数')
  })

  it('requires exact confirmation and links accepted deletion tasks', async () => {
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a :href="to.path + \'?task_id=\' + to.query.task_id"><slot /></a>', props: ['to'] } } } })
    // 用户输入确认前必须可见外部副作用说明；本地删除成功不能被理解为供应商撤回成功。
    expect(wrapper.text()).toContain('删除本地数据不能撤回已发送的邮件或已生效的日程变更。')
    expect(wrapper.text()).toContain('结果未知的操作也可能已在供应商侧生效。')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    expect(allDataButton).toBeDefined()
    if (!allDataButton) throw new Error('All-data button missing')
    await allDataButton.trigger('click')
    expect(wrapper.text()).toContain('DELETE ALL DATA')
    const input = wrapper.findAll('input').find((node) => node.attributes('autocomplete') === 'off')
    expect(input).toBeDefined()
    if (!input) throw new Error('Confirmation input missing')
    await input.setValue('DELETE ALL DATA')
    await allDataButton.trigger('click')
    await flushPromises()
    expect(wrapper.find('a[href="/tasks?task_id=all-task"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/tasks?task_id=source-task"]').exists()).toBe(false)
  })

  it('keeps both destructive controls disabled after a 202 task receipt', async () => {
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a :href="to.path + \'?task_id=\' + to.query.task_id"><slot /></a>', props: ['to'] } } } })
    const sourceButton = wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!sourceButton || !allDataButton) throw new Error('Deletion controls missing')
    await sourceButton.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')?.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find((button) => button.text() === '删除全部数据')?.attributes('disabled')).toBeDefined()
    expect(wrapper.find('a[href="/tasks?task_id=source-task"]').exists()).toBe(true)
  })

  it('submits a source-cache deletion only once during a rapid double click', async () => {
    vi.mocked(requestSourceCacheDeletion).mockClear()
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const sourceButton = wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')
    if (!sourceButton) throw new Error('Source deletion control missing')
    await Promise.all([sourceButton.trigger('click'), sourceButton.trigger('click')]); await flushPromises()
    expect(requestSourceCacheDeletion).toHaveBeenCalledTimes(1)
  })

  it('does not start task polling when an unmounted deletion request later resolves', async () => {
    vi.useFakeTimers(); vi.mocked(getTask).mockClear()
    let resolveRequest: ((value: { task_id: string; status: string }) => void) | undefined
    vi.mocked(requestSourceCacheDeletion).mockImplementationOnce(() => new Promise((resolve) => { resolveRequest = resolve }))
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const sourceButton = wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')
    if (!sourceButton) throw new Error('Source deletion control missing')
    await sourceButton.trigger('click'); wrapper.unmount()
    resolveRequest?.({ task_id: 'source-task', status: 'queued' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(4_000)
    expect(getTask).not.toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('restores controls and shows failure after the accepted task becomes failed', async () => {
    vi.mocked(getTask).mockResolvedValueOnce({ id: 'source-task', kind: 'privacy.source_cache_deletion', status: 'failed', retry_of_task_id: null, error_code: 'cleanup_failed', event_cursor: '1', steps: [] })
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const sourceButton = wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!sourceButton || !allDataButton) throw new Error('Deletion controls missing')
    await sourceButton.trigger('click'); await flushPromises()
    expect(sourceButton.attributes('disabled')).toBeUndefined()
    expect(allDataButton.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('删除任务失败')
  })

  it('redirects when all-data task polling returns the expected revoked-session status', async () => {
    vi.mocked(getTask).mockRejectedValueOnce(new ProblemError({ type: 'about:blank', title: 'Unauthorized', status: 401, detail: '', instance: '', error_code: 'session_expired', trace_id: 'test' }))
    vi.mocked(getCurrentUser).mockRejectedValueOnce(new ProblemError({ type: 'about:blank', title: 'Unauthorized', status: 401, detail: '', instance: '', error_code: 'session_expired', trace_id: 'test' }))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsPage }, { path: '/login', component: { template: '<p>login</p>' } }] })
    await router.push('/settings'); await router.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), router], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const confirmation = wrapper.findAll('input').find((node) => node.attributes('autocomplete') === 'off')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!confirmation || !allDataButton) throw new Error('All-data controls missing')
    await confirmation.setValue('DELETE ALL DATA'); await allDataButton.trigger('click'); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(getCurrentUser).toHaveBeenCalledTimes(1)
  })

  it('keeps deletion pending when missing all-data task does not mean the session was revoked', async () => {
    vi.mocked(getTask).mockRejectedValueOnce(new ProblemError({ type: 'about:blank', title: 'Not found', status: 404, detail: '', instance: '', error_code: 'task_not_found', trace_id: 'test' }))
    vi.mocked(getCurrentUser).mockResolvedValueOnce({ id: 'user-1', email: 'admin@example.test', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsPage }, { path: '/login', component: { template: '<p>login</p>' } }] })
    await router.push('/settings'); await router.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), router], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const confirmation = wrapper.findAll('input').find((node) => node.attributes('autocomplete') === 'off')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!confirmation || !allDataButton) throw new Error('All-data controls missing')
    await confirmation.setValue('DELETE ALL DATA'); await allDataButton.trigger('click'); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
    expect(wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')?.attributes('disabled')).toBeDefined()
    expect(allDataButton.attributes('disabled')).toBeDefined()
  })

  it('keeps deletion pending when the follow-up session check temporarily fails', async () => {
    vi.mocked(getTask).mockRejectedValueOnce(new ProblemError({ type: 'about:blank', title: 'Not found', status: 404, detail: '', instance: '', error_code: 'task_not_found', trace_id: 'test' }))
    vi.mocked(getCurrentUser).mockRejectedValueOnce(new Error('temporary network failure'))
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsPage }, { path: '/login', component: { template: '<p>login</p>' } }] })
    await router.push('/settings'); await router.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), router], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const confirmation = wrapper.findAll('input').find((node) => node.attributes('autocomplete') === 'off')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!confirmation || !allDataButton) throw new Error('All-data controls missing')
    await confirmation.setValue('DELETE ALL DATA'); await allDataButton.trigger('click'); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
    expect(wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')?.attributes('disabled')).toBeDefined()
    expect(allDataButton.attributes('disabled')).toBeDefined()
  })

  it('keeps destructive controls pending when all-data task succeeds but the session remains valid', async () => {
    vi.mocked(getTask).mockResolvedValueOnce({ id: 'all-task', kind: 'privacy.all_data_deletion', status: 'succeeded', retry_of_task_id: null, error_code: null, event_cursor: '1', steps: [] })
    vi.mocked(getCurrentUser).mockResolvedValueOnce({ id: 'user-1', email: 'admin@example.test', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsPage }, { path: '/login', component: { template: '<p>login</p>' } }] })
    await router.push('/settings'); await router.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), router], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
    const confirmation = wrapper.findAll('input').find((node) => node.attributes('autocomplete') === 'off')
    const allDataButton = wrapper.findAll('button').find((button) => button.text() === '删除全部数据')
    if (!confirmation || !allDataButton) throw new Error('All-data controls missing')
    await confirmation.setValue('DELETE ALL DATA'); await allDataButton.trigger('click'); await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
    expect(wrapper.findAll('button').find((button) => button.text() === '删除来源缓存')?.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find((button) => button.text() === '删除全部数据')?.attributes('disabled')).toBeDefined()
  })
})
