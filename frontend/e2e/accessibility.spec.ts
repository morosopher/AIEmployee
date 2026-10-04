import { expect, test } from '@playwright/test'
import { expectAccessible } from './support/axe'
import { openVisualView, visualViews } from './support/visualWorkspace'

/** 同一真实页面在三档布局各扫描一次；既有 E2E 仍只由 chromium project 执行。 */
for (const view of visualViews) {
  test(`${view} has no serious or critical accessibility violations`, async ({
    page,
  }, info) => {
    const capture = await openVisualView(page, view)
    await expectAccessible(page, info, view)
    expect(capture.mutations).toEqual([])
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
  })
}
