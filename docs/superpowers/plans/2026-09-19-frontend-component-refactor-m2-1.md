# M2.1 前端组件库重构实施计划

> **执行方式：** 严格按任务编号串行执行，一次只做一个任务；每个任务先写失败测试并观察预期失败，再实现最小完整改动，最后用指定提交信息提交。步骤使用复选框（`- [ ]`）跟踪。未经用户要求不启动子 Agent 或并行代理。

**目标：** 在不改动服务端契约、状态管理、SSE 行为、安全清洗和 M2 范围边界的前提下，用 PrimeVue 4.5.5（MIT）+ Tailwind CSS 4 重建前端全部页面的外观与交互，建立统一设计 token、无障碍门禁、许可证与主版本锁定门禁，并写出 M2.1 验收记录。

**架构：** 展示层替换。`src/api/`、`src/stores/`、`src/composables/useTaskEvents.ts`、`src/features/*` 中的投影与恢复逻辑、`MarkdownMessage.vue` 的 DOMPurify 清洗、`src/router/` 守卫在本里程碑视为只读。新增 `src/design/` 作为 token 与状态映射的唯一来源；组件按需自动注册；旧 scoped CSS 逐页删除。

**技术栈：** Vue 3.5、TypeScript strict、Vite 7、Pinia、vue-router、PrimeVue 4.5.5、`@primevue/forms` 4.5.5、`@primeuix/themes` 2.x、`primeicons` 7、Tailwind CSS 4、`@tailwindcss/vite`、`tailwindcss-primeui`、`unplugin-vue-components`、`@primevue/auto-import-resolver`、`@vueuse/core` 15、`zod` 4、Vitest、`@testing-library/vue` 8、Playwright、`@axe-core/playwright`、pnpm、just。

---

## 执行规则

- 事实来源是 `docs/superpowers/specs/2026-09-19-frontend-component-refactor-m2-1-design.md`。若实现证据与规格的选型、许可、只读边界、无障碍或安全约束冲突，停止并向用户说明。
- 每个任务按红—绿—重构执行：先新增或调整聚焦测试并运行观察预期失败，再实现，再运行聚焦测试、`pnpm --dir frontend type-check`、`pnpm --dir frontend lint`，最后提交。
- 页面迁移任务（Task 4～15）在动模板前必须先把该页面既有单测和相关 E2E 的选择器改为 `getByRole`/`getByLabelText`/`getByText`，并在旧实现上确认通过；这一步与迁移本身在同一任务内但作为 Step 1 单独完成。
- 行为层文件（`src/api/**`、`src/stores/**`、`src/composables/**`、`src/features/**` 中既有非 `schema.ts`/`presentation.ts` 的文件、`src/router/**`）在 M2.1 视为只读。显式新增的 `schema.spec.ts`、`features/forms/problemFields.ts` 及其测试属于本计划文件清单许可；Task 8 前异步确认端口及 Task 9 前两文件最小错误码元数据例外仅按各自独立 fix 和回归执行，其他既有行为层仍只读。若迁移暴露行为层缺陷，先停止当前任务，以独立 `fix:` 提交修复并附回归测试，再继续。
- 测试禁止依赖 PrimeVue 内部 class（`p-*`）、内部 DOM 层级或 `data-pc-*` 属性；只依赖角色、label、可见文本和自有 `data-testid`。
- 组件测试新增内容使用 `@testing-library/vue`；需要断言 Emits/Props 的既有 Vue Test Utils 测试可保留，同一文件不混用两种风格。
- 不新增任何 `localStorage`/`sessionStorage`/`document.cookie` 写入；不引入 CDN 资源；不使用 `v-html` 或 `escape=false` 渲染 API 返回的字符串。
- 不出现 M2 范围外的可操作入口或占位 UI；不实现暗色模式、多语言、主题切换。
- 许可证默认白名单以规格 §3.2 为准；基线例外仅限该节授权表的包名、精确版本和许可证原文组合，保留生产/开发 scope 隔离，升级不继承例外。新增例外须先获用户批准并同步事实来源与测试。
- PrimeVue 相关包固定精确版本 `4.5.5`；出现 `primevue@5`、`@primeui/*`、`@primeuix/themes@3` 即视为违规。
- 每个任务的提交只包含该任务列出的文件，提交信息使用任务末尾给定的文本。
- Task 16 之前 `just check` 中的样式检查为警告模式；Task 16 之后转为失败模式。`check` 保持快速许可/版本/样式等检查；`ci` 在构建后生成体积报告并检查预算。Task 17 提交前及最终文档提交后均运行完整 `just ci`，读取并留档输出。
- Task 2 迁移前核对并冻结真实 live region 清单（触发场景、语义、文案）。2026-10-01 的迁移前源码清点为 status 43 / alert 32 / dialog 1，基准提交为 `4ce84ae`，可从该提交的 `frontend/src/**/*.vue` 按单双引号 `role` 属性重新清点；本地执行产物 `.superpowers/sdd/2026-09-19-frontend-component-refactor-m2-1/live-region-baseline.json` 仅作辅助，不作为唯一事实来源。该计数不包含运行时组件内置 role。Task 2～15 逐项保留旧语义/文案，组件抽取、内置 role、新抽屉导致的数量变化在同一提交记录并补运行时测试；禁止空 role 凑数。Task 16 汇总清单及运行时证据。
- Forms + zod 仅用于工作设置、邮件与日程；登录聊天保留提交逻辑。时区沿用本地 Intl IANA + UTC，保留服务端当前值及显式合法 IANA，不新增列表接口或推断用户时区。
- 页面相关 E2E 在旧实现和迁移后都必须运行；Task 6/7 补跑 daily-brief、action-workspace、reconnect，Task 9 补跑 settings。共享编辑器组件变更覆盖 calendar/actions/brief 消费者；因 DOM 调整需扩充 E2E 文件名单时先记录差异，业务断言不变。
- 所有测试数据使用 `src/test-support/` 既有合成 fixture；不复制真实邮件、姓名或地址。

## 文件地图

### 工具链、设计系统与门禁

- 修改：`frontend/package.json`、`frontend/pnpm-lock.yaml`、`frontend/pnpm-workspace.yaml`
- 修改：`frontend/vite.config.ts`、`frontend/tsconfig.app.json`、`frontend/eslint.config.js`、`frontend/index.html`、`frontend/src/main.ts`、`frontend/src/env.d.ts`
- 创建：`frontend/src/design/tokens.ts`、`frontend/src/design/status.ts`、`frontend/src/design/locale.ts`、`frontend/src/design/primevue.ts`、`frontend/src/design/app.css`
- 创建：`frontend/src/design/tokens.spec.ts`、`frontend/src/design/status.spec.ts`、`frontend/src/design/primevue.spec.ts`
- 创建：`frontend/src/test-support/renderWithPlugins.ts`
- 创建：`scripts/check-frontend-licenses.sh`、`scripts/check-frontend-styles.sh`、`scripts/report-frontend-bundle.sh`
- 修改：`justfiles/test.just`、`scripts/test-tooling.sh`

### 骨架与基础组件

- 修改：`frontend/src/components/AppShell.vue`、`frontend/src/components/AppShell.spec.ts`
- 创建：`frontend/src/components/AppNavigation.vue`、`frontend/src/components/SystemAlertBanner.vue`、`frontend/src/components/TimelineDrawer.vue`
- 创建：`frontend/src/components/EmptyState.vue`、`frontend/src/components/EmptyState.spec.ts`、`frontend/src/components/StatusTag.vue`、`frontend/src/components/StatusTag.spec.ts`、`frontend/src/components/ProblemMessage.vue`、`frontend/src/components/ProblemMessage.spec.ts`
- 修改：`frontend/src/components/SourceLink.vue`、`frontend/src/components/MarkdownMessage.vue`

### 页面与业务组件

- 修改：`frontend/src/pages/*.vue` 全部 9 个页面及其 `*.spec.ts`
- 修改：`frontend/src/components/` 中其余 M2 组件及其 `*.spec.ts`
- 创建：`frontend/src/features/settings/schema.ts`、`frontend/src/features/mail/schema.ts`、`frontend/src/features/calendar/schema.ts` 及对应 `schema.spec.ts`
- 创建：`frontend/src/features/forms/problemFields.ts`、`frontend/src/features/forms/problemFields.spec.ts`
- Task 8 前独立 fix：修改 `frontend/src/features/connections/useConnections.ts`，创建 `frontend/src/features/connections/useConnections.spec.ts`（仅确认端口回归）。
- Task 9 前独立 fix：修改 `features/actions/recovery.ts` 与 `features/calendar/useCalendarProposalEditor.ts` 的最小错误码投影，新增 `features/actions/recovery.spec.ts` 并增强 `pages/CalendarProposalPage.spec.ts`；具体边界见 Task 9 前置。

### E2E 与验收

- 修改：`frontend/e2e/*.spec.ts`
- 创建：`frontend/e2e/support/axe.ts`、`frontend/e2e/accessibility.spec.ts`、`frontend/e2e/layout.spec.ts`
- 修改：`frontend/AGENTS.md`、`AGENTS.md`、`CLAUDE.md`、`README.md`、`docs/acceptance-checklist.md`
- 创建：`docs/releases/2026-09-19-m2-1-frontend-refactor-evidence.md`

---

### Task 1: 引入工具链、设计 token、PrimeVue 配置与门禁

**Files:**
- Modify: `frontend/package.json`、`frontend/pnpm-lock.yaml`、`frontend/pnpm-workspace.yaml`
- Modify: `frontend/vite.config.ts`、`frontend/tsconfig.app.json`、`frontend/eslint.config.js`、`frontend/index.html`、`frontend/src/main.ts`、`frontend/src/env.d.ts`
- Create: `frontend/src/design/tokens.ts`、`frontend/src/design/status.ts`、`frontend/src/design/locale.ts`、`frontend/src/design/primevue.ts`、`frontend/src/design/app.css`
- Create: `frontend/src/design/tokens.spec.ts`、`frontend/src/design/status.spec.ts`、`frontend/src/design/primevue.spec.ts`
- Create: `frontend/src/test-support/renderWithPlugins.ts`
- Create: `scripts/check-frontend-licenses.sh`、`scripts/check-frontend-styles.sh`、`scripts/report-frontend-bundle.sh`
- Modify: `justfiles/test.just`、`scripts/test-tooling.sh`

