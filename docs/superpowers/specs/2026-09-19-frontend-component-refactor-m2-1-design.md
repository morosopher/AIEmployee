# AI Employee M2.1：前端组件库重构设计

> 日期：2026-09-19
>
> 状态：已批准（2026-09-19）
>
> 前置里程碑：M2「可执行邮件与日历助手」（2026-09-15 验收通过）
>
> 后续里程碑：M2.2「上线收尾」

## 1. 背景与设计结论

M2 交付的前端是一个功能完整但视觉朴素的 Vue 3 单页应用：9 个路由页面、
25 个组件、2 个 Pinia Store、1 个 SSE composable 和 7 个 feature 目录。它没有引入
任何 UI 组件库或 CSS 体系，21 个页面与组件文件各自维护 scoped CSS，样式中硬编码了
12 种颜色、5 个互不一致的断点，也没有共享的设计 token。与此同时，它的行为层质量较高：
API、Store、SSE reducer 和页面均有单测，E2E 覆盖 11 条流程，模板中大量使用
`role="status"`、`role="alert"` 和程序化 label，测试以角色和 label 作为选择器。

M2.1 的目标是**只替换展示层**：用成熟的组件库和统一的设计 token 重建页面外观和交互，
让操作中心、编辑器、审批预览和连接页达到可长期使用的产品质量，同时不改变任何服务端
契约、状态管理、SSE 行为、安全清洗和 M2 范围边界。

设计结论如下：

- 采用 **PrimeVue 4.5.5（MIT）** 作为组件库，**Tailwind CSS 4** 作为布局与间距工具层，
  两者通过 `tailwindcss-primeui` 共享同一套 token。
- 明确**不升级到 PrimeVue 5**。PrimeVue 5.0 起改为 PrimeUI 商业许可，个人虽可免费使用
  Community License，但需注册许可 Key、每年续期确认资格，且包为编译分发。4.x 系列保持
  MIT，官方承诺继续发布安全修复。功能冻结的代价可接受；未来若需迁移，必须先经新的
  ADR 批准。
- 采用 PrimeVue **styled 模式 + Aura 预设定制**，不采用 unstyled 模式。项目只有一名维护
  者，styled 模式的开箱无障碍和一致性收益远高于完全自定义外观的收益。
- 采用**绞杀式逐页迁移**：每个页面迁移完成即删除其旧 scoped CSS，一次提交只迁移一个
  页面或一组基础组件；里程碑结束时不允许两套样式体系并存。
- `api/`、`stores/`、`composables/useTaskEvents`、`features/` 中的领域投影逻辑、
  Markdown 清洗和路由守卫**不在本里程碑修改**；如迁移过程中发现这些层的缺陷，单独
  记录并按 `fix:` 提交，不混入视觉重构。Task 8 前确认端口及 Task 9 前最小错误码元数据的已批准例外见 §7。
- 前端 `AGENTS.md` 中「未获批准不要引入新的 UI 框架或 CSS 体系」的规则由本规格批准
  修订为明确的技术栈清单。

2026-10-01 补充：完整任务历史列表按 [`完整任务历史补充设计`](2026-10-01-complete-task-history-design.md) 独立实施后供 Task 7 使用。该规格第 8 节是下文后端／API／列表状态只读约束的唯一新增限定例外；它不变更任务执行、审批、重试、SSE 或其他产品能力，也不意味着其余 M2.1 已验收。

## 2. 范围

### 2.1 M2.1 包含

- 引入并锁定 PrimeVue 4.5.5、`@primeuix/themes` 2.x、`tailwindcss-primeui`、
  `@primevue/forms` 4.5.5、`primeicons` 7、Tailwind CSS 4 与 `@tailwindcss/vite`、
  `unplugin-vue-components` 与 `@primevue/auto-import-resolver`、`@vueuse/core`、
  `zod`、`@testing-library/vue`、`@axe-core/playwright`。
- 建立以语义 token 命名的设计系统：色板、排版、间距、圆角、阴影、断点、动效时长，
  全部由 Aura 预设定制和 Tailwind `@theme` 单一来源导出。
- 重建应用骨架：左侧导航、顶部系统告警、中间内容、右侧任务时间线；窄屏切换为抽屉
  导航和抽屉时间线。
- 逐页重构登录、今日简报、对话、任务历史、连接、设置、操作中心、邮件编辑器、日程
  编辑器、日程重新准备、审批预览和 `needs_attention` 面板的展示层。
- 工作设置、邮件与日程编辑表单统一使用 PrimeVue Forms 与 zod 做**格式级**校验；
  登录与聊天换用 PrimeVue 输入控件并保留现有提交逻辑。领域规则仍以服务端 Problem Details 为准。
