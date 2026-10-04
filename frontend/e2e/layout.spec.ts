import { writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { expect, test } from '@playwright/test'
import { settlePresentation, expectAccessible } from './support/axe'
import { openVisualView, visualViews } from './support/visualWorkspace'

/** 截图只使用共享合成数据；哈希使 Task17 可关联实际产物，不把二进制或报告提交进仓库。 */
for (const view of visualViews) {
  test(`${view} keeps the expected navigation and timeline layout`, async ({
    page,
  }, info) => {
    const capture = await openVisualView(page, view)
    const width = page.viewportSize()?.width ?? 0
    expect([375, 900, 1400]).toContain(width)
    await settlePresentation(page)
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width)
    if (view !== 'login') {
      await expect(
        page.getByRole('navigation', { name: '主导航', exact: true }),
      ).toHaveCount(width < 768 ? 0 : 1)
      await expect(
        page.getByRole('button', { name: '打开导航', exact: true }),
      ).toHaveCount(width < 768 ? 1 : 0)
      const ownDetail = ['actions', 'approval', 'needs_attention'].includes(
        view,
      )
      await expect(
        page.getByRole('button', { name: '任务时间线', exact: true }),
      ).toHaveCount(!ownDetail && width >= 768 && width < 1280 ? 1 : 0)
      await expect(
        page.getByRole('complementary', { name: '任务时间线', exact: true }),
      ).toHaveCount(!ownDetail && width >= 1280 ? 1 : 0)
      await expect(
        page.getByRole('button', { name: '打开任务时间线', exact: true }),
      ).toHaveCount(!ownDetail && width < 768 ? 1 : 0)
    }
    const artifact = info.outputPath(`${view}-${width}.png`)
    const bytes = await page.screenshot({ path: artifact, fullPage: true })
    const record = {
      view,
      width,
      artifact,
      sha256: createHash('sha256').update(bytes).digest('hex'),
    }
    await writeFile(
      info.outputPath('capture.json'),
      JSON.stringify(record, null, 2),
    )
    await info.attach('screenshot', {
      path: artifact,
      contentType: 'image/png',
    })
    await info.attach('screenshot-sha256', {
      body: JSON.stringify(record),
      contentType: 'application/json',
    })
    expect(capture.mutations).toEqual([])
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
  })
}

/** 真实 Drawer 在三种布局只有一个时间线实例；键盘循环、Esc 与 inert 都通过浏览器验证。 */
test('the global timeline uses its real desktop, tablet and mobile interaction', async ({
  page,
}, info) => {
  const capture = await openVisualView(page, 'brief')
  const width = page.viewportSize()?.width ?? 0
  if (width >= 1280) {
    const timeline = page.getByRole('complementary', {
      name: '任务时间线',
      exact: true,
    })
    await expect(timeline).toBeVisible()
    await expect(timeline.getByRole('status')).toHaveText(
      '请选择一个任务查看执行时间线。',
    )
    const navigation = await page
      .getByRole('navigation', { name: '主导航', exact: true })
      .boundingBox()
    const main = await page.getByRole('main').boundingBox()
    const aside = await timeline.boundingBox()
    expect(
      navigation &&
        main &&
        aside &&
        navigation.x + navigation.width <= main.x &&
        main.x + main.width <= aside.x,
    ).toBe(true)
  } else if (width >= 768) {
    const toggle = page.getByRole('button', { name: '任务时间线', exact: true })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await toggle.focus()
    await toggle.press('Enter')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(
      page.getByRole('complementary', { name: '执行时间线', exact: true }),
    ).toHaveCount(1)
    await toggle.press('Space')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle).toBeFocused()
    await expect(page.getByRole('dialog')).toHaveCount(0)
  }
  const names = width < 768 ? ['打开导航', '打开任务时间线'] : []
  for (const name of names) {
    const trigger = page.getByRole('button', { name, exact: true })
    await trigger.focus()
    await trigger.press('Enter')
    const dialog = page.getByRole('dialog')
    await expect(dialog).toHaveCount(1)
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(
      await page
        .locator('main')
        .evaluate((node) => node.closest('[inert]') !== null),
    ).toBe(true)
    await expect
      .poll(() =>
        dialog.evaluate((node) => node.contains(document.activeElement)),
      )
      .toBe(true)
    for (const key of ['Tab', 'Tab', 'Shift+Tab', 'Shift+Tab']) {
      await page.keyboard.press(key)
      expect(
        await dialog.evaluate((node) => node.contains(document.activeElement)),
      ).toBe(true)
    }
    await expectAccessible(page, info, name)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    expect(
      await page
        .locator('main')
        .evaluate((node) => node.closest('[inert]') !== null),
    ).toBe(false)
  }
  expect(capture.mutations).toEqual([])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