- [ ] **Step 1: 写失败的 token、状态映射与 PrimeVue 配置测试**

~~~typescript
// frontend/src/design/tokens.spec.ts
it('exposes every legacy hex colour as a semantic token', () => {
  expect(semanticColors.primary).toBe('#164e9c')
  expect(semanticColors.danger).toBe('#a61b1b')
  expect(semanticColors.warn.foreground).toBe('#946200')
  expect(semanticColors.warn.background).toBe('#fff5df')
  expect(Object.keys(semanticColors)).not.toContain('gray1')
})

it('meets WCAG AA contrast for text on surface tokens', () => {
  expect(contrastRatio(semanticColors.text.primary, semanticColors.surface.page)).toBeGreaterThanOrEqual(4.5)
  expect(contrastRatio('#ffffff', semanticColors.primary)).toBeGreaterThanOrEqual(4.5)
  expect(contrastRatio('#ffffff', semanticColors.danger)).toBeGreaterThanOrEqual(4.5)
})

// frontend/src/design/status.spec.ts
it('maps every task status to a severity and a Chinese label', () => {
  for (const status of TASK_STATUSES) {
    expect(taskStatusPresentation(status)).toMatchObject({ severity: expect.any(String), label: expect.any(String) })
  }
  expect(taskStatusPresentation('needs_attention').severity).toBe('danger')
})

// frontend/src/design/primevue.spec.ts
it('configures PrimeVue with the Aura-derived preset, zh-CN locale, no ripple, and a CSS layer below utilities', () => {
  expect(primeVueOptions.ripple).toBe(false)
  expect(primeVueOptions.locale?.today).toBe('今天')
  expect(primeVueOptions.theme.options.cssLayer).toMatchObject({ name: 'primevue', order: 'theme, base, primevue, utilities' })
})

it('reads the CSP nonce from the meta tag when present', () => {
  document.head.innerHTML = '<meta name="csp-nonce" content="synthetic-nonce">'
  expect(resolveCspNonce(document)).toBe('synthetic-nonce')
})
~~~

`TASK_STATUSES` 从 `src/api/types.ts` 既有联合类型派生；`contrastRatio` 是 `tokens.ts` 内的纯函数。

- [ ] **Step 2: 运行并观察预期失败**

Run: `pnpm --dir frontend test:unit --run src/design`

Expected: FAIL，`src/design` 模块不存在。

- [ ] **Step 3: 安装依赖并锁定版本**

在 `frontend/` 执行 `pnpm add primevue@4.5.5 @primevue/forms@4.5.5 @primeuix/themes@2 primeicons@7 tailwindcss@4 @tailwindcss/vite@4 tailwindcss-primeui@0.6 @vueuse/core@15 zod@4` 与 `pnpm add -D @primevue/auto-import-resolver@4.5.5 unplugin-vue-components@32 @testing-library/vue@8 @testing-library/jest-dom @axe-core/playwright`。随后把 `package.json` 中四个 PrimeVue 包改为精确版本 `4.5.5`，`@primeuix/themes` 改为 `~2.x` 当前次版本。`pnpm-workspace.yaml` 的 `allowBuilds` 只在实际需要时新增条目，并在注释中说明原因。

记录 `pnpm licenses list --prod --json` 与 `pnpm audit` 输出摘要，写入提交说明正文；出现默认白名单以外且不精确匹配规格 §3.2 授权基线例外的生产依赖即停止。

- [ ] **Step 4: 建立设计 token、状态映射、locale 与 PrimeVue 配置**

`tokens.ts` 导出 `semanticColors`、`contrastRatio` 和由 `definePreset(Aura, …)` 生成的 `appPreset`；`status.ts` 导出任务、能力、审批、连接状态到 `severity`/label 的映射；`locale.ts` 导出完整 zh-CN PrimeVue locale；`primevue.ts` 导出 `primeVueOptions` 与 `resolveCspNonce`。`app.css` 只包含 `@import "tailwindcss"`、`@plugin "tailwindcss-primeui"`、`@layer` 顺序声明、字体栈和 `prefers-reduced-motion` 规则。

`main.ts` 安装 PrimeVue、`ToastService`、`ConfirmationService`，并导入 `app.css` 与 `primeicons/primeicons.css`。`vite.config.ts` 增加 `@tailwindcss/vite` 与 `unplugin-vue-components` 的 `PrimeVueResolver`，并设置 `build.reportCompressedSize: true`。`env.d.ts` 引入 `unplugin-vue-components` 生成的 `components.d.ts` 类型；该生成文件加入根 `.gitignore`（路径 `frontend/components.d.ts`）。

`renderWithPlugins.ts` 为 Testing Library 提供安装了 PrimeVue、Pinia 和路由 stub 的 `render` 包装，后续所有组件测试复用。

- [ ] **Step 5: 添加许可证、主版本、样式与体积门禁**

`scripts/check-frontend-licenses.sh`：读取 `pnpm licenses list --prod --json`，允许 MIT、ISC、BSD-2-Clause、BSD-3-Clause、Apache-2.0，开发依赖额外允许 MPL-2.0；基线例外严格匹配规格 §3.2 授权表的包名、精确版本和许可证原文，生产例外可用于开发树，开发例外不得用于生产树，同包升级不继承例外；扫描 `pnpm-lock.yaml`，出现 `primevue@5`、`/@primeui/`、`@primeuix/themes@3` 即 exit 1。

在 `scripts/test-tooling.sh` 的合成 Fake 中先覆盖精确版本通过、同名不同版本失败、不同许可失败、开发例外进入生产失败，并观察 RED 后实现 GREEN。

`scripts/check-frontend-styles.sh`：扫描 `frontend/src/**/*.vue`，报告 `#[0-9a-fA-F]{3,6}\b` 颜色字面量与 `@media` 出现次数；`--strict` 时非零即 exit 1，默认只打印警告。

`scripts/report-frontend-bundle.sh`：解析 `pnpm --dir frontend build` 输出，打印首屏入口 JS 与 CSS 的 gzip 大小；`--budget` 时超出 `FRONTEND_JS_BUDGET_KB`/`FRONTEND_CSS_BUDGET_KB` 即 exit 1。本任务先记录基线并写入提交说明。

`justfiles/test.just` 的 `check` 增加 `bash scripts/check-frontend-licenses.sh` 与 `bash scripts/check-frontend-styles.sh`；`ci` 在 `pnpm --dir frontend build` 后增加 `bash scripts/report-frontend-bundle.sh`。`scripts/test-tooling.sh` 断言三个脚本存在、可执行，并断言 `check` recipe 包含许可证检查。

- [ ] **Step 6: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/design`

Expected: PASS。

Run: `pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend build`

Expected: PASS；记录构建输出中的 gzip 体积作为基线。

Run: `bash scripts/check-frontend-licenses.sh && bash scripts/check-frontend-styles.sh && bash scripts/test-tooling.sh`

Expected: 许可证检查 PASS；样式检查打印现有 12 种颜色与 5 个 `@media` 的警告但 exit 0；tooling 检查 PASS。

Run: `pnpm --dir frontend test:unit --run`

Expected: PASS，既有页面不受影响。

- [ ] **Step 7: 提交**

~~~bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/vite.config.ts frontend/tsconfig.app.json frontend/eslint.config.js frontend/index.html frontend/src/main.ts frontend/src/env.d.ts frontend/src/design frontend/src/test-support/renderWithPlugins.ts .gitignore scripts/check-frontend-licenses.sh scripts/check-frontend-styles.sh scripts/report-frontend-bundle.sh justfiles/test.just scripts/test-tooling.sh
git commit -m "feat: add PrimeVue and Tailwind design foundation"
~~~

---

### Task 2: 重建 AppShell、导航、系统告警与时间线抽屉

**Files:**
- Modify: `frontend/src/components/AppShell.vue`、`frontend/src/components/AppShell.spec.ts`
- Create: `frontend/src/components/AppNavigation.vue`、`frontend/src/components/SystemAlertBanner.vue`、`frontend/src/components/TimelineDrawer.vue`
- Create: `frontend/src/components/AppNavigation.spec.ts`、`frontend/src/components/SystemAlertBanner.spec.ts`、`frontend/src/components/TimelineDrawer.spec.ts`
- Modify: `frontend/e2e/reconnect.spec.ts`

- [ ] **Step 1: 把 AppShell 既有测试改为角色选择器并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/components/AppShell.spec.ts`

Expected: PASS。

- [ ] **Step 2: 写失败的骨架测试**

~~~typescript
it('marks the current route in the primary navigation', async () => {
  const { getByRole } = await renderWithPlugins(AppShell, { route: '/actions' })
  expect(getByRole('link', { name: '操作中心' })).toHaveAttribute('aria-current', 'page')
})

it('opens the navigation drawer from the menu button on narrow screens and returns focus on close', async () => {
  setViewport(600)
  const { getByRole, queryByRole } = await renderWithPlugins(AppShell)
  const trigger = getByRole('button', { name: '打开导航' })
  await fireEvent.click(trigger)
  await waitFor(() => expect(getByRole('dialog', { name: '主导航' })).toBeVisible())
  await fireEvent.keyDown(getByRole('dialog', { name: '主导航' }), { key: 'Escape', code: 'Escape' })
  await waitFor(() => expect(queryByRole('dialog', { name: '主导航' })).toBeNull())
  await waitFor(() => expect(trigger).toHaveFocus())
})

it('keeps the overdue brief alert as role=alert with the diagnostic link', async () => {
  vi.mocked(getSystemAlerts).mockResolvedValue({ alerts: [{ kind: 'brief_overdue', diagnostic_task_id: TASK_ID }] })
  const { findByRole } = await renderWithPlugins(AppShell)
  const alert = await findByRole('alert')
  expect(within(alert).getByRole('link', { name: '查看诊断任务' })).toHaveAttribute('href', `/tasks?task_id=${TASK_ID}`)
})

it('hides the global timeline column on /actions at every breakpoint', async () => {
  setViewport(1400)
  const { queryByRole } = await renderWithPlugins(AppShell, { route: '/actions' })
  expect(queryByRole('complementary', { name: '任务时间线' })).toBeNull()
})
~~~

`setViewport` 是 `renderWithPlugins.ts` 提供的 `matchMedia` stub 助手。

- [ ] **Step 3: 运行并观察预期失败**

