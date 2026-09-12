import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatPage from './ChatPage.vue'
import * as conversations from '@/api/conversations'
import * as actions from '@/api/actions'
import * as mail from '@/api/mail'
import * as calendar from '@/api/calendar'
import { ProblemError } from '@/api/client'
import type { Message, TaskSnapshot } from '@/api/types'
import type { useTaskEvents } from '@/composables/useTaskEvents'
import { useTasksStore } from '@/stores/tasks'
import {
  actionSnapshot,
  DRAFT_ID,
  NOW,
  PROPOSAL_ID,
  TASK_ID,
} from '@/test-support/actionFixtures'
import { mailApproval } from '@/test-support/editorFixtures'

vi.mock('@/api/conversations', () => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  sendMessage: vi.fn(),
  deleteConversation: vi.fn(),
}))
vi.mock('@/api/actions', async (original) => ({
  ...(await original<typeof import('@/api/actions')>()),
  getAction: vi.fn(),
  listActions: vi.fn(),
}))
vi.mock('@/api/mail', async (original) => ({
  ...(await original<typeof import('@/api/mail')>()),
  createMailDraft: vi.fn(),
  submitMailDraft: vi.fn(),
}))
vi.mock('@/api/calendar', async (original) => ({
  ...(await original<typeof import('@/api/calendar')>()),
  createCalendarProposal: vi.fn(),
  submitCalendarProposal: vi.fn(),
}))
const stream = vi.hoisted(() => ({
  recover: undefined as (() => void) | undefined,
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: (...args: Parameters<typeof useTaskEvents>) => {
    stream.recover = args[2]
    return ref('connected')
  },
}))
const conversation = {
  id: '00000000-0000-0000-0000-000000000501',
  title: '合成会话',
  created_at: NOW,
  updated_at: NOW,
}
let messages: Message[]
let wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  vi.clearAllMocks()
  stream.recover = undefined
  messages = [
    {
      id: 'synthetic-message',
      role: 'assistant',
      content_markdown: '',
      task_id: TASK_ID,
      created_at: NOW,
    },
  ]
  vi.mocked(conversations.listConversations).mockResolvedValue([conversation])
  vi.mocked(conversations.getConversation).mockImplementation(async () => ({
    conversation,
    messages,
  }))
  vi.mocked(conversations.sendMessage).mockResolvedValue({ task_id: TASK_ID })
  vi.mocked(actions.getAction).mockResolvedValue(
    actionSnapshot({
      status: 'waiting_approval',
      execution: null,
      approval: mailApproval(),
    }),
  )
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
})

/** 保留真实路由和任务 Store，使模型文字与持久任务状态的边界可以被直接断言。 */
async function renderPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat', component: ChatPage },
      {
        path: '/mail/drafts/:draftId',
        component: { template: '<p>编辑草稿</p>' },
      },
      {
        path: '/calendar/proposals/:proposalId',
        component: { template: '<p>编辑提案</p>' },
      },
      { path: '/actions', component: { template: '<p>操作中心</p>' } },
    ],
  })
  await router.push('/chat')
  const pinia = createPinia()
  const wrapper = mount(ChatPage, { global: { plugins: [pinia, router] } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router, tasks: useTasksStore(pinia) }
}

type MessageRead = Awaited<ReturnType<typeof conversations.getConversation>>

/** 只控制端口完成顺序；真实页面、消息覆盖和任务终态 watch 均保持生产实现。 */
function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (cause: Error) => void
} {
  let resolve: (value: T) => void = () => undefined
  let reject: (cause: Error) => void = () => undefined
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept
    reject = fail
  })
  return { promise, resolve, reject }
}

/** 重连必须走 composable 注册的恢复回调，不能在测试中直接替换页面消息。 */
function recoverMessages(): void {
  if (!stream.recover)
    throw new Error('Task recovery callback was not registered')
  stream.recover()
}

/** 构造与编辑链接一致的准备任务，不把普通聊天结果伪装为可信写入任务。 */
function preparationTask(
  kind: 'prepare_mail_draft' | 'prepare_calendar_proposal',
  status: 'running' | 'queued' | 'succeeded',
  cursor: string,
): TaskSnapshot {
  return {
    id: TASK_ID,
    kind,
    status,
    retry_of_task_id: null,
    error_code: null,
    event_cursor: cursor,
    steps: [],
  }
}

/** 返回完整的合成最终消息；链接只打开本地编辑器，不表示邮件已发送或日程已执行。 */
function preparedMessage(path: string): Message {
  return {
    id: 'synthetic-prepared-result',
    role: 'assistant',
    content_markdown: `[打开准备结果](${path})`,
    task_id: TASK_ID,
    created_at: NOW,
  }
}

