/** 纯展示组件以真实 Props／回调触发，插件、Message、Dialog 和表单控件全部保留。 */
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/vue'
import { expect } from 'vitest'
import { defineComponent, h } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import AppShell from '@/components/AppShell.vue'
import TaskHistoryList from '@/components/TaskHistoryList.vue'
import { useTasksStore } from '@/stores/tasks'
import { historyPage } from '../taskHistoryFixtures'
import { ProblemError } from '@/api/client'
import type { Brief, TaskSnapshot } from '@/api/types'
import ActionConfirmationDialog from '@/components/ActionConfirmationDialog.vue'
import ActionDetail from '@/components/ActionDetail.vue'
import ApprovalCard from '@/components/ApprovalCard.vue'
import BriefView from '@/components/BriefView.vue'
import CalendarConflictNotice from '@/components/CalendarConflictNotice.vue'
import CalendarRepreparePanel from '@/components/CalendarRepreparePanel.vue'
import CapabilityRows from '@/components/CapabilityRows.vue'
import EditorConnectionSelect from '@/components/EditorConnectionSelect.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import NeedsAttentionPanel from '@/components/NeedsAttentionPanel.vue'
import SystemAlertBanner from '@/components/SystemAlertBanner.vue'
import TaskTimeline from '@/components/TaskTimeline.vue'
import { renderWithPlugins, setViewport } from '../renderWithPlugins'
import { actionSnapshot, NOW, TASK_ID } from '../actionFixtures'
import {
  calendarApproval,
  connection,
  connectionCapabilities,
  mailApproval,
} from '../editorFixtures'
import {
  assertLive,
  assertAdditional,
  deferred,
  json,
  network,
  problem,
} from './assertions'

/** 可复用合成简报；不包含真实正文或个人来源。 */
export const brief: Brief = {
  id: 'synthetic-brief',
  local_date: '2030-01-01',
  version: 1,
  task_id: TASK_ID,
  source_cutoff: NOW,
  completeness: 'partial',
  headline: '合成简报',
  structured_content: {},
  markdown: '',
  warnings: ['合成来源未同步'],
  items: [],
}
const rejected = () =>
  new ProblemError({
    type: 'about:blank',
    title: 'Synthetic',
    detail: '',
    instance: '',
    status: 409,
    error_code: 'manual_resolution_version_conflict',
    trace_id: 'synthetic-live-trace',
  })

/** 系统 loading／error／overdue 同时出现仍为独立的原三条公告。 */
async function shellAlerts() {
  await renderWithPlugins(SystemAlertBanner, {
    props: {
      loading: true,
      error: true,
      alerts: [
        {
          code: 'daily_brief_overdue',
          severity: 'critical',
          local_date: '2030-01-01',
          diagnostic_task_id: TASK_ID,
        },
      ],
    },
  })
  await assertLive('AppShell:34', '正在检查系统告警')
  await assertLive('AppShell:41', '系统告警暂时无法刷新。')
  await assertLive('AppShell:48', '每日简报已逾期')
}

/** 动作状态与同一旧到期文案的两个独立出处都实际渲染；数量来自原组合而非额外副本。 */
async function actionDetail() {
  const view = await renderWithPlugins(ActionDetail, {
    props: {
      snapshot: actionSnapshot({ status: 'reconciling', approval: null }),
      timezone: 'UTC',
    },
  })
  await assertLive(
    'ActionDetail:72',
    '正在只读核对执行结果，请等待服务端更新。',
  )
  await view.rerender({
    snapshot: actionSnapshot({
      status: 'succeeded',
      approval: {
        ...mailApproval(),
        status: 'expired',
        content_status: 'redacted',
        preview: null,
      },
    }),
  })
  await assertLive('ActionDetail:85', '内容已到期，仅保留执行历史。', 2)
  await view.rerender({
    snapshot: actionSnapshot({
      status: 'waiting_approval',
      approval: calendarApproval(),
      execution: null,
    }),
  })
  await assertLive(
    'ActionDetail:92',
    '部分日历来源尚未同步（1 个连接），冲突检查可能不完整。',
  )
  network(() => problem('draft_version_conflict'))
  await fireEvent.click(
    screen.getByRole('button', { name: '撤回审批以继续编辑' }),
  )
  await assertLive('ActionDetail:154', '版本或状态已变化，请重新加载后核对。')
}