Run: `pnpm --dir frontend test:unit --run src/components/AppShell.spec.ts src/components/AppNavigation.spec.ts src/components/SystemAlertBanner.spec.ts src/components/TimelineDrawer.spec.ts`

Expected: FAIL，新组件不存在且旧实现无 `aria-current`/抽屉。

- [ ] **Step 4: 实现骨架**

`AppNavigation.vue` 用 PrimeVue `Menu` 渲染六个既有入口，`RouterLink` 作为 item 模板，当前路由加 `aria-current="page"`；`< md` 时包裹在 `Drawer` 中，触发按钮 `aria-label="打开导航"`，关闭后焦点返回触发按钮。`SystemAlertBanner.vue` 用 `Message` 渲染加载（`severity="secondary"`、`role="status"`）、失败（`warn`、`role="alert"`）、逾期（`error`、`role="alert"`）三种状态，60 秒轮询与任务终态触发逻辑原样保留在 `AppShell.vue`。`TimelineDrawer.vue` 在 `≥ xl` 渲染为 `<aside aria-label="任务时间线">`，`md` 到 `< xl` 为可折叠 `Panel`，`< md` 为 `Drawer`。`AppShell.vue` 挂载 `Toast` 与 `ConfirmDialog` 出口，用 Tailwind grid 实现三档布局，删除全部 scoped CSS。

- [ ] **Step 5: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/components/AppShell.spec.ts src/components/AppNavigation.spec.ts src/components/SystemAlertBanner.spec.ts src/components/TimelineDrawer.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint`

Expected: PASS。

Run: `pnpm --dir frontend test:e2e e2e/reconnect.spec.ts`

Expected: PASS。

- [ ] **Step 6: 提交**

~~~bash
git add frontend/src/components/AppShell.vue frontend/src/components/AppShell.spec.ts frontend/src/components/AppNavigation.vue frontend/src/components/AppNavigation.spec.ts frontend/src/components/SystemAlertBanner.vue frontend/src/components/SystemAlertBanner.spec.ts frontend/src/components/TimelineDrawer.vue frontend/src/components/TimelineDrawer.spec.ts frontend/e2e/reconnect.spec.ts
git commit -m "feat: rebuild app shell with PrimeVue layout"
~~~

---

### Task 3: 建立基础展示组件

**Files:**
- Create: `frontend/src/components/EmptyState.vue`、`frontend/src/components/EmptyState.spec.ts`
- Create: `frontend/src/components/StatusTag.vue`、`frontend/src/components/StatusTag.spec.ts`
- Create: `frontend/src/components/ProblemMessage.vue`、`frontend/src/components/ProblemMessage.spec.ts`
- Modify: `frontend/src/components/SourceLink.vue`、`frontend/src/components/MarkdownMessage.vue`
- Modify: `frontend/src/components/BriefView.spec.ts`（仅选择器）

- [ ] **Step 1: 写失败的基础组件测试**

~~~typescript
it('renders an empty state with icon hidden from assistive technology and an optional action', async () => {
  const { getByText, getByRole, container } = await renderWithPlugins(EmptyState, { props: { title: '暂无草稿', actionLabel: '新建草稿' } })
  expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull()
  expect(getByRole('button', { name: '新建草稿' })).toBeEnabled()
})

it('renders a status tag with text and never relies on colour alone', async () => {
  const { getByText } = await renderWithPlugins(StatusTag, { props: { kind: 'task', value: 'reconciling' } })
  expect(getByText('核对中')).toBeVisible()
})

it('shows a problem with recovery action and trace id without stack or raw html', async () => {
  const problem = new ProblemError({ type: 'about:blank', title: 'Conflict', status: 409, detail: '<b>x</b>', instance: '', trace_id: 'trace-1', error_code: 'approval_version_conflict' })
  const { getByRole, getByText, queryByText } = await renderWithPlugins(ProblemMessage, { props: { problem, actionLabel: '重新加载' } })
  expect(getByRole('alert')).toBeVisible()
  expect(getByText('trace-1')).toBeVisible()
  expect(queryByText('x')).toBeNull()
  expect(getByRole('button', { name: '重新加载' })).toBeEnabled()
})

it('keeps sanitized markdown output and external link attributes', async () => {
  const { getByRole } = await renderWithPlugins(MarkdownMessage, { props: { content: '[a](https://example.test) <script>x</script>' } })
  expect(getByRole('link', { name: 'a' })).toHaveAttribute('rel', 'noopener noreferrer')
  expect(document.querySelector('script')).toBeNull()
})
~~~

- [ ] **Step 2: 运行并观察预期失败**

Run: `pnpm --dir frontend test:unit --run src/components/EmptyState.spec.ts src/components/StatusTag.spec.ts src/components/ProblemMessage.spec.ts`

Expected: FAIL，组件不存在。

- [ ] **Step 3: 实现基础组件**

`EmptyState.vue`：图标（`aria-hidden`）、标题、可选说明与 `Button`。`StatusTag.vue`：接收 `kind` 与状态值，通过 `design/status.ts` 得到 `severity` 与 label，渲染 `Tag`。`ProblemMessage.vue`：接收 `ProblemError`，渲染 `Message severity="error"` 加 `role="alert"`，文本只用 `title`/`error_code` 映射的中文说明与 `trace_id`，不渲染 `detail` 原文；可选恢复按钮。`SourceLink.vue` 改用 `Button link` 外观并保留 `rel`。`MarkdownMessage.vue` 只调整排版类，清洗逻辑不动。

- [ ] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/components && pnpm --dir frontend type-check && pnpm --dir frontend lint`

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add frontend/src/components/EmptyState.vue frontend/src/components/EmptyState.spec.ts frontend/src/components/StatusTag.vue frontend/src/components/StatusTag.spec.ts frontend/src/components/ProblemMessage.vue frontend/src/components/ProblemMessage.spec.ts frontend/src/components/SourceLink.vue frontend/src/components/MarkdownMessage.vue frontend/src/components/BriefView.spec.ts
git commit -m "feat: add shared PrimeVue presentation components"
~~~

---

### Task 4: 迁移登录页

**Files:**
- Modify: `frontend/src/pages/LoginPage.vue`
- Create: `frontend/src/pages/LoginPage.spec.ts`
- Modify: `frontend/src/components/ProblemMessage.vue`、`frontend/src/components/ProblemMessage.spec.ts`（最小受控本地说明接口及回归）
- Modify: `frontend/e2e/daily-brief.spec.ts`（登录步骤选择器）

- [x] **Step 1: 为登录页补齐角色选择器测试并在旧实现上确认通过**

真实旧实现的 401 文案为 `Invalid credentials`，尚无中文恢复说明；Step 1 保留该事实，Step 2 再增加中文恢复说明的失败断言。`ProblemMessage` 允许可选受控本地说明 Prop，仅接收固定或已映射文案，不透传任意服务端 title/detail；登录 401 保留已知原文并补充「请检查邮箱和密码后重试。」，非 ProblemError 保留「登录暂时不可用，请稍后重试。」。

写 `LoginPage.spec.ts` 覆盖：邮箱与密码有程序化 label、提交中禁用按钮、401 时 `role="alert"` 显示真实既有提示且不清空邮箱、成功后按 `redirect` 跳转。

Run: `pnpm --dir frontend test:unit --run src/pages/LoginPage.spec.ts`

Expected: PASS。

- [x] **Step 2: 增加迁移后才成立的断言并观察失败**

新增断言：密码输入框有「显示密码」切换按钮且 `aria-pressed` 正确；表单位于 `role="form"` 并有 `aria-labelledby`。

Run: `pnpm --dir frontend test:unit --run src/pages/LoginPage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 用 Card、InputText、Password、Button、Message 重建登录页**

`Password` 关闭 `feedback`，开启 `toggleMask`；错误使用 `ProblemMessage`；删除 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/LoginPage.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/daily-brief.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/pages/LoginPage.vue frontend/src/pages/LoginPage.spec.ts frontend/e2e/daily-brief.spec.ts frontend/src/components/ProblemMessage.vue frontend/src/components/ProblemMessage.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate login page to PrimeVue"
~~~

---

### Task 5: 迁移今日简报

**Files:**
- Modify: `frontend/src/pages/TodayBriefPage.vue`、`frontend/src/pages/TodayBriefPage.spec.ts`
- Modify: `frontend/src/components/BriefView.vue`、`frontend/src/components/BriefView.spec.ts`
- Modify: `frontend/e2e/daily-brief.spec.ts`

- [ ] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/TodayBriefPage.spec.ts src/components/BriefView.spec.ts`

Expected: PASS。

- [ ] **Step 2: 写失败的迁移断言**

覆盖：加载时 Skeleton 与 `role="status"` 文本并存；部分成功时 `Message warn` 列出缺失来源与最后同步时间；来源引用在 `Accordion` 内且键盘可展开；空简报显示 `EmptyState` 与「生成简报」按钮；生成中按钮 `loading` 禁用。

Run: `pnpm --dir frontend test:unit --run src/pages/TodayBriefPage.spec.ts src/components/BriefView.spec.ts`

Expected: FAIL。

- [ ] **Step 3: 用 Card、Tag、Skeleton、Message、Accordion 重建**

来源引用、数据截止时间、完整性文案原样保留；删除 scoped CSS。

- [ ] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/TodayBriefPage.spec.ts src/components/BriefView.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/daily-brief.spec.ts`

Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add frontend/src/pages/TodayBriefPage.vue frontend/src/pages/TodayBriefPage.spec.ts frontend/src/components/BriefView.vue frontend/src/components/BriefView.spec.ts frontend/e2e/daily-brief.spec.ts
git commit -m "feat: migrate daily brief to PrimeVue"
~~~

---

### Task 6: 迁移对话页

**Files:**
- Modify: `frontend/src/pages/ChatPage.vue`、`frontend/src/pages/ChatPage.spec.ts`

- [ ] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/ChatPage.spec.ts`

Expected: PASS。

- [ ] **Step 2: 写失败的迁移断言**

覆盖：输入框为 `Textarea autoResize` 且 Enter 发送、Shift+Enter 换行；发送中按钮 `loading`；现有 REST 最终消息、重连重读与实时任务持久状态正确呈现，不增加当前不存在的临时 delta 投影；断线时显示 `Message warn` 且不把任务标为失败；保留可信本地邮件/日程编辑器链接及打开行为，操作中心按钮仍只指向既有 `/actions`，不新增入口。登录与聊天提交逻辑保持，Enter 处理尊重 IME composition。