- 用 Toast、内联 Message、Skeleton、空状态组件统一加载、空、失败、部分成功、断线和
  恢复状态的呈现。
- 建立可自动化的无障碍门禁：关键页面在 Playwright 中执行 axe 扫描，组件测试覆盖
  键盘操作与焦点管理。
- 建立包体积预算、许可证检查和 PrimeVue 主版本锁定的 CI 门禁。
- 同步更新前端 `AGENTS.md`、根 `CLAUDE.md`、README 和验收清单。

### 2.2 M2.1 不包含

- 任何后端修改、API/SSE 契约变更、数据库迁移。
- 新增业务功能或可操作入口；M2 明确排除的能力在 M2.1 中同样不得以「占位 UI」形式出现。
- 暗色模式、多语言框架、用户可配置主题。界面固定为简体中文和浅色主题。
- PrimeVue 5 或任何 PrimeUI 商业许可产品（PrimeBlocks、Theme Designer、Pro 组件）。
- 更换状态库、请求库、路由库或测试运行器。
- 图表、拖拽日历网格、富文本编辑器、文件上传控件。
- 生产部署、CSP 策略、TLS 与运维流程；这些属于 M2.2。
- 修改 Playwright 覆盖的用户流程语义；E2E 只允许因 DOM 结构变化而调整选择器。

### 2.3 发布边界

M2.1 完成的定义是：全部页面迁移完毕、旧 scoped CSS 与硬编码颜色清零、`just ci` 通过、
无障碍扫描零严重问题、包体积在预算内、许可证检查通过，并写出 M2.1 验收记录。
未完成任一项不得宣称 M2.1 完成。M2.1 不重新打开 M2 的功能验收，也不改变 M2 发布证据。

## 3. 技术选型与许可

### 3.1 依赖清单

| 包 | 版本策略 | 许可 | 用途 |
|---|---|---|---|
| `primevue` | 固定 `4.5.5`，禁止 `^` | MIT | 组件库 |
| `@primevue/forms` | 固定 `4.5.5` | MIT | 表单状态与校验集成 |
| `@primevue/auto-import-resolver` | 固定 `4.5.5` | MIT | 按需自动注册组件 |
| `@primeuix/themes` | `2.x`，锁定次版本 | MIT | Aura 预设与 token API |
| `primeicons` | `7.x` | MIT | 图标字体，随构建本地打包 |
| `tailwindcss`、`@tailwindcss/vite` | `4.x` | MIT | 工具类与 Vite 集成 |
| `tailwindcss-primeui` | `0.6.x` | MIT | 把 PrimeVue token 暴露为 Tailwind 工具类 |
| `unplugin-vue-components` | `32.x`（开发依赖） | MIT | 组件自动导入 |
| `@vueuse/core` | `15.x` | MIT | 媒体查询、焦点、事件监听等 composable |
| `zod` | `4.x` | MIT | 表单 schema 与格式校验 |
| `@testing-library/vue` | `8.x`（开发依赖） | MIT | 以用户视角编写组件测试 |
| `@axe-core/playwright` | 最新稳定（开发依赖） | MPL-2.0 | E2E 无障碍扫描 |

实施时在锁定前重新核对每个包的许可字段、最近发布时间和已知漏洞，并把结果写入
Task 1 的提交说明。`@primeuix/themes` 3.x 和 `@primeuix/*` 1.x 系列已随 PrimeVue 5
改为商业许可，实施时必须核对实际解析的传递依赖全部为 MIT。

### 3.2 PrimeVue 主版本锁定

- `package.json` 中 PrimeVue 相关包使用精确版本，不使用范围符号。
- 新增 CI 检查：解析 `pnpm-lock.yaml`，若出现 `primevue@5`、`@primeui/*` 或
  `@primeui/license-manager` 即失败。
- 新增许可证检查：`pnpm licenses list --json` 的生产依赖默认只允许 MIT、ISC、BSD-2/3、
  Apache-2.0；开发依赖额外允许 MPL-2.0。仅下表已批准的基线例外可超出默认白名单，
  必须同时匹配包名、精确版本和 pnpm 报告的许可证原文。出现 `SEE LICENSE IN` 或未知许可即失败。
- 4.x 停止安全修复或出现无法修复的漏洞时，由新的 ADR 决定迁移路线；候选路线是
  PrimeVue 5 Community License 或 Reka UI + shadcn-vue。本规格不预建任何兼容层。

#### 已批准的基线许可证例外

