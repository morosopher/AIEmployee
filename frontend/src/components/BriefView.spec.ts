/** 通过可见动作名称、来源标签与正文查询简报；不依赖链接或按钮的渲染顺序。 */
import { fireEvent } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { describe, expect, it, vi } from 'vitest'
import BriefView from './BriefView.vue'
import type { Brief } from '@/api/types'

describe('BriefView', () => {
  it.each([
    ['mail.reply', 'email_thread'],
    ['calendar.update', 'calendar_event'],
  ])(
    'keeps every %s source explicit and creates nothing on render',
    async (kind, sourceType) => {
      const sources = [1, 2].map((id) => ({
        source_type: sourceType ?? '',
        source_id: `00000000-0000-0000-0000-00000000000${id}`,
        provider_url: null,
      }))
      const brief: Brief = {
        id: 'brief',
        task_id: 'task',
        local_date: '2030-01-01',
        version: 1,
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
            source_refs: sources,
            suggested_action_kind: kind ?? null,
          },
        ],
      }
      const propose = vi.fn()
      const { getAllByRole, getByRole } = await renderWithPlugins(BriefView, {
        props: { brief, propose },
      })
      expect(propose).not.toHaveBeenCalled()
      const actionLabel =
        kind === 'mail.reply' ? '创建回复草稿' : '创建修改提案'
      expect(
        getAllByRole('button', {
          name: new RegExp(`^${actionLabel}（来源 [12]）$`),
        }),
      ).toHaveLength(2)
      await fireEvent.click(
        getByRole('button', { name: `${actionLabel}（来源 2）` }),
      )
      expect(propose).toHaveBeenCalledWith(kind, sources[1])
    },
  )

  it('does not make unknown actions or nonlocal source IDs executable', async () => {
    const brief: Brief = {
      id: 'brief',
      task_id: 'task',
      local_date: '2030-01-01',
      version: 1,
      source_cutoff: '2030-01-01T00:00:00Z',
      completeness: 'complete',
      headline: '',
      structured_content: {},
      markdown: '',
      warnings: [],
      items: [
        {
          position: 1,
          section: '',
          priority: '',
          title: '',
          body_markdown: '',
          source_refs: [
            {
              source_type: 'email_thread',
              source_id: 'provider-thread-not-a-local-uuid',
              provider_url: null,
            },
          ],
          suggested_action_kind: 'mail.reply',
        },
      ],
    }
    const { queryByRole } = await renderWithPlugins(BriefView, {
      props: { brief, propose: vi.fn() },
    })
    expect(queryByRole('button', { name: '创建回复草稿' })).toBeNull()
  })
  it('renders completeness, cutoff, warnings and source links', async () => {
    const { getByText, getByRole } = await renderWithPlugins(BriefView, {
      props: {
        brief: {
          id: 'b1',
          local_date: '2026-08-04',
          version: 2,
          task_id: 't1',
          source_cutoff: '2026-08-04T08:00:00Z',
          completeness: 'partial',
          headline: '今日简报',
          structured_content: {},
          markdown: '# 标题',
          warnings: ['Calendar unavailable'],
          items: [
            {
              position: 1,
              section: '邮件',
              priority: 'high',
              title: '跟进',
              body_markdown: '内容',
              source_refs: [
                {
                  source_type: 'gmail',
                  source_id: 'm1',
                  provider_url: 'https://mail.google.com',
                },
              ],
              suggested_action_kind: null,
            },
          ],
        },
      },
    })
    expect(getByText(/完整性：partial/)).toBeVisible()
    expect(getByText('Calendar unavailable')).toBeVisible()
    expect(getByText(/2026-08-04T08:00:00Z/)).toBeVisible()
    expect(
      getByRole('link', { name: 'gmail 来源' }).getAttribute('href'),
    ).toContain('mail.google.com')
  })
})