Run: `pnpm --dir frontend test:unit --run src/pages/ChatPage.spec.ts`

Expected: FAIL。

- [ ] **Step 3: 重建对话页**

`useTaskEvents` 与 Store 调用不动；删除 scoped CSS。

- [ ] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/ChatPage.spec.ts src/composables && pnpm --dir frontend type-check && pnpm --dir frontend lint`

Expected: PASS，`useTaskEvents.spec.ts` 未改动且通过。

- [ ] **Step 5: 提交**

~~~bash
git add frontend/src/pages/ChatPage.vue frontend/src/pages/ChatPage.spec.ts
git commit -m "feat: migrate chat page to PrimeVue"
~~~

---

### Task 7: 迁移任务历史与时间线

**前置条件：** 完整任务历史规格 `docs/superpowers/specs/2026-10-01-complete-task-history-design.md` 已获用户复核。先执行 `docs/superpowers/plans/2026-10-01-complete-task-history.md` 的 Task 1～7，交付受用户隔离的真实任务列表查询与前端列表状态；该补充计划 Task 8 同时执行本任务的展示迁移与集成，不重复实现。迁移前的前后端没有历史列表接口，禁止以单个选中任务或 Pinia 缓存替代完整历史。补充计划必须划清 TasksPage 与本任务的文件／验证归属；新增能力仅限补充规格第 8 节，其余行为层继续只读。

**实施交接（2026-10-01）：** 补充计划 Task 8 已完成真实列表与本任务的同一次展示迁移，提交主题为 `feat: migrate task history and timeline to PrimeVue`。本任务不再安排第二次迁移；独立审查已通过，2026-10-02补充基准 `9aa1b3b` 的完整CI及显式体积预算通过，详见[任务历史验收记录](../../releases/2026-10-01-task-history-evidence.md)。文档提交后的复验按补充计划独立留档，再从Task 8前置及Task 8～17接续。功能使用 PostgreSQL 摘要 API 与独立列表状态，摘要不写入完整 Task Store；取消、重试幂等及恢复提案审批保持原契约。最小范围补充为 AppShell 在 `/tasks` 排除全局时间线副本／第三布局列，其他页面保留原抽屉。

验证：旧 11 项组件安全网和 10 项相关 E2E 先绿色，新列表／接线／Timeline／AppShell／分页焦点均有 RED；最终 `just check` 通过（后端 2725、前端 41 文件 450 测试），相关 E2E 12 项通过，含真实认证与既有 POST 创建 25 条未访问任务后跨页找回。恢复提案原精确文案、审批及写入断言未降低；仅登记新增 GET `/tasks` 的精确参数契约，并允许合法可见性探测。

Live region：TasksPage 的 3 status／2 alert、TaskTimeline 的 1 status／2 alert 静态语义与旧文案全部保留，改由显式 role 的 Message 承载；TaskHistoryList 新增 2 status（加载／新任务）及 1 alert（安全错误）。DataView／Timeline 不新增播报；任务页移除外壳重复时间线，避免同一错误或空提示重复播报。角色运行时、窄屏焦点、旧 SSE 重放及恢复入口均有本轮测试证据。运行证据与最终修复见[任务历史验收记录](../../releases/2026-10-01-task-history-evidence.md)；未将此次 UI 集成称为 M2.1 发布验收。

**Files:**
- Modify: `frontend/src/pages/TasksPage.vue`、`frontend/src/pages/TasksPage.spec.ts`
- Modify: `frontend/src/components/TaskTimeline.vue`、`frontend/src/components/TaskTimeline.spec.ts`
- 同次集成补充文件由完整任务历史计划 Task 8 列出，包含 TaskHistoryList、AppShell 去重与必要 E2E 契约 fixture。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/TasksPage.spec.ts src/components/TaskTimeline.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：任务列表为 `DataView` 且每项状态用 `StatusTag`；时间线为 `Timeline`，每个事件文案与旧实现完全一致（用既有 fixture 逐条比对）；重试按钮在非终态禁用；空列表 `EmptyState`；SSE 重放测试不改动。

Run: `pnpm --dir frontend test:unit --run src/pages/TasksPage.spec.ts src/components/TaskTimeline.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建**

删除两者的 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/TasksPage.spec.ts src/components/TaskTimeline.spec.ts src/stores/tasks.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/pages/TasksPage.vue frontend/src/pages/TasksPage.spec.ts frontend/src/components/TaskTimeline.vue frontend/src/components/TaskTimeline.spec.ts
git commit -m "feat: migrate task history and timeline to PrimeVue"
~~~

---

### Task 8 前置独立修复：异步确认展示端口

实施完成：`70aedc0`，新增8项确认边界回归及原页面11项、连接E2E4项通过，`just check`后端2730／前端466通过。后续页面只消费该端口。

**Files:**
- Modify: `frontend/src/features/connections/useConnections.ts`
- Create: `frontend/src/features/connections/useConnections.spec.ts`

先写并运行失败回归：异步确认未完成或被取消时零断开请求；等待确认与执行期间忙碌互斥；确认后只调用一次 `disconnectConnection(connection.id)`，按既有规则 `catalog.load()`，失败及卸载保护不变。逐字保留「确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。」。

最小实现为可注入并 await 的异步确认回调，默认保留既有确认语义；不全局替换 `window.confirm`，不在页面复制 API 请求路径。该端口不改变外部写入和审批规则。Task 8 页面通过 `ConfirmDialog` 提供回调，只有一次确认。

Run: `pnpm --dir frontend test:unit --run src/features/connections/useConnections.spec.ts src/pages/ConnectionsPage.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/connections.spec.ts`

读取完整输出，通过 `just check` 并审阅 diff 后独立提交：

~~~bash
git add frontend/src/features/connections/useConnections.ts frontend/src/features/connections/useConnections.spec.ts
git commit -m "fix: inject async connection disconnect confirmation"
~~~

### Task 8: 迁移连接页与能力行

实施验证：原11项组件安全网及28项相关E2E先绿色；迁移、显式IANA、确认焦点及Message默认播报各有RED。最终聚焦44项、相关E2E33项通过；just check后端2730／前端477项通过，类型、lint、生产构建和体积预算通过。JS gzip333429B，相对基线增加193056B，CSS9026B，仍在既定预算内。

Live region：页面原4 status／2 alert、能力行原1 status／0 alert数量保持。加载、空态、通知用Message并显式polite；两类错误仍alert/assertive；原连接状态文案由sr-only保留，可见中文StatusTag不重复播报。能力状态沿用原文案，新降级Message为note/off，状态行已播报“暂不可用”。真实DOM测试覆盖加载、空、错误恢复、部分失败、授权通知、降级；ConfirmDialog唯一出口、Esc焦点、禁用／移除触发器回退均有运行证据。

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage.vue`、`frontend/src/pages/ConnectionsPage.spec.ts`
- Modify: `frontend/src/components/CapabilityRows.vue`
- Modify（确认框退出后的背景隔离解除与焦点恢复）: `frontend/src/components/AppShell.vue`、`frontend/src/components/AppShell.spec.ts`
- Modify: `frontend/e2e/connections.spec.ts`
- Modify（仅连接页语义选择器）: `frontend/e2e/action-workspace.spec.ts`、`frontend/e2e/m2-actions.spec.ts`

执行口径核对：旧页面的“继续授权”是已验证URL链接，管理员同意是说明文字和原重新授权入口，不能为迁移增加管理员权限动作。保持这些控件和原文；测试定位只用角色、名称及自有 `data-testid`，跨页面用例的HTTP／审批／未知请求断言不变。原 `data-connection-id`／`data-capability` 可在迁移前补等价自有测试标识，先证明旧UI语义安全网。

确认焦点回归：PrimeVue 4.5.5 的 `hide` 在退出动画开始时触发，异步取消仍可能尚未解除触发按钮禁用。AppShell 在 `after-hide` 后移除背景 `inert` 并等待 DOM 更新，再向有效触发按钮归还焦点；按钮已禁用或移除时聚焦可程序化聚焦的主内容。保持退出期间背景隔离，不延时轮询、不改变断开请求逻辑。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/ConnectionsPage.spec.ts && pnpm --dir frontend test:e2e e2e/connections.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：每个连接为 `Card`，能力为 `DataTable` 且列头有 `scope`；`ToggleSwitch` 有程序化 label 且切换中禁用；保留独立启用／重新授权按钮、继续授权链接及管理员同意说明，操作可用性沿用服务端状态；能力降级用 `Message warn`；仅保留既有去设置入口（默认账户/日历 `Select` 属于 Task 9）；断开连接走 `ConfirmDialog` 且焦点返回。

Run: `pnpm --dir frontend test:unit --run src/pages/ConnectionsPage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建**

`useConnectionCatalog` 不动；删除 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/ConnectionsPage.spec.ts src/components/AppShell.spec.ts src/features/connections/useConnections.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/connections.spec.ts e2e/action-workspace.spec.ts e2e/m2-actions.spec.ts e2e/reconnect.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/pages/ConnectionsPage.vue frontend/src/pages/ConnectionsPage.spec.ts frontend/src/components/CapabilityRows.vue frontend/src/components/AppShell.vue frontend/src/components/AppShell.spec.ts frontend/e2e/connections.spec.ts frontend/e2e/action-workspace.spec.ts frontend/e2e/m2-actions.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate connections page to PrimeVue"
~~~

---

### Task 9 前置独立修复：最小错误码元数据（2026-10-03 用户已批准）

**Files:**
- Modify: `frontend/src/features/actions/recovery.ts`
- Modify: `frontend/src/features/calendar/useCalendarProposalEditor.ts`（仅两处特殊恢复对象保留元数据）
- Create: `frontend/src/features/actions/recovery.spec.ts`
- Modify: `frontend/src/pages/CalendarProposalPage.spec.ts`（真实错误投影及原恢复边界回归）

现有设置／邮件／日程逻辑已在内部将 `ProblemError` 转为 `ActionRecovery`，UI 无法读取原始错误码。给该安全恢复对象增加可选只读 `problem?: Readonly<Pick<ProblemDetails, 'error_code'>>`，仅复制真实 `ProblemError` 的码；不传 title/detail/status 或完整响应。原 message/action/traceId、普通异常和 EditorInputError 的处理不变。日程两处特殊对象保留元数据，同时逐字保留“提案事实尚未读取完整，请重新加载后继续编辑或提交。”和“候选已保存，请重新加载最新提案后继续编辑。”。

