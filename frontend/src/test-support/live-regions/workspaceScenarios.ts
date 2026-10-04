/** 路由工作区保持真实API解析、Store与SSE接线；只有HTTP／EventSource传输使用合成边界。 */
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/vue'
import { expect } from 'vitest'
import type { TaskSnapshot } from '@/api/types'
import LoginPage from '@/pages/LoginPage.vue'
import TodayBriefPage from '@/pages/TodayBriefPage.vue'
import ChatPage from '@/pages/ChatPage.vue'
import TasksPage from '@/pages/TasksPage.vue'
import ConnectionsPage from '@/pages/ConnectionsPage.vue'
import SettingsPage from '@/pages/SettingsPage.vue'
import ActionCenterWorkspace from '@/components/ActionCenterWorkspace.vue'
import { useActionsStore } from '@/stores/actions'
import { renderWithPlugins } from '../renderWithPlugins'
import { DRAFT_ID, NOW, TASK_ID, userSettings } from '../actionFixtures'
import { connection } from '../editorFixtures'
import { historyPage } from '../taskHistoryFixtures'
import { TaskEventSource } from '../taskEventSource'
import { assertLive, deferred, json, network, problem } from './assertions'
import { brief } from './coreScenarios'

const task: TaskSnapshot = {
  id: TASK_ID,
  kind: 'trusted_action',
  status: 'running',
  retry_of_task_id: null,
  error_code: null,
  event_cursor: '1',
  steps: [],
}

async function loginPage() {
  network(() => problem('invalid_credentials', 401))
  await renderWithPlugins(LoginPage)
  await fireEvent.update(
    screen.getByLabelText('邮箱'),
    'login@mail.example.test',
  )
  await fireEvent.update(screen.getByLabelText('密码'), 'synthetic-password')
  await fireEvent.submit(screen.getByRole('form'))
  await assertLive(
    'LoginPage:82',
    'Invalid credentials，请检查邮箱和密码后重试。',
  )
}

async function briefPage() {
  const read = deferred<Response>()
  network(() => read.promise)
  await renderWithPlugins(TodayBriefPage)
  await assertLive('TodayBriefPage:51', '正在加载…')
  read.resolve(problem('internal_error', 500))
  await assertLive('TodayBriefPage:57', '暂无今日简报或加载失败。')
  cleanup()
  const value = {
    ...brief,
    items: [
      {
        position: 1,
        section: '合成建议',
        priority: 'high',
        title: '合成来源',
        body_markdown: '',
        suggested_action_kind: 'mail.reply',
        source_refs: [
          {
            source_type: 'email_thread',
            source_id: DRAFT_ID,
            provider_url: null,
          },
        ],
      },
    ],
  }
  network((url, init) =>
    init?.method === 'POST'
      ? deferred<Response>().promise
      : json(url.endsWith('/today') ? value : [value]),
  )
  await renderWithPlugins(TodayBriefPage)
  await fireEvent.click(
    await screen.findByRole('button', { name: '创建回复草稿' }),
  )
  await assertLive('TodayBriefPage:68', '正在创建本地编辑对象…')
}

async function connectionsPage() {
  const read = deferred<Response>()
  network(() => read.promise)
  await renderWithPlugins(ConnectionsPage)
  await assertLive('ConnectionsPage:64', '正在加载连接能力…')
  read.resolve(json([]))
  await assertLive(
    'ConnectionsPage:70',
    '尚未连接账户。请选择供应商开始只读授权。',
  )
  cleanup()
  network(() => problem('internal_error', 500))
  await renderWithPlugins(ConnectionsPage)
  await assertLive('ConnectionsPage:76', '请求失败，请重试。')
  cleanup()
  network((url, init) =>
    init?.method === 'POST'
      ? json({ gmail_task_id: TASK_ID, calendar_task_id: TASK_ID })
      : url.endsWith('/connections')
        ? json([connection()])
        : problem('internal_error', 500),
  )
  await renderWithPlugins(ConnectionsPage)
  await assertLive('ConnectionsPage:102', '连接状态：connected')
  await assertLive('ConnectionsPage:107', '能力加载失败，请刷新后重试。')
  await fireEvent.click(screen.getByRole('button', { name: '立即同步' }))
  await assertLive(
    'ConnectionsPage:83',
    '同步已排队，数据完整性以任务结果为准。',
  )
}

