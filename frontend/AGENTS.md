# 前端协作指南

## 适用范围

本文件适用于 `frontend/` 下的 Vue 应用、组件测试和 Playwright E2E，并继承根目录 `AGENTS.md`。本文件不得放宽根文件中的 M1 范围、安全、隐私、审批和真实性要求。

## 前端定位与目录边界

前端提供类似 ChatGPT 的可信任务交互体验，展示对话、每日简报、来源、任务状态、执行时间线、审批、连接和设置。目标结构：

```text
frontend/
├── package.json
├── src/
│   ├── api/                 # REST 客户端、Problem Details、API/SSE 类型
│   ├── components/          # 可复用展示与交互组件
│   ├── composables/         # SSE 生命周期等复用行为
│   ├── features/            # 按业务能力组织的组合逻辑
│   ├── pages/               # 路由级页面
│   ├── router/
│   └── stores/              # Pinia 与事件 reducer
└── e2e/                     # Playwright 用户流程
```

页面负责组合，组件负责清晰的展示或交互，Store 负责可共享状态，`api/` 负责所有服务端通信。不要把请求、事件归并和复杂领域判断散落在页面组件中。

## 开发与验证命令

优先从仓库根目录运行：

- `just web`
- `just test-frontend`
- `just test-e2e`
- `just lint`
- `just typecheck`
- `just check`
- `just ci`

聚焦调试可使用 `pnpm --dir frontend test:unit --run <pattern>`、`pnpm --dir frontend type-check`、`pnpm --dir frontend lint` 和 `pnpm --dir frontend build`。依赖必须通过 `pnpm` 更新并提交 `pnpm-lock.yaml`；禁止使用 npm 或 yarn 生成第二份锁文件。

## Vue 与 TypeScript 编码规范

- 使用 Vue 3 Composition API 和 `<script setup lang="ts">`，保持 TypeScript strict，不用 `any`、`@ts-ignore` 或非空断言掩盖未知状态。
- 导出的组件、Props/Emits 契约、composable、Store、API 函数、事件 reducer 及复杂工具函数必须使用详细中文 TSDoc/JSDoc，按 `@param`、`@returns`、`@throws` 等标准标签说明输入、输出和异常。复杂模板区块、响应式副作用、SSE 重放、状态归并及安全清洗逻辑使用中文行内注释解释意图和边界，但不得用注释逐句复述直观代码。
- Props、Emits、Slots、API 响应、SSE 事件和 Store state 必须显式类型化；不可信 JSON 在进入状态层前验证或安全收窄。
- 组件保持单一职责。页面级数据编排放在 feature/composable/store，不创建同时负责请求、状态机、Markdown 和布局的巨型组件。
- 计算值使用 `computed`，副作用使用范围明确的 `watch`/生命周期；清理定时器、事件监听和连接，避免组件卸载后的写入。
- 不复制服务端领域规则。冲突、分类、审批有效性和任务状态迁移以服务端结果为准，前端只做展示和允许操作判断。
- API 保持服务端 `snake_case` 契约；如确需映射，集中在 `api/`，不得在组件中混用两套字段名。
- 延续现有设计 token 和组件模式；未获批准不要引入新的 UI 框架、状态库、请求库或 CSS 体系。
- 所有交互必须提供加载、空数据、失败、部分成功、断线和恢复状态，不把失败隐藏成空白页面。

## API、状态与 SSE 约束

- REST 请求只能通过 `src/api/` 的统一客户端发起，默认携带 Cookie，并统一解析 RFC 9457 Problem Details、`error_code` 和 `trace_id`。
- CSRF Token 由认证状态和 API 客户端管理；OAuth Token、模型密钥和会话秘密不得进入浏览器存储。
- PostgreSQL/服务端快照是持久状态来源。Pinia 和浏览器存储只是客户端投影，页面刷新后必须能从 API 恢复。
- `useTaskEvents` 统一管理 EventSource 生命周期、重连、心跳超时和可见连接状态；页面组件不得各自实现 SSE。
- 持久事件按 `sequence` 排序和去重；使用 `Last-Event-ID` 补齐遗漏事件。检测到过期或序列间隙时重新获取任务快照。
- `assistant.delta` 和 `heartbeat` 是可丢失临时事件；最终 AssistantMessage、任务结果和审批状态必须以服务端持久记录覆盖临时 UI。
- 不能仅因连接断开就把任务标记为失败，也不能因收到文本增量就提前标记成功。
- Store 中的事件 reducer 必须是确定性的，并能安全处理重复、乱序、刷新恢复和未知向后兼容事件。
- 前端不得直接访问 PostgreSQL、Redis 或供应商 API；所有数据和动作通过经过认证授权的后端接口。

