import { fireEvent, screen, waitFor } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { ref } from 'vue'
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
  connection: undefined as ReturnType<typeof ref<string>> | undefined,
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: (...args: Parameters<typeof useTaskEvents>) => {
    stream.recover = args[2]
    stream.connection = ref('connected')
    return stream.connection
  },
}))
const conversation = {
  id: '00000000-0000-0000-0000-000000000501',
  title: '合成会话',
  created_at: NOW,
  updated_at: NOW,
}
let messages: Message[]
beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 无布局观察器；保留真实组件，仅替换浏览器尺寸通知端口。
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  stream.recover = undefined
  messages = [
    {
      id: 'synthetic-message',
      role: 'assistant',
      content_markdown: '合成初始消息',
      task_id: TASK_ID,
      created_at: NOW,
    },
  ]
  vi.mocked(conversations.listConversations).mockResolvedValue([conversation])
  vi.mocked(conversations.getConversation).mockImplementation(async () => ({
    conversation,
    messages,
  }))
  vi.mocked(conversations.sendMessage)
    .mockReset()
    .mockResolvedValue({ task_id: TASK_ID })
  vi.mocked(actions.getAction).mockResolvedValue(
    actionSnapshot({
      status: 'waiting_approval',
      execution: null,
      approval: mailApproval(),
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

/** 使用统一插件渲染真实页面；最终消息与任务 Store 保持生产实现。 */
async function renderPage() {
  const { router, pinia } = await renderWithPlugins(ChatPage, {
    route: '/chat',
  })
  await waitFor(() =>
    expect(screen.queryByText('正在加载会话…')).not.toBeInTheDocument(),
  )
  return { router, tasks: useTasksStore(pinia) }
}

/** 排空已完成端口的微任务，保留原有竞态测试显式控制的读取顺序。 */
async function flushPromises(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
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
  it('sends with Enter once and exposes a busy disabled send control until REST accepts', async () => {
    messages = []
    const receipt = deferred<{ task_id: string }>()
    vi.mocked(conversations.sendMessage).mockReturnValueOnce(receipt.promise)
    await renderPage()
    const input = screen.getByRole('textbox', { name: '消息' })
    await fireEvent.update(input, '合成键盘消息')
    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '正在发送…' })).toBeDisabled(),
    )
    expect(screen.getByRole('button', { name: '正在发送…' })).toHaveAttribute(
      'aria-busy',
      'true',
    )
    expect(input).toBeDisabled()
    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    expect(conversations.sendMessage).toHaveBeenCalledTimes(1)
    receipt.resolve({ task_id: TASK_ID })
    await waitFor(() => expect(input).toHaveValue(''))
  })

  it('keeps Shift+Enter and IME confirmation available without sending', async () => {
    await renderPage()
    const input = screen.getByRole('textbox', { name: '消息' })
    await fireEvent.update(input, '合成输入')
    const newline = new KeyboardEvent('keydown', {
      key: 'Enter',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    })
    await fireEvent(input, newline)
    expect(newline.defaultPrevented).toBe(false)
    await fireEvent.compositionStart(input)
    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    await fireEvent.compositionEnd(input)
    await fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    await fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })
    expect(conversations.sendMessage).not.toHaveBeenCalled()
    expect(input).toHaveValue('合成输入')
  })

  it('grows the message textarea with content without using an internal component selector', async () => {
    await renderPage()
    const input = screen.getByRole('textbox', { name: '消息' })
    // jsdom 不提供布局：只模拟公开浏览器尺寸，让真实 Textarea 的 autoResize 执行。
    Object.defineProperty(input, 'offsetParent', {
      configurable: true,
      value: document.body,
    })
    Object.defineProperty(input, 'scrollHeight', {
      configurable: true,
      value: 240,
    })
    await fireEvent.update(input, '合成第一行\n合成第二行')
    expect(input.style.height).toBe('240px')
  })

  it('preserves one polite task status while disconnected and keeps the persisted task running', async () => {
    const { tasks } = await renderPage()
    tasks.setTask(preparationTask('prepare_mail_draft', 'running', '1'))
    if (stream.connection) stream.connection.value = 'reconnecting'
    await flushPromises()
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent(
      '任务：执行中 · 正在恢复实时连接',
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(tasks.tasks[TASK_ID]?.status).toBe('running')
  })

  it('creates the initial local conversation only when the server list is empty', async () => {
    vi.mocked(conversations.listConversations).mockResolvedValueOnce([])
    vi.mocked(conversations.createConversation).mockResolvedValueOnce(
      conversation,
    )
    await renderPage()
    expect(
      screen.getByRole('button', { name: conversation.title }),
    ).toBeVisible()
    expect(conversations.createConversation).toHaveBeenCalledTimes(1)
  })

  it('preserves confirmation before deleting a conversation', async () => {
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true)
    await renderPage()
    await fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(conversations.deleteConversation).not.toHaveBeenCalled()
    await fireEvent.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: conversation.title }),
      ).not.toBeInTheDocument(),
    )
    expect(confirm).toHaveBeenCalledWith('确定删除会话？')
    expect(conversations.deleteConversation).toHaveBeenCalledWith(
      conversation.id,
    )
    confirm.mockRestore()
  })

  it('preserves the action snapshot error alert and the separate task status', async () => {
    vi.mocked(actions.getAction).mockRejectedValueOnce(
      readFailure('synthetic-snapshot'),
    )
    const { tasks } = await renderPage()
    tasks.setTask({
      ...preparationTask('prepare_mail_draft', 'running', '1'),
      kind: 'trusted_action',
    })
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        '操作快照读取失败，请在操作中心重新加载。',
      ),
    )
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('links the action centre only to the existing workspace', async () => {
    await renderPage()
    expect(screen.getByRole('link', { name: '操作中心' })).toHaveAttribute(
      'href',
      '/actions',
    )
  })

  it.each([
    ['prepare_mail_draft', `/mail/drafts/${DRAFT_ID}`],
    ['prepare_calendar_proposal', `/calendar/proposals/${PROPOSAL_ID}`],
  ] as const)(
    'keeps the latest %s result when an older recovery read completes last',
    async (kind, path) => {
      const initialMessages = [...messages]
      const { tasks } = await renderPage()
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
      expect(
        screen.getByRole('link', { name: '打开准备结果' }).getAttribute('href'),
      ).toBe(path)
      older.resolve({ conversation, messages: initialMessages })
      await flushPromises()
      expect(screen.getByText('合成初始消息')).toBeVisible()
      expect(screen.getByRole('link', { name: '打开准备结果' })).toBeVisible()
      expect(
        screen.getByRole('link', { name: '打开准备结果' }).getAttribute('href'),
      ).toBe(path)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each(['older-error', 'latest-error'] as const)(
    'keeps messages and errors owned by the latest read when receiving %s',
    async (outcome) => {
      const initialMessages = [...messages]
      const { tasks } = await renderPage()
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
        expect(screen.getByRole('link', { name: '打开准备结果' })).toBeVisible()
        older.reject(readFailure('synthetic-older-read'))
        await flushPromises()
        expect(screen.queryByRole('alert')).not.toBeInTheDocument()
        expect(
          screen
            .getByRole('link', { name: '打开准备结果' })
            .getAttribute('href'),
        ).toBe(`/mail/drafts/${DRAFT_ID}`)
      } else {
        latest.reject(readFailure('synthetic-latest-read'))
        await flushPromises()
        expect(screen.getByRole('alert').textContent).toContain(
          'synthetic-latest-read',
        )
        older.resolve(result)
        await flushPromises()
        expect(screen.getByRole('alert').textContent).toContain(
          'synthetic-latest-read',
        )
        expect(screen.getByText('合成初始消息')).toBeVisible()
        expect(
          screen.queryByRole('link', { name: '打开准备结果' }),
        ).not.toBeInTheDocument()
      }
    },
  )

  it('retains an accepted send receipt while recovery reads the same conversation', async () => {
    messages = []
    const receipt = deferred<{ task_id: string }>()
    vi.mocked(conversations.sendMessage).mockReturnValueOnce(receipt.promise)
    const { tasks } = await renderPage()
    tasks.setTask(preparationTask('prepare_mail_draft', 'queued', '1'))
    await fireEvent.update(
      screen.getByRole('textbox', { name: '消息' }),
      '请准备一封邮件草稿',
    )
    await fireEvent.click(screen.getByRole('button', { name: '发送' }))
    recoverMessages()
    await flushPromises()
    expect(conversations.getConversation).toHaveBeenCalledTimes(2)
    expect(
      screen
        .getByRole('button', { name: '正在发送…' })
        .getAttribute('disabled'),
    ).toBeDefined()
    receipt.resolve({ task_id: TASK_ID })
    await flushPromises()
    expect(screen.getByTestId('chat-task-status').textContent).toContain('排队')
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveProperty(
      'value',
      '',
    )
    expect(
      screen.getByRole('textbox', { name: '消息' }).getAttribute('disabled'),
    ).toBeNull()
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
      await renderPage()
      const older = deferred<MessageRead>()
      const latest = deferred<MessageRead>()
      vi.mocked(conversations.getConversation)
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(latest.promise)
      recoverMessages()
      await flushPromises()
      await fireEvent.click(screen.getByRole('button', { name: other.title }))
      expect(screen.getByText('正在加载会话…')).toBeVisible()
      expect(conversations.getConversation).toHaveBeenLastCalledWith(other.id)
      latest.resolve({ conversation: other, messages: [] })
      await flushPromises()
      if (outcome === 'success') older.resolve({ conversation, messages })
      else older.reject(readFailure('synthetic-old-conversation'))
      await flushPromises()
      expect(screen.queryByText('正在加载会话…')).not.toBeInTheDocument()
      expect(screen.queryByText('合成初始消息')).not.toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(
        screen.getByRole('textbox', { name: '消息' }).getAttribute('disabled'),
      ).toBeNull()
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
      const { router } = await renderPage()
      const link = screen.getByRole('link', { name: '打开本地对象' })
      expect(link.getAttribute('href')).toBe(path.replace('/api/v1', ''))
      await fireEvent.click(link)
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
      await renderPage()
      // 被拒绝的 Markdown 可被 linkify 识别出普通 HTTPS 文本；不能把它当作编辑链接。
      expect(screen.queryByRole('link', { name: '不可信链接' })).toBeNull()
      const links = screen
        .queryAllByRole('link')
        .filter((link) => link.textContent !== '操作中心')
      expect(
        links.every((link) =>
          /^https?:\/\//i.test(link.getAttribute('href') ?? ''),
        ),
      ).toBe(true)
      expect(
        links.every(
          (link) =>
            link.getAttribute('target') === '_blank' &&
            link.getAttribute('rel') === 'noopener noreferrer',
        ),
      ).toBe(true)
    },
  )

  it('sends explicit preparation text only through chat and renders queued progress from the task', async () => {
    messages = []
    const { tasks } = await renderPage()
    await fireEvent.update(
      screen.getByRole('textbox', { name: '消息' }),
      '请准备一封邮件草稿',
    )
    await fireEvent.click(screen.getByRole('button', { name: '发送' }))
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
    expect(screen.getByTestId('chat-task-status').textContent).toContain('排队')
    expect(screen.getByTestId('chat-task-status').textContent).not.toContain(
      '已发送',
    )
    expect(mail.createMailDraft).not.toHaveBeenCalled()
    expect(actions.getAction).not.toHaveBeenCalled()
    expect(
      screen.queryByRole('region', { name: '人工审批' }),
    ).not.toBeInTheDocument()
  })

  it('loads action editor_url and approval only after a genuine trusted task snapshot is recovered', async () => {
    const { tasks } = await renderPage()
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
    expect(screen.getByRole('link', { name: '打开邮件草稿' })).toHaveAttribute(
      'href',
      `/mail/drafts/${DRAFT_ID}?task=${TASK_ID}`,
    )
    expect(screen.getByRole('region', { name: '人工审批' })).toBeVisible()
  })
})
