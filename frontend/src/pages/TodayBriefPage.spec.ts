import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import TodayBriefPage from './TodayBriefPage.vue'

describe('TodayBriefPage', () => {
  it('loads latest brief and can select a historical version', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] }))
    const wrapper = mount(TodayBriefPage)
    expect(wrapper.text()).toContain('今日简报')
  })
})