先写失败回归证明原码在两条路径丢失；覆盖真实码最小复制、原文案／动作／追踪编号不变、普通错误无伪造投影及特殊恢复、版本锁、迟到响应与卸载保护。仅展示元数据，不改请求、意图、epoch、refreshRequired 或最低版本。运行相关页面／动作／恢复测试、类型、lint、相关 E2E 和 `just check`，独立提交 `fix: preserve minimal problem codes for form presentation` 并审查。Task 9／11／12 随后把该真实投影接入表单映射，禁止从中文消息反推错误码或复制 API 请求。

- [x] **实现与回归（2026-10-04）**：RED 10 项因缺少错误码失败，GREEN 43 项通过；相关页面与恢复回归 101 项通过；类型、lint、相关 E2E 17 项及 `just check`（后端 2730 项、前端 496 项）通过。两个生产文件以外的行为层未修改，Form 接线仍按后续 Task 9／11／12 执行。

### Task 9: 迁移设置页并建立表单校验与服务端错误映射

**Files:**
- Create: `frontend/src/features/forms/problemFields.ts`、`frontend/src/features/forms/problemFields.spec.ts`
- Create: `frontend/src/features/settings/schema.ts`、`frontend/src/features/settings/schema.spec.ts`
- Modify: `frontend/src/pages/SettingsPage.vue`、`frontend/src/pages/SettingsPage.spec.ts`
- Modify: `frontend/src/components/WorkSettingsForm.vue`、`frontend/src/components/WorkingHoursFields.vue`
- Create: `frontend/src/components/WorkSettingsForm.spec.ts`、`frontend/src/components/SettingsTimeInput.vue`（timeOnly 墙上时间适配）、`frontend/e2e/settings.spec.ts`
- Modify: `frontend/src/components/AppShell.vue`、`frontend/src/components/AppShell.spec.ts`（仅复用页面 `modal-change` 展示通知与背景隔离／焦点回归）
- Verify: `frontend/e2e/daily-brief.spec.ts`（原语义选择器可继续使用，无需修改）
- Modify: `frontend/e2e/action-workspace.spec.ts`（设置段语义选择器与原业务断言）

2026-10-04 展示细化：设置页异步加载现有 `WorkSettingsForm`，Form/zod/重控件保留于该边界内，并提供加载、失败、重试（浏览器缓存失败模块，明确整页重载重试）；不改路由配置及预算。七日字段作为完整 tuple 映射注册，增删与重载同步 Form；格式校验仅在提交时更新，避免 blur 错误提示重排导致按钮点击落空；每日 `ToggleSwitch` 仅派生空／非空，开启空日插入空区间要求明确填写。简报时间只无损归一零秒 `HH:mm:00`，非零秒／小数秒明确提示重新选择，不静默截断。页面注册未保存离开提示，使用全局 `ConfirmDialog`；删除确认 `Dialog` 通过既有 `modal-change` 模式通知 AppShell，不引入新 Store。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/SettingsPage.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的 schema 与错误映射测试**

测试使用既有合成 `UserSettings` fixture 和完整 `working_hours`（monday 至 sunday，每日零到多个 `[start, end]` 字符串二元组）：逐日逐区间构造结束不晚于开始的失败用例，同时覆盖合法多区间与空日。不得引入 `{ start, end }`、`work_start`/`work_end` 第二套字段。

时区测试覆盖本地 `Intl.supportedValuesOf('timeZone')` 加 `UTC`、服务端当前值，以及未出现在有限 fallback 但显式合法的 IANA；不读取不存在的服务端列表，也不按宿主时区自动选择。

错误映射测试使用现有七字段 `ProblemDetails` fixture 的最小码投影：逐个已知 `error_code` 映射安全中文表单级错误，未知码（包括继承属性名）得到安全 fallback；改变 `title`/`detail` 不影响映射，也不显示原文。真实页面从前置修复的 `ActionRecovery.problem` 接线，保留恢复动作、追踪编号及必要上下文原文。字段格式错误由 zod 提供；不制造 `errors` 数组或扩展 API 解析。

- [x] **Step 3: 运行并观察预期失败**

Run: `pnpm --dir frontend test:unit --run src/features/forms src/features/settings`

Expected: FAIL，模块不存在。

- [x] **Step 4: 实现 schema、映射与设置页**

`problemFields.ts` 导出 `problemToFormError` 与已知 `error_code` 到安全中文表单级说明的冻结映射表；`settings/schema.ts` 导出 zod schema 与 `zodResolver`。`WorkSettingsForm.vue` 改用 `Form`，字段用 `Select filter`（时区）、`DatePicker timeOnly`（工作时间）、`InputNumber`（会议缓冲）、`ToggleSwitch`；默认发送账户、默认日历账户与默认日历使用 `Select`，保留失效原值与明确重选提示；七日多区间模型与提交参数不变；提交中 `loading`；409 显示 `EditorRecovery` 风格恢复动作且不清空输入。「删除全部数据」入口沿用既有 `DELETE ALL DATA` 输入确认，改为 `Dialog modal`，说明文案不变。删除 scoped CSS。

- [x] **Step 5: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/features/forms src/features/settings src/pages/SettingsPage.spec.ts src/api/privacy.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint`

Expected: PASS。

- [x] **Step 6: 提交**

~~~bash
git add frontend/src/features/forms frontend/src/features/settings/schema.ts frontend/src/features/settings/schema.spec.ts frontend/src/pages/SettingsPage.vue frontend/src/pages/SettingsPage.spec.ts frontend/src/components/WorkSettingsForm.vue frontend/src/components/WorkSettingsForm.spec.ts frontend/src/components/WorkingHoursFields.vue frontend/src/components/SettingsTimeInput.vue frontend/src/components/AppShell.vue frontend/src/components/AppShell.spec.ts frontend/e2e/settings.spec.ts frontend/e2e/action-workspace.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate settings to PrimeVue forms"
~~~

2026-10-04 实施证据：旧页选择器／完整 fixture 基线单测 12 项、相关 E2E 9 项通过；schema／映射及真实 Form 已观察 RED 后实现。最终聚焦 66 项、相关 E2E 16 项、`just check`（后端 2730、前端 532）、生产构建及体积预算通过。入口 JS gzip 332902 B（比基线增加 192529 B），CSS 8959 B；异步表单 59010 B。生产浏览器 `/brief` 仅加载入口，进入 `/settings` 才加载表单 chunk；加载失败重载重试、删除 Dialog 焦点陷阱／Esc／返回、未保存取消／离开、409 草稿保留及两种宿主时区已实测。完整里程碑 CI 仍在 Task 17。

Live region 同提交记录：WorkSettingsForm 原 3 个 status 场景（设置加载、目录加载、已保存）保持原文并显式 polite，原 3 个 alert 场景（目录失败、validation、恢复错误）保持；Message 内置 alert/assertive 承接原 role，真实错误码说明与原文相同只渲染一次。SettingsPage 原会话／删除错误两个 alert 场景保持，删除错误在页面与 Dialog 间互斥呈现。新增异步模块加载 status、失败 alert、字段格式错误 alert、删除 Dialog 与未保存 ConfirmDialog 场景；DatePicker 为新增控件浮层语义。运行时单测与浏览器校验上述实际状态，无空 role 补数。

---

### Task 10: 迁移操作中心与详情

**Files:**
- Create: `frontend/src/components/ActionCenterWorkspace.vue`（Task 10 展示组合的真实异步边界；页面宿主保留加载、失败与完整重载重试，行为层不变）
- Modify: `frontend/src/pages/ActionsPage.vue`、`frontend/src/pages/ActionsPage.spec.ts`
- Modify: `frontend/src/components/ActionDetail.vue`、`frontend/src/components/LinkedActionPanel.vue`
- Modify: `frontend/e2e/actions.spec.ts`、`frontend/e2e/action-workspace.spec.ts`、`frontend/e2e/m2-actions.spec.ts`

Live region 同提交记录：原 ActionsPage 的创建／列表／连接／快照四个 status 与列表／快照错误两个 alert 原文移入唯一 ActionCenterWorkspace；ActionDetail 的核对／内容到期／缺失来源三个 status 与动作错误一个 alert、LinkedActionPanel 的关联加载／连接恢复两个 status 与读取失败／归属不匹配两个 alert 均用 Message 原文承载。原九个 status、五个 alert 的业务场景保持，页面宿主仅增加互斥的模块加载 status／失败 alert；StatusTag、历史到期 Tag、Panel 与 Timeline 不增加 live region。详情与既有 ApprovalCard 同时显示的两条到期公告沿用原基线，未凑数或删减；运行时验证两处仍各一条。

展示适配记录：六个 Tabs 只切当前页分组；Paginator 使用公开 headless slot 的 first（1-based）／rows 映射旧 offset／limit，不提供 totalRecords，不显示总页数，失败后重试仍读取同一偏移。Splitter 保留单一工作区和详情实例，按 Tailwind 默认 xl/md 断点改变布局；DataTable 使用原类型＋ID稳定键以保留刷新重排后的焦点。异步展示边界只在操作中心下载，加载失败明确完整重载重试；行为层与路由仍只读。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/ActionsPage.spec.ts && pnpm --dir frontend test:e2e e2e/actions.spec.ts e2e/action-workspace.spec.ts e2e/m2-actions.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：六个分组为 `Tabs`，Tab 键盘可切换且 `aria-selected` 正确；列表为 `DataTable`，列头 `scope="col"`，状态列用 `StatusTag`；供应商/类型/状态筛选为带 label 的 `Select`，变更后调用的 API 参数与旧实现完全一致（用 `vi.mocked(listActions).mock.calls` 比对）；`Paginator` 的 `first`/`rows` 仅在展示层映射为既有 `offset`/`limit`，API 参数不变；详情在 `≥ xl` 为 `Splitter` 右栏、`< md` 为独立区域；内容到期时显示「内容已过期」历史状态；聚焦与重连后重读快照的既有测试不动；零浏览器存储断言保留。

Run: `pnpm --dir frontend test:unit --run src/pages/ActionsPage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建**

