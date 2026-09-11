import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import MailDraftPage from './MailDraftPage.vue'
import * as mail from '@/api/mail'
import * as connections from '@/api/connections'
import * as actions from '@/api/actions'
import { cancelTask, ProblemError } from '@/api/client'
import { useTasksStore } from '@/stores/tasks'
import {
  actionSnapshot,
  CONNECTION_ID,
  DRAFT_ID,
  mailDraft,
  TASK_ID,
} from '@/test-support/actionFixtures'
import {
  connection,
  connectionCapabilities,
  mailApproval,
} from '@/test-support/editorFixtures'
import type { MailDraft } from '@/api/types'

vi.mock('@/api/mail', async (original) => ({
  ...(await original<typeof import('@/api/mail')>()),
  getMailDraft: vi.fn(),
  updateMailDraft: vi.fn(),
  submitMailDraft: vi.fn(),
  generateMailDraft: vi.fn(),
  createMailDraft: vi.fn(),
}))
vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
}))
vi.mock('@/api/actions', async (original) => ({
  ...(await original<typeof import('@/api/actions')>()),
  getAction: vi.fn(),
  listActions: vi.fn(),
}))
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  cancelTask: vi.fn(),
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))

let current: MailDraft
let wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  vi.clearAllMocks()
  current = {
    ...mailDraft(),
    to: ['recipient@example.test'],
    subject: 'Synthetic heading',
  }
  vi.mocked(mail.getMailDraft).mockImplementation(async () => current)
  vi.mocked(mail.updateMailDraft).mockImplementation(
    async (_id, input) =>
      (current = { ...current, ...input, version: input.version + 1 }),
  )
  vi.mocked(mail.submitMailDraft).mockResolvedValue({
    task_id: TASK_ID,
    status: 'queued',
  })
  vi.mocked(mail.generateMailDraft).mockResolvedValue({
    task_id: TASK_ID,
    status: 'queued',
  })
  vi.mocked(connections.listConnections).mockResolvedValue([
    connection(),
    connection('microsoft'),
  ])
  vi.mocked(connections.getConnectionCapabilities).mockImplementation(
    async (id) =>
      connectionCapabilities(id === CONNECTION_ID ? 'google' : 'microsoft'),
  )
  vi.mocked(actions.listActions).mockResolvedValue({
    items: [],
    limit: 50,
    offset: 0,
  })
  vi.mocked(actions.getAction).mockResolvedValue(
    actionSnapshot({
      status: 'waiting_approval',
      approval: mailApproval(),
      execution: null,
    }),
  )
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
})

/** 真实路由与 Pinia 保留版本恢复流程；只有 HTTP 端口和 SSE 使用合成替身。 */
async function renderPage(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/mail/drafts/:draftId', component: MailDraftPage },
      { path: '/actions', component: { template: '<p>操作中心</p>' } },
      { path: '/connections', component: { template: '<p>连接</p>' } },
    ],
  })
  await router.push(`/mail/drafts/${DRAFT_ID}${query}`)
  const pinia = createPinia()
  const wrapper = mount(MailDraftPage, {
    attachTo: document.body,
    global: { plugins: [pinia, router] },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router, tasks: useTasksStore(pinia) }
}

