import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/connections'
import CapabilityRows from '@/components/CapabilityRows.vue'
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

  it.each(['google', 'microsoft'] as const)(
    'allows explicit reauthorization of an enabled capability on the existing %s account',
    async (provider) => {
      // 已启用的 scope 仍可能存在未知续期；真实页面必须保留原连接恢复入口。
      const account = connection(provider)
      const before = connectionCapabilities(provider)
      const after = connectionCapabilities(provider)
      after.capabilities = after.capabilities.map((row) => ({
        ...row,
        status: 'authorizing',
      }))
      const authorizationUrl =
        provider === 'google'
          ? 'https://accounts.google.com/o/oauth2/v2/auth'
          : 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
      vi.mocked(api.listConnections).mockResolvedValue([account])
      vi.mocked(api.getConnectionCapabilities)
        .mockResolvedValueOnce(before)
        .mockResolvedValue(after)
      vi.mocked(api.enableConnectionCapability).mockResolvedValue({
        authorization_url: authorizationUrl,
        requested_capabilities: [
          'mail.read',
          'mail.send',
          'calendar.read',
          'calendar.write',
        ],
      })
      const target = document.createElement('div')
      document.body.append(target)
      const wrapper = mount(ConnectionsPage, { attachTo: target })
      try {
        await flushPromises()
        const button = wrapper.find('button[name="enable-mail.read"]')
        expect(button.exists()).toBe(true)
        expect(button.attributes('disabled')).toBeUndefined()
        expect(button.text()).toContain('重新授权')
        expect(
          wrapper.find('[data-testid="authorization-link"]').exists(),
        ).toBe(false)
        expect(api.enableConnectionCapability).not.toHaveBeenCalled()

        await button.trigger('click')
        await flushPromises()
        const link = wrapper.get('[data-testid="authorization-link"]')
        expect(link.attributes('href')).toBe(authorizationUrl)
        expect(document.activeElement).toBe(link.element)
        expect(wrapper.text()).toContain(
          '本次授权包含：mail.read、mail.send、calendar.read、calendar.write',
        )
        expect(
          wrapper.get('[data-capability="mail.read"] [role="status"]').text(),
        ).toBe('授权中')
        expect(api.enableConnectionCapability).toHaveBeenCalledExactlyOnceWith(
          account.id,
          'mail.read',
          provider,
        )
        expect(api.startGoogleConnection).not.toHaveBeenCalled()
        expect(api.startMicrosoftConnection).not.toHaveBeenCalled()
        expect(api.disableConnectionCapability).not.toHaveBeenCalled()
        expect(api.disconnectConnection).not.toHaveBeenCalled()
      } finally {
        wrapper.unmount()
        target.remove()
      }
    },
  )

  it.each([
    { state: 'busy', busy: true, disconnected: false },
    { state: 'disconnected', busy: false, disconnected: true },
  ])(
    'keeps enabled-capability reauthorization disabled when $state',
    async (state) => {
      // 新入口不能绕过既有互斥请求和断开状态，禁用按钮也不能发出 enable 事件。
      const wrapper = mount(CapabilityRows, {
        props: {
          capabilities: connectionCapabilities(),
          busy: state.busy,
          disconnected: state.disconnected,
        },
      })
      try {
        const button = wrapper.find('button[name="enable-mail.read"]')
        expect(button.exists()).toBe(true)
        expect(button.attributes('disabled')).toBeDefined()
        await button.trigger('click')
        expect(wrapper.emitted('enable')).toBeUndefined()
      } finally {
        wrapper.unmount()
      }
    },
  )

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

  it('can start a fresh authorization after reloading an authorizing capability', async () => {
    // URL 不持久化；页面重新挂载后必须允许用新 attempt 恢复未完成授权。
    const capabilities = connectionCapabilities()
    capabilities.capabilities = capabilities.capabilities.map((row) =>
      row.capability === 'mail.send' ? { ...row, status: 'authorizing' } : row,
    )
    vi.mocked(api.listConnections).mockResolvedValue([connection()])
    vi.mocked(api.getConnectionCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.enableConnectionCapability).mockResolvedValue({
      authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
      requested_capabilities: ['mail.read', 'mail.send'],
    })
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    const retry = wrapper.get('button[name="enable-mail.send"]')
    expect(retry.attributes('disabled')).toBeUndefined()
    await retry.trigger('click')
    await flushPromises()
    expect(
      wrapper.get('[data-testid="authorization-link"]').attributes('href'),
    ).toBe('https://accounts.google.com/o/oauth2/v2/auth')
    wrapper.unmount()
  })

  it('reads fresh capability status after starting authorization', async () => {
    // 独立前后快照捕捉“只生成 URL 却不更新状态”的缺陷，不由前端推算已授权。
    const before = connectionCapabilities()
    before.capabilities = before.capabilities.map((row) =>
      row.capability === 'mail.send' ? { ...row, status: 'disabled' } : row,
    )
    const after = connectionCapabilities()
    after.capabilities = after.capabilities.map((row) =>
      ['mail.read', 'mail.send'].includes(row.capability)
        ? { ...row, status: 'authorizing' }
        : row,
    )
    vi.mocked(api.listConnections).mockResolvedValue([connection()])
    vi.mocked(api.getConnectionCapabilities)
      .mockResolvedValueOnce(before)
      .mockResolvedValue(after)
    vi.mocked(api.enableConnectionCapability).mockResolvedValue({
      authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
      requested_capabilities: ['mail.read', 'mail.send'],
    })
    const wrapper = mount(ConnectionsPage)
    await flushPromises()
    await wrapper.get('button[name="enable-mail.send"]').trigger('click')
    await flushPromises()
    expect(
      wrapper.get('[data-capability="mail.send"] [role="status"]').text(),
    ).toBe('授权中')
    expect(
      wrapper.get('[data-capability="mail.read"] [role="status"]').text(),
    ).toBe('授权中')
    wrapper.unmount()
  })

  it('moves focus to the next authorization step after a capability click', async () => {
    // 实际 DOM 焦点证明操作反馈可达；链接不会仅出现在用户看不到的页面上方。
    const capabilities = connectionCapabilities()
    capabilities.capabilities = capabilities.capabilities.map((row) =>
      row.capability === 'mail.send' ? { ...row, status: 'disabled' } : row,
    )
    vi.mocked(api.listConnections).mockResolvedValue([connection()])
    vi.mocked(api.getConnectionCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.enableConnectionCapability).mockResolvedValue({
      authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
      requested_capabilities: ['mail.read', 'mail.send'],
    })
    const target = document.createElement('div')
    document.body.append(target)
    const wrapper = mount(ConnectionsPage, { attachTo: target })
    try {
      await flushPromises()
      await wrapper.get('button[name="enable-mail.send"]').trigger('click')
      await flushPromises()
      expect(document.activeElement).toBe(
        wrapper.get('[data-testid="authorization-link"]').element,
      )
    } finally {
      wrapper.unmount()
      target.remove()
    }
  })
})
