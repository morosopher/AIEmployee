import { expect, test } from '@playwright/test'
import {
  connection,
  connectionCapabilities,
} from '../src/test-support/editorFixtures'
import { editorJson, editorWorkspace } from './fixtures/editorApi'

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
    const card = page.locator(`[data-connection-id="${account.id}"]`)
    const retry = card.locator('button[name="enable-mail.send"]')
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
      card.locator('[data-capability="mail.send"] [role="status"]'),
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
    const card = page.locator(`[data-connection-id="${account.id}"]`)
    const retry = card.locator('button[name="enable-mail.read"]')
    const link = page.getByTestId('authorization-link')
    // scope 仍为 enabled 时也必须可主动恢复，不能要求先关闭或走首次连接。
    await expect(
      card.locator('[data-capability="mail.read"] [role="status"]'),
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
      card.locator('[data-capability="mail.read"] [role="status"]'),
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
