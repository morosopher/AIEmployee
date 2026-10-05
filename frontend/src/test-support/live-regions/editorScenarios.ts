/** 编辑器公告通过真实展示 Props、表单提交和受控 HTTP 触发，不替换 Forms／Message 或业务 hook。 */
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/vue'
import { expect } from 'vitest'
import { reactive } from 'vue'
import type { CalendarProposal } from '@/api/types'
import MailDraftPage from '@/pages/MailDraftPage.vue'
import CalendarProposalPage from '@/pages/CalendarProposalPage.vue'
import MailEditorForm from '@/components/MailEditorForm.vue'
import CalendarEditorForm from '@/components/CalendarEditorForm.vue'
import WorkSettingsForm from '@/components/WorkSettingsForm.vue'
import LinkedActionPanel from '@/components/LinkedActionPanel.vue'
import { useActionsStore } from '@/stores/actions'
import {
  renderWithPlugins,
  type RenderWithPluginsResult,
} from '../renderWithPlugins'
import {
  actionSnapshot,
  calendarProposal,
  CONNECTION_ID,
  DRAFT_ID,
  mailDraft,
  PROPOSAL_ID,
  TASK_ID,
  userSettings,
} from '../actionFixtures'
import {
  calendarFields,
  connection,
  connectionCapabilities,
} from '../editorFixtures'
import {
  assertLive,
  assertAdditional,
  deferred,
  json,
  network,
  problem,
} from './assertions'

const entries = [
  {
    connection: connection(),
    capabilities: connectionCapabilities(),
    error: null,
  },
]
/** 完整事实使用既有合成提案字段；incomplete/null 明确表示尚未检查，不能用空成功代替。 */
function proposal(): CalendarProposal {
  return {
    ...calendarProposal(),
    ...calendarFields(),
    calendar_id: 'synthetic-google-calendar',
    notification_policy: 'none',
    required_confirmations: [],
    editor_facts: {
      reprepare_source: null,
      restore_source: null,
      before_status: 'not_applicable',
      before: null,
      conflict_status: 'incomplete',
      conflicts: null,
    },
  }
}

/** 原生路由参数用于真实 hook；统一渲染器只含 catch-all，补入目标匹配后显式重新解析。 */
async function bindEditorRoute(
  view: RenderWithPluginsResult,
  pattern: string,
  path: string,
): Promise<void> {
  view.router.addRoute({ path: pattern, component: { render: () => null } })
  await view.router.replace(path)
}

async function loading() {
  network(() => deferred<Response>().promise)
  const mailView = await renderWithPlugins(MailDraftPage, {
    route: `/mail/drafts/${DRAFT_ID}`,
  })
  await bindEditorRoute(
    mailView,
    '/mail/drafts/:draftId',
    `/mail/drafts/${DRAFT_ID}`,
  )
  await assertLive('MailDraftPage:72', '正在加载草稿与账户…')
  cleanup()
  {
    const view = await renderWithPlugins(CalendarProposalPage, {
      route: `/calendar/proposals/${PROPOSAL_ID}`,
    })
    await bindEditorRoute(
      view,
      '/calendar/proposals/:proposalId',
      `/calendar/proposals/${PROPOSAL_ID}`,
    )
  }
  await assertLive('CalendarProposalPage:124', '正在加载提案与日历目录…')
}

/** 原页面抽取到异步 Form 的公告仍使用真实组件及模型，不模拟组件内部角色。 */
async function mailForm() {
  const form = {
    connection_id: CONNECTION_ID,
    to: 'to@mail.example.test',
    cc: '',
    bcc: '',
    subject: '合成主题',
    body_text: '',
  }
  const view = await renderWithPlugins(MailEditorForm, {
    props: {
      modelValue: form,
      draft: { ...mailDraft(), to: ['to@mail.example.test'] },
      entries,
      recipientSummary: { count: 1, inputCount: 1, error: null },
      dirty: true,
      busy: false,
      locked: false,
      canSubmit: false,
      generationRunning: true,
      generationFailed: false,
      generationConnection: 'reconnecting',
      instruction: '',
      error: null,
      creating: false,
    },
  })
  await assertAdditional('AutoComplete:search', '未找到结果', 3)
  await assertLive('MailDraftFields:72', '当前收件人数：1 位')
  await assertLive('MailDraftPage:101', '有未保存的输入，请先保存后审阅。')
  await assertLive(
    'MailDraftPage:144',
    '正在草拟，请等待服务端保存结果。连接恢复中，任务状态以服务端为准。',
  )
  await view.rerender({ generationRunning: false, generationFailed: true })
  await assertLive(
    'MailDraftPage:154',
    '草拟失败，原草稿仍保留。请检查任务历史并重新加载后再试。',
  )
  await view.rerender({
    recipientSummary: { count: null, inputCount: 2, error: '合成收件人错误' },
  })
  await assertLive(
    'MailDraftFields:72',
    '收件人数待核对。 当前输入地址：2 项。',
  )
  await fireEvent.update(
    screen.getByRole('combobox', { name: '收件人 To' }),
    'to',
  )
  await assertAdditional('AutoComplete:search', '共有 1 条结果')
  await assertAdditional('AutoComplete:search', '未找到结果', 2)
  await assertAdditional('AutoComplete:selected', '已选择 1 项')
}