`useActionCenter`、`useActionControls`、`stores/actions.ts` 不动；删除 168 行 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/ActionsPage.spec.ts src/stores/actions.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/actions.spec.ts e2e/action-workspace.spec.ts e2e/m2-actions.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/components/ActionCenterWorkspace.vue frontend/src/pages/ActionsPage.vue frontend/src/pages/ActionsPage.spec.ts frontend/src/components/ActionDetail.vue frontend/src/components/LinkedActionPanel.vue frontend/e2e/actions.spec.ts frontend/e2e/action-workspace.spec.ts frontend/e2e/m2-actions.spec.ts
git commit -m "feat: migrate action center to PrimeVue"
~~~

---

### Task 11: 迁移邮件编辑器

**Files:**
- Create: `frontend/src/components/MailEditorForm.vue`（真实异步 Form 展示边界；原 hook 留在页面）
- Modify: `frontend/src/components/ProblemMessage.vue`、`frontend/src/components/ProblemMessage.spec.ts`（受控本地 description-only 模式，无伪造错误码）
- Modify: `frontend/src/pages/CalendarProposalPage.spec.ts`、`frontend/src/pages/CalendarRepreparePage.spec.ts`（共享 Select 的最小角色选择器／PrimeVue 插件适配）
- Create: `frontend/src/features/mail/schema.ts`、`frontend/src/features/mail/schema.spec.ts`
- Modify: `frontend/src/pages/MailDraftPage.vue`、`frontend/src/pages/MailDraftPage.spec.ts`
- Modify: `frontend/src/components/MailDraftFields.vue`、`frontend/src/components/LocalEditorFrame.vue`、`frontend/src/components/EditorConnectionSelect.vue`、`frontend/src/components/EditorRecovery.vue`
- Modify: `frontend/e2e/mail-editor.spec.ts`、`frontend/e2e/calendar-editor.spec.ts`（后者仅适配 Task 10 后共享详情的旧 class 选择器）

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/MailDraftPage.spec.ts && pnpm --dir frontend test:e2e e2e/mail-editor.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的 schema 与迁移断言**

schema 只校验：To 至少一个格式合法的地址、CC/BCC 每项格式合法、主题非空且长度上限、正文非空。断言：收件人为 `AutoComplete multiple`，建议只来自当前草稿已有收件人且不发起任何请求；回复/全部回复时线程与收件人锁定字段只读并有说明；「生成正文」按钮在生成中 `loading`，失败显示 `ProblemMessage`；保存冲突显示 `EditorRecovery` 且不清空输入；提交审批前显示收件人数与不可撤销提示；页面不含任何附件、格式或富文本控件（断言 `queryByRole('toolbar')` 为空、无 `input[type=file]`）；零浏览器存储。

Run: `pnpm --dir frontend test:unit --run src/features/mail src/pages/MailDraftPage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建**

`features/mail` 既有逻辑、`useLocalActionCreation`、`api/mail.ts` 不动；删除 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/features/mail src/pages/MailDraftPage.spec.ts src/components && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/mail-editor.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/e2e/calendar-editor.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md frontend/src/components/MailEditorForm.vue frontend/src/components/ProblemMessage.vue frontend/src/components/ProblemMessage.spec.ts frontend/src/pages/CalendarProposalPage.spec.ts frontend/src/pages/CalendarRepreparePage.spec.ts frontend/src/features/mail/schema.ts frontend/src/features/mail/schema.spec.ts frontend/src/pages/MailDraftPage.vue frontend/src/pages/MailDraftPage.spec.ts frontend/src/components/MailDraftFields.vue frontend/src/components/LocalEditorFrame.vue frontend/src/components/EditorConnectionSelect.vue frontend/src/components/EditorRecovery.vue frontend/e2e/mail-editor.spec.ts
git commit -m "feat: migrate mail editor to PrimeVue forms"
~~~

---

### Task 12: 迁移日程编辑器

**Files:**
- Create（Task12 审查修复）：`frontend/src/components/CalendarWallTimeFields.vue`、`frontend/src/components/CalendarWallTimeFields.spec.ts`（公开 footer 中的墙上时分秒校验／按钮交互）；规格 §6／7 已同步限定替代原生 clock，保留 showTime 日期弹层。

- Create: `frontend/src/components/CalendarEditorForm.vue`（异步 Form/zod 展示边界）、`frontend/src/components/CalendarDateTimeInput.vue`（日期／墙上时间控件适配）、`frontend/src/pages/CalendarPresentation.spec.ts`（真实展示交互与格式验证）
- Modify: `frontend/src/pages/CalendarRepreparePage.spec.ts`（仅共享输入选择器兼容）
- Modify: `frontend/src/components/CalendarApprovalPreview.vue`（仅向冲突展示传冻结 `after.timezone`，不改 Task 14 其余展示）；冲突保留完整原 ISO，另附显式 IANA 可读范围。
- Validate: `frontend/e2e/action-workspace.spec.ts`、`frontend/e2e/calendar-reprepare.spec.ts`、`frontend/e2e/m2-actions.spec.ts`（共享字段、冲突／缺失来源组件及冻结审批消费者）；保留原业务、安全与存储断言。

- Create: `frontend/src/features/calendar/schema.ts`、`frontend/src/features/calendar/schema.spec.ts`
- Modify: `frontend/src/pages/CalendarProposalPage.vue`、`frontend/src/pages/CalendarProposalPage.spec.ts`
- Modify: `frontend/src/components/CalendarProposalFields.vue`、`frontend/src/components/CalendarTargetFields.vue`、`frontend/src/components/CalendarConflictNotice.vue`、`frontend/src/components/MissingCalendarConnections.vue`
- Modify: `frontend/e2e/calendar-editor.spec.ts`、`frontend/e2e/calendar-restore.spec.ts`

**Task 12 同提交运行时记录（2026-10-04）：**

- 原页面加载、恢复来源缺失、未保存、来源未确认、冲突未检查、候选部分来源六个 `status` 及原快照不可用 `alert` 逐字保留；其中未保存／来源未确认迁入异步 Form。`EditorRecovery` 仍只有一个实例承担当前错误、原动作与追踪号，真实错误码映射补在同一公告内。
- 异步表单新增互斥的加载 `status`／失败 `alert`；zod 最多五个字段错误 `alert`。未保存离开复用外壳 `ConfirmDialog`，不重复挂载。
- 冲突组件的命名 section 原本已形成 region；现在以单一 `Message warn` 显式保持 `region` + `aria-live=polite`。缺失连接 `Message info` 使用 `role=presentation` + `aria-live=off`，不在父公告中重复播报。完整原 ISO 及文案保留，附加明确 IANA 可读范围；冻结审批只传冻结时区。
- DatePicker 的内置日历公告保留；锚定浮层明确 `aria-modal=false`，与允许在原输入继续手输的真实交互一致。公开 PT 将 `aria-selected` 从普通 span 移到 gridcell，并提供日期按钮与选中／今日键盘入口；Esc 回原输入、键盘选日以及弹层 axe 均经实际 dist 验证。
- 新鲜验证：`just check`（后端 2730、前端 592 项）、五组相关 E2E 36 项、纽约宿主格式／展示 33 项全部通过；实际 dist 三视口（1440／1024／390）、弹层及错误态 axe serious/critical 为零。首屏 JS gzip 328486 B、CSS gzip 8656 B，预算通过。本任务未执行完整 `just ci`，最终门禁仍归 Task 16／17。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/CalendarProposalPage.spec.ts && pnpm --dir frontend test:e2e e2e/calendar-editor.spec.ts e2e/calendar-restore.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的 schema 与迁移断言**

schema 只校验：标题非空、开始/结束为合法时间且结束晚于开始、时区为显式合法 IANA（本地 Intl 列表 + UTC，保留服务端当前值，不以有限 fallback 排除合法值）、参会人地址格式。断言：全天字段用日期分支，定时字段用 `DatePicker showTime` 墙上时间分支；沿用既有全天日期转换与 `features/calendar/time.ts`、DST 歧义拒绝，提交值仍是既有日期或 ISO 格式与 IANA 时区（比对 API mock 参数），不得直接 `Date.toISOString()` 推断宿主机时区；时区 `Select` 明示且无自动推断；修改任一时间字段后旧冲突结果被清除并重新计算（既有逻辑，只改断言选择器）；冲突提示 `Message warn` 最多三个候选时间按钮，选择后回填字段；工作时间外警告可见；参会人 `Chip` 列表；无删除/取消/重复日程/会议链接控件；ETag 冲突显示恢复动作。

Run: `pnpm --dir frontend test:unit --run src/features/calendar src/pages/CalendarProposalPage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建**

`api/calendar.ts`、`api/calendarFields.ts`、`features/calendar` 既有逻辑不动；删除 scoped CSS。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/features/calendar src/pages/CalendarProposalPage.spec.ts src/api/calendarEditors.spec.ts src/api/calendarRestore.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/calendar-editor.spec.ts e2e/calendar-restore.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/features/calendar/schema.ts frontend/src/features/calendar/schema.spec.ts frontend/src/pages/CalendarProposalPage.vue frontend/src/pages/CalendarProposalPage.spec.ts frontend/src/components/CalendarProposalFields.vue frontend/src/components/CalendarTargetFields.vue frontend/src/components/CalendarConflictNotice.vue frontend/src/components/MissingCalendarConnections.vue frontend/e2e/calendar-editor.spec.ts frontend/e2e/calendar-restore.spec.ts frontend/src/components/CalendarEditorForm.vue frontend/src/components/CalendarDateTimeInput.vue frontend/src/pages/CalendarPresentation.spec.ts frontend/src/pages/CalendarRepreparePage.spec.ts frontend/src/components/CalendarApprovalPreview.vue docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate calendar editor to PrimeVue forms"
~~~

---

### Task 13: 迁移日程重新准备与字段对比

**Files:**
- Modify: `frontend/src/components/CalendarRepreparePanel.vue`、`frontend/src/components/CalendarFieldsComparison.vue`
- Modify: `frontend/src/pages/CalendarProposalPage.vue`、`frontend/src/pages/CalendarRepreparePage.spec.ts`
- Create: `frontend/src/components/CalendarFieldsComparison.spec.ts`
- Modify: `frontend/e2e/calendar-reprepare.spec.ts`、`frontend/e2e/calendar-editor.spec.ts`

**展示层接线补充（2026-10-05）：** 页面三处重新准备绑定共用同一 UI ConfirmDialog 回调；只在用户确认且原提案／来源仍有效时调用原 hook。取消、Esc、等待互斥、路由切换及卸载清理仅属于展示交互，不改行为层。共享字段表格同时被编辑器和冻结审批预览消费，旧实现及迁移后补跑 `calendar-editor`、`calendar-restore`、`provider-errors` 和 `m2-actions` 日程相关 E2E；只适配必要语义选择器，不更改请求次数或幂等断言。

