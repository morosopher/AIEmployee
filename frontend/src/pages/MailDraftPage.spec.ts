import { fireEvent, waitFor } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { defineComponent, h } from 'vue'
import { RouterView } from 'vue-router'
import ConfirmDialog from 'primevue/confirmdialog'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
beforeEach(() => {
  vi.clearAllMocks()
  current = {
    ...mailDraft(),
    to: ['recipient@example.test'],
    subject: 'Synthetic heading',
    body_text: 'Synthetic body',
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
/** 等待真实表单异步校验及 Vue 更新；HTTP 端口和 SSE 仍使用合成替身。 */
async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

/** 通过真实 RouterView 安装命名参数，保留版本恢复和导航卸载行为。 */
async function renderPage(query = '') {
  const view = await renderWithPlugins(
    defineComponent({
      render: () => h('div', [h(RouterView), h(ConfirmDialog)]),
    }),
  )
  view.router.addRoute({
    path: '/mail/drafts/:draftId',
    component: MailDraftPage,
  })
  view.router.addRoute({
    path: '/actions',
    component: { template: '<p>操作中心</p>' },
  })
  view.router.addRoute({
    path: '/connections',
    component: { template: '<p>连接</p>' },
  })
  await view.router.push(`/mail/drafts/${DRAFT_ID}${query}`)
  await view.findByLabelText('纯文本正文')
  await flushPromises()
  return { view, router: view.router, tasks: useTasksStore(view.pinia) }
}

/** 按可见标签编辑收件人：兼容迁移前文本框与迁移后的 chips，不跳过任何原始输入。 */
async function fillRecipient(
  view: Awaited<ReturnType<typeof renderPage>>['view'],
  label: string,
  value: string,
) {
  for (const button of view.queryAllByRole('button', {
    name: new RegExp(`^移除${label}中的`),
  })) {
    await fireEvent.click(button)
  }
  await fireEvent.update(view.getByLabelText(label), value)
}

/** 从可见 chips 和未确认输入还原展示内容，断言仍核对完整大小写与地址顺序。 */
function recipientValue(
  view: Awaited<ReturnType<typeof renderPage>>['view'],
  label: string,
): string {
  const chips = view
    .queryAllByRole('button', { name: new RegExp(`^移除${label}中的`) })
    .map(
      (button) =>
        button.getAttribute('aria-label')?.replace(`移除${label}中的`, '') ??
        '',
    )
  const pending = (view.getByLabelText(label) as HTMLInputElement).value
  return [...chips, ...(pending ? [pending] : [])].join(', ')
}

/** 原生及 PrimeVue Select 均只从公开选项选择账户，不依赖内部 DOM。 */
async function chooseAccount(
  view: Awaited<ReturnType<typeof renderPage>>['view'],
) {
  const select = view.getByRole('combobox', { name: '发送账户' })
  if (select instanceof HTMLSelectElement)
    await fireEvent.update(select, connection('microsoft').id)
  else {
    await fireEvent.click(select)
    await fireEvent.mouseDown(
      await view.findByRole('option', { name: /Microsoft/ }),
    )
  }
}

describe('MailDraftPage', () => {
  it('keeps a named active descendant for keyboard chip navigation and deletion without requests', async () => {
    const { view } = await renderPage()
    const input = view.getByRole('combobox', { name: '收件人 To' })
    input.focus()
    await fireEvent.keyDown(input, { key: 'ArrowLeft', code: 'ArrowLeft' })
    const group = view.getByRole('group', { name: '收件人 To已确认地址' })
    expect(group).toHaveFocus()
    const activeId = group.getAttribute('aria-activedescendant')
    expect(activeId).toBeTruthy()
    expect(document.getElementById(activeId ?? '')).toHaveAttribute(
      'aria-label',
      'recipient@example.test',
    )
    await fireEvent.keyDown(group, { key: 'Backspace', code: 'Backspace' })
    expect(input).toHaveFocus()
    expect(recipientValue(view, '收件人 To')).toBe('')
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
  })

  it('shows generation loading while the request is pending and blocks duplicate clicks', async () => {
    let release: (value: { task_id: string; status: 'queued' }) => void = () =>
      undefined
    vi.mocked(mail.generateMailDraft).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
    )
    const { view } = await renderPage()
    await fireEvent.click(view.getByText('使用模型草拟正文'))
    await fireEvent.update(view.getByLabelText('草拟要求'), 'Synthetic request')
    const button = view.getByRole('button', { name: '草拟正文' })
    await fireEvent.click(button)
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toBeDisabled()
    await fireEvent.click(button)
    expect(mail.generateMailDraft).toHaveBeenCalledTimes(1)
    release({ task_id: TASK_ID, status: 'queued' })
    await flushPromises()
  })

  it('saves all 255 Unicode subject characters without native input truncation', async () => {
    const { view } = await renderPage()
    const subject = '😀'.repeat(255)
    await fireEvent.update(view.getByLabelText('主题'), subject)
    expect(view.getByLabelText('主题')).not.toHaveAttribute('maxlength')
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await waitFor(() =>
      expect(mail.updateMailDraft).toHaveBeenCalledWith(
        DRAFT_ID,
        expect.objectContaining({ subject }),
      ),
    )
  })

  it('keeps keyboard-confirmed addresses and pending text in the exact saved payload', async () => {
    const { view } = await renderPage()
    const input = view.getByLabelText('抄送 CC')
    await fireEvent.update(input, 'One@MAIL.EXAMPLE.TEST；Two@example.test')
    await fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    expect(input).toHaveValue('')
    expect(recipientValue(view, '抄送 CC')).toBe(
      'One@MAIL.EXAMPLE.TEST, Two@example.test',
    )
    await fireEvent.update(input, 'Third@example.test')
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await waitFor(() =>
      expect(mail.updateMailDraft).toHaveBeenCalledWith(
        DRAFT_ID,
        expect.objectContaining({
          cc: [
            'One@MAIL.EXAMPLE.TEST',
            'Two@example.test',
            'Third@example.test',
          ],
        }),
      ),
    )
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
  })

  it('keeps one conflict announcement, mapped problem and exact unsaved input until explicit reload', async () => {
    vi.mocked(mail.updateMailDraft).mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'private title',
        detail: 'private detail',
        status: 409,
        instance: '',
        error_code: 'draft_version_conflict',
        trace_id: 'synthetic-conflict',
      }),
    )
    const { view } = await renderPage()
    await fireEvent.update(view.getByLabelText('主题'), 'Synthetic local edit')
    await fireEvent.update(
      view.getByLabelText('抄送 CC'),
      'Pending@EXAMPLE.TEST',
    )
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    const alert = await view.findByRole('alert')
    expect(alert).toHaveTextContent('邮件草稿版本已变化，请重新加载后核对。')
    expect(alert).toHaveTextContent('synthetic-conflict')
    expect(view.getAllByRole('alert')).toHaveLength(1)
    expect(view.getByLabelText('主题')).toHaveValue('Synthetic local edit')
    expect(view.getByLabelText('抄送 CC')).toHaveValue('Pending@EXAMPLE.TEST')
    expect(view.queryByText(/private/)).toBeNull()
    expect(mail.updateMailDraft).toHaveBeenCalledTimes(1)
    expect(view.getByRole('button', { name: '重新加载后核对' })).toBeEnabled()
  })

  it('cancels dirty navigation without requests, restores focus and allows explicit discard', async () => {
    const { view, router } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('主题'),
      'Unsaved synthetic input',
    )
    const link = view.getByRole('link', { name: '返回操作中心' })
    link.focus()
    await fireEvent.click(link)
    const dialog = await view.findByRole('alertdialog')
    await waitFor(() =>
      expect(view.getByRole('button', { name: '继续编辑' })).toHaveFocus(),
    )
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('alertdialog')).toBeNull())
    expect(router.currentRoute.value.path).toBe(`/mail/drafts/${DRAFT_ID}`)
    expect(view.getByLabelText('主题')).toHaveValue('Unsaved synthetic input')
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
    await fireEvent.click(link)
    await fireEvent.click(
      await view.findByRole('button', { name: '放弃修改并离开' }),
    )
    await waitFor(() => expect(router.currentRoute.value.path).toBe('/actions'))
  })

  it('registers a real form with three multiple recipient controls and suggestions limited to this draft', async () => {
    current = {
      ...current,
      cc: ['Current@example.test'],
      recipient_suggestions: ['historical@example.test'],
    }
    const { view } = await renderPage()
    expect(view.getByRole('form', { name: '邮件草稿表单' })).toBeVisible()
    for (const label of ['收件人 To', '抄送 CC', '密送 BCC']) {
      expect(view.getByRole('combobox', { name: label })).toBeVisible()
      expect(
        view.getByRole('group', { name: `${label}已确认地址` }),
      ).toBeVisible()
    }
    await fireEvent.update(view.getByLabelText('密送 BCC'), 'Current')
    const suggested = await view.findByRole('option', {
      name: 'Current@example.test',
      selected: false,
    })
    expect(
      view.queryByRole('option', { name: 'historical@example.test' }),
    ).toBeNull()
    await fireEvent.click(suggested)
    expect(recipientValue(view, '密送 BCC')).toBe('Current@example.test')
    expect(connections.listConnections).toHaveBeenCalledTimes(1)
    expect(mail.getMailDraft).toHaveBeenCalledTimes(1)
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
    expect(view.queryByRole('toolbar')).toBeNull()
    expect(view.container.querySelector('input[type=file]')).toBeNull()
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it.each([
    ['主题', '', '请填写主题。'],
    ['主题', '😀'.repeat(256), '主题最多 255 个字符。'],
    ['纯文本正文', '   ', '请填写纯文本正文。'],
  ])(
    'blocks invalid %s through registered Form state and keeps the full input',
    async (label, value, message) => {
      const { view } = await renderPage()
      await fireEvent.update(view.getByLabelText(label), value)
      await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
      expect(await view.findByText(message)).toBeVisible()
      expect(view.getByLabelText(label)).toHaveAttribute('aria-invalid', 'true')
      expect(view.getByLabelText(label)).toHaveValue(value)
      expect(mail.updateMailDraft).not.toHaveBeenCalled()
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
    },
  )

  it('preserves unconfirmed recipient text during chip removal and rejects it on save', async () => {
    const { view } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('收件人 To'),
      'synthetic-invalid',
    )
    await fireEvent.click(
      view.getByRole('button', {
        name: '移除收件人 To中的recipient@example.test',
      }),
    )
    expect(view.getByLabelText('收件人 To')).toHaveValue('synthetic-invalid')
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    expect(await view.findByRole('alert')).toHaveTextContent(
      '请填写有效的邮箱地址，多个地址用逗号分隔。',
    )
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
  })

  it('allows explicit body generation on an empty saved draft without submitting the Form', async () => {
    current = { ...current, to: [], subject: '', body_text: '' }
    const { view, tasks } = await renderPage()
    await fireEvent.click(view.getByText('使用模型草拟正文'))
    await fireEvent.update(
      view.getByLabelText('草拟要求'),
      'Write synthetic text',
    )
    await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
    await waitFor(() => expect(mail.generateMailDraft).toHaveBeenCalledTimes(1))
    expect(view.getByRole('button', { name: '草拟正文' })).toHaveAttribute(
      'aria-busy',
      'true',
    )
    expect(
      view
        .getAllByRole('status')
        .filter((node) => node.textContent?.includes('正在草拟')),
    ).toHaveLength(1)
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
    tasks.setTask({
      id: TASK_ID,
      kind: 'mail_draft.generate',
      status: 'failed',
      retry_of_task_id: null,
      error_code: 'model_unavailable',
      event_cursor: '3',
      steps: [],
    })
    expect(await view.findByRole('alert')).toHaveTextContent(
      '草拟失败，原草稿仍保留。请检查任务历史并重新加载后再试。',
    )
    expect(view.getAllByRole('alert')).toHaveLength(1)
  })

  it.each(['domain-case', 'local-case'] as const)(
    'preserves recipient input and counts mailbox identity by %s before saving',
    async (difference) => {
      const to = 'CaseUser@mail.example.test'
      const cc =
        difference === 'domain-case'
          ? 'CaseUser@MAIL.EXAMPLE.TEST'
          : 'caseUser@MAIL.EXAMPLE.TEST'
      current = { ...current, to: [to], cc: [], bcc: [] }
      const { view } = await renderPage()
      await fillRecipient(view, '抄送 CC', cc)
      const count = () =>
        view
          .getAllByRole('status')
          .find((node) => /收件人数/.test(node.textContent ?? ''))
          ?.textContent ?? ''
      if (difference === 'domain-case') {
        expect(count()).toContain('收件人数待核对')
        expect(count()).toContain('收件人地址重复')
      } else expect.soft(count()).toContain('当前收件人数：2 位')

      // 以真实保存入口证明校验边界；去重只归一域名，人数反馈不能改写用户原始地址。
      await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
      await flushPromises()
      await flushPromises()
      if (difference === 'domain-case') {
        expect(mail.updateMailDraft).not.toHaveBeenCalled()
        expect(view.getByRole('button', { name: '提交审批' })).toBeDisabled()
      } else {
        expect.soft(mail.updateMailDraft).toHaveBeenCalledWith(
          DRAFT_ID,
          expect.objectContaining({
            version: 1,
            to: [to],
            cc: [cc],
            bcc: [],
          }),
        )
        expect.soft(count()).toContain('当前收件人数：2 位')
        expect.soft(view.container.textContent).toContain('版本 2')
      }
      expect(recipientValue(view, '收件人 To')).toBe(to)
      expect(recipientValue(view, '抄送 CC')).toBe(cc)
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
    },
  )

  it('shows the current recipient count across To CC and BCC before submission and after edits', async () => {
    current = {
      ...current,
      to: ['one@mail.example.test'],
      cc: ['two@mail.example.test'],
      bcc: ['three@mail.example.test'],
    }
    const { view } = await renderPage()
    const count = () =>
      view
        .getAllByRole('status')
        .find((node) => /收件人数/.test(node.textContent ?? ''))?.textContent ??
      ''
    expect(count()).toContain('当前收件人数：3 位')
    await fillRecipient(
      view,
      '收件人 To',
      'one@mail.example.test, four@mail.example.test',
    )
    expect(count()).toContain('当前收件人数：4 位')
    await fillRecipient(view, '抄送 CC', '')
    expect(count()).toContain('当前收件人数：3 位')
    // 人数来自正在核对的输入，不等待保存，也不能因此提前创建审批。
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await flushPromises()
    await flushPromises()
    expect(count()).toContain('当前收件人数：3 位')
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
  })

  it.each(['duplicate', 'malformed'] as const)(
    'marks recipient count unavailable for %s input without weakening validation',
    async (kind) => {
      current = { ...current, to: ['one@mail.example.test'], cc: [], bcc: [] }
      const { view } = await renderPage()
      await fillRecipient(
        view,
        '抄送 CC',
        kind === 'duplicate' ? 'one@MAIL.EXAMPLE.TEST' : 'invalid-address',
      )
      await fillRecipient(view, '密送 BCC', 'two@mail.example.test')
      const count =
        view
          .getAllByRole('status')
          .find((node) => /收件人数/.test(node.textContent ?? ''))
          ?.textContent ?? ''
      expect(count).toContain('收件人数待核对')
      expect(count).not.toContain('当前收件人数：3 位')
      if (kind === 'duplicate') {
        expect(count).toContain('当前输入地址：3 项')
        expect(count).toContain('收件人地址重复')
      }
      await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
      await flushPromises()
      expect(mail.updateMailDraft).not.toHaveBeenCalled()
      expect(mail.submitMailDraft).not.toHaveBeenCalled()
      expect(view.getByRole('button', { name: '提交审批' })).toBeDisabled()
      await fillRecipient(view, '抄送 CC', 'three@mail.example.test')
      expect(
        view
          .getAllByRole('status')
          .find((node) => /收件人数/.test(node.textContent ?? ''))
          ?.textContent ?? '',
      ).toContain('当前收件人数：3 位')
    },
  )

  it.each(['reply', 'reply_all'] as const)(
    'locks the account, source and subject of %s while keeping ordinary text controls',
    async (mode) => {
      current = {
        ...current,
        mode,
        source_thread_id: 'synthetic-provider-thread',
        recipient_suggestions: ['suggested@example.test'],
      }
      const { view } = await renderPage()
      expect(view.getByRole('combobox', { name: '发送账户' })).toHaveAttribute(
        'aria-disabled',
        'true',
      )
      expect(view.getByLabelText('主题')).toBeDisabled()
      expect(view.container.textContent).toContain('synthetic-provider-thread')
      expect(view.getByLabelText('纯文本正文')).toBeVisible()
      expect(view.container.textContent).toContain('不可撤回')
      await fillRecipient(view, '抄送 CC', 'cc@example.test')
      await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
      await flushPromises()
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
    const { view, router } = await renderPage()
    await chooseAccount(view)
    expect(view.getByRole('button', { name: '提交审批' })).toBeDisabled()
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await flushPromises()
    await flushPromises()
    expect(mail.updateMailDraft).toHaveBeenCalledWith(
      DRAFT_ID,
      expect.objectContaining({
        version: 1,
        connection_id: connection('microsoft').id,
      }),
    )
    expect(mail.submitMailDraft).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '提交审批' }))
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
      const { view } = await renderPage()
      await fillRecipient(view, '收件人 To', recipients)
      await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
      await flushPromises()
      expect(mail.updateMailDraft).not.toHaveBeenCalled()
      await waitFor(() =>
        expect(view.getAllByRole('alert').length).toBeGreaterThan(0),
      )
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
    const { view } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('主题'),
      'Revised synthetic heading',
    )
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await flushPromises()
    await flushPromises()
    expect(view.container.textContent).toContain('synthetic-version-trace')
    current = { ...current, version: 5 }
    await fireEvent.click(view.getByRole('button', { name: '重新加载草稿' }))
    await flushPromises()
    expect(view.container.textContent).toContain('版本 5')
    expect(mail.updateMailDraft).toHaveBeenCalledTimes(1)
  })

  it('shows generation pending and failure from the task snapshot without treating model text as sent', async () => {
    const { view, tasks } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('草拟要求'),
      'Write a short synthetic reply',
    )
    await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
    await flushPromises()
    expect(view.container.textContent).toContain('正在草拟')
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
    expect(view.container.textContent).toContain('草拟失败')
    expect(view.getByLabelText('纯文本正文')).toHaveProperty(
      'value',
      current.body_text,
    )
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
      const { view, tasks } = await renderPage()
      await fireEvent.update(
        view.getByLabelText('草拟要求'),
        'Write a synthetic reply',
      )
      await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
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
      await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
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
    const { view } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('草拟要求'),
      'Write a synthetic reply',
    )
    await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
    await flushPromises()
    const firstIntent = vi.mocked(mail.generateMailDraft).mock.calls[0]?.[2]
    expect(mail.generateMailDraft).toHaveBeenCalledTimes(1)
    await fireEvent.click(view.getByRole('button', { name: '草拟正文' }))
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
    const { view, router } = await renderPage()
    await fireEvent.update(
      view.getByLabelText('主题'),
      'Synthetic pending edit',
    )
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await flushPromises()
    await flushPromises()
    const oldDraft = { ...current, version: 2 }
    current = {
      ...current,
      id: '00000000-0000-0000-0000-000000000399',
      version: 5,
    }
    const navigation = router.push(`/mail/drafts/${current.id}`)
    await fireEvent.click(
      await view.findByRole('button', { name: '放弃修改并离开' }),
    )
    await navigation
    await flushPromises()
    expect(mail.getMailDraft).toHaveBeenLastCalledWith(current.id)
    expect(view.getByLabelText('纯文本正文')).toBeEnabled()
    expect(view.container.textContent).toContain('版本 5')
    finishSave(oldDraft)
    await flushPromises()
    expect(view.container.textContent).toContain('版本 5')
    expect(view.getByLabelText('纯文本正文')).toBeEnabled()
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
    const { view } = await renderPage(`?task=${TASK_ID}`)
    expect(view.getByLabelText('纯文本正文')).toBeDisabled()
    await fireEvent.click(
      view.getByRole('button', { name: '撤回审批以继续编辑' }),
    )
    await flushPromises()
    expect(cancelTask).toHaveBeenCalledWith(TASK_ID)
    expect(view.getByLabelText('纯文本正文')).toBeEnabled()
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
    const { view } = await renderPage()
    await chooseAccount(view)
    await fireEvent.click(view.getByRole('button', { name: '保存草稿' }))
    await flushPromises()
    await flushPromises()
    expect(view.container.textContent).toContain('历史操作的账户归属无法核实')
    expect(mail.createMailDraft).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '新建空白对象' }))
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
    const { view } = await renderPage()
    expect(view.container.textContent).toContain('部分账户能力读取失败')
    expect(view.container.textContent).toContain('synthetic-catalog-trace')
    expect(view.getByRole('combobox', { name: '发送账户' })).toHaveTextContent(
      /Google/,
    )
    expect(mail.updateMailDraft).not.toHaveBeenCalled()
  })
})