async function calendarForm() {
  const value = proposal()
  await renderWithPlugins(CalendarEditorForm, {
    props: {
      modelValue: {
        connection_id: CONNECTION_ID,
        calendar_id: value.calendar_id,
        title: '合成日程',
        description: '',
        location: '',
        starts_at: '2030-01-01T09:00',
        ends_at: '2030-01-01T10:00',
        timezone: 'Asia/Shanghai',
        all_day: false,
        attendees: '',
        notification_policy: 'none',
      },
      proposal: value,
      entries,
      locked: false,
      busy: false,
      dirty: true,
      fieldsDirty: true,
      sourceDirty: true,
      error: null,
      recoveryBusy: false,
      newVersionAvailable: false,
    },
  })
  await assertLive(
    'CalendarProposalPage:203',
    '有未保存或未确认的修改，请保存后重新检查。',
  )
  await assertLive(
    'CalendarProposalPage:209',
    '请先确认目标日历，再编辑其他字段。',
  )
}

/** 状态取自真实 GET 投影：过期前快照、不可恢复来源、未检查与部分候选都不得静默消失。 */
async function calendarFacts() {
  let value: CalendarProposal = {
    ...proposal(),
    operation_kind: 'update',
    status: 'applied',
    target_event_id: 'synthetic-event',
    base_etag: 'synthetic-etag',
    before_snapshot_id: DRAFT_ID,
    editor_facts: {
      reprepare_source: null,
      restore_source: null,
      before_status: 'unavailable',
      before: null,
      conflict_status: 'incomplete',
      conflicts: null,
    },
  }
  network((url) =>
    url.includes('/calendar/proposals/')
      ? json(value)
      : url.endsWith('/connections')
        ? json([connection()])
        : json(connectionCapabilities()),
  )
  {
    const view = await renderWithPlugins(CalendarProposalPage, {
      route: `/calendar/proposals/${PROPOSAL_ID}`,
    })
    await bindEditorRoute(
      view,
      '/calendar/proposals/:proposalId',
      `/calendar/proposals/${PROPOSAL_ID}`,
    )
  }
  await assertLive(
    'CalendarProposalPage:184',
    '恢复来源已不可用，请核对历史快照保留期和目标日程。',
  )
  await assertLive(
    'CalendarProposalPage:250',
    '原始修改前快照已不可用，无法核对前后差异。请返回来源重新创建提案。',
  )
  await assertLive(
    'CalendarProposalPage:272',
    '尚无当前输入的冲突检查，请补齐并保存后重新检查。未检查参会人可用性。',
  )
  cleanup()
  value = {
    ...proposal(),
    availability: {
      proposal_id: PROPOSAL_ID,
      version: 1,
      completeness: 'partial',
      missing_connections: [connection('microsoft').id],
      attendee_availability_checked: false,
      candidates: [],
    },
  }
  {
    const view = await renderWithPlugins(CalendarProposalPage, {
      route: `/calendar/proposals/${PROPOSAL_ID}`,
    })
    await bindEditorRoute(
      view,
      '/calendar/proposals/:proposalId',
      `/calendar/proposals/${PROPOSAL_ID}`,
    )
  }
  await assertLive('CalendarProposalPage:290', '部分日历来源缺失（1 个连接）。')
}

