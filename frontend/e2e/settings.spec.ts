import { test, expect, type Page } from '@playwright/test'
import {
  editorJson,
  editorProblem,
  editorWorkspace,
} from './fixtures/editorApi'
import { userSettings } from '../src/test-support/actionFixtures'
import { connection } from '../src/test-support/editorFixtures'

/** 设置页仅使用合成 API；未声明请求由 editorWorkspace 拒绝，不访问真实账户。 */
async function settingsWorkspace(page: Page) {
  const capture = await editorWorkspace(page)
  await page.route('**/api/v1/auth/sessions', (route) => editorJson(route, []))
  await page.route('**/api/v1/settings', (route) =>
    editorJson(route, userSettings()),
  )
  return capture
}

test('deletion Dialog traps keyboard focus, isolates the shell and restores the trigger on Escape', async ({
  page,
}) => {
  const capture = await settingsWorkspace(page)
  await page.goto('/settings')
  await expect(
    page.getByRole('button', { name: '保存', exact: true }),
  ).toBeVisible()
  const trigger = page.getByRole('button', {
    name: '删除全部数据',
    exact: true,
  })
  await trigger.focus()
  await trigger.press('Enter')
  const dialog = page.getByRole('dialog', { name: '删除全部数据', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog).toHaveAttribute('aria-modal', 'true')
  await expect(dialog.getByLabel('确认全部删除')).toBeFocused()
  expect(
    await page
      .locator('main')
      .evaluate((node) => node.closest('[inert]') !== null),
  ).toBe(true)
  await expect(dialog).toContainText(
    '删除本地数据不能撤回已发送的邮件或已生效的日程变更。结果未知的操作也可能已在供应商侧生效。',
  )
  for (let i = 0; i < 8; i += 1) {
    await page.keyboard.press(i < 4 ? 'Tab' : 'Shift+Tab')
    expect(
      await dialog.evaluate((node) => node.contains(document.activeElement)),
    ).toBe(true)
  }
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()
  expect(
    await page
      .locator('main')
      .evaluate((node) => node.closest('[inert]') !== null),
  ).toBe(false)
  expect(capture.mutations).toEqual([])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('dirty settings use the global confirmation and native unload boundary without losing input on cancel', async ({
  page,
}) => {
  const capture = await settingsWorkspace(page)
  await page.goto('/settings')
  await page.getByLabel('语言', { exact: true }).fill('en-US')
  const leave = page.getByRole('link', { name: '操作中心', exact: true })
  await leave.click()
  const dialog = page.getByRole('alertdialog', { name: '离开设置页' })
  await expect(dialog).toContainText('工作设置尚未保存，确定离开？')
  expect(
    await page
      .locator('main')
      .evaluate((node) => node.closest('[inert]') !== null),
  ).toBe(true)
  await dialog.getByRole('button', { name: '继续编辑' }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByLabel('语言', { exact: true })).toHaveValue('en-US')
  await expect(leave).toBeFocused()
  expect(
    await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    }),
  ).toBe(true)
  await leave.click()
  await dialog.getByRole('button', { name: '放弃修改并离开' }).click()
  await expect(page).toHaveURL(/\/actions$/)
  expect(
    await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    }),
  ).toBe(false)
  expect(capture.mutations).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('load failure recovers and a pending save is exclusive before a 409 preserves the draft', async ({
  page,
}) => {
  const capture = await settingsWorkspace(page)
  let reads = 0
  let release: (() => void) | undefined
  await page.route('**/api/v1/settings', async (route) => {
    if (route.request().method() === 'PATCH') {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return editorProblem(route, 'request_validation_failed', 409)
    }
    reads += 1
    return reads === 1
      ? editorProblem(route, 'internal_error', 500)
      : editorJson(route, userSettings())
  })
  await page.goto('/settings')
  await expect(page.getByRole('alert')).toContainText(
    '请求失败，请核对输入后重试。',
  )
  await page.getByRole('button', { name: '重新加载设置' }).click()
  await page.getByLabel('邮件正文保留天数').fill('45')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('button', { name: '正在保存…' })).toBeDisabled()
  await page.getByRole('form').evaluate((node: HTMLFormElement) => {
    node.requestSubmit()
    node.requestSubmit()
  })
  expect(
    capture.mutations.filter((item) => item.path === '/settings'),
  ).toHaveLength(1)
  release?.()
  await expect(page.getByRole('alert')).toContainText(
    '输入未通过校验，请检查字段后重试。',
  )
  await expect(page.getByRole('alert')).toContainText(
    '版本或状态已变化，请重新加载后核对。',
  )
  await expect(page.getByRole('alert')).toContainText('synthetic-editor-trace')
  await expect(page.getByLabel('邮件正文保留天数')).toHaveValue('45')
  expect(capture.pageErrors).toEqual([])
})