/** 审批原三条公告：到期、决定忙碌／落定、可恢复错误；固定远期避免依赖实际时钟。 */
async function approval() {
  const pending = deferred<void>()
  const value = { ...mailApproval(), expires_at: '2099-01-01T00:00:00Z' }
  const view = await renderWithPlugins(ApprovalCard, {
    props: {
      approval: { ...value, content_status: 'redacted', preview: null },
      decide: () => pending.promise,
    },
  })
  await assertAdditional('ApprovalCard:countdown', '审批剩余')
  await assertLive('ApprovalCard:171', '内容已到期，仅保留执行历史。')
  await view.rerender({ approval: value })
  await fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await assertLive('ApprovalCard:221', '正在记录决定…')
  pending.resolve()
  await assertLive(
    'ApprovalCard:221',
    '决定已记录，执行结果以服务端后续状态为准。',
  )
  cleanup()
  await renderWithPlugins(ApprovalCard, {
    props: {
      approval: value,
      decide: async () => {
        throw rejected()
      },
    },
  })
  await fireEvent.click(screen.getByRole('button', { name: '批准' }))
  await assertLive('ApprovalCard:229', '版本或状态已变化，请重新加载后核对。')
}

/** 原人工核对提示移入打开的 Dialog 时，背景不保留重复副本。 */
async function attention() {
  const pending = deferred<void>()
  const view = await renderWithPlugins(NeedsAttentionPanel, {
    props: {
      snapshot: actionSnapshot(),
      reconcile: () => pending.promise,
      resolve: async () => {
        throw rejected()
      },
    },
  })
  await fireEvent.click(screen.getByRole('button', { name: '重新核对' }))
  await assertLive('NeedsAttentionPanel:123', '正在记录核对请求…')
  pending.resolve()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '确认未执行' })).toBeEnabled(),
  )
  await fireEvent.click(screen.getByRole('button', { name: '确认未执行' }))
  await assertLive('ActionConfirmationDialog:47', '确认未执行')
  await assertLive(
    'NeedsAttentionPanel:151',
    '此决定只记录人工结果，不会调用供应商写接口。确认未执行不会自动重发；如需再次执行，必须创建新草稿或提案并重新审批。',
  )
  await fireEvent.click(screen.getByRole('button', { name: '确认记录结果' }))
  await assertLive(
    'NeedsAttentionPanel:129',
    '版本或状态已变化，请重新加载后核对。',
  )
  await fireEvent.click(screen.getByRole('button', { name: '取消' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await assertLive(
    'NeedsAttentionPanel:129',
    '版本或状态已变化，请重新加载后核对。',
  )
  view.unmount()
  // 单独渲染原 dialog 组件，确保清单没有把其他组件的同名角色当作替身。
  await renderWithPlugins(ActionConfirmationDialog, {
    props: { title: '合成确认', busy: false },
  })
  await assertLive('ActionConfirmationDialog:47', '合成确认')
}

/** 时间线原空态、失败码与重试拒绝保持原 role／原文。 */
async function timeline() {
  const task: TaskSnapshot = {
    id: TASK_ID,
    kind: 'daily_brief',
    status: 'failed',
    retry_of_task_id: null,
    error_code: 'provider_unavailable',
    event_cursor: '0',
    steps: [],
  }
  const view = await renderWithPlugins(TaskTimeline, {
    props: {
      task: null,
      retry: async () => {
        throw rejected()
      },
      follow: () => undefined,
    },
  })
  await assertLive('TaskTimeline:77', '请选择一个任务查看执行时间线。')
  await view.rerender({ task })
  await assertLive('TaskTimeline:89', '错误代码：provider_unavailable')
  await fireEvent.click(screen.getByRole('button', { name: '重试任务' }))
  await assertLive('TaskTimeline:131', '重试被服务器拒绝')
}

/** 来源与目录警告保留动态数量；静态降级说明和缺失列表不能产生嵌套播报。 */
async function sources() {
  await renderWithPlugins(BriefView, { props: { brief } })
  await assertLive('BriefView:33', '部分数据未完成')
  cleanup()
  await renderWithPlugins(CapabilityRows, {
    props: {
      capabilities: connectionCapabilities(),
      busy: false,
      disconnected: false,
    },
  })
  await assertLive('CapabilityRows:50', '已启用', 4)
  cleanup()
  await renderWithPlugins(EditorConnectionSelect, {
    props: {
      modelValue: connection().id,
      entries: [
        {
          connection: connection(),
          capabilities: null,
          error: {
            message: '合成能力失败',
            action: 'reload',
            traceId: 'synthetic-live-trace',
          },
        },
      ],
      capability: 'mail.send',
      label: '发送账户',
      disabled: false,
    },
  })
  await assertLive(
    'EditorConnectionSelect:56',
    '部分账户能力读取失败，已保留当前选择。请到连接页面刷新后返回。',
  )
  await assertLive(
    'EditorConnectionSelect:73',
    '当前账户能力不可用，请检查连接授权。',
  )
  cleanup()
  await renderWithPlugins(EditorConnectionSelect, {
    props: {
      modelValue: connection().id,
      entries: [
        {
          connection: connection(),
          capabilities: connectionCapabilities(),
          error: null,
        },
      ],
      capability: 'mail.send',
      label: '发送账户',
      disabled: false,
    },
  })
  await fireEvent.click(screen.getByRole('combobox', { name: '发送账户' }))
  await assertAdditional('Select:selected', '已选择 1 项')

  cleanup()
  await renderWithPlugins(EditorRecovery, {
    props: {
      error: {
        message: '版本或状态已变化，请重新加载后核对。',
        action: 'reload',
        traceId: 'synthetic-live-trace',
      },
      busy: false,
    },
  })
  await assertLive('EditorRecovery:16', '版本或状态已变化，请重新加载后核对。')
}

/** 原同步状态的三分支全部触发；原无 role 的 polite 区域另行断言，拒绝内层 alert。 */
async function calendarSources() {
  const view = await renderWithPlugins(CalendarRepreparePanel, {
    props: {
      connectionId: connection().id,
      entries: [],
      source: { event_id: 'synthetic-event', requires_sync: true },
      busy: false,
      canPrepare: false,
      syncTaskId: TASK_ID,
      syncStatus: 'running',
      syncRunning: true,
    },
  })
  await assertLive(
    'CalendarRepreparePanel:40',
    '本地日程版本尚未更新，请先同步来源账户，任务完成后重新读取提案。',
  )
  await assertLive('CalendarRepreparePanel:46', '同步进行中')
  await view.rerender({ syncRunning: false, syncStatus: 'succeeded' })
  await assertLive('CalendarRepreparePanel:46', '同步已完成，请重新读取提案。')
  await view.rerender({ syncStatus: 'failed' })
  await assertLive(
    'CalendarRepreparePanel:46',
    '同步未完成，请检查任务结果和连接。',
  )
  await view.rerender({ source: null })
  await assertLive(
    'CalendarRepreparePanel:89',
    '原来源无法核实，请 重新选择来源 。',
  )
  cleanup()
  const preview = calendarApproval().preview
  if (preview?.kind !== 'calendar') throw new Error('Missing calendar fixture')
  await renderWithPlugins(CalendarConflictNotice, {
    props: { conflicts: preview.conflicts },
  })
  await assertAdditional(
    'CalendarConflictNotice:polite',
    '未检查参会人可用性。',
  )
  const region = screen.getByRole('region', { name: '日程冲突检查' })
  expect(region).toHaveAttribute('aria-live', 'polite')
  expect(region).toHaveTextContent('未检查参会人可用性。')
  expect(region).toHaveTextContent('当前安排位于工作时间外。')
  expect(
    region.querySelectorAll(
      '[role="alert"], [role="status"], [aria-live="polite"], [aria-live="assertive"]',
    ),
  ).toHaveLength(0)
}

export const coreScenarios = {
  shellAlerts,
  actionDetail,
  approval,
  attention,
  timeline,
  sources,
  calendarSources,
  shellAdditions,
  historyAdditions,
}

/** 全局反馈来自真实Store输入与服务；RouterView测试内容只触发已有UI端口，不替换任何浮层。 */
async function shellAdditions() {
  network(() => json({ alerts: [] }))
  setViewport(375)
  const Controls = defineComponent({
    setup() {
      const confirm = useConfirm(),
        toast = useToast()
      return () =>
        h('div', [
          h(
            'button',
            {
              onClick: () =>
                confirm.require({
                  header: '合成确认',
                  message: '合成确认内容',
                  rejectProps: { label: '取消' },
                }),
            },
            '打开合成确认',
          ),
          h(
            'button',
            {
              onClick: () =>
                toast.add({ severity: 'error', summary: '合成非阻断错误' }),
            },
            '产生合成提示',
          ),
        ])
    },
  })
  const view = await renderWithPlugins(AppShell, {
    route: '/brief',
    global: { stubs: { RouterView: Controls } },
  })
  const tasks = useTasksStore(view.pinia)
  tasks.setConnectionState(TASK_ID, 'reconnecting')
  await assertAdditional(
    'AppShell:disconnect',
    '任务连接已断开，正在尝试恢复。',
  )
  tasks.setConnectionState(TASK_ID, 'connected')
  await assertAdditional('AppShell:recoveryToast', '任务连接已恢复')
  tasks.setConnectionState(TASK_ID, 'connected')
  await assertAdditional('AppShell:recoveryToast', '任务连接已恢复')
  for (const [button, id, title] of [
    ['打开导航', 'AppNavigation:drawer', '主导航'],
    ['打开任务时间线', 'TimelineDrawer:drawer', '任务时间线'],
  ] as const) {
    await fireEvent.click(screen.getByRole('button', { name: button }))
    await assertAdditional(id, title)
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  }
  await fireEvent.click(screen.getByRole('button', { name: '打开合成确认' }))
  await assertAdditional('AppShell:confirmation', '合成确认内容')
  await fireEvent.keyDown(screen.getByRole('alertdialog'), {
    key: 'Escape',
    code: 'Escape',
  })
  await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
  await fireEvent.click(screen.getByRole('button', { name: '产生合成提示' }))
  await assertAdditional('AppShell:errorToast', '合成非阻断错误')
}

/** 完整任务历史新增公告与原详情分离；每次变更都作用于真实列表组件。 */
async function historyAdditions() {
  const view = await renderWithPlugins(TaskHistoryList, {
    props: {
      page: historyPage(),
      filters: {
        scope: 'business',
        kind: null,
        status: null,
        created_from_date: null,
        created_to_date: null,
      },
      loading: true,
      error: null,
      newTaskHint: false,
    },
  })
  await assertAdditional('TaskHistoryList:loading', '正在加载任务历史…')
  await view.rerender({ loading: false, newTaskHint: true })
  await assertAdditional('TaskHistoryList:newTask', '有新任务，点击刷新')
  await view.rerender({ error: '合成历史读取失败' })
  await assertAdditional(
    'TaskHistoryList:error',
    '刷新失败，仍显示上次读取的数据。合成历史读取失败',
  )
}