/** 错误仅带非敏感追踪编号，便于核对哪次读取拥有当前恢复提示。 */
function readFailure(traceId: string): ProblemError {
  return new ProblemError({
    type: 'about:blank',
    title: 'Synthetic message read failure',
    detail: '',
    status: 503,
    instance: '',
    error_code: 'temporarily_unavailable',
    trace_id: traceId,
  })
}

describe('ChatPage', () => {
  it.each([
    ['prepare_mail_draft', `/mail/drafts/${DRAFT_ID}`],
    ['prepare_calendar_proposal', `/calendar/proposals/${PROPOSAL_ID}`],
  ] as const)(
    'keeps the latest %s result when an older recovery read completes last',
    async (kind, path) => {
      const initialMessages = [...messages]
      const { wrapper, tasks } = await renderPage()
      tasks.setTask(preparationTask(kind, 'running', '1'))
      await flushPromises()
      const older = deferred<MessageRead>()
      const latest = deferred<MessageRead>()
      vi.mocked(conversations.getConversation)
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(latest.promise)

      recoverMessages()
      await flushPromises()
      tasks.setTask(preparationTask(kind, 'succeeded', '2'))
      await flushPromises()
      expect(conversations.getConversation).toHaveBeenCalledTimes(3)
      latest.resolve({
        conversation,
        messages: [...initialMessages, preparedMessage(path)],
      })
      await flushPromises()
      expect(wrapper.get('a[data-editor-link]').attributes('href')).toBe(path)
      older.resolve({ conversation, messages: initialMessages })
      await flushPromises()
      expect(wrapper.findAll('.markdown-message')).toHaveLength(2)
      expect(wrapper.get('a[data-editor-link]').attributes('href')).toBe(path)
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each(['older-error', 'latest-error'] as const)(
    'keeps messages and errors owned by the latest read when receiving %s',
    async (outcome) => {
      const initialMessages = [...messages]
      const { wrapper, tasks } = await renderPage()
      tasks.setTask(preparationTask('prepare_mail_draft', 'running', '1'))
      await flushPromises()
      const older = deferred<MessageRead>()
      const latest = deferred<MessageRead>()
      vi.mocked(conversations.getConversation)
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(latest.promise)
      recoverMessages()
      await flushPromises()
      tasks.setTask(preparationTask('prepare_mail_draft', 'succeeded', '2'))
      await flushPromises()
      expect(conversations.getConversation).toHaveBeenCalledTimes(3)
      const result = {
        conversation,
        messages: [
          ...initialMessages,
          preparedMessage(`/mail/drafts/${DRAFT_ID}`),
        ],
      }
      if (outcome === 'older-error') {
        latest.resolve(result)
        await flushPromises()
        expect(wrapper.find('a[data-editor-link]').exists()).toBe(true)
        older.reject(readFailure('synthetic-older-read'))
        await flushPromises()
        expect(wrapper.find('[role="alert"]').exists()).toBe(false)
        expect(wrapper.get('a[data-editor-link]').attributes('href')).toBe(
          `/mail/drafts/${DRAFT_ID}`,
        )
      } else {
        latest.reject(readFailure('synthetic-latest-read'))
        await flushPromises()
        expect(wrapper.get('[role="alert"]').text()).toContain(
          'synthetic-latest-read',
        )
        older.resolve(result)
        await flushPromises()
        expect(wrapper.get('[role="alert"]').text()).toContain(
          'synthetic-latest-read',
        )
        expect(wrapper.findAll('.markdown-message')).toHaveLength(1)
        expect(wrapper.find('a[data-editor-link]').exists()).toBe(false)
      }
    },
  )

  it('retains an accepted send receipt while recovery reads the same conversation', async () => {
    messages = []
    const receipt = deferred<{ task_id: string }>()
    vi.mocked(conversations.sendMessage).mockReturnValueOnce(receipt.promise)
    const { wrapper, tasks } = await renderPage()
    tasks.setTask(preparationTask('prepare_mail_draft', 'queued', '1'))
    await wrapper
      .get('textarea[aria-label="消息"]')
      .setValue('请准备一封邮件草稿')
    await wrapper.get('form').trigger('submit')
    recoverMessages()
    await flushPromises()
    expect(conversations.getConversation).toHaveBeenCalledTimes(2)
    expect(
      wrapper.get('button[type="submit"]').attributes('disabled'),
    ).toBeDefined()
    receipt.resolve({ task_id: TASK_ID })
    await flushPromises()
    expect(wrapper.get('[data-testid="chat-task-status"]').text()).toContain(
      '排队',
    )
    expect(wrapper.get('textarea[aria-label="消息"]').element).toHaveProperty(
      'value',
      '',
    )
    expect(
      wrapper.get('textarea[aria-label="消息"]').attributes('disabled'),
    ).toBeUndefined()
    expect(conversations.sendMessage).toHaveBeenCalledTimes(1)
    expect(conversations.getConversation).toHaveBeenCalledTimes(3)
  })

  it.each(['success', 'error'] as const)(
    'ignores an old conversation read %s after selection and preserves loading state',
    async (outcome) => {
      const other = {
        ...conversation,
        id: '00000000-0000-0000-0000-000000000502',
        title: 'Synthetic other conversation',
      }
      vi.mocked(conversations.listConversations).mockResolvedValue([
        conversation,
        other,
      ])
      const { wrapper } = await renderPage()
      const older = deferred<MessageRead>()
      const latest = deferred<MessageRead>()
      vi.mocked(conversations.getConversation)
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(latest.promise)
      recoverMessages()
      await flushPromises()
      await wrapper
        .get('[aria-label="会话历史"] > div:nth-child(2) button')
        .trigger('click')
      expect(wrapper.text()).toContain('正在加载会话')
      expect(conversations.getConversation).toHaveBeenLastCalledWith(other.id)
      latest.resolve({ conversation: other, messages: [] })
      await flushPromises()
      if (outcome === 'success') older.resolve({ conversation, messages })
      else older.reject(readFailure('synthetic-old-conversation'))
      await flushPromises()
      expect(wrapper.text()).not.toContain('正在加载会话')
      expect(wrapper.findAll('.markdown-message')).toHaveLength(0)
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
      expect(
        wrapper.get('textarea[aria-label="消息"]').attributes('disabled'),
      ).toBeUndefined()
    },
  )

  it.each([
    `/api/v1/mail/drafts/${DRAFT_ID}`,
    `/mail/drafts/${DRAFT_ID}`,
    `/api/v1/calendar/proposals/${PROPOSAL_ID}`,
    `/calendar/proposals/${PROPOSAL_ID}`,
  ])(
    'opens a server local result %s without creating or approving an action',
    async (path) => {
      messages[0] = {
        ...(messages[0] as Message),
        content_markdown: `[打开本地对象](${path})`,
      }
      const { wrapper, router } = await renderPage()
      const link = wrapper.get('a[data-editor-link]')
      expect(link.attributes('href')).toBe(path.replace('/api/v1', ''))
      await link.trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.path).toBe(path.replace('/api/v1', ''))
      expect(mail.createMailDraft).not.toHaveBeenCalled()
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each([
    `/api/v1/mail/drafts/${DRAFT_ID}?next=https://example.test`,
    '/settings',
    '//example.test',
    'javascript:alert(1)',
  ])(
    'does not turn an arbitrary relative or script path into an editor link (%s)',
    async (path) => {
      messages[0] = {
        ...(messages[0] as Message),
        content_markdown: `[不可信链接](${path})`,
      }
      const { wrapper } = await renderPage()
      // 被拒绝的 Markdown 可被 linkify 识别出普通 HTTPS 文本；不能把它当作编辑链接。
      expect(
        wrapper.find('.markdown-message a[data-editor-link]').exists(),
      ).toBe(false)
      const links = wrapper.findAll('.markdown-message a')
      expect(
        links.every((link) =>
          /^https?:\/\//i.test(link.attributes('href') ?? ''),
        ),
      ).toBe(true)
      expect(
        links.every(
          (link) =>
            link.attributes('target') === '_blank' &&
            link.attributes('rel') === 'noopener noreferrer',
        ),
      ).toBe(true)
    },
  )

  it('sends explicit preparation text only through chat and renders queued progress from the task', async () => {
    messages = []
    const { wrapper, tasks } = await renderPage()
    await wrapper
      .get('textarea[aria-label="消息"]')
      .setValue('请准备一封邮件草稿')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(conversations.sendMessage).toHaveBeenCalledWith(
      conversation.id,
      '请准备一封邮件草稿',
      expect.any(String),
    )
    tasks.setTask({
      id: TASK_ID,
      kind: 'prepare_mail_draft',
      status: 'queued',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '1',
      steps: [],
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="chat-task-status"]').text()).toContain(
      '排队',
    )
    expect(
      wrapper.get('[data-testid="chat-task-status"]').text(),
    ).not.toContain('已发送')
    expect(mail.createMailDraft).not.toHaveBeenCalled()
    expect(actions.getAction).not.toHaveBeenCalled()
    expect(wrapper.find('[aria-label="人工审批"]').exists()).toBe(false)
  })

  it('loads action editor_url and approval only after a genuine trusted task snapshot is recovered', async () => {
    const { wrapper, tasks } = await renderPage()
    expect(actions.getAction).not.toHaveBeenCalled()
    tasks.setTask({
      id: TASK_ID,
      kind: 'trusted_action',
      status: 'waiting_approval',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '1',
      steps: [],
    })
    await flushPromises()
    expect(actions.getAction).toHaveBeenCalledWith(TASK_ID)
    expect(
      wrapper
        .find(`a[href="/mail/drafts/${DRAFT_ID}?task=${TASK_ID}"]`)
        .exists(),
    ).toBe(true)
    expect(wrapper.find('[aria-label="人工审批"]').exists()).toBe(true)
  })
})