describe('MailDraftPage', () => {
  it.each(['reply', 'reply_all'] as const)(
    'locks the account, source and subject of %s while keeping ordinary text controls',
    async (mode) => {
      current = {
        ...current,
        mode,
        source_thread_id: 'synthetic-provider-thread',
        recipient_suggestions: ['suggested@example.test'],
      }
      const { wrapper } = await renderPage()
      expect(
        wrapper.get('select[aria-label="发送账户"]').attributes('disabled'),
      ).toBeDefined()
      expect(
        wrapper.get('input[aria-label="主题"]').attributes('disabled'),
      ).toBeDefined()
      expect(wrapper.text()).toContain('synthetic-provider-thread')
      expect(wrapper.find('textarea[aria-label="纯文本正文"]').exists()).toBe(
        true,
      )
      expect(wrapper.find('datalist option').attributes('value')).toBe(
        'suggested@example.test',
      )
      expect(wrapper.text()).toContain('不可撤回')
      await wrapper
        .get('input[aria-label="抄送 CC"]')
        .setValue('cc@example.test')
      await wrapper.get('button[name="save-draft"]').trigger('click')
      expect(
        vi.mocked(mail.updateMailDraft).mock.calls[0]?.[1],
      ).not.toHaveProperty('connection_id')
      expect(
        vi.mocked(mail.updateMailDraft).mock.calls[0]?.[1],
      ).not.toHaveProperty('subject')
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
    },
  )

  it('saves a new account in the same version and submits only a separately reviewed saved version', async () => {
    const { wrapper, router } = await renderPage()
    await wrapper
      .get('select[aria-label="发送账户"]')
      .setValue(connection('microsoft').id)
    expect(
      wrapper.get('button[name="submit-draft"]').attributes('disabled'),
    ).toBeDefined()
    await wrapper.get('button[name="save-draft"]').trigger('click')
    await flushPromises()
    expect(mail.updateMailDraft).toHaveBeenCalledWith(
      DRAFT_ID,
      expect.objectContaining({
        version: 1,
        connection_id: connection('microsoft').id,
      }),
    )
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
    await wrapper.get('button[name="submit-draft"]').trigger('click')
    await flushPromises()
    expect(mail.submitMailDraft).toHaveBeenCalledWith(
      DRAFT_ID,
      2,
      expect.objectContaining({ key: expect.any(String) }),
    )
    expect(router.currentRoute.value.query.task).toBe(TASK_ID)
  })

  it.each([
    'invalid-address',
    Array.from({ length: 51 }, (_, i) => `p${i}@example.test`).join(','),
  ])(
    'rejects invalid or excessive recipients before saving',
    async (recipients) => {
      const { wrapper } = await renderPage()
      await wrapper.get('input[aria-label="收件人 To"]').setValue(recipients)
      await wrapper.get('button[name="save-draft"]').trigger('click')
      expect(mail.updateMailDraft).not.toHaveBeenCalled()
      expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    },
  )

  it('requires reload after a version conflict and does not replay a stale save', async () => {
    vi.mocked(mail.updateMailDraft).mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        detail: '',
        status: 409,
        instance: '',
        error_code: 'draft_version_conflict',
        trace_id: 'synthetic-version-trace',
      }),
    )
    const { wrapper } = await renderPage()
    await wrapper
      .get('input[aria-label="主题"]')
      .setValue('Revised synthetic heading')
    await wrapper.get('button[name="save-draft"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('synthetic-version-trace')
    current = { ...current, version: 5 }
    await wrapper.get('button[name="reload-editor"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('版本 5')
    expect(mail.updateMailDraft).toHaveBeenCalledTimes(1)
  })

  it('shows generation pending and failure from the task snapshot without treating model text as sent', async () => {
    const { wrapper, tasks } = await renderPage()
    await wrapper
      .get('textarea[aria-label="草拟要求"]')
      .setValue('Write a short synthetic reply')
    await wrapper.get('button[name="generate-draft"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('正在草拟')
    tasks.setTask({
      id: TASK_ID,
      kind: 'mail_draft.generate',
      status: 'failed',
      retry_of_task_id: null,
      error_code: 'model_unavailable',
      event_cursor: '3',
      steps: [],
    })
    await flushPromises()
    expect(wrapper.text()).toContain('草拟失败')
    expect(
      wrapper.get('textarea[aria-label="纯文本正文"]').element,
    ).toHaveProperty('value', current.body_text)
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
  })

  it.each(['failed', 'cancelled'] as const)(
    'uses a new generation intent only after an explicit retry of a known %s task',
    async (status) => {
      vi.mocked(mail.generateMailDraft)
        .mockResolvedValueOnce({ task_id: TASK_ID, status: 'queued' })
        .mockResolvedValueOnce({
          task_id: '00000000-0000-0000-0000-000000000399',
          status: 'queued',
        })
      const { wrapper, tasks } = await renderPage()
      await wrapper
        .get('textarea[aria-label="草拟要求"]')
        .setValue('Write a synthetic reply')
      await wrapper.get('button[name="generate-draft"]').trigger('click')
      await flushPromises()
      const firstIntent = vi.mocked(mail.generateMailDraft).mock.calls[0]?.[2]
      tasks.setTask({
        id: TASK_ID,
        kind: 'mail_draft.generate',
        status,
        retry_of_task_id: null,
        error_code: 'model_unavailable',
        event_cursor: '3',
        steps: [],
      })
      await flushPromises()
      expect(mail.generateMailDraft).toHaveBeenCalledTimes(1)
      await wrapper.get('button[name="generate-draft"]').trigger('click')
      await flushPromises()
      expect(mail.generateMailDraft).toHaveBeenCalledTimes(2)
      expect(vi.mocked(mail.generateMailDraft).mock.calls[1]?.[2].key).not.toBe(
        firstIntent?.key,
      )
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
    },
  )

  it('reuses the generation intent after a transport failure with an unknown task result', async () => {
    vi.mocked(mail.generateMailDraft).mockRejectedValueOnce(
      new Error('Synthetic transport unavailable'),
    )
    const { wrapper } = await renderPage()
    await wrapper
      .get('textarea[aria-label="草拟要求"]')
      .setValue('Write a synthetic reply')
    await wrapper.get('button[name="generate-draft"]').trigger('click')
    await flushPromises()
    const firstIntent = vi.mocked(mail.generateMailDraft).mock.calls[0]?.[2]
    expect(mail.generateMailDraft).toHaveBeenCalledTimes(1)
    await wrapper.get('button[name="generate-draft"]').trigger('click')
    await flushPromises()
    expect(mail.generateMailDraft).toHaveBeenCalledTimes(2)
    expect(vi.mocked(mail.generateMailDraft).mock.calls[1]?.[2].key).toBe(
      firstIntent?.key,
    )
  })

  it('releases the previous draft pending state on navigation and ignores its late save', async () => {
    let finishSave: (value: MailDraft) => void = () => {
      throw new Error('Synthetic save was not started')
    }
    vi.mocked(mail.updateMailDraft).mockImplementationOnce(
      () =>
        new Promise<MailDraft>((resolve) => {
          finishSave = resolve
        }),
    )
    const { wrapper, router } = await renderPage()
    await wrapper
      .get('input[aria-label="主题"]')
      .setValue('Synthetic pending edit')
    await wrapper.get('button[name="save-draft"]').trigger('click')
    await flushPromises()
    const oldDraft = { ...current, version: 2 }
    current = {
      ...current,
      id: '00000000-0000-0000-0000-000000000399',
      version: 5,
    }
    await router.push(`/mail/drafts/${current.id}`)
    await flushPromises()
    expect(mail.getMailDraft).toHaveBeenLastCalledWith(current.id)
    expect(
      wrapper.get('textarea[aria-label="纯文本正文"]').attributes('disabled'),
    ).toBeUndefined()
    expect(wrapper.text()).toContain('版本 5')
    finishSave(oldDraft)
    await flushPromises()
    expect(wrapper.text()).toContain('版本 5')
    expect(
      wrapper.get('textarea[aria-label="纯文本正文"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('withdraws the known frozen task through cancelTask before reloading the editable draft', async () => {
    current = { ...current, status: 'awaiting_approval' }
    vi.mocked(cancelTask).mockImplementation(async () => {
      current = { ...current, status: 'editing', version: 2 }
      return {
        id: TASK_ID,
        kind: 'trusted_action',
        status: 'cancelled',
        retry_of_task_id: null,
        error_code: null,
        event_cursor: '4',
        steps: [],
      }
    })
    const { wrapper } = await renderPage(`?task=${TASK_ID}`)
    expect(
      wrapper.get('textarea[aria-label="纯文本正文"]').attributes('disabled'),
    ).toBeDefined()
    await wrapper.get('button[name="withdraw-action"]').trigger('click')
    await flushPromises()
    expect(cancelTask).toHaveBeenCalledWith(TASK_ID)
    expect(
      wrapper.get('textarea[aria-label="纯文本正文"]').attributes('disabled'),
    ).toBeUndefined()
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
  })

  it('explains unavailable historical binding and only creates an empty replacement after an explicit click', async () => {
    vi.mocked(mail.updateMailDraft).mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        detail: '',
        status: 409,
        instance: '',
        error_code: 'historical_action_binding_unavailable',
        trace_id: 'synthetic-history-trace',
      }),
    )
    vi.mocked(mail.createMailDraft).mockResolvedValue({
      ...mailDraft(),
      id: '00000000-0000-0000-0000-000000000399',
    })
    const { wrapper } = await renderPage()
    await wrapper
      .get('select[aria-label="发送账户"]')
      .setValue(connection('microsoft').id)
    await wrapper.get('button[name="save-draft"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('历史操作的账户归属无法核实')
    expect(mail.createMailDraft).not.toHaveBeenCalled()
    await wrapper.get('button[name="new-local-object"]').trigger('click')
    await flushPromises()
    expect(mail.createMailDraft).toHaveBeenCalledWith(
      { mode: 'new' },
      expect.objectContaining({ key: expect.any(String) }),
    )
  })

  it('explains a partially unavailable account catalogue without changing the current account', async () => {
    vi.mocked(connections.getConnectionCapabilities).mockImplementation(
      async (id) => {
        if (id !== CONNECTION_ID)
          throw new ProblemError({
            type: 'about:blank',
            title: 'Synthetic unavailable',
            detail: '',
            instance: '',
            status: 503,
            error_code: 'service_unavailable',
            trace_id: 'synthetic-catalog-trace',
          })
        return connectionCapabilities()
      },
    )
    const { wrapper } = await renderPage()
    expect(wrapper.text()).toContain('部分账户能力读取失败')
    expect(wrapper.text()).toContain('synthetic-catalog-trace')
    expect(wrapper.get('select[aria-label="发送账户"]').element).toHaveProperty(
      'value',
      CONNECTION_ID,
    )
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
  })
})
