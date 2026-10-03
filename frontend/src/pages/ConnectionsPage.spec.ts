import { fireEvent, waitFor, within } from '@testing-library/vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import ConfirmDialog from 'primevue/confirmdialog'
import * as api from '@/api/connections'
import { ProblemError } from '@/api/client'
import type { ActionProvider, CapabilityName, Connection } from '@/api/types'
import CapabilityRows from '@/components/CapabilityRows.vue'
import {
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
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

/** 排空合成 API 与 Vue 焦点更新，保持既有断言不依赖计时等待。 */
async function flush(): Promise<void> {
  for (let index = 0; index < 20; index += 1) await Promise.resolve()
  await nextTick()
}

/** 将 Vue 测试容器收窄为真实 HTML，避免用类型断言掩盖 SVG 等未知根节点。 */
function queries(root: Element) {
  if (!(root instanceof HTMLElement))
    throw new TypeError('Expected an HTML test container')
  return within(root)
}

/** 通过页面自己的稳定身份标识选中账户，卡片内部仅使用角色和可见文案。 */
function account(root: Element, provider: ActionProvider = 'google') {
  return within(
    queries(root).getByTestId(`connection-${connection(provider).id}`),
  )
}

/** 自有标识只表达能力身份，不依赖 UI 库生成的 DOM 层级或 class。 */
function capability(root: Element, name: CapabilityName) {
  return within(queries(root).getByTestId(`capability-${name}`))
}

/** 授权链接固定命名为用户可见的下一步，保持链接 href 和实际焦点验证。 */
function authorization(root: Element) {
  return queries(root).getByRole('link', {
    name: /^继续 (Google|Microsoft) 授权$/,
  })
}

async function renderPage() {
  // 应用实际由 AppShell 提供唯一 ConfirmDialog；这里只补真实出口，不模拟服务行为。
  const host = defineComponent({
    setup: () => () => h('div', [h(ConnectionsPage), h(ConfirmDialog)]),
  })
  const view = await renderWithPlugins(host)
  await flush()
  return view
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'confirm').mockReturnValue(false)
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
afterEach(() => {
  vi.restoreAllMocks()
})

describe('ConnectionsPage', () => {
  /** 逐项核对旧 loading／empty 文案及礼貌播报，避免 Message 默认 assertive 提升播报级别。 */
  it('announces loading and empty results once with the original polite messages', async () => {
    let finish: (value: Connection[]) => void = () => {
      throw new Error('request not initialized')
    }
    vi.mocked(api.listConnections).mockReturnValue(
      new Promise<Connection[]>((resolve) => {
        finish = resolve
      }),
    )
    const view = await renderPage()
    expect(view.getAllByRole('status')).toHaveLength(1)
    expect(view.getByRole('status')).toHaveTextContent('正在加载连接能力…')
    expect(view.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(view.queryByRole('alert')).not.toBeInTheDocument()
    finish([])
    await flush()
    expect(view.getAllByRole('status')).toHaveLength(1)
    expect(view.getByRole('status')).toHaveTextContent(
      '尚未连接账户。请选择供应商开始只读授权。',
    )
    expect(view.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(view.queryByText('正在加载连接能力…')).not.toBeInTheDocument()
  })

  it('announces one safe catalog failure with trace and recovers after explicit refresh', async () => {
    vi.mocked(api.listConnections).mockRejectedValueOnce(
      new ProblemError({
        type: 'about:blank',
        title: 'Synthetic unavailable',
        detail: 'Synthetic raw detail',
        status: 503,
        instance: '',
        error_code: 'synthetic_unavailable',
        trace_id: 'trace-connections',
      }),
    )
    const view = await renderPage()
    expect(view.getAllByRole('alert')).toHaveLength(1)
    expect(view.getByRole('alert')).toHaveTextContent('请求失败，请重试。')
    expect(view.getByRole('alert')).toHaveTextContent(
      '追踪编号：trace-connections',
    )
    expect(view.getByRole('alert')).toHaveAttribute('aria-live', 'assertive')
    expect(view.queryByText('Synthetic raw detail')).not.toBeInTheDocument()
    expect(
      view.queryByText('尚未连接账户。请选择供应商开始只读授权。'),
    ).not.toBeInTheDocument()
    vi.mocked(api.listConnections).mockResolvedValueOnce([])
    await fireEvent.click(view.getByRole('button', { name: '刷新连接' }))
    await flush()
    expect(view.queryByRole('alert')).not.toBeInTheDocument()
    expect(view.getByRole('status')).toHaveTextContent(
      '尚未连接账户。请选择供应商开始只读授权。',
    )
  })

  it('keeps a degraded capability in its original status region without a second announcement', async () => {
    const capabilities = connectionCapabilities()
    capabilities.capabilities = capabilities.capabilities.map((row) =>
      row.capability === 'mail.send' ? { ...row, status: 'degraded' } : row,
    )
    const view = await renderWithPlugins(CapabilityRows, {
      props: { capabilities, busy: false, disconnected: false },
    })
    expect(view.getAllByRole('status')).toHaveLength(4)
    expect(
      capability(view.container, 'mail.send').getByRole('status'),
    ).toHaveTextContent('暂不可用')
    const explanation = view.getByRole('note')
    expect(explanation).toHaveTextContent(
      '能力暂不可用，请重新授权或稍后刷新。',
    )
    expect(explanation).toHaveAttribute('aria-live', 'off')
    expect(view.queryByRole('alert')).not.toBeInTheDocument()
    expect(
      view.getByRole('button', { name: '重新授权 mail.send' }),
    ).toBeEnabled()
  })

  it('starts each provider only after an explicit click and exposes its authorization link', async () => {
    const view = await renderPage()
    expect(api.startMicrosoftConnection).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '连接 Microsoft' }))
    await flush()
    expect(api.startMicrosoftConnection).toHaveBeenCalledTimes(1)
    expect(authorization(view.container)).toHaveAttribute(
      'href',
      expect.stringContaining('login.microsoftonline.com'),
    )
    await fireEvent.click(view.getByRole('button', { name: '连接 Google' }))
    await flush()
    expect(api.startGoogleConnection).toHaveBeenCalledTimes(1)
    expect(authorization(view.container)).toHaveAttribute(
      'href',
      expect.stringContaining('accounts.google.com'),
    )
  })

  it('renders four capability rows per account and prevents disabling an active read dependency', async () => {
    const view = await renderPage()
    expect(view.getAllByTestId(/^capability-/)).toHaveLength(8)
    const card = account(view.container)
    expect(card.getByRole('switch', { name: '关闭 mail.read' })).toBeDisabled()
    expect(card.getByText('请先关闭 mail.send。')).toBeVisible()
    expect(card.getAllByText('synthetic-scope')).toHaveLength(4)
    expect(card.getByRole('switch', { name: '关闭 mail.send' })).toBeEnabled()
  })

  it.each(['google', 'microsoft'] as const)(
    'allows explicit reauthorization of an enabled capability on the existing %s account',
    async (provider) => {
      const selected = connection(provider)
      const before = connectionCapabilities(provider)
      const after = connectionCapabilities(provider)
      after.capabilities = after.capabilities.map((row) => ({
        ...row,
        status: 'authorizing',
      }))
      const url =
        provider === 'google'
          ? 'https://accounts.google.com/o/oauth2/v2/auth'
          : 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
      vi.mocked(api.listConnections).mockResolvedValue([selected])
      vi.mocked(api.getConnectionCapabilities)
        .mockResolvedValueOnce(before)
        .mockResolvedValue(after)
      vi.mocked(api.enableConnectionCapability).mockResolvedValue({
        authorization_url: url,
        requested_capabilities: [
          'mail.read',
          'mail.send',
          'calendar.read',
          'calendar.write',
        ],
      })
      const view = await renderPage()
      const button = view.getByRole('button', { name: '重新授权 mail.read' })
      expect(button).toBeEnabled()
      expect(
        view.queryByRole('link', { name: /^继续 / }),
      ).not.toBeInTheDocument()
      expect(api.enableConnectionCapability).not.toHaveBeenCalled()
      await fireEvent.click(button)
      await flush()
      const link = authorization(view.container)
      expect(link).toHaveAttribute('href', url)
      expect(link).toHaveFocus()
      expect(
        view.getByText(
          /本次授权包含：mail.read、mail.send、calendar.read、calendar.write/,
        ),
      ).toBeVisible()
      expect(
        view
          .getByText(
            /本次授权包含：mail.read、mail.send、calendar.read、calendar.write/,
          )
          .closest('[role="status"]'),
      ).toHaveAttribute('aria-live', 'polite')
      expect(
        capability(view.container, 'mail.read').getByRole('status'),
      ).toHaveTextContent('授权中')
      expect(api.enableConnectionCapability).toHaveBeenCalledExactlyOnceWith(
        selected.id,
        'mail.read',
        provider,
      )
      expect(api.startGoogleConnection).not.toHaveBeenCalled()
      expect(api.startMicrosoftConnection).not.toHaveBeenCalled()
      expect(api.disableConnectionCapability).not.toHaveBeenCalled()
      expect(api.disconnectConnection).not.toHaveBeenCalled()
    },
  )

  it.each([
    { state: 'busy', busy: true, disconnected: false },
    { state: 'disconnected', busy: false, disconnected: true },
  ])(
    'keeps enabled-capability reauthorization disabled when $state',
    async (state) => {
      const view = await renderWithPlugins(CapabilityRows, {
        props: {
          capabilities: connectionCapabilities(),
          busy: state.busy,
          disconnected: state.disconnected,
        },
      })
      const button = view.getByRole('button', { name: '重新授权 mail.read' })
      expect(button).toBeDisabled()
      // 原生激活遵守 disabled；直接 dispatchEvent 会绕过浏览器禁用控件规则。
      button.click()
      expect(view.emitted().enable).toBeUndefined()
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
    const view = await renderPage()
    expect(view.getByText(/请联系管理员批准所需委托权限/)).toBeVisible()
    expect(
      capability(view.container, 'mail.send').getByRole('status'),
    ).toHaveTextContent('需要操作')
    await fireEvent.click(
      view.getByRole('button', { name: '重新授权 mail.send' }),
    )
    await flush()
    expect(api.enableConnectionCapability).toHaveBeenCalledWith(
      connection('microsoft').id,
      'mail.send',
      'microsoft',
    )
    expect(view.getByText(/本次授权包含：mail.read、mail.send/)).toBeVisible()
  })

  it('keeps successful accounts visible when one capability request fails and supports refresh', async () => {
    vi.mocked(api.getConnectionCapabilities).mockImplementation(async (id) => {
      if (id === connection('microsoft').id) throw new Error('unavailable')
      return connectionCapabilities()
    })
    const view = await renderPage()
    expect(view.getAllByTestId(/^capability-/)).toHaveLength(4)
    expect(view.getByRole('alert')).toHaveTextContent('能力加载失败')
    expect(view.getByRole('alert')).toHaveAttribute('aria-live', 'assertive')
    expect(view.getAllByRole('status')).toHaveLength(6)
    expect(account(view.container).getAllByRole('status')[0]).toHaveTextContent(
      '连接状态：connected',
    )
    expect(view.getByRole('button', { name: '刷新连接' })).toBeVisible()
  })

  it('can start a fresh authorization after reloading an authorizing capability', async () => {
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
    const view = await renderPage()
    const retry = view.getByRole('button', { name: '重新授权 mail.send' })
    expect(retry).toBeEnabled()
    await fireEvent.click(retry)
    await flush()
    expect(authorization(view.container)).toHaveAttribute(
      'href',
      'https://accounts.google.com/o/oauth2/v2/auth',
    )
  })

  it('reads fresh capability status after starting authorization', async () => {
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
    const view = await renderPage()
    await fireEvent.click(view.getByRole('button', { name: '启用 mail.send' }))
    await flush()
    expect(
      capability(view.container, 'mail.send').getByRole('status'),
    ).toHaveTextContent('授权中')
    expect(
      capability(view.container, 'mail.read').getByRole('status'),
    ).toHaveTextContent('授权中')
  })

  it('moves focus to the next authorization step after a capability click', async () => {
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
    const view = await renderPage()
    await fireEvent.click(view.getByRole('button', { name: '启用 mail.send' }))
    await flush()
    expect(authorization(view.container)).toHaveFocus()
  })

  it('names account cards and capability tables with four rows and column scopes', async () => {
    const view = await renderPage()
    const card = view.getByRole('article', {
      name: 'Google · google@mail.example.test',
    })
    const table = within(card).getByRole('table', { name: '连接能力' })
    expect(within(table).getAllByRole('row')).toHaveLength(5)
    for (const header of within(table).getAllByRole('columnheader')) {
      expect(header).toHaveAttribute('scope', 'col')
    }
    expect(
      within(table).getByRole('columnheader', { name: '授权范围' }),
    ).toBeVisible()
    expect(
      view.queryByRole('combobox', { name: /默认/ }),
    ).not.toBeInTheDocument()
  })

  it('keeps switches bound to server closure facts and resets an unsuccessful pending change', async () => {
    const view = await renderWithPlugins(CapabilityRows, {
      props: {
        capabilities: connectionCapabilities(),
        busy: false,
        disconnected: false,
      },
    })
    expect(view.getByRole('switch', { name: '关闭 mail.read' })).toBeDisabled()
    const close = view.getByRole('switch', { name: '关闭 mail.send' })
    expect(close).not.toBeChecked()
    await fireEvent.click(close)
    expect(view.emitted().disable).toEqual([['mail.send']])
    expect(
      capability(view.container, 'mail.send').getByRole('status'),
    ).toHaveTextContent('已启用')
    await view.rerender({ busy: true })
    expect(close).toBeDisabled()
    // 请求失败时父组件只解除 busy，不能把控件的临时值冒充服务端已关闭。
    await view.rerender({ busy: false })
    await flush()
    expect(close).not.toBeChecked()
  })

  it('shows verification times in the explicitly supplied IANA zone', async () => {
    const capabilities = connectionCapabilities()
    capabilities.capabilities = capabilities.capabilities.map((row) => ({
      ...row,
      last_verified_at: '2030-01-01T00:00:00Z',
    }))
    const view = await renderWithPlugins(CapabilityRows, {
      props: {
        capabilities,
        busy: false,
        disconnected: false,
        timezone: 'Asia/Tokyo',
      },
    })
    expect(
      view.getAllByText('最近验证：2030/01/01 09:00:00（Asia/Tokyo）'),
    ).toHaveLength(4)
  })

  it('uses one real confirmation dialog and cancellation never disconnects an account', async () => {
    const view = await renderPage()
    const trigger = account(view.container).getByRole('button', {
      name: '断开',
    })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('alertdialog', { name: '断开连接' })
    expect(view.getAllByRole('alertdialog')).toHaveLength(1)
    expect(dialog).toHaveTextContent(
      '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
    )
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(window.confirm).not.toHaveBeenCalled()
    expect(view.getByRole('button', { name: '连接 Google' })).toBeDisabled()
    await fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    await flush()
    await waitFor(() => {
      expect(view.queryByRole('alertdialog')).not.toBeInTheDocument()
    })
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(trigger).toBeEnabled()
  })

  it('disconnects the confirmed account once and refreshes its exact server status', async () => {
    let finish: (value: null) => void = () => {
      throw new Error('request not initialized')
    }
    vi.mocked(api.disconnectConnection).mockReturnValue(
      new Promise<null>((resolve) => {
        finish = resolve
      }),
    )
    const view = await renderPage()
    await fireEvent.click(
      account(view.container).getByRole('button', { name: '断开' }),
    )
    const dialog = await view.findByRole('alertdialog', { name: '断开连接' })
    await fireEvent.click(
      within(dialog).getByRole('button', { name: '确认断开' }),
    )
    await flush()
    expect(api.disconnectConnection).toHaveBeenCalledExactlyOnceWith(
      connection().id,
    )
    expect(view.getByRole('button', { name: '连接 Google' })).toBeDisabled()
    vi.mocked(api.listConnections).mockResolvedValue([
      { ...connection(), status: 'disconnected' },
      connection('microsoft'),
    ])
    finish(null)
    await flush()
    expect(
      account(view.container).queryByRole('button', { name: '断开' }),
    ).not.toBeInTheDocument()
    expect(
      account(view.container).getByRole('button', { name: '重新连接' }),
    ).toBeEnabled()
    expect(api.listConnections).toHaveBeenCalledTimes(2)
    expect(window.confirm).not.toHaveBeenCalled()
  })

  it('unmounts a pending confirmation without leaving a late disconnect action', async () => {
    const view = await renderPage()
    await fireEvent.click(
      account(view.container).getByRole('button', { name: '断开' }),
    )
    await view.findByRole('alertdialog', { name: '断开连接' })
    view.unmount()
    await flush()
    expect(view.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(api.disconnectConnection).not.toHaveBeenCalled()
    expect(api.listConnections).toHaveBeenCalledTimes(1)
  })
})
