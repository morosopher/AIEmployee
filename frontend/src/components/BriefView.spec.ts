import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import BriefView from './BriefView.vue'

describe('BriefView', () => {
  it('renders completeness, cutoff, warnings and source links', () => {
    const wrapper = mount(BriefView, {
      props: {
        brief: {
          id: 'b1', local_date: '2026-08-04', version: 2, task_id: 't1',
          source_cutoff: '2026-08-04T08:00:00Z', completeness: 'partial',
          headline: '今日简报', structured_content: {}, markdown: '# 标题',
          warnings: ['Calendar unavailable'], items: [{ position: 1, section: '邮件', priority: 'high', title: '跟进', body_markdown: '内容', source_refs: [{ source_type: 'gmail', source_id: 'm1', provider_url: 'https://mail.google.com' }], suggested_action_kind: null }],
        },
      },
    })
    expect(wrapper.text()).toContain('partial')
    expect(wrapper.text()).toContain('Calendar unavailable')
    expect(wrapper.text()).toContain('2026-08-04T08:00:00Z')
    expect(wrapper.find('a').attributes('href')).toContain('mail.google.com')
  })
})