**Task 13 同提交运行时记录（2026-10-05）：**

- CalendarRepreparePanel 原三个条件 `status`（来源要求同步、同步任务进度、来源不可核实）均由 Message 原文承载，并显式 `aria-live="polite"`；进行中／成功／未完成仍按原任务事实选择文字，没有新增默认 assertive 副本。Panel 内置 region 取代原 section，公开 header slot id 维持唯一名称“重新准备修改提案”，不增加 live region。
- CalendarFieldsComparison 不增加 live region；DataTable 保留日程修改前后／拟创建的日程名称、列头 scope 与纯文本。修改有原值／新值列，创建无伪造原值；字段行增加可见“已变化／未变化／拟创建”及 aria-label。空值原“无”附“（未填写）”，原语和数组元素保真比较，不让真实文字“无”与空值占位碰撞，不写回业务 diff。
- 页面三处恢复入口复用 AppShell 已有 ConfirmDialog 的单一 `alertdialog`，仅用户点击时出现；不复制审批警告或业务状态公告。取消／Esc 零创建、等待互斥、旧回调／路由／源版本失效、全局背景 inert 与焦点归还均由运行时回归验证。原审批、同步、刷新、ETag 和未知结果的请求意图不变。

**本任务验证：** 旧实现语义化单测 15 项、相关 E2E 49 项通过；迁移后聚焦 116 项、完整前端单测 628 项、相关 E2E 51 项通过，`just check` 通过。最终构建首屏 JS gzip 329,922 B（上限 340,373 B），CSS gzip 8,582 B，预算通过。完整 CI 和剩余旧样式门禁按 Task 16／17 执行。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/pages/CalendarRepreparePage.spec.ts && pnpm --dir frontend test:e2e e2e/calendar-reprepare.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：字段对比为 `DataTable`，列为「字段 / 原值 / 新值」，变化行有 `aria-label` 说明差异且不只靠颜色；重新准备走 `ConfirmDialog`；版本冲突 `calendar_event_version_conflict` 显示 `recovery=new_version` 入口且需再次显式操作。

Run: `pnpm --dir frontend test:unit --run src/pages/CalendarRepreparePage.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建并删除 scoped CSS**

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/pages/CalendarRepreparePage.spec.ts src/api/calendarReprepare.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/calendar-reprepare.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/components/CalendarRepreparePanel.vue frontend/src/components/CalendarFieldsComparison.vue frontend/src/components/CalendarFieldsComparison.spec.ts frontend/src/pages/CalendarProposalPage.vue frontend/src/pages/CalendarRepreparePage.spec.ts frontend/e2e/calendar-reprepare.spec.ts frontend/e2e/calendar-editor.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate calendar reprepare to PrimeVue"
~~~

---

### Task 14: 迁移审批预览、审批卡片与确认对话框

**Files:**
- Modify: `frontend/src/components/ApprovalCard.vue`、`frontend/src/components/ApprovalCard.spec.ts`
- Modify: `frontend/src/components/MailApprovalPreview.vue`、`frontend/src/components/CalendarApprovalPreview.vue`
- Modify: `frontend/src/components/ActionConfirmationDialog.vue`
- Create: `frontend/src/components/ActionConfirmationDialog.spec.ts`
- Modify: `frontend/e2e/provider-errors.spec.ts`
- Modify: `frontend/src/components/AppShell.vue`、`frontend/src/components/AppShell.spec.ts`、`frontend/src/components/NeedsAttentionPanel.spec.ts`
- Create: `frontend/src/components/actionDialogContext.ts`
- Modify: 本计划 Task 14 的文件范围、live region 与验证记录
- Modify: `frontend/e2e/m2-actions.spec.ts`、`frontend/e2e/mail-editor.spec.ts`

**Task 14 共享消费者预检补充：** 深层 `ActionConfirmationDialog` 通过类型化 UI 注入端口通知 AppShell 隔离背景；每次打开持有独立释放凭据，退出动画后或卸载时幂等释放，旧实例不得解除新模态。只扩展 AppShell 展示层和对应测试，不新增 Store 或修改 NeedsAttentionPanel 行为。其既有测试迁移语义查询与真实 Dialog。旧／新 E2E 扩充 `m2-actions`、`mail-editor`、`calendar-editor`、`calendar-restore`、`calendar-reprepare`、`daily-brief`，保留所有请求／版本／副作用断言；仅有新增断言的文件提交。live region 逐项变化与运行时证据在本任务完成记录同步。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/components/ApprovalCard.spec.ts && pnpm --dir frontend test:e2e e2e/provider-errors.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：风险等级 `Tag`；载荷字段为 `DataTable` 且正文以纯文本 `<p>`/`<pre>` 之外的方式逐字渲染（既有「不含 `img`/`pre`、Markdown 不解析」断言保留）；过期倒计时在 `role="status"` 内；批准/拒绝期间两个按钮均 `loading` 禁用；409 后显示恢复动作且既有 `recovery=new_version` 链接断言通过；确认对话框为 `Dialog modal`，打开后焦点在首个按钮，Esc 关闭，关闭后焦点返回触发按钮，背景 `inert`；能力撤销、哈希冲突、版本变化各显示对应恢复动作。

Run: `pnpm --dir frontend test:unit --run src/components/ApprovalCard.spec.ts src/components/ActionConfirmationDialog.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建并删除 scoped CSS**

`api/approvals.ts`、`api/approvalPreviews.ts` 不动。

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/components && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/provider-errors.spec.ts e2e/m2-actions.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/components/ApprovalCard.vue frontend/src/components/ApprovalCard.spec.ts frontend/src/components/MailApprovalPreview.vue frontend/src/components/CalendarApprovalPreview.vue frontend/src/components/ActionConfirmationDialog.vue frontend/src/components/ActionConfirmationDialog.spec.ts frontend/e2e/provider-errors.spec.ts frontend/src/components/AppShell.vue frontend/src/components/AppShell.spec.ts frontend/src/components/actionDialogContext.ts frontend/src/components/NeedsAttentionPanel.spec.ts frontend/e2e/m2-actions.spec.ts frontend/e2e/mail-editor.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate approval previews to PrimeVue"
~~~

**Task 14 完成记录（2026-10-05）：**

- 旧实现先完成 ApprovalCard／NeedsAttentionPanel 单测语义查询迁移（15 项通过），相关七文件 E2E 59 项通过后才改消费源码；迁移 RED 为 8 项预期失败、30 项通过。
- 风险以 Tag 展示，M1 legacy 与 M2 冻结载荷保留各自 Props；邮件正文逐字纯文本，日程复用原冻结前后值、时区、通知、ETag 与恢复信息。批准／拒绝均 loading＋disabled，原决定、哈希、版本、409 锁与恢复路由未修改。删除审批错误和邮件主题原可被角色／文本替代的 testid。
- 原 ApprovalCard 的到期内容 status、记录中／已记录 status 和错误 alert 原文不变；新增一个 polite status 展示剩余时间及本地截止提示，源码 status 2→3、alert 1→1。展示 timer 独立于原 expiry timer 和权威状态，卸载清理且保留最长时限保护。邮件／日程静态风险 Message 使用 note/off，均不新增 alert；CalendarConflictNotice 的原 polite region 不变。
- ActionConfirmationDialog 的源码显式 dialog 1→0，由真实 PrimeVue modal Dialog 提供同一个运行时 dialog；父级插槽内原人工结果警告 alert 原文保留。AppShell 原有 role 数量不变，新增专用 UI 注入端口让深层浮层直接隔离背景；凭据按实例释放，旧实例关闭不得解除新模态。取消先锁按钮、nextTick 后退出再经 after-hide 完成；离场节点的迟到点击及 busy 往返不能重新启用确认，等待期卸载和替换由 disposed/实例凭据隔离，权威终态或卸载同样清理，先解除 inert 再回有效 trigger 或 main。
- 聚焦组件目录 115 项通过；完整 just check 含后端 2730／前端 640 项通过（含退出动画竞争新增回归）。迁移后七文件 E2E 59 项通过；补充审批预览与人工确认 Dialog 的 axe（serious／critical 为零）后 mail-editor 5 项再次通过，保留原请求计数、版本与供应商副作用断言。type-check、lint、构建及原体积预算通过（入口 JS gzip 330057B／上限 340373B，CSS 8338B），无依赖变更。
- 非门禁失败的既有提示：后端 Starlette 弃用警告，尚待 Task 15 迁移的 NeedsAttentionPanel 四个颜色字面量，以及构建的 Zod 注释解释和默认大块提示。Task 15 继续核对打开模态期间人工核对 busy/error 的单次可感知位置；本任务不提前改动其组件行为或样式。完整里程碑 CI 仍归 Task 17。

---

### Task 15: 迁移 needs_attention 面板

**Files:**
- Modify: `frontend/src/components/NeedsAttentionPanel.vue`、`frontend/src/components/NeedsAttentionPanel.spec.ts`
- Modify: `frontend/e2e/mail-editor.spec.ts`（仅补充真实模态隔离下忙碌／错误提示可感知、单次呈现及既有恢复动作的展示断言）
- Modify: 本实施计划（准确文件名单、live region 与验证记录）

Task 14 已提供真实 `Dialog modal` 与 AppShell 背景隔离。本任务保持 `act`、generation、choice、两个 watcher、最新 task_version 及请求语义不变；若原 busy／error 位于 inert 背景，则根据 choice 有无将同一原提示单次呈现于 Dialog 或普通面板，保留原文、traceId、reload 与 Dialog 完整警告。事实摘要使用静态 `Message error`，不增加无条件 assertive 播报。

- [x] **Step 1: 选择器改为角色并在旧实现上确认通过**

Run: `pnpm --dir frontend test:unit --run src/components/NeedsAttentionPanel.spec.ts`

Expected: PASS。

- [x] **Step 2: 写失败的迁移断言**

覆盖：当前核对尝试与最后错误在 `Message error` 内；供应商检查链接 `rel="noopener noreferrer"`；「重新核对」「确认已执行」「确认未执行」三个按钮，任一进行中全部禁用；「确认未执行不会自动重发」警告紧邻按钮且可被辅助技术读到；人工确认走 `Dialog modal`；409 竞争后显示服务端最新状态。

