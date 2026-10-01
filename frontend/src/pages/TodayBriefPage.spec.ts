import { fireEvent, waitFor } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { afterEach, describe, expect, it, vi } from 'vitest'
import TodayBriefPage from './TodayBriefPage.vue'
import {
  calendarProposal,
  mailDraft,
  DRAFT_ID,
  PROPOSAL_ID,
} from '@/test-support/actionFixtures'

afterEach(() => {
  vi.unstubAllGlobals()
})

/** 真实客户端与内存路由验证来源绑定请求；全部查询依赖用户可访问的名称。 */
async function renderPage() {
  return renderWithPlugins(TodayBriefPage, { route: '/brief' })
}

describe('TodayBriefPage', () => {
  it('loads latest brief and can select a historical version', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => [] }),
    )
    const { getByRole } = await renderPage()
    expect(getByRole('heading', { name: '今日简报' })).toBeVisible()
  })
  it('keeps loading text alongside decorative skeletons', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )
    const page = await renderPage()
    expect(page.getByRole('status')).toHaveTextContent('正在加载…')
    expect(page.getByTestId('brief-loading-skeleton')).toHaveAttribute(
      'aria-hidden',
      'true',
    )
  })

  it('shows an honest empty state and disables generation while its request is pending', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === 'POST') return new Promise<Response>(() => {})
        return new Response('{}', { status: 404 })
      }),
    )
    const page = await renderPage()
    expect(
      await page.findByRole('heading', { name: '暂无可展示的简报' }),
    ).toBeVisible()
    expect(page.getByRole('alert')).toHaveTextContent(
      '暂无今日简报或加载失败。',
    )
    const generate = page.getByRole('button', { name: '生成简报' })
    await fireEvent.click(generate)
    expect(generate).toBeDisabled()
    expect(generate).toHaveAttribute('aria-busy', 'true')
  })

  it('selects historical content and returns to the latest without creating a task', async () => {
    const latest = {
      id: 'b2',
      local_date: '2030-01-01',
      version: 2,
      task_id: 't2',
      source_cutoff: '2030-01-01T00:00:00Z',
      completeness: 'complete',
      headline: '最新合成内容',
      structured_content: {},
      markdown: '',
      warnings: [],
      items: [],
    }
    const historical = {
      ...latest,
      id: 'b1',
      version: 1,
      headline: '历史合成内容',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url.includes('/briefs?')
                ? [latest, historical]
                : url.endsWith('/b1')
                  ? historical
                  : latest,
            ),
          ),
      ),
    )
    const page = await renderPage()
    await page.findByRole('heading', { name: '最新合成内容' })
    await fireEvent.click(page.getByRole('combobox', { name: '历史版本' }))
    // 下拉展开新增的内置 status 只播报选中数量，不重复页面加载或告警。
    expect(await page.findByRole('status')).toHaveTextContent('已选择 1 项')
    // PrimeVue Select 在指针按下时选择；显式派发此用户事件，click 本身不会产生 mousedown。
    await fireEvent.mouseDown(
      await page.findByRole('option', { name: '版本 1' }),
    )
    expect(
      await page.findByRole('heading', { name: '历史合成内容' }),
    ).toBeVisible()
    expect(page.getByText('正在查看历史版本')).toBeVisible()
    await fireEvent.click(page.getByRole('button', { name: '返回最新' }))
    expect(
      await page.findByRole('heading', { name: '最新合成内容' }),
    ).toBeVisible()
    expect(page.getByRole('button', { name: '生成新版本' })).toBeEnabled()
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
      const { findByRole, router } = await renderPage()
      const action = await findByRole('button', {
        name: kind === 'mail.reply' ? '创建回复草稿' : '创建修改提案',
      })
      expect(
        fetch.mock.calls.filter(([, init]) => init?.method === 'POST'),
      ).toHaveLength(0)
      await fireEvent.click(action)
      await waitFor(() =>
        expect(router.currentRoute.value.path).not.toBe('/brief'),
      )
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