本表是基线例外的唯一完整授权清单。生产例外同样适用于开发依赖树；开发例外只适用于
开发依赖树。相同包的其他版本不继承例外，许可原文变化也不得借用例外；任何新增例外
必须先获得用户批准并同步本表、实施计划、协作规则和门禁回归测试。默认白名单及
PrimeVue 商业产品线禁令保持不变。

| scope | 包名 | 精确版本 | 许可证原文 |
|---|---|---|---|
| prod | `argparse` | `2.0.1` | `Python-2.0` |
| dev | `@csstools/color-helpers` | `6.1.0` | `MIT-0` |
| dev | `@csstools/css-syntax-patches-for-csstree` | `1.1.7` | `MIT-0` |
| dev | `jackspeak` | `3.4.3` | `BlueOak-1.0.0` |
| dev | `lru-cache` | `11.5.2` | `BlueOak-1.0.0` |
| dev | `minimatch` | `10.2.6` | `BlueOak-1.0.0` |
| dev | `minipass` | `7.1.3` | `BlueOak-1.0.0` |
| dev | `package-json-from-dist` | `1.0.1` | `BlueOak-1.0.0` |
| dev | `path-scurry` | `1.11.1` | `BlueOak-1.0.0` |
| dev | `mdn-data` | `2.27.1` | `CC0-1.0` |

### 3.3 不引入的库

- `lucide-vue-next`：PrimeIcons 已随 PrimeVue 提供且图标语义一致，不引入第二套图标。
- `pinia-plugin-persistedstate` 或任何持久化插件：违反「敏感状态不进浏览器存储」约束。
- `vue-i18n`：界面固定中文，PrimeVue 内置文案通过 `locale` 配置提供 zh-CN 值。
- 任何 HTTP 客户端：`src/api/client.ts` 已统一处理 Cookie、CSRF 和 Problem Details。

## 4. 设计系统

### 4.1 单一 token 来源

所有视觉常量由 `src/design/tokens.ts` 中的 Aura 预设定制导出，Tailwind 通过
`tailwindcss-primeui` 读取同一组 CSS 变量。组件和页面禁止再写十六进制颜色、像素级
间距或独立断点。不引入 stylelint；改为在 `just check` 中用脚本断言 `src/**/*.vue`
不含 `#[0-9a-f]{3,6}` 颜色字面量和 `@media` 查询，迁移期间该检查为警告，步骤 16
之后转为强制失败。

### 4.2 语义色板

现有硬编码颜色映射为语义 token，映射关系在 Task 1 固定：

| 语义 | 现有值 | 用途 |
|---|---|---|
| `primary` | `#164e9c` | 主按钮、链接、当前导航 |
| `danger` | `#a61b1b` | 逾期告警、拒绝、`needs_attention` |
| `warn` | `#946200` / `#fff5df` | 工作时间外、部分成功、能力降级 |
| `surface` | `#f6f8fb` / `#fff` / `#dce2ea` | 背景、卡片、分隔线 |
| `text` | `#1e293b` / `#485365` / `#a8b1bf` | 正文、次级、禁用 |

新增 `success` 与 `info` 由 Aura 默认 token 派生。任务状态、能力状态和审批状态的
颜色映射集中在 `src/design/status.ts`，组件只引用语义名。

### 4.3 排版、间距与圆角

- 字体栈使用系统 UI 字体，中文优先 `"PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"`，
  不引入 Web 字体文件。
- 字号使用 Tailwind 默认刻度，正文 `text-sm`（14px）与 `text-base`（16px），标题不超过
  三级。
- 间距使用 4px 基数刻度；卡片内边距固定 `p-4`，页面外边距固定 `p-6`，窄屏 `p-4`。
- 圆角统一为 Aura `border.radius` 三档，不自定义。

### 4.4 断点与布局

统一使用 Tailwind 默认断点，替换现有 5 个不一致的 `max-width` 查询：

| 断点 | 宽度 | 布局 |
|---|---|---|
| `< md` | `< 768px` | 单栏；导航与时间线为抽屉 |
| `md` 到 `< xl` | `768px` 到 `< 1280px` | 双栏；时间线为可折叠侧边抽屉 |
| `≥ xl` | `≥ 1280px` | 三栏：导航 14rem、内容自适应、时间线 20rem |

操作中心与任务历史页在任何断点都不显示全局时间线栏。操作中心沿用 M2 独立详情区域；任务历史按补充规格使用自有列表／详情，桌面内部双列、窄屏内联详情及返回列表焦点恢复，避免重复时间线与占位重试入口。其他页面保持上表全局抽屉布局。

### 4.5 动效

只使用 PrimeVue 内置过渡；全局尊重 `prefers-reduced-motion`，通过 PrimeVue
`ripple: false` 和 Tailwind `motion-reduce:` 变体关闭非必要动画。

