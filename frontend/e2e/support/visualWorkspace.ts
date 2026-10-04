import { expect, type Page } from '@playwright/test'
import type { Brief, CalendarProposal } from '../../src/api/types'
import {
  actionSnapshot,
  calendarProposal,
  DRAFT_ID,
  mailDraft,
  NOW,
  PROPOSAL_ID,
  TASK_ID,
} from '../../src/test-support/actionFixtures'
import {
  calendarFields,
  mailApproval,
} from '../../src/test-support/editorFixtures'
import {
  editorAction,
  editorJson,
  editorProblem,
  editorWorkspace,
} from '../fixtures/editorApi'

/** 七个最低验收视图；每个浏览器上下文都使用相同合成事实，布局项目只改变视口。 */
export const visualViews = [
  'login',
  'brief',
  'actions',
  'mail',
  'calendar',
  'approval',
  'needs_attention',
] as const
export type VisualView = (typeof visualViews)[number]

/**
 * 安装既有 API fixture 并进入真实路由；数据只通过 API 边界进入真实 Store／组件。
 * @param page 独立浏览器页面。@param view 待验收视图。
 * @returns 未声明请求、页面异常和真实写请求的内存记录；扫描／截图不得产生业务副作用。
 */
export async function openVisualView(page: Page, view: VisualView) {
  const capture = await editorWorkspace(page)
  await page.clock.setSystemTime(new Date(NOW))
  let path = '/actions'
  if (view === 'login') {
    await page.route('**/api/v1/auth/me', (route) =>
      editorProblem(route, 'invalid_credentials', 401),
    )
    path = '/login'
  } else if (view === 'brief') {
    const brief: Brief = {
      id: 'synthetic-brief',
      local_date: '2030-01-01',
      version: 1,
      task_id: TASK_ID,
      source_cutoff: NOW,
      completeness: 'partial',
      headline: '合成每日简报',
      structured_content: {},
      markdown: '今日会议和邮件均来自合成测试数据。',
      warnings: ['部分来源尚未同步'],
      items: [],
    }
    await page.route('**/api/v1/briefs/today', (route) =>
      editorJson(route, brief),
    )
    await page.route('**/api/v1/briefs?*', (route) =>
      editorJson(route, [brief]),
    )
    path = '/brief'
  } else if (view === 'mail') {
    await page.route(`**/api/v1/mail/drafts/${DRAFT_ID}`, (route) =>
      editorJson(route, {
        ...mailDraft(),
        to: ['recipient@mail.example.test'],
        subject: '合成邮件主题',
      }),
    )
    path = `/mail/drafts/${DRAFT_ID}`
  } else if (view === 'calendar') {
    const proposal: CalendarProposal = {
      ...calendarProposal(),
      ...calendarFields(),
      calendar_id: 'synthetic-google-calendar',
      notification_policy: 'none',
      required_confirmations: [],
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'not_applicable',
        before: null,
        conflict_status: 'incomplete',
        conflicts: null,
      },
    }
    await page.route(`**/api/v1/calendar/proposals/${PROPOSAL_ID}`, (route) =>
      editorJson(route, proposal),
    )
    path = `/calendar/proposals/${PROPOSAL_ID}`
  } else if (view === 'approval' || view === 'needs_attention') {
    const snapshot =
      view === 'approval'
        ? actionSnapshot({
            status: 'waiting_approval',
            error_code: null,
            approval: mailApproval(),
            execution: null,
          })
        : actionSnapshot()
    await editorAction(page, () => snapshot)
    path = `/actions?task=${TASK_ID}`
  }
  await page.goto(path)
  const names: Record<VisualView, string> = {
    login: '登录 AI Employee',
    brief: '今日简报',
    actions: '操作中心',
    mail: '邮件草稿',
    calendar: '日程提案',
    approval: '操作中心',
    needs_attention: '操作中心',
  }
  await expect(
    page.getByRole('heading', { name: names[view], level: 1, exact: true }),
  ).toBeVisible()
  if (view === 'brief')
    await expect(page.getByText('合成每日简报', { exact: true })).toBeVisible()
  if (view === 'mail')
    await expect(page.getByLabel('主题', { exact: true })).toHaveValue(
      '合成邮件主题',
    )
  if (view === 'calendar')
    await expect(page.getByLabel('日程标题', { exact: true })).toHaveValue(
      'Synthetic meeting',
    )
  if (view === 'approval')
    await expect(
      page.getByRole('button', { name: '批准', exact: true }),
    ).toBeVisible()
  if (view === 'needs_attention')
    await expect(
      page.getByRole('button', { name: '确认未执行', exact: true }),
    ).toBeVisible()
  return capture
}
