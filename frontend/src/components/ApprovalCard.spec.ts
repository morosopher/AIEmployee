import { flushPromises, mount } from '@vue/test-utils'
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

  it('renders a mail approval as frozen fields and literal plain text', () => {
    const wrapper = mount(ApprovalCard, {
      props: { approval: mailApproval(), decide: vi.fn() },
    })
    expect(wrapper.get('[data-testid="mail-approval-subject"]').text()).toBe(
      'Synthetic subject',
    )
    expect(wrapper.text()).toContain('不可撤回')
    expect(wrapper.text()).toContain('全部回复')
    expect(wrapper.text()).toContain('bcc@mail.example.test')
    expect(wrapper.text()).toContain('**plain text**')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('pre').exists()).toBe(false)
    expect(wrapper.text()).toContain('高风险')
    expect(wrapper.text()).toContain('冻结版本 3')
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
    const wrapper = mount(ApprovalCard, {
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
    await wrapper.get('button[name="approved"]').trigger('click')
    await flushPromises()
    expect(
      wrapper.get('[data-testid="approval-error"] a').attributes('href'),
    ).toBe(`${path}?task=${task}&recovery=new_version`)
    expect(decide).toHaveBeenCalledTimes(1)
    expect(
      wrapper.get('button[name="approved"]').attributes('disabled'),
    ).toBeDefined()
    wrapper.unmount()
  })

  it('shows calendar before/after, exact zone, ETag, notifications and source warnings', () => {
    const wrapper = mount(ApprovalCard, {
      props: { approval: calendarApproval(), decide: vi.fn() },
      global: {
        stubs: {
          RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
        },
      },
    })
    expect(wrapper.get('[aria-label="日程前后对比"]').text()).toContain(
      'Synthetic room',
    )
    expect(wrapper.text()).toContain('Asia/Shanghai')
    expect(wrapper.text()).toContain('synthetic-etag-1')
    expect(wrapper.text()).toContain('不发送通知')
    expect(wrapper.text()).toContain('外部同步')
    expect(wrapper.text()).toContain('工作时间外')
    expect(wrapper.text()).toContain('未检查参会人可用性')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('only submits one exact frozen decision while a request is in flight', async () => {
    const decide = vi.fn(() => new Promise<void>(() => undefined))
    const wrapper = mount(ApprovalCard, {
      props: { approval: mailApproval(), decide },
    })
    await wrapper.get('button[name="approved"]').trigger('click')
    await wrapper.get('button[name="rejected"]').trigger('click')
    expect(decide).toHaveBeenCalledTimes(1)
    expect(decide).toHaveBeenCalledWith(
      mailApproval().id,
      'approved',
      2,
      'a'.repeat(64),
    )
    expect(
      wrapper.get('button[name="rejected"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('expires an open card without relying on a page reload', async () => {
    const decide = vi.fn()
    const wrapper = mount(ApprovalCard, {
      props: { approval: mailApproval(), decide },
    })
    await vi.advanceTimersByTimeAsync(600_001)
    expect(
      wrapper.get('button[name="approved"]').attributes('disabled'),
    ).toBeDefined()
    expect(wrapper.text()).toContain('已过期')
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
      const wrapper = mount(ApprovalCard, {
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
      await wrapper.get('button[name="approved"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="approval-error"]').text()).toContain(
        label,
      )
      expect(wrapper.text()).toContain('synthetic-trace')
      expect(decide).toHaveBeenCalledTimes(1)
      expect(
        wrapper.get('button[name="approved"]').attributes('disabled'),
      ).toBeDefined()
    },
  )

  it('keeps redacted history readable and never permits approval', () => {
    const wrapper = mount(ApprovalCard, {
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
    expect(wrapper.text()).toContain('内容已到期')
    expect(
      wrapper.get('button[name="approved"]').attributes('disabled'),
    ).toBeDefined()
    expect(wrapper.find('[data-testid="mail-approval-subject"]').exists()).toBe(
      false,
    )
  })

  it('submits frozen version and hash and disables duplicate decisions', async () => {
    const decide = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(ApprovalCard, {
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
    await wrapper.get('button').trigger('click')
    expect(decide).toHaveBeenCalledWith('a1', 'approved', 2, 'a'.repeat(64))
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})