## 5. 应用骨架

### 5.1 AppShell

- 左侧导航使用 PrimeVue `Menu` 渲染六个既有入口，当前路由以 `aria-current="page"`
  标记；窄屏改为 `Drawer` 并用汉堡按钮触发。
- 顶部系统告警区域使用 `Message`：加载中 `severity="secondary"` 且 `role="status"`，
  刷新失败 `severity="warn"` 且 `role="alert"`，简报逾期 `severity="error"` 且
  `role="alert"`，保留「查看诊断任务」链接。60 秒轮询与任务终态触发逻辑不变。
- 右侧任务时间线在 `≥ xl` 常驻，`md` 到 `< xl` 折叠为可展开面板，`< md` 为 `Drawer`。
- 全局 `Toast` 和 `ConfirmDialog` 出口挂在 AppShell，页面通过 `useToast` 触发。

### 5.2 PrimeVue 全局配置

- `theme.preset` 为定制 Aura；`theme.options.cssLayer` 启用并把 PrimeVue 层排在
  Tailwind `utilities` 之前，保证工具类可以覆盖组件样式。
- `locale` 提供 zh-CN 完整文案，包括日期选择器的星期、月份、`aria` 标签和「清除」
  「今天」等按钮文案。
- `csp.nonce` 预留为从 `<meta name="csp-nonce">` 读取的值。M2.1 不配置 CSP，但 styled
  模式会向 `<head>` 注入 `<style>`；M2.2 若启用 CSP，必须通过该 nonce 通道放行主题样式，
  由 M2.2 规格决定具体策略，M2.1 不得为此改用 `unsafe-inline`。
- `ripple` 关闭；`inputVariant` 使用 `outlined`。

## 6. 页面与组件映射

下表是迁移的权威对照。左列是现有文件，右列是用于重建它的 PrimeVue 组件；「保留」表示
该组件的 Props/Emits 契约不变，只重写模板与样式。

| 现有文件 | PrimeVue 组件 | 说明 |
|---|---|---|
| `pages/LoginPage.vue` | `Card`、`InputText`、`Password`、`Button`、`Message` | Password 关闭强度提示与显隐切换以外的功能 |
| `components/AppShell.vue` | `Menu`、`Drawer`、`Message`、`Toast`、`ConfirmDialog` | 见第 5 节 |
| `pages/TodayBriefPage.vue`、`components/BriefView.vue` | `Card`、`Tag`、`Skeleton`、`Message`、`Accordion` | 来源引用与数据截止时间保持可见 |
| `components/SourceLink.vue` | `Button link` | 保留 `rel="noopener noreferrer"` |
| `pages/ChatPage.vue` | `Textarea autoResize`、`Button`、`ScrollPanel` | 保留既有最终消息、可信本地编辑器链接与实时任务状态，不新增临时 delta 投影 |
| `components/MarkdownMessage.vue` | 无 | 保留 DOMPurify 清洗，仅调整排版类 |
| `pages/TasksPage.vue`、`components/TaskTimeline.vue` | `DataView`、`Timeline`、`Tag`、`Button` | 时间线事件文案不变 |
| `pages/ConnectionsPage.vue`、`components/CapabilityRows.vue` | `Card`、`DataTable`、`ToggleSwitch`、`Tag`、`Button` | 「启用」「继续授权」「重新授权」保持独立按钮 |
| `pages/SettingsPage.vue`、`components/WorkSettingsForm.vue`、`components/WorkingHoursFields.vue` | `Form`、`Select`、`DatePicker timeOnly`、`InputNumber`、`ToggleSwitch`、`Button` | 时区选择用 `Select filter`，选项来自本地 IANA 数据并保留服务端当前值及显式合法 IANA 值（见第 7 节） |
| `pages/ActionsPage.vue` | `Tabs`、`DataTable`、`Select`、`Paginator`、`Tag`、`Splitter` | 筛选、分页参数与服务端契约不变 |
| `components/ActionDetail.vue`、`components/LinkedActionPanel.vue` | `Panel`、`Timeline`、`Tag`、`Button link` | 窄屏以独立区域展示 |
| `pages/MailDraftPage.vue`、`components/MailDraftFields.vue`、`components/LocalEditorFrame.vue` | `Form`、`AutoComplete multiple`（收件人）、`InputText`、`Textarea`、`Message`、`Button` | 仅纯文本正文；不出现附件或格式工具栏 |
| `components/EditorConnectionSelect.vue` | `Select` | 选项展示供应商与能力状态 |
| `components/EditorRecovery.vue` | `Message`、`Button` | 版本冲突与刷新恢复文案不变 |
| `pages/CalendarProposalPage.vue`、`components/CalendarProposalFields.vue`、`components/CalendarTargetFields.vue` | `Form`、`DatePicker`、`Select`、`Chip`、`Message` | 全天日期与定时墙上时间分支保留，只有定时分支用 showTime；时区明示，不做自动推断 |
| `components/CalendarWallTimeFields.vue` | `InputText`、`Button` | 日程 DatePicker 定时 footer 的小时／分钟／秒直接编辑墙上字符串，替换会按宿主 DST 归一的原生 clock 子视图 |
| `components/CalendarConflictNotice.vue` | `Message warn`、`Button` | 最多三个候选时间以按钮列表呈现 |
| `components/CalendarRepreparePanel.vue`、`components/CalendarFieldsComparison.vue` | `Panel`、`DataTable`（字段对比） | 前后差异逐字段高亮 |
| `components/MissingCalendarConnections.vue` | `Message info`、`Button link` | 无 |
| `components/ApprovalCard.vue`、`MailApprovalPreview.vue`、`CalendarApprovalPreview.vue` | `Card`、`Tag`（风险等级）、`DataTable`（载荷字段）、`Button`、`Message` | 批准/拒绝期间 `loading` 禁用；过期倒计时用 `role="status"` |
| `components/ActionConfirmationDialog.vue` | `Dialog modal` | 焦点陷阱、Esc 关闭、关闭后焦点返回触发按钮 |
| `components/NeedsAttentionPanel.vue` | `Panel`、`Message error`、`Button`、`Dialog` | 「确认未执行不会自动重发」警告保留在按钮旁 |

