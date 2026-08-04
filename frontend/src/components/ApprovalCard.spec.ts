import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ApprovalCard from './ApprovalCard.vue'

describe('ApprovalCard', () => {
  it('submits frozen version and hash and disables duplicate decisions', async () => {
    const decide = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(ApprovalCard, { props: { approval: { id: 'a1', tool: 'calendar.create', payload: { title: 'Demo' }, version: 2, payload_hash: 'a'.repeat(64), status: 'pending', expires_at: '2026-08-04T09:00:00Z' }, decide } })
    await wrapper.get('button').trigger('click')
    expect(decide).toHaveBeenCalledWith('a1', 'approved', 2, 'a'.repeat(64))
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})
