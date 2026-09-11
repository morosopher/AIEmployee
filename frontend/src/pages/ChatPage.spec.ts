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
import type { Message } from '@/api/types'
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
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
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

describe('ChatPage', () => {
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
