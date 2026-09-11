import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import TodayBriefPage from './TodayBriefPage.vue'
import {
  calendarProposal,
  mailDraft,
  DRAFT_ID,
  PROPOSAL_ID,
} from '@/test-support/actionFixtures'

let wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
  vi.unstubAllGlobals()
})

/** 真实客户端断言来源绑定请求，避免只测试组件回调却遗漏页面使用了旧任务接口。 */
async function renderPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/brief', component: TodayBriefPage },
      { path: '/mail/drafts/:draftId', component: { template: '<p>草稿</p>' } },
      {
        path: '/calendar/proposals/:proposalId',
        component: { template: '<p>提案</p>' },
      },
    ],
  })
  await router.push('/brief')
  const wrapper = mount(TodayBriefPage, {
    global: { plugins: [createPinia(), router] },
  })
  wrappers.push(wrapper)
  return { wrapper, router }
}

describe('TodayBriefPage', () => {
  it('loads latest brief and can select a historical version', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => [] }),
    )
    const { wrapper } = await renderPage()
    expect(wrapper.text()).toContain('今日简报')
  })
  it.each([
    ['mail.reply', 'email_thread'],
    ['calendar.update', 'calendar_event'],
  ])(
    'creates a source-bound %s editing object after an explicit click',
    async (kind, sourceType) => {
      const sourceId = '00000000-0000-0000-0000-000000000601'
      const brief = {
        id: 'brief',
        local_date: '2030-01-01',
        version: 1,
        task_id: 'brief-task',
        source_cutoff: '2030-01-01T00:00:00Z',
        completeness: 'complete',
        headline: '合成简报',
        structured_content: {},
        markdown: '',
        warnings: [],
        items: [
          {
            position: 1,
            section: '建议',
            priority: 'high',
            title: '合成建议',
            body_markdown: '',
            source_refs: [
              {
                source_type: sourceType,
                source_id: sourceId,
                provider_url: null,
              },
            ],
            suggested_action_kind: kind,
          },
        ],
      }
      const fetch = vi.fn(
        async (url: string, init?: RequestInit) =>
          new Response(
            JSON.stringify(
              init?.method === 'POST'
                ? kind === 'mail.reply'
                  ? mailDraft()
                  : calendarProposal()
                : url.includes('/briefs?')
                  ? [brief]
                  : brief,
            ),
          ),
      )
      vi.stubGlobal('fetch', fetch)
      const { wrapper, router } = await renderPage()
      await flushPromises()
      expect(
        fetch.mock.calls.filter(([, init]) => init?.method === 'POST'),
      ).toHaveLength(0)
      await wrapper.get('button[name="prepare-source-action"]').trigger('click')
      await flushPromises()
      const post = fetch.mock.calls.filter(
        ([, init]) => init?.method === 'POST',
      )
      expect(post).toHaveLength(1)
      expect(post[0]?.[0]).toBe(
        kind === 'mail.reply'
          ? '/api/v1/mail/drafts'
          : '/api/v1/calendar/proposals',
      )
      expect(JSON.parse(String(post[0]?.[1]?.body))).toEqual(
        kind === 'mail.reply'
          ? { mode: 'reply', source_thread_id: sourceId }
          : {
              operation_kind: 'update',
              initialization: 'shell',
              event_id: sourceId,
            },
      )
      expect(router.currentRoute.value.path).toBe(
        kind === 'mail.reply'
          ? `/mail/drafts/${DRAFT_ID}`
          : `/calendar/proposals/${PROPOSAL_ID}`,
      )
      expect(
        fetch.mock.calls.some(
          ([url]) => url.includes('/submit') || url.includes('/decision'),
        ),
      ).toBe(false)
    },
  )
})