for (const timezoneId of ['America/Los_Angeles', 'Pacific/Auckland']) {
  test.describe(`wall times in browser ${timezoneId}`, () => {
    test.use({ timezoneId })
    test('keeps inactive defaults and exact tuple wall times, including cleared and reopened days', async ({
      page,
    }) => {
      const capture = await settingsWorkspace(page)
      let snapshot = {
        ...userSettings(),
        default_mail_connection_id: connection('microsoft').id,
        default_calendar_connection_id: connection().id,
        default_calendar_id: 'synthetic-google-calendar',
      }
      await page.route('**/api/v1/connections', (route) =>
        editorJson(route, [connection()]),
      )
      await page.route('**/api/v1/settings', (route) => {
        if (route.request().method() === 'PATCH')
          snapshot = { ...snapshot, ...route.request().postDataJSON() }
        return editorJson(route, snapshot)
      })
      await page.goto('/settings')
      await expect(
        page.getByRole('combobox', { name: '默认发送账户', exact: true }),
      ).toContainText('默认账户已不可用，请重选')
      await expect(
        page.getByRole('combobox', { name: '默认日历', exact: true }),
      ).toContainText('Synthetic calendar')
      // 重复选择当前账户不意味着用户要清空日历。
      await page
        .getByRole('combobox', { name: '默认日历账户', exact: true })
        .click()
      await page.getByRole('option', { name: /Google/ }).click()
      await expect(
        page.getByRole('combobox', { name: '默认日历', exact: true }),
      ).toContainText('Synthetic calendar')
      await page.getByRole('switch', { name: '星期一工作日' }).uncheck()
      await page.getByRole('switch', { name: '星期二工作日' }).check()
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect(page.getByRole('alert')).toContainText('HH:mm')
      expect(capture.mutations).toHaveLength(0)
      await page.getByLabel('星期二开始 1', { exact: true }).fill('25:00')
      await page.getByLabel('星期二结束 1', { exact: true }).fill('17:00')
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect(
        page.getByLabel('星期二开始 1', { exact: true }),
      ).toHaveValue('25:00')
      expect(capture.mutations).toHaveLength(0)
      await page.getByLabel('星期二开始 1', { exact: true }).fill('10:00')
      await page.getByRole('button', { name: '添加星期二区间' }).click()
      await page.getByLabel('星期二开始 2', { exact: true }).fill('18:00')
      await page.getByLabel('星期二结束 2', { exact: true }).fill('19:00')
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect(
        page.getByRole('region', { name: '工作设置' }).getByRole('status'),
      ).toHaveText('已保存')
      expect(capture.mutations).toHaveLength(1)
      expect(capture.mutations[0]?.payload).toEqual({
        ...userSettings(),
        updated_at: undefined,
        brief_time: '08:00',
        default_mail_connection_id: connection('microsoft').id,
        default_calendar_connection_id: connection().id,
        default_calendar_id: 'synthetic-google-calendar',
        working_hours: {
          ...userSettings().working_hours,
          monday: [],
          tuesday: [
            ['10:00', '17:00'],
            ['18:00', '19:00'],
          ],
        },
      })
      expect(capture.unexpected).toEqual([])
      expect(capture.pageErrors).toEqual([])
    })
  })
}

test('legacy seconds stay visible and require an explicit minute choice before saving', async ({
  page,
}) => {
  const capture = await settingsWorkspace(page)
  await page.route('**/api/v1/settings', (route) =>
    editorJson(route, { ...userSettings(), brief_time: '08:00:30' }),
  )
  await page.goto('/settings')
  await expect(page.getByLabel('简报时间', { exact: true })).toHaveValue(
    '08:00:30',
  )
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('请明确重新选择分钟精度')
  expect(capture.mutations).toHaveLength(0)
  await page.getByLabel('简报时间', { exact: true }).fill('08:01')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(
    page.getByRole('region', { name: '工作设置' }).getByRole('status'),
  ).toHaveText('已保存')
  expect(capture.mutations[0]?.payload).toMatchObject({ brief_time: '08:01' })
})

test('the heavy form is loaded on settings demand with visible loading and retry after module fetch failure', async ({
  page,
}) => {
  const capture = await settingsWorkspace(page)
  let attempts = 0
  let release: (() => void) | undefined
  await page.route('**/src/components/WorkSettingsForm.vue*', async (route) => {
    attempts += 1
    if (attempts === 1) return route.abort('failed')
    await new Promise<void>((resolve) => {
      release = resolve
    })
    await route.continue()
  })
  await page.goto('/actions')
  expect(attempts).toBe(0)
  await page.getByRole('link', { name: '设置', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText(
    '工作设置表单加载失败，请重试。',
  )
  await page.getByRole('button', { name: '重试加载表单' }).click()
  await expect(
    page.getByRole('status').filter({ hasText: '正在加载工作设置表单…' }),
  ).toHaveText('正在加载工作设置表单…')
  await expect.poll(() => attempts).toBe(2)
  release?.()
  await expect(
    page.getByRole('button', { name: '保存', exact: true }),
  ).toBeVisible()
  expect(capture.pageErrors).toEqual([])
})
