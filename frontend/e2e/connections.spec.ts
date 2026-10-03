import { expect, test } from '@playwright/test'
import {
  connection,
  connectionCapabilities,
} from '../src/test-support/editorFixtures'
import {
  editorJson,
  editorProblem,
  editorWorkspace,
} from './fixtures/editorApi'

// 小视口覆盖在能力卡片下方操作时，下一步链接必须滚入视野的真实浏览器行为。
test.use({ viewport: { width: 390, height: 560 } })

for (const provider of ['google', 'microsoft'] as const) {
  test(`${provider} authorization can resume after reload and reveals its next step`, async ({
    page,
  }) => {
    const capture = await editorWorkspace(page)
    // 此流程只验证页面和 HTTP 契约；即使页面意外导航，也不能访问真实供应商。
    await page.route('https://**', (route) => route.abort())
    const account = connection(provider)
    const capabilities = connectionCapabilities(provider)
    capabilities.capabilities = capabilities.capabilities.map((row) =>
      row.capability === 'mail.send' ? { ...row, status: 'authorizing' } : row,
    )
    await page.route(
      `**/api/v1/connections/${account.id}/capabilities`,
      (route) => editorJson(route, capabilities),
    )
    let attempts = 0
    const authorizationBase =
      provider === 'google'
        ? 'https://accounts.google.com/o/oauth2/v2/auth'
        : 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
    const enablePath = `/connections/${account.id}/capabilities/mail.send/enable`
    await page.route(`**/api/v1${enablePath}`, (route) => {
      attempts += 1
      return editorJson(route, {
        authorization_url: `${authorizationBase}?state=synthetic-state-${attempts}`,
        requested_capabilities: ['mail.read', 'mail.send'],
      })
    })

    await page.goto('/connections')
    const card = page.getByTestId(`connection-${account.id}`)
    const retry = card.getByRole('button', {
      name: '重新授权 mail.send',
      exact: true,
    })
    const link = page.getByTestId('authorization-link')
    await expect(retry).toBeEnabled()
    await retry.click()
    await expect(link).toHaveAttribute(
      'href',
      `${authorizationBase}?state=synthetic-state-1`,
    )
    await expect(link).toBeFocused()
    await expect(link).toBeInViewport()

    // 刷新不保留旧 URL；必须仍能明确发起新的授权，不自动重放上一次的 state。
    await page.reload()
    await expect(link).toHaveCount(0)
    await expect(retry).toBeEnabled()
    await retry.click()
    await expect(link).toHaveAttribute(
      'href',
      `${authorizationBase}?state=synthetic-state-2`,
    )
    await expect(link).toBeFocused()
    await expect(
      card.getByTestId('capability-mail.send').getByRole('status'),
    ).toHaveText('授权中')
    expect(
      capture.mutations.map(({ path, method }) => ({ path, method })),
    ).toEqual([
      { path: enablePath, method: 'POST' },
      { path: enablePath, method: 'POST' },
    ])
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
  })

  test(`${provider} enabled capability can explicitly recover its existing connection`, async ({
    page,
  }) => {
    const capture = await editorWorkspace(page)
    await page.route('https://**', (route) => route.abort())
    const account = connection(provider)
    const before = connectionCapabilities(provider)
    const after = connectionCapabilities(provider)
    after.capabilities = after.capabilities.map((row) => ({
      ...row,
      status: 'authorizing',
    }))
    let attempts = 0
    await page.route(
      `**/api/v1/connections/${account.id}/capabilities`,
      (route) => editorJson(route, attempts ? after : before),
    )
    const authorizationBase =
      provider === 'google'
        ? 'https://accounts.google.com/o/oauth2/v2/auth'
        : 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
    const enablePath = `/connections/${account.id}/capabilities/mail.read/enable`
    await page.route(`**/api/v1${enablePath}`, (route) => {
      attempts += 1
      return editorJson(route, {
        authorization_url: `${authorizationBase}?state=synthetic-recovery-${attempts}`,
        requested_capabilities: [
          'mail.read',
          'mail.send',
          'calendar.read',
          'calendar.write',
        ],
      })
    })

    await page.goto('/connections')
    const card = page.getByTestId(`connection-${account.id}`)
    const retry = card.getByRole('button', {
      name: '重新授权 mail.read',
      exact: true,
    })
    const link = page.getByTestId('authorization-link')
    // scope 仍为 enabled 时也必须可主动恢复，不能要求先关闭或走首次连接。
    await expect(
      card.getByTestId('capability-mail.read').getByRole('status'),
    ).toHaveText('已启用')
    await expect(retry).toBeEnabled()
    await expect(link).toHaveCount(0)
    expect(capture.mutations).toEqual([])
    await retry.click()
    await expect(link).toHaveAttribute(
      'href',
      `${authorizationBase}?state=synthetic-recovery-1`,
    )
    await expect(link).toBeFocused()
    await expect(link).toBeInViewport()
    await expect(
      page.getByText(
        '本次授权包含：mail.read、mail.send、calendar.read、calendar.write。请继续授权，完成后将返回连接页。',
        { exact: true },
      ),
    ).toBeVisible()
    await expect(
      card.getByTestId('capability-mail.read').getByRole('status'),
    ).toHaveText('授权中')

    // 刷新只恢复服务端状态；新的授权 attempt 仍需要第二次明确点击。
    await page.reload()
    await expect(link).toHaveCount(0)
    await expect(retry).toBeEnabled()
    expect(capture.mutations).toHaveLength(1)
    await retry.click()
    await expect(link).toHaveAttribute(
      'href',
      `${authorizationBase}?state=synthetic-recovery-2`,
    )
    expect(
      capture.mutations.map(({ path, method }) => ({ path, method })),
    ).toEqual([
      { path: enablePath, method: 'POST' },
      { path: enablePath, method: 'POST' },
    ])
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
  })
}

