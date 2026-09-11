import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/connections'
import {
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'
import ConnectionsPage from './ConnectionsPage.vue'

vi.mock('@/api/connections', async (original) => ({
  ...(await original<typeof import('@/api/connections')>()),
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
  startGoogleConnection: vi.fn(),
  startMicrosoftConnection: vi.fn(),
  enableConnectionCapability: vi.fn(),
  disableConnectionCapability: vi.fn(),
  disconnectConnection: vi.fn(),
  syncConnection: vi.fn(),
}))

describe('ConnectionsPage', () => {
  beforeEach(() => {
    vi.mocked(api.listConnections).mockResolvedValue([
      connection(),
      connection('microsoft'),
    ])
    vi.mocked(api.getConnectionCapabilities).mockImplementation(async (id) =>
      connectionCapabilities(id === connection().id ? 'google' : 'microsoft'),
    )
    vi.mocked(api.startGoogleConnection).mockResolvedValue({
      authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
    })
    vi.mocked(api.startMicrosoftConnection).mockResolvedValue({
      authorization_url:
        'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    })
  })
  afterEach(() => vi.clearAllMocks())

  it('starts each provider only after an explicit click and exposes its authorization link', async () => {
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    expect(api.startMicrosoftConnection).not.toHaveBeenCalled()
    await wrapper.get('button[name="connect-microsoft"]').trigger('click')
    await flushPromises()
    expect(api.startMicrosoftConnection).toHaveBeenCalledTimes(1)
    expect(
      wrapper.get('a[data-testid="authorization-link"]').attributes('href'),
    ).toContain('login.microsoftonline.com')
    await wrapper.get('button[name="connect-google"]').trigger('click')
    await flushPromises()
    expect(api.startGoogleConnection).toHaveBeenCalledTimes(1)
    expect(
      wrapper.get('a[data-testid="authorization-link"]').attributes('href'),
    ).toContain('accounts.google.com')
  })

  it('renders four capability rows per account and prevents disabling an active read dependency', async () => {
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    expect(wrapper.findAll('[data-capability]')).toHaveLength(8)
    const card = wrapper.get(`[data-connection-id="${connection().id}"]`)
    expect(
      card.get('button[name="disable-mail.read"]').attributes('disabled'),
    ).toBeDefined()
    expect(card.text()).toContain('先关闭 mail.send')
    expect(card.text()).toContain('synthetic-scope')
    expect(
      card.get('button[name="disable-mail.send"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('explains Microsoft administrator consent and reauthorizes the exact capability', async () => {
    const capabilities = connectionCapabilities('microsoft')
    capabilities.capabilities = capabilities.capabilities.map((row) =>
      row.capability === 'mail.send'
        ? {
            ...row,
            status: 'action_required',
            last_error_code: 'microsoft_admin_consent_required',
          }
        : row,
    )
    vi.mocked(api.getConnectionCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.listConnections).mockResolvedValue([connection('microsoft')])
    vi.mocked(api.enableConnectionCapability).mockResolvedValue({
      authorization_url:
        'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
      requested_capabilities: ['mail.read', 'mail.send'],
    })
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    expect(wrapper.text()).toContain('管理员同意')
    expect(wrapper.text()).toContain('需要操作')
    await wrapper.get('button[name="enable-mail.send"]').trigger('click')
    await flushPromises()
    expect(api.enableConnectionCapability).toHaveBeenCalledWith(
      connection('microsoft').id,
      'mail.send',
      'microsoft',
    )
    expect(wrapper.text()).toContain('mail.read')
  })

  it('keeps successful accounts visible when one capability request fails and supports refresh', async () => {
    vi.mocked(api.getConnectionCapabilities).mockImplementation(async (id) => {
      if (id === connection('microsoft').id) throw new Error('unavailable')
      return connectionCapabilities()
    })
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    expect(wrapper.findAll('[data-capability]')).toHaveLength(4)
    expect(wrapper.text()).toContain('能力加载失败')
    expect(wrapper.find('button[name="refresh-connections"]').exists()).toBe(
      true,
    )
  })
})