## 7. 表单与校验

- 工作设置、邮件与日程编辑表单使用 `@primevue/forms` 的 `Form` 与 `zodResolver`；
  登录与聊天只换 PrimeVue 输入控件，保留提交逻辑，不引入新的 schema。schema 位于对应
  `features/*/schema.ts`，只表达格式约束：必填、邮箱格式、长度上限、时间先后、合法 IANA 时区。
- 工作时间沿用 `working_hours` 七日多区间模型，格式校验逐一区间检查时间先后；
  收件人数量、工作时间领域规则、冲突、版本、能力和审批有效性仍由服务端裁决。
  现有 Problem Details 只提供 `error_code` 等基本字段，没有字段错误数组契约；
  `features/forms/problemFields.ts` 按已知 `error_code` 映射安全中文表单级错误，未知码使用
  安全 fallback，不匹配服务端文案，不渲染原始 detail。字段格式错误由 zod 提供，
  不伪造 `errors` fixture，不修改 `api/validation.ts` 或其他 API 解析。
- 2026-10-03 用户批准最小错误码元数据例外：仅在 `features/actions/recovery.ts`
  的 `ActionRecovery` 增加可选只读 `problem.error_code`，只从真实 `ProblemError`
  复制；保留原 `message`、`action`、`traceId`，不携带 `title`、`detail` 或完整响应。
  `features/calendar/useCalendarProposalEditor.ts` 两处特殊恢复对象只保留该元数据，
  原提示、版本锁、请求顺序、幂等意图、epoch 和卸载保护不变。普通异常和
  `EditorInputError` 不伪造元数据。先独立修复并回归，再由 Task 9／11／12 的真实
  Form 将最小投影交给 `problemToFormError`；上下文恢复说明及动作继续保留。
- 时区选项沿用本地 `Intl.supportedValuesOf('timeZone')` 加 `UTC`，保留服务端快照当前值
  与用户显式输入的合法 IANA 时区；有限 fallback 不得误拒已有值。不新增服务端列表接口，
  不从宿主机推断用户时区。日程保留全天日期及定时墙上时间转换、既有 `time.ts` 与 DST
  歧义拒绝，不直接用 `Date.toISOString()` 把本地选值解释为宿主机时区。
- DatePicker 保留定时 `showTime` 模式、日期弹层及原组合输入；原生 clock 子视图通过公开 PT
  隐藏且退出焦点／无障碍树，公开 footer 的 `CalendarWallTimeFields` 用 InputText/Buttons
  直接编辑墙上时分秒。日期 Date 仅作中午的日历显示载体，日格点击／键盘选值读取公开
  `context.date` 并保留原时间后缀，不让宿主 DST 归一后的 Date 回流业务输入。
  无效、未完整数字即时保留并由 schema 拒绝；全天切回定时不自动填小时。
  footer 保留原时钟的正反 Tab 循环与 Esc 返回输入；非模态声明不取消已有键盘循环。
