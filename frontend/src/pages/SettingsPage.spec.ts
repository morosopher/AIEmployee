/** 页面保留隐私／会话状态机；测试仅通过角色与 label 操作真实控件。 */
import {
  DOMWrapper,
  enableAutoUnmount,
  flushPromises,
  mount,
  type VueWrapper,
} from '@vue/test-utils'
import {
  getByLabelText,
  getByRole,
  getAllByRole,
  queryByRole,
} from '@testing-library/vue'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import { primeVueOptions } from '@/design/primevue'
import { installViewport, restoreViewport } from '@/test-support/viewport'
import { getCurrentUser, getTask, ProblemError } from '@/api/client'
import {
  requestAllDataDeletion,
  requestSourceCacheDeletion,
} from '@/api/privacy'
import { listSessions, revokeSession } from '@/api/auth'
import { createMemoryHistory, createRouter } from 'vue-router'
import SettingsPage from './SettingsPage.vue'
import { getSettings, updateSettings } from '@/api/settings'
import { listConnections, getConnectionCapabilities } from '@/api/connections'
import { userSettings } from '@/test-support/actionFixtures'
import {
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'

vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
}))
vi.mock('@/api/privacy', () => ({
  requestSourceCacheDeletion: vi.fn(),
  requestAllDataDeletion: vi.fn(),
}))
vi.mock('@/api/auth', () => ({ listSessions: vi.fn(), revokeSession: vi.fn() }))
vi.mock('@/api/settings', () => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}))
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  getTask: vi.fn(),
  getCurrentUser: vi.fn(),
}))

enableAutoUnmount(afterEach)
beforeEach(() => {
  vi.resetAllMocks()
  installViewport()
  vi.mocked(getSettings).mockResolvedValue(userSettings())
  vi.mocked(updateSettings).mockImplementation(async (patch) => ({
    ...userSettings(),
    ...patch,
    working_hours: { ...userSettings().working_hours, ...patch.working_hours },
  }))
  vi.mocked(listConnections).mockResolvedValue([])
  vi.mocked(listSessions).mockResolvedValue([])
  vi.mocked(requestSourceCacheDeletion).mockResolvedValue({
    task_id: 'source-task',
    status: 'queued',
  })
  vi.mocked(requestAllDataDeletion).mockResolvedValue({
    task_id: 'all-task',
    status: 'queued',
  })
  vi.mocked(getTask).mockResolvedValue({
    id: 'source-task',
    kind: 'privacy.source_cache_deletion',
    status: 'queued',
    retry_of_task_id: null,
    error_code: null,
    event_cursor: '0',
    steps: [],
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  restoreViewport()
})

/** 真实内存路由保留会话撤销跳转；挂载后的工作表单异步依赖由 flushPromises 等待。 */
async function renderSettings() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: { template: '<p>settings</p>' } },
      { path: '/login', component: { template: '<p>login</p>' } },
      { path: '/tasks', component: { template: '<p>tasks</p>' } },
    ],
  })
  await router.push('/settings')
  const wrapper = mount(SettingsPage, {
    attachTo: document.body,
    global: {
      plugins: [
        createPinia(),
        [PrimeVue, primeVueOptions],
        ConfirmationService,
        router,
      ],
      stubs: { transition: false },
    },
  })
  await vi.waitFor(() => {
    expect(queryByRole(wrapper.element as HTMLElement, 'form')).not.toBeNull()
  })
  return { wrapper, router }
}
function button(wrapper: VueWrapper, name: string) {
  return new DOMWrapper(
    getByRole(wrapper.element as HTMLElement, 'button', { name }),
  )
}
function field(label: string) {
  return new DOMWrapper(getByLabelText(document.body, label))
}
/** 控件由原生 select 迁移为 Select；选项仍由用户明确选择，不直接改组件状态。 */
async function select(label: string, option: string | RegExp) {
  const control = getByRole(document.body, 'combobox', { name: label })
  await new DOMWrapper(control).trigger('click')
  const list = document.getElementById(
    control.getAttribute('aria-controls') ?? '',
  )
  if (!list) throw new Error('Select listbox is missing')
  await new DOMWrapper(getByRole(list, 'option', { name: option })).trigger(
    'mousedown',
  )
}
async function submit() {
  await new DOMWrapper(getByRole(document.body, 'form')).trigger('submit')
  await flushPromises()
}
async function confirmAll(wrapper: VueWrapper) {
  await button(wrapper, '删除全部数据').trigger('click')
  await field('确认全部删除').setValue('DELETE ALL DATA')
  await new DOMWrapper(
    getByRole(document.body, 'button', { name: '确认删除全部数据' }),
  ).trigger('click')
  await flushPromises()
}
function problem(status: number) {
  return new ProblemError({
    type: 'about:blank',
    title: 'Synthetic',
    status,
    detail: '',
    instance: '',
    error_code: status === 401 ? 'session_expired' : 'task_not_found',
    trace_id: 'test',
  })
}

