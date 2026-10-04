import { writeFile } from 'node:fs/promises'
import AxeBuilder from '@axe-core/playwright'
import { expect, type Page, type TestInfo } from '@playwright/test'

/**
 * 等待当前有限过渡实际完成；保留真实颜色、焦点及组件动画，不屏蔽 axe 规则或使用固定睡眠。
 * Skeleton 等无限装饰动画不阻塞；调用方必须先等待业务内容而不是扫描加载占位。
 * @param page 已进入目标业务状态的页面。
 */
export async function settlePresentation(page: Page): Promise<void> {
  await expect(page.getByRole('listbox', { includeHidden: true })).toHaveCount(
    0,
  )
  await page.evaluate(async () => {
    await document.fonts.ready
    const animations = document
      .getAnimations()
      .filter(
        (animation) =>
          animation.playState === 'running' &&
          Number.isFinite(
            Number(animation.effect?.getComputedTiming().endTime),
          ),
      )
    await Promise.all(
      animations.map((animation) => animation.finished.catch(() => undefined)),
    )
  })
}

/**
 * 扫描完整页面（含打开的真实 portal）；保存不含 DOM／正文的规则和数量摘要供验收追溯。
 * @param page 当前合成场景。@param info Playwright 产物目录。@param name 稳定视图名。
 */
export async function expectAccessible(
  page: Page,
  info: TestInfo,
  name: string,
): Promise<void> {
  await settlePresentation(page)
  const result = await new AxeBuilder({ page }).analyze()
  const violations = result.violations.filter((item) =>
    ['serious', 'critical'].includes(item.impact ?? ''),
  )
  const summary = result.violations.map((item) => ({
    id: item.id,
    impact: item.impact,
    nodes: item.nodes.length,
    targets: item.nodes.map((node) => node.target),
  }))
  const record = JSON.stringify(
    {
      view: name,
      project: info.project.name,
      viewport: page.viewportSize(),
      violations: summary,
    },
    null,
    2,
  )
  const artifact = info.outputPath(`axe-${name}.json`)
  await writeFile(artifact, record)
  await info.attach(`axe-${name}`, {
    path: artifact,
    contentType: 'application/json',
  })
  expect(
    violations.map((item) => ({
      id: item.id,
      impact: item.impact,
      targets: item.nodes.map((node) => node.target),
    })),
  ).toEqual([])
}