async function chatPage() {
  const read = deferred<Response>()
  network(() => read.promise)
  await renderWithPlugins(ChatPage)
  await assertLive('ChatPage:41', '正在加载会话…')
  read.resolve(problem('internal_error', 500))
  await assertLive('ChatPage:47', '请求失败，请重试。')
  cleanup()
  const conversation = {
    id: DRAFT_ID,
    title: '合成会话',
    created_at: NOW,
    updated_at: NOW,
  }
  network((url) =>
    url.endsWith('/conversations')
      ? json([conversation])
      : url.includes('/conversations/')
        ? json({
            conversation,
            messages: [
              {
                id: 'synthetic-message',
                role: 'assistant',
                content_markdown: '合成最终消息',
                task_id: TASK_ID,
                created_at: NOW,
              },
            ],
          })
        : url.includes('/tasks/')
          ? json(task)
          : problem('internal_error', 500),
  )
  await renderWithPlugins(ChatPage)
  await waitFor(() =>
    expect(
      TaskEventSource.instances.filter((source) => !source.closed),
    ).toHaveLength(1),
  )
  const source = TaskEventSource.instances.find((item) => !item.closed)
  if (!source) throw new Error('Missing chat event source')
  source.emit(
    'task.snapshot',
    {
      id: '1',
      sequence: '1',
      task_id: TASK_ID,
      occurred_at: NOW,
      step_id: null,
      event: 'task.snapshot',
      payload: task,
    },
    '1',
  )
  source.onerror?.(new Event('error'))
  await assertLive('ChatPage:90', '任务：执行中 · 正在恢复实时连接')
  await assertLive('ChatPage:97', '操作快照读取失败，请在操作中心重新加载。')
  source.onopen?.(new Event('open'))
  await assertLive('ChatPage:90', '任务：执行中')
}

async function tasksPage() {
  const read = deferred<Response>()
  network((url) =>
    url.startsWith('/api/v1/tasks?') || url === '/api/v1/tasks'
      ? json(historyPage())
      : read.promise,
  )
  await renderWithPlugins(TasksPage, { route: `/tasks?task_id=${TASK_ID}` })
  await assertLive('TasksPage:124', '实时连接：connecting')
  await assertLive('TasksPage:129', '正在恢复任务快照…')
  read.resolve(problem('internal_error', 500))
  await assertLive('TasksPage:135', '无法加载该任务，请刷新页面后重试。')
  cleanup()
  network((url) =>
    url.startsWith('/api/v1/tasks?') || url === '/api/v1/tasks'
      ? json(historyPage())
      : json({
          ...task,
          kind: 'calendar.restore.prepare',
          status: 'succeeded',
        }),
  )
  await renderWithPlugins(TasksPage, { route: `/tasks?task_id=${TASK_ID}` })
  await assertLive(
    'TasksPage:143',
    '准备完成后仍需核对并提交新的审批，日程尚未因此恢复。',
  )
  await assertLive(
    'TasksPage:155',
    '恢复提案结果已不可用，请返回操作中心核对原修改和保留期。',
  )
}

async function settingsPage() {
  network((url, init) =>
    init?.method === 'POST' || url.endsWith('/sessions')
      ? problem('internal_error', 500)
      : url.endsWith('/settings')
        ? json(userSettings())
        : json([]),
  )
  await renderWithPlugins(SettingsPage)
  await assertLive('SettingsPage:187', '无法加载会话。')
  await fireEvent.click(screen.getByRole('button', { name: '删除来源缓存' }))
  await assertLive('SettingsPage:236', '来源缓存删除任务创建失败，请稍后重试。')
}

async function actionsPage() {
  const list = deferred<Response>(),
    snapshot = deferred<Response>()
  network((url) =>
    url === '/api/v1/actions'
      ? list.promise
      : url.startsWith('/api/v1/actions/')
        ? snapshot.promise
        : json(task),
  )
  const view = await renderWithPlugins(ActionCenterWorkspace, {
    route: `/actions?task=${TASK_ID}`,
  })
  await assertLive('ActionsPage:164', '正在加载操作…')
  await assertLive('ActionsPage:302', '正在建立实时连接')
  await assertLive('ActionsPage:311', '正在恢复操作快照…')
  list.resolve(problem('internal_error', 500))
  await assertLive('ActionsPage:178', '无法刷新操作，请重试。')
  network(() => json({ items: [], limit: 50, offset: 0 }))
  await useActionsStore(view.pinia).refreshList()
  await assertLive('ActionsPage:164', '当前页 0 项操作')
  snapshot.resolve(problem('internal_error', 500))
  await assertLive('ActionsPage:318', '无法刷新操作，请重试。')
  network(() => deferred<Response>().promise)
  await fireEvent.click(screen.getByRole('button', { name: '新邮件' }))
  await assertLive('ActionsPage:106', '正在创建本地编辑对象…')
}

export const workspaceScenarios = {
  loginPage,
  briefPage,
  connectionsPage,
  chatPage,
  tasksPage,
  settingsPage,
  actionsPage,
}
