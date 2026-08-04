import { describe, expect, it, vi } from 'vitest'

import { getSystemAlerts } from './system'

describe('system api', () => {
  it('parses snake_case overdue alert fields', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ alerts: [{ code: 'daily_brief_overdue', severity: 'critical', local_date: '2026-08-04', diagnostic_task_id: 'diag-1' }] }),
    }))
    await expect(getSystemAlerts()).resolves.toEqual({
      alerts: [{ code: 'daily_brief_overdue', severity: 'critical', local_date: '2026-08-04', diagnostic_task_id: 'diag-1' }],
    })
  })
})
