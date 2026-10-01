/** 系统告警逐项保留迁移前 live region 语义与原文，不重复嵌套播报。 */
import { describe, expect, it } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import SystemAlertBanner from './SystemAlertBanner.vue'

describe('SystemAlertBanner', () => {
  it('preserves loading, failure and overdue messages independently', async () => {
    const view = await renderWithPlugins(SystemAlertBanner, {
      props: {
        loading: true,
        error: true,
        alerts: [
          {
            code: 'daily_brief_overdue',
            severity: 'critical',
            local_date: '2026-08-04',
            diagnostic_task_id: 'diag-1',
          },
        ],
      },
    })
    expect(view.getByRole('status')).toHaveTextContent('正在检查系统告警')
    expect(view.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(view.getAllByRole('alert')).toHaveLength(2)
    expect(view.getByText('系统告警暂时无法刷新。')).toBeVisible()
    expect(view.getByText('每日简报已逾期')).toBeVisible()
    expect(view.getByRole('link', { name: '查看诊断任务' })).toHaveAttribute(
      'href',
      '/tasks?task_id=diag-1',
    )
    await view.rerender({ loading: false, error: false, alerts: [] })
    expect(view.queryByRole('alert')).toBeNull()
    expect(view.queryByRole('status')).toBeNull()
  })
})
