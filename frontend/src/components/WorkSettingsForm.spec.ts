/** 用真实 PrimeVue Form、控件与只读 hook 验证输入到 PATCH 的通路。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, waitFor, within } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { userSettings } from '@/test-support/actionFixtures'
import {
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'
import { getSettings, updateSettings } from '@/api/settings'
import { listConnections, getConnectionCapabilities } from '@/api/connections'
import { ProblemError } from '@/api/client'
import type { UserSettings } from '@/api/types'
import WorkSettingsForm from './WorkSettingsForm.vue'

vi.mock('@/api/settings', () => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}))
vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
}))
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getSettings).mockImplementation(async () => userSettings())
  vi.mocked(updateSettings).mockImplementation(async (patch) => ({
    ...userSettings(),
    ...patch,
    working_hours: { ...userSettings().working_hours, ...patch.working_hours },
  }))
  vi.mocked(listConnections).mockResolvedValue([connection()])
  vi.mocked(getConnectionCapabilities).mockResolvedValue(
    connectionCapabilities(),
  )
})

/** 输入事件后等待 Form resolver 结算，不能以实现侧状态代替用户界面断言。 */
async function fill(element: HTMLElement, value: string) {
  await fireEvent.update(element, value)
  await fireEvent.blur(element)
}

async function renderForm() {
  const result = await renderWithPlugins(WorkSettingsForm)
  await result.findByRole('button', { name: '保存' })
  return result
}

