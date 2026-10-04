import { fireEvent, within } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProblemError } from '@/api/client'
import { calendarApproval, mailApproval } from '@/test-support/editorFixtures'
import ApprovalCard from './ApprovalCard.vue'

describe('ApprovalCard', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime('2030-01-01T00:00:00Z')
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders a mail approval as frozen fields and literal plain text', async () => {
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval: mailApproval(), decide: vi.fn() },
    })
    expect(wrapper.getByText('Synthetic subject').textContent).toBe(
      'Synthetic subject',
    )
    expect(wrapper.container.textContent).toContain('不可撤回')
    expect(wrapper.container.textContent).toContain('全部回复')
    expect(wrapper.container.textContent).toContain('bcc@mail.example.test')
    expect(wrapper.container.textContent).toContain('**plain text**')
    expect(Boolean(wrapper.container.querySelector('img'))).toBe(false)
    expect(Boolean(wrapper.container.querySelector('pre'))).toBe(false)
    expect(wrapper.container.textContent).toContain('高风险')
    expect(wrapper.container.textContent).toContain('冻结版本 3')
  })

  it('links the production calendar conflict to the explicit new-version recovery route', async () => {
    const decide = vi.fn().mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        detail: '',
        instance: '',
        error_code: 'calendar_event_version_conflict',
        trace_id: 'synthetic-calendar-trace',
      }),
    )
    const path = '/calendar/proposals/00000000-0000-0000-0000-000000000301'
    const task = '00000000-0000-0000-0000-000000000302'
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: {
        approval: calendarApproval(),
        decide,
        editorUrl: `${path}?task=${task}`,
      },
      global: {
        stubs: {
          RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
        },
      },
    })
    await fireEvent.click(wrapper.getByRole('button', { name: '批准' }))
    await Promise.resolve()
    expect(
      within(wrapper.getByRole('alert')).getByRole('link').getAttribute('href'),
    ).toBe(`${path}?task=${task}&recovery=new_version`)
    expect(decide).toHaveBeenCalledTimes(1)
    expect(
      wrapper.getByRole('button', { name: '批准' }).getAttribute('disabled'),
    ).not.toBeNull()
    wrapper.unmount()
  })

  it('shows calendar before/after, exact zone, ETag, notifications and source warnings', async () => {
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval: calendarApproval(), decide: vi.fn() },
      global: {
        stubs: {
          RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
        },
      },
    })
    expect(
      wrapper.getByRole('table', { name: '日程修改前后' }).textContent,
    ).toContain('Synthetic room')
    expect(wrapper.container.textContent).toContain('Asia/Shanghai')
    expect(wrapper.container.textContent).toContain('synthetic-etag-1')
    expect(wrapper.container.textContent).toContain('不发送通知')
    expect(wrapper.container.textContent).toContain('外部同步')
    expect(wrapper.container.textContent).toContain('工作时间外')
    expect(wrapper.container.textContent).toContain('未检查参会人可用性')
    expect(Boolean(wrapper.container.querySelector('pre'))).toBe(false)
  })

  it('only submits one exact frozen decision while a request is in flight', async () => {
    const decide = vi.fn(() => new Promise<void>(() => undefined))
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval: mailApproval(), decide },
    })
    await fireEvent.click(wrapper.getByRole('button', { name: '批准' }))
    await fireEvent.click(wrapper.getByRole('button', { name: '拒绝' }))
    expect(decide).toHaveBeenCalledTimes(1)
    expect(decide).toHaveBeenCalledWith(
      mailApproval().id,
      'approved',
      2,
      'a'.repeat(64),
    )
    expect(
      wrapper.getByRole('button', { name: '拒绝' }).getAttribute('disabled'),
    ).not.toBeNull()
  })

  it('expires an open card without relying on a page reload', async () => {
    const decide = vi.fn()
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval: mailApproval(), decide },
    })
    await vi.advanceTimersByTimeAsync(600_001)
    expect(
      wrapper.getByRole('button', { name: '批准' }).getAttribute('disabled'),
    ).not.toBeNull()
    expect(wrapper.container.textContent).toContain('已过期')
    wrapper.unmount()
  })

  it.each([
    ['draft_version_conflict', '重新加载'],
    ['connection_scope_missing', '重新授权'],
    ['approval_payload_hash_mismatch', '创建新版本'],
  ])(
    'offers a stable recovery for %s without retrying the decision',
    async (code, label) => {
      const decide = vi.fn().mockRejectedValue(
        new ProblemError({
          type: 'about:blank',
          title: 'Conflict',
          status: 409,
          detail: '',
          instance: '',
          error_code: code,
          trace_id: 'synthetic-trace',
        }),
      )
      const wrapper = await renderWithPlugins(ApprovalCard, {
        props: {
          approval: mailApproval(),
          decide,
          reload: vi.fn(),
          editorUrl: '/mail/drafts/00000000-0000-0000-0000-000000000301',
        },
        global: {
          stubs: {
            RouterLink: {
              props: ['to'],
              template: '<a :href="to"><slot /></a>',
            },
          },
        },
      })
      await fireEvent.click(wrapper.getByRole('button', { name: '批准' }))
      await Promise.resolve()
      expect(wrapper.getByRole('alert').textContent).toContain(label)
      expect(wrapper.container.textContent).toContain('synthetic-trace')
      expect(decide).toHaveBeenCalledTimes(1)
      expect(
        wrapper.getByRole('button', { name: '批准' }).getAttribute('disabled'),
      ).not.toBeNull()
    },
  )

  it('keeps redacted history readable and never permits approval', async () => {
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: {
        approval: {
          ...mailApproval(),
          content_status: 'redacted',
          preview: null,
          status: 'expired',
        },
        decide: vi.fn(),
      },
    })
    expect(wrapper.container.textContent).toContain('内容已到期')
    expect(
      wrapper.getByRole('button', { name: '批准' }).getAttribute('disabled'),
    ).not.toBeNull()
    expect(Boolean(wrapper.queryByText('Synthetic subject'))).toBe(false)
  })

  it('submits frozen version and hash and disables duplicate decisions', async () => {
    const decide = vi.fn().mockResolvedValue(undefined)
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: {
        approval: {
          id: 'a1',
          tool: 'calendar.create',
          payload: { title: 'Demo' },
          version: 2,
          payload_hash: 'a'.repeat(64),
          status: 'pending',
          expires_at: '2099-08-04T09:00:00Z',
        },
        decide,
      },
    })
    await fireEvent.click(wrapper.getByRole('button', { name: '批准' }))
    expect(decide).toHaveBeenCalledWith('a1', 'approved', 2, 'a'.repeat(64))
    expect(
      wrapper.getByRole('button', { name: '批准' }).getAttribute('disabled'),
    ).not.toBeNull()
  })

  /** 新展示断言在旧实现为红：真实字段表、风险文字及展示倒计时，均不改变冻结请求。 */
  it('renders exact payload columns and a polite countdown with no duplicate static warnings', async () => {
    const approval = mailApproval()
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval, decide: vi.fn() },
    })
    const table = wrapper.getByRole('table', { name: '邮件冻结载荷' })
    expect(
      within(table).getByRole('columnheader', { name: '字段' }),
    ).toHaveAttribute('scope', 'col')
    const row = within(table).getByRole('row', { name: /纯文本正文/ })
    expect(within(row).getByRole('cell').textContent).toBe(
      approval.preview?.kind === 'mail' ? approval.preview.body_text : '',
    )
    expect(wrapper.getByText('高风险', { exact: true })).toBeVisible()
    const countdown = wrapper
      .getAllByRole('status')
      .find((node) => node.textContent?.includes('审批剩余'))
    expect(countdown).toHaveAttribute('aria-live', 'polite')
    expect(countdown).toHaveTextContent('10 分 0 秒')
    await vi.advanceTimersByTimeAsync(1000)
    expect(countdown).toHaveTextContent('9 分 59 秒')
    expect(wrapper.queryByRole('alert')).toBeNull()
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['approved', 'rejected'] as const)(
    'marks both decisions busy while %s is pending',
    async (decision) => {
      const decide = vi.fn(() => new Promise<void>(() => undefined))
      const wrapper = await renderWithPlugins(ApprovalCard, {
        props: { approval: mailApproval(), decide },
      })
      await fireEvent.click(
        wrapper.getByRole('button', {
          name: decision === 'approved' ? '批准' : '拒绝',
        }),
      )
      for (const label of ['批准', '拒绝']) {
        const button = wrapper.getByRole('button', { name: label })
        expect(button).toBeDisabled()
        expect(button).toHaveAttribute('aria-busy', 'true')
      }
      expect(
        wrapper
          .getAllByRole('status')
          .filter((node) => node.textContent === '正在记录决定…'),
      ).toHaveLength(1)
      expect(decide).toHaveBeenCalledWith(
        mailApproval().id,
        decision,
        2,
        'a'.repeat(64),
      )
    },
  )

  /** 展示 timer 不能覆盖原长时限保护、版本守卫和迟到响应隔离。 */
  it('keeps a long-lived approval enabled and ignores a decision error from its previous version', async () => {
    let rejectOld: (error: Error) => void = () => undefined
    const decide = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectOld = reject
        }),
    )
    const approval = { ...mailApproval(), expires_at: '2099-08-04T09:00:00Z' }
    const wrapper = await renderWithPlugins(ApprovalCard, {
      props: { approval, decide },
    })
    await vi.advanceTimersByTimeAsync(1001)
    expect(wrapper.getByRole('button', { name: '批准' })).toBeEnabled()
    await fireEvent.click(wrapper.getByRole('button', { name: '批准' }))
    await wrapper.rerender({
      approval: { ...approval, version: 3, payload_hash: 'b'.repeat(64) },
    })
    rejectOld(new Error('synthetic old failure'))
    await Promise.resolve()
    expect(wrapper.queryByRole('alert')).toBeNull()
    expect(wrapper.getByRole('button', { name: '批准' })).toBeEnabled()
    wrapper.unmount()
    // Message 的真实 Vue 过渡有两帧调度；完成这两帧后必须没有审批或显示 timer 遗留。
    vi.advanceTimersToNextFrame()
    vi.advanceTimersToNextFrame()
    expect(vi.getTimerCount()).toBe(0)
  })
})