async function settingsForm() {
  const settingsRead = deferred<Response>(),
    catalogRead = deferred<Response>()
  let failSave = false
  network((url, init) =>
    init?.method === 'PATCH'
      ? failSave
        ? problem('draft_version_conflict')
        : json(userSettings())
      : url.endsWith('/settings')
        ? settingsRead.promise
        : catalogRead.promise,
  )
  await renderWithPlugins(WorkSettingsForm)
  await assertLive('WorkSettingsForm:35', '正在加载设置…')
  settingsRead.resolve(json(userSettings()))
  await assertLive('WorkSettingsForm:161', '正在读取可用账户与日历…')
  catalogRead.resolve(problem('internal_error', 500))
  await assertLive(
    'WorkSettingsForm:170',
    '部分账户目录加载失败，已保留原默认选择。',
  )
  await fireEvent.update(screen.getByLabelText('星期一结束 1'), '08:00')
  await fireEvent.submit(screen.getByRole('form'))
  // 格式错误现在由真实 zod 提前呈现；原文仍在唯一字段 Message 内，不再等旧 hook 才报错。
  await assertLive(
    'WorkSettingsForm:202',
    '每个工作区间的结束时间必须晚于开始时间。',
  )
  await fireEvent.update(screen.getByLabelText('星期一结束 1'), '17:00')
  await fireEvent.submit(screen.getByRole('form'))
  await assertLive('WorkSettingsForm:196', '已保存')
  failSave = true
  await fireEvent.update(screen.getByLabelText('语言'), 'en-US')
  await fireEvent.submit(screen.getByRole('form'))
  await assertLive(
    'WorkSettingsForm:208',
    '版本或状态已变化，请重新加载后核对。',
  )
  await fireEvent.keyDown(screen.getByRole('combobox', { name: 'IANA 时区' }), {
    key: 'ArrowDown',
    code: 'ArrowDown',
  })
  await fireEvent.update(
    await screen.findByRole('searchbox'),
    'No/Synthetic_Zone',
  )
  await assertAdditional('Select:filter', '未找到结果')
}

async function linkedAction() {
  const read = deferred<Response>()
  network(() => read.promise)
  const view = await renderWithPlugins(LinkedActionPanel, {
    props: { localId: DRAFT_ID, itemKind: 'mail_draft', taskQuery: TASK_ID },
  })
  await assertLive('LinkedActionPanel:55', '正在读取关联任务…')
  read.resolve(problem('internal_error', 500))
  await assertLive(
    'LinkedActionPanel:61',
    '关联任务读取失败，请到操作中心重新加载。',
  )
  const actions = useActionsStore(view.pinia)
  network(() => json(actionSnapshot({ local_action: null, approval: null })))
  await actions.loadSnapshot(TASK_ID)
  await assertLive(
    'LinkedActionPanel:70',
    '此任务与当前编辑对象不匹配，请在操作中心核对。',
  )
  network(() => json(actionSnapshot({ approval: null })))
  await actions.loadSnapshot(TASK_ID)
  await assertLive(
    'LinkedActionPanel:77',
    '任务连接正在恢复，状态以服务端为准。',
  )
  await waitFor(() =>
    expect(
      screen.queryByText('此任务与当前编辑对象不匹配，请在操作中心核对。'),
    ).toBeNull(),
  )
}

export const editorScenarios = {
  loading,
  mailForm,
  calendarForm,
  calendarFacts,
  settingsForm,
  linkedAction,
  mailFieldAdditions,
  settingsFieldAdditions,
  calendarFieldAdditions,
}

/** 邮件字段通过真实Form提交生成公告；独立场景保留默认时限，不把错误角色替换成桩。 */
async function mailFieldAdditions() {
  const mail = reactive({
    connection_id: CONNECTION_ID,
    to: '',
    cc: '',
    bcc: '',
    subject: '',
    body_text: '',
  })
  await renderWithPlugins(MailEditorForm, {
    props: {
      modelValue: mail,
      draft: mailDraft(),
      entries,
      recipientSummary: { count: 0, inputCount: 0, error: null },
      dirty: true,
      busy: false,
      locked: false,
      canSubmit: false,
      generationRunning: false,
      generationFailed: false,
      generationConnection: 'connected',
      instruction: '',
      error: null,
      creating: false,
    },
  })
  for (const label of ['抄送 CC', '密送 BCC'])
    await fireEvent.update(
      screen.getByRole('combobox', { name: label }),
      'invalid',
    )
  await fireEvent.submit(screen.getByRole('form'))
  await assertAdditional('MailEditorForm:fieldErrors', '', 5)
  expect(
    screen
      .getAllByRole('alert')
      .map((node) => node.textContent?.trim())
      .sort(),
  ).toEqual(
    [
      '请至少填写一位收件人。',
      '请填写有效的邮箱地址，多个地址用逗号分隔。',
      '请填写有效的邮箱地址，多个地址用逗号分隔。',
      '请填写主题。',
      '请填写纯文本正文。',
    ].sort(),
  )
}