describe('WorkSettingsForm', () => {
  it('renders a named real Form and rejects locale/time/number syntax with associated errors', async () => {
    const ui = await renderForm()
    const form = ui.getByRole('form', { name: '工作偏好与保留周期' })
    await fill(ui.getByLabelText('语言', { exact: true }), '<invalid>')
    await fireEvent.submit(form)
    await waitFor(() =>
      expect(ui.getByRole('alert')).toHaveTextContent('请输入有效的语言标签'),
    )
    expect(updateSettings).not.toHaveBeenCalled()
    expect(ui.getByLabelText('语言', { exact: true })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    await fill(ui.getByLabelText('语言', { exact: true }), 'zh-CN')
    await fill(ui.getByLabelText('简报时间', { exact: true }), '08:00:01')
    await fireEvent.submit(form)
    await waitFor(() =>
      expect(ui.getByRole('alert')).toHaveTextContent('请明确重新选择分钟精度'),
    )
    expect(updateSettings).not.toHaveBeenCalled()
  })

  it('keeps legacy precision visible until explicit correction and then permits saving', async () => {
    vi.mocked(getSettings).mockResolvedValueOnce({
      ...userSettings(),
      brief_time: '08:00:30',
    })
    const ui = await renderForm()
    expect(ui.getByLabelText('简报时间', { exact: true })).toHaveValue(
      '08:00:30',
    )
    expect(ui.getByRole('button', { name: '选择星期一开始 1' })).toBeVisible()
    expect(ui.getByRole('button', { name: '选择星期一结束 1' })).toBeVisible()
    await fireEvent.submit(ui.getByRole('form'))
    await ui.findByRole('alert')
    expect(updateSettings).not.toHaveBeenCalled()
    await fill(ui.getByLabelText('简报时间', { exact: true }), '08:01')
    await fireEvent.submit(ui.getByRole('form'))
    await ui.findByText('已保存', { exact: true })
    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({ brief_time: '08:01' }),
    )
  })

  it('normalizes only zero seconds and saves the exact editable whitelist including retention and selected defaults', async () => {
    const ui = await renderForm()
    expect(ui.getByLabelText('简报时间', { exact: true })).toHaveValue('08:00')
    await fill(ui.getByLabelText('邮件正文保留天数'), '45')
    await fill(ui.getByLabelText('会议缓冲（0–120 分钟）'), '30')
    await fireEvent.submit(ui.getByRole('form'))
    await ui.findByText('已保存', { exact: true })
    expect(updateSettings).toHaveBeenCalledWith({
      timezone: 'Asia/Shanghai',
      locale: 'zh-CN',
      brief_time: '08:00',
      email_body_retention_days: 45,
      source_metadata_retention_days: 90,
      workspace_history_retention_days: 365,
      default_mail_connection_id: null,
      default_calendar_connection_id: null,
      default_calendar_id: null,
      working_hours: userSettings().working_hours,
      meeting_buffer_minutes: 30,
    })
  })

  it('synchronizes toggles, additions and removals as one seven-day tuple field, rejecting an unfilled reopened day', async () => {
    const ui = await renderForm()
    const monday = ui.getByRole('switch', { name: '星期一工作日' })
    await fireEvent.click(monday)
    expect(ui.queryByLabelText('星期一开始 1')).toBeNull()
    await fireEvent.click(monday)
    await fireEvent.submit(ui.getByRole('form'))
    await waitFor(() =>
      expect(ui.getByRole('alert')).toHaveTextContent('HH:mm'),
    )
    expect(updateSettings).not.toHaveBeenCalled()
    await fill(ui.getByLabelText('星期一开始 1'), '10:00')
    await fill(ui.getByLabelText('星期一结束 1'), '12:00')
    await fireEvent.click(ui.getByRole('button', { name: '添加星期一区间' }))
    await fill(ui.getByLabelText('星期一开始 2'), '13:00')
    await fill(ui.getByLabelText('星期一结束 2'), '17:00')
    await fireEvent.click(ui.getByRole('button', { name: '删除星期一区间 1' }))
    await fireEvent.submit(ui.getByRole('form'))
    await ui.findByText('已保存', { exact: true })
    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        working_hours: {
          ...userSettings().working_hours,
          monday: [['13:00', '17:00']],
        },
      }),
    )
  })

  it('never submits the previous valid wall time when DatePicker receives invalid manual text', async () => {
    const ui = await renderForm()
    const start = ui.getByLabelText('星期一开始 1')
    await fill(start, '25:30')
    expect(start).toHaveValue('25:30')
    await fireEvent.submit(ui.getByRole('form'))
    await waitFor(() =>
      expect(ui.getByRole('alert')).toHaveTextContent('HH:mm'),
    )
    expect(updateSettings).not.toHaveBeenCalled()
  })

  it('maps a real ProblemError through recovery, keeps input on 409 and reloads Form fields from a new snapshot', async () => {
    vi.mocked(updateSettings).mockRejectedValueOnce(
      new ProblemError({
        type: 'about:blank',
        title: '<script>private</script>',
        detail: 'private detail',
        status: 409,
        instance: '',
        error_code: 'draft_version_conflict',
        trace_id: 'settings-trace',
      }),
    )
    const ui = await renderForm()
    await fill(ui.getByLabelText('邮件正文保留天数'), '45')
    await fireEvent.submit(ui.getByRole('form'))
    const alert = await ui.findByRole('alert')
    expect(alert).toHaveTextContent('邮件草稿版本已变化，请重新加载后核对。')
    expect(alert).toHaveTextContent('版本或状态已变化，请重新加载后核对。')
    expect(alert).toHaveTextContent('settings-trace')
    expect(alert).not.toHaveTextContent('private')
    expect(ui.getByLabelText('邮件正文保留天数')).toHaveValue('45')
    vi.mocked(getSettings).mockResolvedValueOnce({
      ...userSettings(),
      email_body_retention_days: 88,
      working_hours: {
        ...userSettings().working_hours,
        monday: [],
        tuesday: [['11:00', '15:00']],
      },
    })
    await fireEvent.click(
      within(alert).getByRole('button', { name: '重新加载设置' }),
    )
    await waitFor(() =>
      expect(ui.getByLabelText('邮件正文保留天数')).toHaveValue('88'),
    )
    expect(ui.queryByLabelText('星期一开始 1')).toBeNull()
    expect(ui.getByLabelText('星期二开始 1')).toHaveValue('11:00')
    await fireEvent.submit(ui.getByRole('form'))
    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(2))
    expect(updateSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({
        email_body_retention_days: 88,
        working_hours: {
          ...userSettings().working_hours,
          monday: [],
          tuesday: [['11:00', '15:00']],
        },
      }),
    )
  })

  it('keeps capability recovery action and avoids repeating identical original and mapped messages', async () => {
    vi.mocked(updateSettings).mockRejectedValueOnce(
      new ProblemError({
        type: 'about:blank',
        title: 'private',
        detail: 'private',
        status: 409,
        instance: '',
        error_code: 'connection_capability_disabled',
        trace_id: 'cap-trace',
      }),
    )
    const ui = await renderForm()
    await fireEvent.submit(ui.getByRole('form'))
    const alert = await ui.findByRole('alert')
    expect(
      within(alert).getAllByText('连接能力不可用，请重新授权并核对设置。'),
    ).toHaveLength(1)
    expect(
      within(alert).getByRole('link', { name: '检查连接并重新授权' }),
    ).toHaveAttribute('href', '/connections')
  })

  it('prevents duplicate submits while saving and retains safe loading/error/retry live regions', async () => {
    let resolve: ((value: UserSettings) => void) | undefined
    vi.mocked(updateSettings).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const ui = await renderForm()
    await fireEvent.submit(ui.getByRole('form'))
    await fireEvent.submit(ui.getByRole('form'))
    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1))
    expect(ui.getByRole('button', { name: '正在保存…' })).toBeDisabled()
    resolve?.(userSettings())
    await ui.findByText('已保存', { exact: true })
  })
})