Run: `pnpm --dir frontend test:unit --run src/components/NeedsAttentionPanel.spec.ts`

Expected: FAIL。

- [x] **Step 3: 重建并删除 scoped CSS**

- [x] **Step 4: 运行聚焦检查**

Run: `pnpm --dir frontend test:unit --run src/components/NeedsAttentionPanel.spec.ts && pnpm --dir frontend type-check && pnpm --dir frontend lint && pnpm --dir frontend test:e2e e2e/m2-actions.spec.ts e2e/mail-editor.spec.ts`

Expected: PASS。

- [x] **Step 5: 提交**

~~~bash
git add frontend/src/components/NeedsAttentionPanel.vue frontend/src/components/NeedsAttentionPanel.spec.ts frontend/e2e/mail-editor.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate needs-attention panel to PrimeVue"
~~~

**执行记录（2026-10-05）：**

- 旧组件单测5项、相关E2E23项先通过；迁移RED为6项单测失败及2项浏览器失败，后者均证明原busy/error节点实际位于inert背景。迁移后12项单测通过；额外Panel标题关联RED先证明具名region缺失，再绑定公开header slot id并恢复通过。
- 原act、generation、choice、task_id/status watcher、最新task_version及回调语义保持不变。供应商链接继续安全过滤；三个决定按钮互斥禁用，原不自动重发警告紧邻按钮并通过aria-describedby关联。
- NeedsAttentionPanel源码status由1增为2、alert由2增为3，原因是choice互斥的面板／Dialog分支；运行时同一busy/error各仅一份。原人工决定警告仍为一个alert；核对事实新增一个note/aria-live=off，不添加assertive公告。取消后错误回到面板；409经原reload显式读取服务端终态，无自动重发。
- 迁移后的m2-actions 18项通过；mail-editor完整6项通过。首次新增409测试的终态fixture误写execution枚举，解析器正确拒绝，已仅修正测试数据并完整复验；最后header修正后的2项受影响浏览器复验通过，覆盖实际区域名、模态内busy/error、原文／trace／reload与权威结果。没有修改API、Store、feature、composable、router或后端。
- 最终`just check` exit0：后端2730／前端647，类型、lint、许可证与样式检查通过，源码颜色／媒体查询均为零。最终build与预算exit0：JS gzip330082B、CSS8244B，未提高原340373B／60000B上限。E2E与check串行，suite数据库、应用角色和advisory锁均为零，anchor仍为20260809_0018。
- 既有Starlette弃用、Zod注释解释与默认大块提示保留；完整里程碑CI归Task17。原始日志和Task15报告仅留本地SDD目录，不提交。

---

### Task 16 前置：修复设置工作时间弹层载体与非模态语义

**Files:** `frontend/src/components/SettingsTimeInput.vue`、新建同目录 `SettingsTimeInput.spec.ts`、
`frontend/e2e/settings.spec.ts`，以及本计划与对应规格。

- 先在旧组件上运行失败回归：NY 宿主 DST 缺失小时日，空／无效工作时间经 timeOnly 选 02:30
  必须保持 02:30；挂载、打开、父级换无效值零隐式回写，blur 保留原文本，卸载不写新实例。
- 用公开 modelValue 提供 2000-01-01 稳定载体；空／无效的 00:00 只定位弹层，不进入原表单。
  用公开 inputId 和更新后同步恢复原字符串；不引用库私有状态或不存在的 defaultDate。
- 沿用日程锚定弹层 `aria-modal=false`，不新增背景 inert；实测 Tab 循环、Esc 回组合输入、
  基本 axe 与用户显式选择前零写请求。API、schema、设置 hook 和共享 time.ts 均不变。
- 运行聚焦组件、设置 E2E、type-check、lint、提交前 check；独立提交
  `fix: preserve settings wall times in time picker`，审查通过后继续 Task 16。

---

### Task 16 前置 A：修正无反馈 Password 的输入语义

**Files:** `frontend/src/pages/LoginPage.vue`、`LoginPage.spec.ts`、
`frontend/e2e/daily-brief.spec.ts`，以及本计划与对应规格。

- 正式 RED 验证真实 Password 在隐藏／显示两态没有无对应 popup 的 expanded/controls/haspopup；
  保留原 label、类型、显隐键盘与登录失败恢复的三条既有 E2E 流程。
- 仅用公开 input-props 移除三属性；真实浏览器 axe 无严重／关键问题，认证和路由只读。
- 与前置 B 源码稳定后统一运行聚焦单测、相关 E2E、一次 check 和构建预算；独立提交
  `fix: correct password input accessibility`，不依赖 Task16 未提交辅助文件。

---

### Task 16: 清理旧样式，启用强制门禁，加入无障碍与布局 E2E

**Files:**
- Modify: `frontend/src/**/*.vue`（仅删除残留样式）
- Create: `frontend/e2e/support/axe.ts`、`frontend/e2e/accessibility.spec.ts`、`frontend/e2e/layout.spec.ts`
- Modify: `frontend/playwright.config.ts`、`justfiles/test.just`、`scripts/check-frontend-styles.sh`、`scripts/test-tooling.sh`
- Create: `frontend/src/test-support/liveRegionInventory.spec.ts`

- [ ] **Step 1: 写失败的门禁与 E2E**

`liveRegionInventory.spec.ts`：汇总 Task 2 前核对冻结的真实基线（源码 status 43 / alert 32 / dialog 1），逐项对照既有 `status`、`alert`、`dialog` 的触发场景、语义与文案；源码扫描只辅助清单，不能机械计数代替运行时验收。组件抽取、PrimeVue 内置 role、新抽屉造成的数量变化须有同提交的逐项原因与运行时测试，证明无漏报/重复播报；禁止空 role 凑数。样式断言沿用 Task 1 已落地的颜色检测（含八位颜色并排除 HTML 实体）及 `@media` 检查。

`accessibility.spec.ts`：对登录、简报、操作中心、邮件编辑器、日程编辑器、审批预览、`needs_attention` 七个视图运行 axe，`serious`/`critical` 为零。`layout.spec.ts`：在 375、900、1400 三个宽度截图并断言导航、时间线的呈现形式。`playwright.config.ts` 增加 `tablet` 与 `mobile` project，仅这两个新 spec 在三种 project 运行。

Run: `pnpm --dir frontend test:unit --run src/test-support/liveRegionInventory.spec.ts`

Expected: FAIL（若仍有残留颜色/`@media`），或 PASS（若前面任务已清理干净；此时直接进入 Step 3）。

- [ ] **Step 2: 清理残留样式**

删除全部 `.vue` 中的 `<style scoped>` 布局块，只允许保留无法用工具类表达且不含颜色与断点的极少数规则，并在注释中说明原因。

- [ ] **Step 3: 把样式检查改为强制并把 axe 加入 E2E**

`justfiles/test.just` 的 `check` 改为 `bash scripts/check-frontend-styles.sh --strict`；`ci` 在 `report-frontend-bundle.sh` 加 `--budget`，预算变量默认 JS 200 KB、CSS 60 KB。`scripts/test-tooling.sh` 断言 `--strict` 与 `--budget` 存在。

- [ ] **Step 4: 运行完整前端验证**

Run: `pnpm --dir frontend test:unit --run && pnpm --dir frontend type-check && pnpm --dir frontend lint && bash scripts/check-frontend-styles.sh --strict && bash scripts/check-frontend-licenses.sh && pnpm --dir frontend build && bash scripts/report-frontend-bundle.sh --budget && pnpm --dir frontend test:e2e`

Expected: 全部 PASS；记录体积数字。

- [ ] **Step 5: 提交**

~~~bash
git add frontend/src frontend/e2e/support/axe.ts frontend/e2e/accessibility.spec.ts frontend/e2e/layout.spec.ts frontend/playwright.config.ts justfiles/test.just scripts/check-frontend-styles.sh scripts/test-tooling.sh
git commit -m "test: enforce frontend style, accessibility, and bundle gates"
~~~

---

### Task 17: 更新文档并冻结 M2.1 验收记录

**Files:**
- Modify: `frontend/AGENTS.md`、`AGENTS.md`、`CLAUDE.md`、`README.md`、`docs/acceptance-checklist.md`
- Create: `docs/releases/2026-09-19-m2-1-frontend-refactor-evidence.md`

- [ ] **Step 1: 运行完整门禁**

Run: `just check`

Expected: PASS，读取完整输出。

Run: `just ci`

Expected: PASS，读取完整输出；记录后端/前端测试数、E2E 数、axe 结果、体积数字、许可证检查结果与 HEAD SHA。

- [ ] **Step 2: 写验收记录**

`docs/releases/2026-09-19-m2-1-frontend-refactor-evidence.md` 记录：验证基准 HEAD、`just ci` 起止时间与退出码、前端单测/E2E 通过数、axe 七视图结果、三种布局截图哈希、live region 基线逐项语义/文案比对、数量变化原因与运行时测试结果、`pnpm-lock.yaml` 中 PrimeVue 版本与许可证检查输出、体积基线与最终值、浏览器基线声明、未覆盖项。不记录任何真实数据。

- [ ] **Step 3: 同步文档**

README：技术栈段落写明 PrimeVue 4.5.5 + Tailwind 4、浏览器基线、`check` 新增门禁，并把「当前实施目标 M2.1」改为「M2.1 已于 <日期> 完成」。验收清单：勾选 M2.1 各项并链接验收记录。前端 `AGENTS.md` 与根 `AGENTS.md`/`CLAUDE.md`：确认技术栈与门禁描述与实现一致，把当前实施目标指向 M2.2 待批准状态。

- [ ] **Step 4: 提交**

~~~bash
git add frontend/AGENTS.md AGENTS.md CLAUDE.md README.md docs/acceptance-checklist.md docs/releases/2026-09-19-m2-1-frontend-refactor-evidence.md
git commit -m "docs: freeze M2.1 frontend refactor evidence"
~~~

- [ ] **Step 5: 在最终文档提交上追加完整门禁并留档**

Run: `just ci`

Expected: PASS，读取完整输出并将最终 HEAD、起止时间、退出码与完整日志另行留档。Step 2 证据正文保留构建基准 HEAD，不为了写入自身提交 SHA 循环修改文档。只有最终文档提交的 CI 通过后才报告 M2.1 完成；失败先定位修复，并对新的最终提交重新验证。