describe('SettingsPage', () => {
  it('edits seven-day intervals, exact defaults and a bounded meeting buffer', async () => {
    vi.mocked(listConnections).mockResolvedValue([connection()])
    vi.mocked(getConnectionCapabilities).mockResolvedValue(
      connectionCapabilities(),
    )
    const { wrapper } = await renderSettings()
    expect(
      getAllByRole(wrapper.element as HTMLElement, 'heading', {
        name: /^星期/,
      }),
    ).toHaveLength(7)
    await select('默认发送账户', /Google/)
    await select('默认日历账户', /Google/)
    await select('默认日历', /Synthetic calendar/)
    await field('会议缓冲（0–120 分钟）').setValue('120')
    await field('会议缓冲（0–120 分钟）').trigger('blur')
    await field('星期一开始 1').setValue('10:00')
    await submit()
    expect(updateSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({
        default_mail_connection_id: connection().id,
        default_calendar_connection_id: connection().id,
        default_calendar_id: 'synthetic-google-calendar',
        meeting_buffer_minutes: 120,
        working_hours: {
          ...userSettings().working_hours,
          monday: [['10:00', '17:00']],
        },
      }),
    )
    expect(vi.mocked(updateSettings).mock.lastCall?.[0]).not.toHaveProperty(
      'updated_at',
    )
    vi.mocked(updateSettings).mockClear()
    await field('会议缓冲（0–120 分钟）').setValue('121')
    await field('会议缓冲（0–120 分钟）').trigger('blur')
    await submit()
    expect(updateSettings).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('0–120')
  })

  it('keeps an inactive saved default visible and never silently picks another account', async () => {
    vi.mocked(getSettings).mockResolvedValue({
      ...userSettings(),
      default_mail_connection_id: connection('microsoft').id,
    })
    vi.mocked(listConnections).mockResolvedValue([connection()])
    vi.mocked(getConnectionCapabilities).mockResolvedValue(
      connectionCapabilities(),
    )
    const { wrapper } = await renderSettings()
    expect(
      getByRole(wrapper.element as HTMLElement, 'combobox', {
        name: '默认发送账户',
      }).textContent,
    ).toContain('默认账户已不可用')
    await submit()
    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        default_mail_connection_id: connection('microsoft').id,
      }),
    )
  })

  it('renders retention controls and saves settings', async () => {
    await renderSettings()
    await field('邮件正文保留天数').setValue('45')
    await field('邮件正文保留天数').trigger('blur')
    await submit()
    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({ email_body_retention_days: 45 }),
    )
  })

  it('requires an exact modal confirmation and links accepted deletion tasks', async () => {
    const { wrapper } = await renderSettings()
    expect(wrapper.text()).toContain(
      '删除本地数据不能撤回已发送的邮件或已生效的日程变更。',
    )
    expect(wrapper.text()).toContain('结果未知的操作也可能已在供应商侧生效。')
    await button(wrapper, '删除全部数据').trigger('click')
    const dialog = getByRole(document.body, 'dialog', { name: '删除全部数据' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.textContent).toContain('DELETE ALL DATA')
    await field('确认全部删除').setValue('delete all data')
    await new DOMWrapper(
      getByRole(dialog, 'button', { name: '确认删除全部数据' }),
    ).trigger('click')
    expect(requestAllDataDeletion).not.toHaveBeenCalled()
    expect(getByRole(dialog, 'alert').textContent).toContain(
      '请输入 DELETE ALL DATA 以确认。',
    )
    await field('确认全部删除').setValue('DELETE ALL DATA')
    await new DOMWrapper(
      getByRole(dialog, 'button', { name: '确认删除全部数据' }),
    ).trigger('click')
    await flushPromises()
    expect(requestAllDataDeletion).toHaveBeenCalledWith(
      'DELETE ALL DATA',
      expect.stringMatching(/^all-data-/),
    )
    expect(
      getByRole(wrapper.element as HTMLElement, 'link', {
        name: '查看全部数据删除任务',
      }).getAttribute('href'),
    ).toBe('/tasks?task_id=all-task')
    expect(
      queryByRole(wrapper.element as HTMLElement, 'link', {
        name: '查看来源缓存删除任务',
      }),
    ).toBeNull()
  })

  it('keeps both destructive controls disabled after a 202 task receipt', async () => {
    const { wrapper } = await renderSettings()
    await button(wrapper, '删除来源缓存').trigger('click')
    await flushPromises()
    expect(button(wrapper, '删除来源缓存').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '删除全部数据').attributes('disabled')).toBeDefined()
    expect(
      getByRole(wrapper.element as HTMLElement, 'link', {
        name: '查看来源缓存删除任务',
      }).getAttribute('href'),
    ).toBe('/tasks?task_id=source-task')
  })

  it('submits a source-cache deletion only once during a rapid double click', async () => {
    const { wrapper } = await renderSettings()
    await Promise.all([
      button(wrapper, '删除来源缓存').trigger('click'),
      button(wrapper, '删除来源缓存').trigger('click'),
    ])
    await flushPromises()
    expect(requestSourceCacheDeletion).toHaveBeenCalledTimes(1)
  })

  it('does not start task polling when an unmounted deletion request later resolves', async () => {
    vi.useFakeTimers()
    let resolve:
      ((value: { task_id: string; status: string }) => void) | undefined
    vi.mocked(requestSourceCacheDeletion).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const { wrapper } = await renderSettings()
    await button(wrapper, '删除来源缓存').trigger('click')
    wrapper.unmount()
    resolve?.({ task_id: 'source-task', status: 'queued' })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(4000)
    expect(getTask).not.toHaveBeenCalled()
  })

  it('restores controls and shows failure after the accepted task becomes failed', async () => {
    vi.mocked(getTask).mockResolvedValueOnce({
      id: 'source-task',
      kind: 'privacy.source_cache_deletion',
      status: 'failed',
      retry_of_task_id: null,
      error_code: 'cleanup_failed',
      event_cursor: '1',
      steps: [],
    })
    const { wrapper } = await renderSettings()
    await button(wrapper, '删除来源缓存').trigger('click')
    await flushPromises()
    expect(
      button(wrapper, '删除来源缓存').attributes('disabled'),
    ).toBeUndefined()
    expect(
      button(wrapper, '删除全部数据').attributes('disabled'),
    ).toBeUndefined()
    expect(
      getByRole(wrapper.element as HTMLElement, 'alert').textContent,
    ).toContain('删除任务失败')
  })

  it('redirects when all-data task polling returns the expected revoked-session status', async () => {
    vi.mocked(getTask).mockRejectedValueOnce(problem(401))
    vi.mocked(getCurrentUser).mockRejectedValueOnce(problem(401))
    const { wrapper, router } = await renderSettings()
    await confirmAll(wrapper)
    expect(router.currentRoute.value.path).toBe('/login')
    expect(getCurrentUser).toHaveBeenCalledTimes(1)
  })

  it.each(['valid', 'network', 'succeeded'] as const)(
    'keeps deletion pending while the all-data session is %s',
    async (mode) => {
      if (mode === 'succeeded')
        vi.mocked(getTask).mockResolvedValueOnce({
          id: 'all-task',
          kind: 'privacy.all_data_deletion',
          status: 'succeeded',
          retry_of_task_id: null,
          error_code: null,
          event_cursor: '1',
          steps: [],
        })
      else vi.mocked(getTask).mockRejectedValueOnce(problem(404))
      if (mode === 'network')
        vi.mocked(getCurrentUser).mockRejectedValueOnce(
          new Error('temporary network failure'),
        )
      else
        vi.mocked(getCurrentUser).mockResolvedValueOnce({
          id: 'user-1',
          email: 'admin@example.test',
          display_name: 'Admin',
          timezone: 'UTC',
          locale: 'zh-CN',
          brief_time: '08:00',
        })
      const { wrapper, router } = await renderSettings()
      await confirmAll(wrapper)
      expect(router.currentRoute.value.path).toBe('/settings')
      expect(getCurrentUser).toHaveBeenCalledTimes(1)
      expect(
        button(wrapper, '删除来源缓存').attributes('disabled'),
      ).toBeDefined()
      expect(
        button(wrapper, '删除全部数据').attributes('disabled'),
      ).toBeDefined()
    },
  )

  it('retains explicit current-session revocation and redirects only after its response', async () => {
    vi.mocked(listSessions).mockResolvedValue([
      {
        id: 'session-1',
        created_at: '2026-10-01T00:00:00Z',
        expires_at: '2026-10-05T00:00:00Z',
        last_seen_at: '2026-10-04T00:00:00Z',
        is_current: true,
      },
    ])
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true)
    const { wrapper, router } = await renderSettings()
    await button(wrapper, '撤销').trigger('click')
    expect(revokeSession).not.toHaveBeenCalled()
    await button(wrapper, '撤销').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith('确定撤销该会话？')
    expect(revokeSession).toHaveBeenCalledWith('session-1')
    expect(router.currentRoute.value.path).toBe('/login')
    confirm.mockRestore()
  })
})
