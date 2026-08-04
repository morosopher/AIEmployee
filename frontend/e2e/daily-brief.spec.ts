import { test, expect } from '@playwright/test'
test('brief navigation is available after login', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: /登录/ })).toBeVisible()
})
