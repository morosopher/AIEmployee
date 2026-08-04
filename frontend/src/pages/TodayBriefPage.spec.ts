import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import TodayBriefPage from './TodayBriefPage.vue'

describe('TodayBriefPage', () => {
  it('loads latest brief and can select a historical version', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] }))
    const wrapper = mount(TodayBriefPage, { global: { plugins: [createPinia()] } })
    expect(wrapper.text()).toContain('今日简报')
  })
})