- 日程格式 resolver 可读取原 hook 已采纳快照的最小只读时间上下文：每个墙上时间、时区
  及全天类型均未变化的字段保留其原明确 offset／秒内精度，按真实时刻判断先后；修改时间
  或时区后仍必须拒绝 DST 歧义。这只是展示校验与库兼容定制，不修改既有 hook/time.ts
  或宣称修复 PrimeVue 库本身。
- Task 16 前置修复设置工作时间的 `SettingsTimeInput`：所有 timeOnly 日期载体固定为
  2000-01-01，空／无效输入仅以 00:00 为弹层定位，不成为表单默认值。挂载、打开、父级换值
  不隐式 emit；公开 inputId 在更新后恢复原字符串，blur 不用载体覆盖无效输入，卸载后不回写。
  只有显式时钟选择或手工输入才更新原字符串。DatePicker 4.5.5 没有 defaultDate prop，
  不使用 min/maxDate 伪造领域限制，不修改共享 time.ts、schema、API 或设置行为 hook。
  与日程 DatePicker 一致，锚定时间弹层显式 `aria-modal=false`，保留 Tab 焦点循环、Esc
  关闭后回到组合输入，不宣称背景 inert；真实模态 Dialog/Drawer 的约束不变。
- 默认发送账户、默认日历账户及默认日历的 `Select` 属于 Task 9 设置页；Task 8 连接页
  只保留既有前往设置入口，不复制默认值写操作。
- 提交按钮在请求进行中 `loading`，请求完成前禁止重复提交；409 冲突显示
  `EditorRecovery` 提供的恢复动作，不清空用户输入。
- 表单不做自动保存到浏览器存储；离开页面前的未保存提示使用 `ConfirmDialog`，
  内容不包含正文摘录；路由离开提示在页面实现，浏览器卸载遵守原生能力边界。
- Task 8 前以独立 `fix:` 提交为 `features/connections/useConnections.ts` 注入并等待异步
  确认回调，附 `features/connections/useConnections.spec.ts` 回归测试；这是 UI 确认端口的
  最小只读例外，不改领域规则。默认保留既有确认语义，断开文案逐字保留，取消零请求，
  等待确认及执行期间保持忙碌互斥；实际 `disconnectConnection` 请求与 `catalog.load`
  刷新、卸载保护规则不变。Task 8 页面用 `ConfirmDialog` 提供回调，不叠加原生确认。
- 显式新增文件为 `features/settings/schema.ts`、`features/mail/schema.ts`、
  `features/calendar/schema.ts` 及各自 `schema.spec.ts`，以及
  `features/forms/problemFields.ts`、`features/forms/problemFields.spec.ts`；这些展示校验
  与测试文件不等于开放既有 feature 行为层；除本节两项明确例外外，其他既有行为文件继续只读。

## 8. 反馈与状态呈现

| 状态 | 呈现 |
|---|---|
| 加载 | `Skeleton` 占位，同时在 `role="status"` 元素中给出文字 |
| 空数据 | 统一 `EmptyState` 组件：图标、一句说明、可选主要动作 |
| 失败 | 内联 `Message error`，附用户可执行的恢复动作和 `trace_id` |
| 部分成功 | `Message warn` 列出缺失来源与最后同步时间 |
| 断线 | AppShell 顶部 `Message` 显示连接状态，来源仍是 `useTaskEvents` |
| 恢复 | 断线恢复后 `Toast success` 一次性提示，不重复 |
| 操作结果 | 非阻断结果使用 `Toast`；审批结果、`needs_attention` 结果仍以内联持久 `Message` 为准 |

`Toast` 是临时提示，不承载必须阅读的信息；任何需要用户决定的内容都使用内联 `Message`
或 `Dialog`。迁移前先核对并冻结真实 live region 清单，逐项记录触发场景、语义及文案。
现有 `status`、`alert`、`dialog` 语义和文案逐一保留；源码计数仅为清单辅助，不能替代运行时
验收。组件抽取、PrimeVue 内置 role、新增导航或时间线抽屉引起的数量变化，必须逐项在
同一提交记录原因，并用运行时测试证明没有漏报或重复播报。禁止添加空 role 凑数。

## 9. 无障碍与键盘

- 所有交互元素可通过 Tab 到达，可见焦点样式来自 Aura `focus.ring` token。
- `Dialog` 与 `Drawer` 启用焦点陷阱，关闭后焦点返回触发元素；Esc 关闭，`modal` 时
  背景 `inert`。
- 状态变化通过既有 live region 宣告；`Toast` 使用 `aria-live="polite"`，错误
  `Toast` 使用 `assertive`。