/** 冻结旧 live-region 的加载／目录失败文案，不靠静态 role 计数凑数。 */
it('preserves settings and catalog loading announcements plus actionable catalog failure', async () => {
  let resolveSettings: ((value: UserSettings) => void) | undefined
  let rejectCatalog: ((reason: Error) => void) | undefined
  vi.mocked(getSettings).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveSettings = resolve
      }),
  )
  vi.mocked(listConnections).mockImplementationOnce(
    () =>
      new Promise((_resolve, reject) => {
        rejectCatalog = reject
      }),
  )
  const ui = await renderWithPlugins(WorkSettingsForm)
  expect(ui.getByRole('status')).toHaveTextContent('正在加载设置…')
  expect(ui.getByRole('status')).toHaveAttribute('aria-live', 'polite')
  resolveSettings?.(userSettings())
  await ui.findByRole('form')
  await waitFor(() =>
    expect(ui.getByRole('status')).toHaveTextContent('正在读取可用账户与日历…'),
  )
  rejectCatalog?.(new Error('synthetic catalog failure'))
  const alert = await ui.findByRole('alert')
  expect(alert).toHaveTextContent('部分账户目录加载失败，已保留原默认选择。')
  expect(alert).toHaveAttribute('aria-live', 'assertive')
  await fireEvent.click(within(alert).getByRole('button', { name: '重试目录' }))
  await waitFor(() => expect(ui.queryByRole('alert')).toBeNull())
})

it('keeps an unavailable saved calendar account and calendar visible and unchanged in the submitted snapshot', async () => {
  vi.mocked(getSettings).mockResolvedValueOnce({
    ...userSettings(),
    default_calendar_connection_id: connection('microsoft').id,
    default_calendar_id: 'unavailable-synthetic-calendar',
  })
  const ui = await renderForm()
  expect(ui.getByRole('combobox', { name: '默认日历账户' })).toHaveTextContent(
    '默认日历账户已不可用，请重选',
  )
  expect(ui.getByRole('combobox', { name: '默认日历' })).toHaveTextContent(
    '默认日历已不可用，请重选',
  )
  await fireEvent.submit(ui.getByRole('form'))
  await ui.findByText('已保存', { exact: true })
  expect(updateSettings).toHaveBeenCalledWith(
    expect.objectContaining({
      default_calendar_connection_id: connection('microsoft').id,
      default_calendar_id: 'unavailable-synthetic-calendar',
    }),
  )
})
