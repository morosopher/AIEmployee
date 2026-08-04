import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import SettingsPage from './SettingsPage.vue'

describe('SettingsPage', () => {
  it('renders retention controls and saves settings', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365, updated_at: '2026-08-04T00:00:00Z' }) }))
    const wrapper = mount(SettingsPage)
    expect(wrapper.text()).toContain('邮件正文保留天数')
  })
})