## 交互与可访问性约束

- 桌面端维持左侧导航、中间内容、右侧可折叠任务时间线；移动端转换为抽屉或独立页面，不强行压缩三栏。
- 重要简报结论展示来源引用、数据截止时间和完整性；部分成功必须明确指出缺失来源及最后同步时间。
- 审批卡片展示工具、精确载荷预览、风险和过期状态；批准/拒绝期间禁用重复提交，并正确处理 409 冲突。
- 错误提示给出用户可执行的恢复动作和 `trace_id`，不要直接展示堆栈、原始 HTML 或供应商敏感响应。
- 交互元素使用语义化 HTML，支持键盘操作、可见焦点和程序化 label；状态变化需要可被辅助技术感知。
- M1 不显示可用的文件上传、Outlook、知识库、长期记忆、通用 Planner 或多 Agent 入口。

## Markdown 与浏览器安全

- Markdown 使用 `markdown-it` 渲染，并在插入 DOM 前使用 DOMPurify 清洗。
- 禁止把模型、邮件、日历或 API 返回的字符串未经清洗交给 `v-html`、动态脚本、样式或 URL。
- 默认禁用原始 HTML；链接协议仅允许明确白名单，外部新窗口链接添加 `rel="noopener noreferrer"`。
- 不把凭据、完整邮件正文或敏感 Prompt 写入 console、错误上报、快照、测试报告、URL query 或 localStorage。
- 前端隐藏按钮不是授权措施；所有敏感操作必须由服务端重新认证、授权和验证审批版本/载荷哈希。

## 前端测试要求

- Vitest 与 Vue Test Utils 测试组件、Store、composable 和事件 reducer；测试靠近对应源文件，名称描述用户可观察行为。
- 组件测试覆盖加载、空、成功、部分成功、错误、断线、重试、审批冲突和无障碍交互，不能只依赖大块快照。
- SSE 测试覆盖重复/乱序事件、断线重连、序列间隙、快照兜底、临时 delta 丢失和最终消息替换。
- Playwright 覆盖登录、连接状态、手动生成简报、查看来源、任务时间线、刷新恢复、SSE 断线、批准/拒绝/过期和错误提示。
- E2E 使用可控后端、Fake 外部工具和合成数据，不连接真实 Gmail、Calendar 或模型账号。
- UI 行为变更至少运行聚焦单测、`pnpm --dir frontend type-check` 和相关 E2E；发布前还必须通过生产构建与 `just ci`。

## 修改策略

- 后端契约变化时先更新 `src/api/` 类型与解析，再更新 Store、组件和测试，避免在各页面临时兼容。
- 新增页面时同时考虑路由保护、导航入口、加载/错误状态、窄屏布局、键盘操作和 E2E 覆盖。
- 新增持久任务事件时同步更新事件联合类型、reducer、时间线文案、重放测试和未知事件兜底。
- 视觉修改复用现有组件和 token，不顺带重构无关页面；大范围视觉系统变更必须先获得设计确认。
- 不持久化可从服务端恢复的敏感或权威状态；确需 localStorage 的非敏感偏好必须版本化并提供默认值。

## 文档与 Git 补充规则

- 用户流程、路由、环境变量、API/SSE 契约或浏览器兼容要求变化时同步更新相应文档。
- 截图和测试 fixture 必须使用合成数据并检查元数据；不得提交包含真实邮件、姓名、地址或 Token 的产物。
- 前端提交不要混入自动生成的 `dist/`、Playwright 报告、coverage、临时截图或无关锁文件变更。

## 前端 Agent 行为约束

- 不以“后端以后会补”为前提伪造成功、审批或持久状态；缺少契约时先明确接口并协调修改。
- 不为了展示效果加入 M1 范围外的可操作入口，也不把静态占位符做成看似可用的功能。
- 诊断交互问题时分别验证 API 快照、事件流、Store reducer 和组件渲染，不能仅凭截图猜测根因。
- 声称页面可用前必须运行类型检查、相关测试，并在涉及关键流程时执行浏览器级验证。