- `DataTable` 提供列头 `scope`、排序按钮 `aria-sort`；筛选控件有程序化 label。
- 图标按钮必须带 `aria-label`；纯装饰图标 `aria-hidden`。
- 颜色不作为唯一信息载体；状态 `Tag` 同时显示文字。
- 对比度满足 WCAG 2.1 AA；Task 1 用工具核对定制 token 的对比度并记录结果。
- Playwright 对登录、简报、操作中心、邮件编辑器、日程编辑器、审批预览、
  `needs_attention` 七个视图运行 axe 扫描，`serious` 与 `critical` 级别问题为零。

## 10. 安全约束

- PrimeVue 组件的所有文本 Props 只接受经过 `api/` 类型收窄的字符串；`Message`、
  `Toast`、`Tag`、`DataTable` 单元格不使用 `v-html` 或 `escape=false`。
- Markdown 渲染仍只在 `MarkdownMessage.vue`，DOMPurify 配置不变。
- 图标字体、Tailwind 产物和主题样式全部随构建打包，不引用任何 CDN。
- `DataTable` 不启用导出、行内编辑或本地状态持久化（`stateStorage`）。
- `AutoComplete` 收件人建议只来自当前草稿已有收件人，不查询通讯录。
- 敏感页面的 `Cache-Control: no-store` 由服务端控制；前端不新增任何
  `localStorage`/`sessionStorage` 写入，现有零存储断言测试保留。
- 依赖安装使用 `--frozen-lockfile`；新增依赖在 Task 1 记录 `pnpm audit` 结果。

## 11. 构建与体积

- Vite 通过 `@tailwindcss/vite` 与 `unplugin-vue-components` 集成，组件按需注册，
  不做 `app.component` 全局注册。
- Task 1 记录 `pnpm build` 的基线产物大小；M2.1 预算为首屏 JS gzip 增量不超过
  200 KB、CSS gzip 不超过 60 KB。超预算需在提交说明中解释并由用户确认。
- `vite build` 增加 `build.reportCompressedSize`，CI 把体积写入日志。
- 浏览器基线随 Tailwind 4 提升为 Chrome 111、Safari 16.4、Firefox 128 及以上；
  README 记录该要求。
- `frontend/Dockerfile` 不变；构建阶段仍为 `pnpm install --frozen-lockfile && pnpm build`。

## 12. 测试策略

### 12.1 迁移前的安全网

- 迁移每个页面前，先确认其现有单测和相关 E2E 在当前实现上通过，并把选择器改为
  `getByRole`/`getByLabelText`/`getByText`；`data-testid` 只在无语义角色的容器上保留。
- 现有 29 处 `data-testid` 逐一评估，能用角色替代的在迁移时删除。
- 组件测试逐步改用 `@testing-library/vue`；Vue Test Utils 继续用于需要检查
  Emits 或 Props 的场景，两者不在同一测试文件混用。

### 12.2 迁移后的断言

- 每个页面迁移提交必须包含：单测绿色、`pnpm --dir frontend type-check`、
  `pnpm --dir frontend lint`、对应 E2E 绿色。
- 测试禁止依赖 PrimeVue 内部 class 名（如 `p-button`）；只依赖角色、label、文本
  和自有 `data-testid`。
- 组件测试必须覆盖：加载、空、成功、部分成功、错误、断线、重试、审批冲突、键盘操作、
  焦点返回。
- SSE 相关测试不修改；若迁移导致其失败，说明展示层误改了行为层，必须回退。

### 12.3 门禁

- `just check` 增加快速检查：许可证、PrimeVue 主版本、硬编码颜色与 `@media`。
- `just ci` 在生产构建后生成包体积报告并检查预算；报告与预算不要求加入快速 `check`。
- `just test-e2e` 增加 axe 扫描步骤。
- M2.1 完成前运行完整 `just ci`。

## 13. 迁移策略与顺序

采用绞杀式迁移，每步一次提交，顺序固定：

1. **工具链与 token**：安装依赖、Vite 与 Tailwind 集成、Aura 预设定制、
   `tokens.ts`、`status.ts`、全局样式入口、PrimeVue 配置与 zh-CN locale、CI 门禁、
   基线体积记录。此步不改任何页面。
2. **AppShell**：导航、告警、时间线抽屉、Toast/ConfirmDialog 出口。
3. **基础展示组件**：`EmptyState`、状态 `Tag` 映射、`SourceLink`、`MarkdownMessage`
   排版。