/** 设置字段独立验证真实八条公告，避免与其他表单累计共用一个用例时限。 */
async function settingsFieldAdditions() {
  network((url) =>
    url.endsWith('/settings') ? json(userSettings()) : json([]),
  )
  await renderWithPlugins(WorkSettingsForm)
  await screen.findByRole('button', { name: '保存' })
  for (const [label, value] of [
    ['IANA 时区', 'No/Synthetic_Zone'],
    ['语言', '<bad>'],
    ['简报时间', '25:00'],
    ['邮件正文保留天数', '0'],
    ['来源元数据保留天数', '0'],
    ['工作区历史保留天数', '0'],
    ['会议缓冲（0–120 分钟）', '121'],
    ['星期一结束 1', '08:00'],
  ] as const) {
    const input = screen.getByLabelText(label)
    await fireEvent.update(input, value)
    if (label === 'IANA 时区') {
      // editable Select 会展开全部时区，blur 不关闭浮层；先用真实 Esc 完成键盘离开，
      // 避免后续字段查询与公告核验持续遍历无关选项，同时确认关闭没有覆盖非法原值。
      expect(input).toHaveAttribute('aria-expanded', 'true')
      await fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
      await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'false'))
      // 公开展开状态会先更新；连隐藏的退出副本也须离开DOM后，再开始下一字段交互。
      await waitFor(() =>
        expect(screen.queryByRole('listbox', { hidden: true })).not.toBeInTheDocument(),
      )
      expect(input).toHaveValue(value)
    }
    await fireEvent.blur(input)
  }
  await fireEvent.submit(screen.getByRole('form'))
  await assertAdditional('WorkSettingsForm:fieldErrors', '', 8)
  expect(
    screen
      .getAllByRole('alert')
      .filter((node) => node.textContent?.includes('1–3650')),
  ).toHaveLength(3)
}

/** 日程保留非法时区和更正后日期错误两阶段，使用同一个真实Form验证依赖校验顺序。 */
async function calendarFieldAdditions() {
  const calendar = reactive({
    connection_id: CONNECTION_ID,
    calendar_id: 'synthetic-google-calendar',
    title: '',
    description: '',
    location: '',
    starts_at: 'invalid-start',
    ends_at: 'invalid-end',
    timezone: 'No/Synthetic_Zone',
    all_day: false,
    attendees: 'invalid',
    notification_policy: 'none' as const,
  })
  await renderWithPlugins(CalendarEditorForm, {
    props: {
      modelValue: calendar,
      proposal: proposal(),
      entries,
      locked: false,
      busy: false,
      dirty: true,
      fieldsDirty: true,
      sourceDirty: false,
      error: null,
      recoveryBusy: false,
      newVersionAvailable: false,
    },
  })
  await fireEvent.submit(screen.getByRole('form'))
  await assertAdditional('CalendarEditorForm:fieldErrors', '', 3)
  expect(
    screen
      .getAllByRole('alert')
      .map((node) => node.textContent?.trim())
      .sort(),
  ).toEqual(
    [
      '请填写日程标题。',
      '请输入有效的 IANA 时区。',
      '请填写有效的参会人邮箱地址，多个地址用逗号分隔。',
    ].sort(),
  )
  await fireEvent.update(
    screen.getByRole('combobox', { name: 'IANA 时区' }),
    'UTC',
  )
  await fireEvent.submit(screen.getByRole('form'))
  await assertAdditional('CalendarEditorForm:fieldErrors', '', 4)
  for (const label of ['开始时间', '结束时间'])
    expect(screen.getByLabelText(label)).toHaveAccessibleDescription(
      '请填写完整的日期和时间。',
    )
}