test('disconnect confirmation keeps keyboard focus and only sends the confirmed account', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const selected = connection()
  let disconnected = false
  let finishRequest: () => void = () => {
    throw new Error('request barrier not initialized')
  }
  const requestBarrier = new Promise<void>((resolve) => {
    finishRequest = resolve
  })
  await page.route('**/api/v1/connections', (route) =>
    editorJson(route, [
      { ...selected, status: disconnected ? 'disconnected' : 'connected' },
      connection('microsoft'),
    ]),
  )
  await page.route(`**/api/v1/connections/${selected.id}`, async (route) => {
    expect(route.request().method()).toBe('DELETE')
    await requestBarrier
    disconnected = true
    await route.fulfill({ status: 204 })
  })
  try {
    await page.goto('/connections')
    const card = page.getByTestId(`connection-${selected.id}`)
    const trigger = card.getByRole('button', { name: '断开', exact: true })
    await trigger.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('alertdialog', { name: '断开连接' })
    await expect(dialog).toBeVisible()
    await expect(
      dialog.getByRole('button', { name: '取消', exact: true }),
    ).toBeFocused()
    expect(
      await trigger.evaluate((element) => element.closest('[inert]') !== null),
    ).toBe(true)
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await expect(trigger).toBeFocused()
    expect(capture.mutations).toEqual([])
    await trigger.click()
    await dialog.getByRole('button', { name: '确认断开', exact: true }).click()
    await expect.poll(() => capture.mutations.length).toBe(1)
    await expect(dialog).not.toBeVisible()
    // 同意后触发按钮仍在请求中被禁用，焦点应落到可用的主内容而非丢在已关闭浮层。
    await expect(page.getByRole('main')).toBeFocused()
    await expect(
      page.getByRole('button', { name: '连接 Google', exact: true }),
    ).toBeDisabled()
    finishRequest()
    await expect(
      card.getByRole('button', { name: '重新连接', exact: true }),
    ).toBeEnabled()
    await expect(trigger).toHaveCount(0)
    expect(
      capture.mutations.map(({ method, path, payload }) => ({
        method,
        path,
        payload,
      })),
    ).toEqual([
      { method: 'DELETE', path: `/connections/${selected.id}`, payload: null },
    ])
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
  } finally {
    finishRequest()
  }
})

test('a failed closure restores the switch to server state before a safe explicit retry', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const selected = connection()
  const before = connectionCapabilities()
  const after = connectionCapabilities()
  after.capabilities = after.capabilities.map((row) =>
    row.capability === 'mail.send' ? { ...row, status: 'disabled' } : row,
  )
  let attempts = 0
  await page.route(
    `**/api/v1/connections/${selected.id}/capabilities`,
    (route) => editorJson(route, attempts > 1 ? after : before),
  )
  const path = `/connections/${selected.id}/capabilities/mail.send/disable`
  await page.route(`**/api/v1${path}`, async (route) => {
    expect(route.request().method()).toBe('POST')
    attempts += 1
    if (attempts === 1) await editorProblem(route, 'synthetic_version_conflict')
    else
      await editorJson(route, { capability: 'mail.send', status: 'disabled' })
  })
  await page.goto('/connections')
  const card = page.getByTestId(`connection-${selected.id}`)
  const close = card.getByRole('switch', {
    name: '关闭 mail.send',
    exact: true,
  })
  await expect(
    card.getByRole('switch', { name: '关闭 mail.read', exact: true }),
  ).toBeDisabled()
  await close.check()
  await expect(page.getByRole('alert')).toContainText('版本或状态已变化')
  await expect(close).not.toBeChecked()
  await expect(close).toBeEnabled()
  await close.check()
  await expect(close).toBeChecked()
  await expect(close).toBeDisabled()
  await expect(
    card.getByTestId('capability-mail.send').getByRole('status'),
  ).toHaveText('已关闭')
  expect(
    capture.mutations.map(({ method, path: actualPath }) => ({
      method,
      path: actualPath,
    })),
  ).toEqual([
    { method: 'POST', path },
    { method: 'POST', path },
  ])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