4. **登录页**。
5. **今日简报**与 `BriefView`。
6. **对话页**。
7. **任务历史**与 `TaskTimeline`。
8. **连接页**与 `CapabilityRows`；先独立提交第 7 节确认端口修复及回归，再接入 `ConfirmDialog`。
9. **设置页**、`WorkSettingsForm`、`WorkingHoursFields`（首个 Forms + zod 表单）。
10. **操作中心**、`ActionDetail`、`LinkedActionPanel`。
11. **邮件编辑器**及其字段、连接选择、恢复组件。
12. **日程编辑器**、冲突提示、目标字段、缺失连接提示。
13. **日程重新准备**与字段对比。
14. **审批预览**、`ApprovalCard`、确认对话框。
15. **`needs_attention` 面板**。
16. **清理**：删除全部旧 scoped CSS 与硬编码颜色，CI 检查转为强制。
17. **文档与验收**：更新 `AGENTS.md`、`CLAUDE.md`、README、验收清单，写 M2.1 验收记录。

每一步的提交信息由实施计划指定。步骤 4 到 15 中任一页面迁移若发现行为层缺陷，先以
独立 `fix:` 提交修复并附回归测试，再继续迁移。

## 14. 文档与规则更新

- 前端 `AGENTS.md`：把「不要引入新的 UI 框架或 CSS 体系」改为「技术栈固定为
  PrimeVue 4.5.x、Tailwind 4、PrimeVue Forms、zod、VueUse；引入其他 UI 库、CSS 体系
  或升级 PrimeVue 主版本必须先获 ADR 批准」；补充 token 使用规则、选择器规则和
  无障碍要求。
- 根 `CLAUDE.md`：当前实施目标改为 M2.1，指向本规格与实施计划；M2 边界保持不变。
- README：更新技术栈、浏览器基线和前端开发说明。
- `docs/acceptance-checklist.md`：新增 M2.1 段落，验收项与第 15 节一致。

## 15. 验收标准

- [ ] 全部 9 个页面与 25 个组件完成迁移，`src/**/*.vue` 中不存在十六进制颜色、
      `@media` 查询和非 Tailwind 的 scoped 布局样式。
- [ ] `pnpm-lock.yaml` 中 PrimeVue 相关包全部为 4.5.5 且许可为 MIT，不存在
      `@primeui/*` 包；许可证检查通过。
- [ ] 桌面三栏、平板双栏、手机单栏三种布局各有 Playwright 截图与 axe 零 serious/critical
      结果。
- [ ] 真实 live region 基线已冻结，既有 `status`、`alert`、`dialog` 语义与文案逐项保留；
      数量变化逐项记录原因，运行时测试证明无漏报/重复播报，禁止空 role 凑数。
- [ ] 所有 `Dialog`/`Drawer` 通过键盘打开、关闭并返回焦点，有组件测试。
- [ ] 服务端 `error_code` 映射安全表单级错误，单测覆盖表单已知码及未知码 fallback；
      zod 提供字段格式错误，不新增或伪造 `errors` 契约。
- [ ] 零浏览器存储写入断言继续通过。
- [ ] 首屏 JS gzip 增量与 CSS gzip 在第 11 节预算内，CI 日志留有记录。
- [ ] 最终文档提交后追加运行 `just ci` 并留档输出；证据正文记录构建基准 HEAD，
      最终提交的 CI 日志另存，不循环修改自引用 SHA。
- [ ] 前端 `AGENTS.md`、`CLAUDE.md`、README、验收清单已更新。

## 16. 风险与控制

| 风险 | 控制 |
|---|---|
| PrimeVue 4.x 功能冻结，未来出现无法绕过的缺陷 | 主版本锁定 + 许可检查阻止误升级；迁移路线由 ADR 决定，本里程碑不预建兼容层 |
| styled 模式注入内联样式与未来 CSP 冲突 | 预留 `csp.nonce` 通道；M2.2 规格决定策略 |
| 展示层重构误改行为层 | 行为层文件在 M2.1 视为只读；SSE、Store、API 测试不允许修改；发现缺陷单独 `fix:` |
| PrimeVue 组件 DOM 结构导致 E2E 大面积失败 | 迁移前先把选择器改为角色/label；禁止依赖内部 class |
| 包体积膨胀 | 按需自动导入、体积预算、CI 报告 |
| Tailwind 4 浏览器基线提升 | README 明示；单管理员产品可接受 |
| 两套样式长期并存 | 每页迁移完即删旧 CSS；步骤 16 的 CI 检查转为强制 |
| 一人维护范围失控 | 固定 17 步顺序，一步一提交；新增功能一律拒绝 |

## 17. 设计完成条件

本规格已明确 M2.1 的目标、范围、技术选型与许可、设计系统、应用骨架、页面组件映射、
表单校验、状态呈现、无障碍、安全、构建、测试、迁移顺序、文档更新、验收标准和风险。
用户批准后，编写 `docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md`
任务级实施计划，并按第 14 节更新协作规则文档后开始实施。
