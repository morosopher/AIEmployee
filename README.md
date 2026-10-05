# AI Employee

AI Employee 是面向单管理员长期使用的可信办公助手。M1「可信任务中心 + 每日办公简报」已经
交付；M2「可执行邮件与日历助手」已于 2026-09-15 完成验收，详见
[发布证据](docs/releases/2026-08-06-m2-release-evidence.md)。M2.1「前端组件库重构」已于 2026-10-05 完成，见
[M2.1 验收记录](docs/releases/2026-09-19-m2-1-frontend-refactor-evidence.md)。当前展示层使用 PrimeVue 4.5.5（MIT）+
Tailwind CSS 4；完整任务历史及 SSE 取消清理仅按已批准补充交付，M2 功能与安全边界保留。
规格见 [M2.1 设计](docs/superpowers/specs/2026-09-19-frontend-component-refactor-m2-1-design.md)。
后续 M2.2「上线收尾」尚无获批规格，不得提前实施。
M2 保留 PostgreSQL 业务事实、
任务审计、Outbox、Checkpoint、SSE 重放和崩溃恢复底座，并增加 Google 与 Microsoft 邮件/日历
连接、只读增量同步、本地草稿与提案，以及受控的真实写入。

真实外部写入默认关闭，且严格限于 `mail.send`、`calendar.create`、`calendar.update`、
`calendar.restore`。每次写入都必须通过全局开关、供应商开关、连接能力、适用环境的专用账户
白名单、冻结载荷哈希和单次人工审批；请求结果未知时只允许只读核对或人工确认，不得盲目重放。

## 快速开始

在当前开发分支的仓库根目录复制 `.env.example` 为本地 `.env`，按
[本地 Compose 启动说明](docs/operations.md#本地-compose-开发与专用账户验收) 准备未提交的
Secret 文件、宿主 UID/GID 和不可变镜像标签，然后执行：

```bash
just doctor
just dev
```

`just dev` 会构建镜像并依次完成数据库角色初始化、迁移和服务启动。保持该终端运行，另开终端
在同一目录创建首次管理员；密码文件须事先准备，内容不会进入命令参数：

```bash
just create-admin admin@example.test secrets/development/admin_password compose
just health
```

浏览器访问 `http://localhost:5173` 后登录；OAuth 回调经 `http://localhost:8000` 进入 API，
成功后自动返回连接页。点击“启用”后还需完成“继续授权”；未完成或刷新丢失链接时可重新授权。
管理员创建拒绝覆盖已有身份，项目不提供公开注册。使用宿主进程开发时，先 `just bootstrap`，
并为宿主配置可达数据库及 Secret 绝对路径；`just create-admin email password_file` 默认在宿主执行。
自动化测试使用合成数据时设置 `APP_ENV=test` 与 `APP_TEST_MODE=true`，二者缺一不可，且不得用于生产。
默认的三层写入开关保持关闭，常规开发、测试和演示不得连接个人 Google/Microsoft 账户或执行
真实外部写入。

登录后可从主导航进入 `/actions` 操作中心，按草稿、提案、待审批、执行或核对、人工确认和
历史查看服务端状态。列表支持筛选与分页；真实任务可展开只读详情、供应商检查链接和审计时间线，
窄屏以独立区域展示详情。重新聚焦或恢复连接会重读快照，敏感预览只驻留内存，内容到期后显示
保留的执行历史。本地编辑对象保留自身标识，操作列表不直接提供编辑、审批或重新发送入口。

## 常用命令

```bash
just doctor
just test
just test-integration
just test-e2e
just lint
just typecheck
just check
just ci
just health
just backup
just restore backup-file
```

`just dev` 启动完整开发进程，`just infra-up` 只启动 PostgreSQL 和 Redis。部署构成、Caddy、
备份恢复与 observability profile 见 [运行手册](docs/operations.md)。M2 验收已完成当时镜像的合成部署、
HTTPS、加密备份恢复及旧格式隔离转换，[验收清单](docs/acceptance-checklist.md) 记录实际覆盖范围。
生产环境尚未部署；非空生产 0019 升级仍须执行运行手册中的专用维护窗口流程。

M2 自动化发布检查从仓库根目录执行 `bash scripts/test-m2-release.sh`。运行前按运行手册准备
固定 Task13 测试 PostgreSQL、独立 Redis DB15、合成 Secret、浏览器与 Docker Compose；脚本在
第一个子命令前拒绝其他数据库地址，随后顺序执行完整 CI、显式审计、故障/迁移/恢复矩阵和敏感
输出扫描。直接运行集成 pytest 与浏览器测试也会复用受管临时数据库，原始测试库只作为只读锚点。

自动化使用 Google/Microsoft Fake，覆盖本地邮件及日程编辑、冻结审批、结果核对和人工确认。
独立 Worker 演练会杀死真实 Taskiq 进程组，以原 pending 消息恢复并核对唯一 ToolExecution；
浏览器结果与进程/队列证据分别记录。敏感扫描在输出生产者全部退出后绑定原始输出摘要，报告和
Secret 不进入提交。M2 验收的完整发布门禁、独立审查和另行授权的 12 项专用账户操作均已通过；真实写入
开关已关闭。发布证据区分各候选版本的执行事实，Microsoft 另一账户类型只完成自动化契约验证。

## 架构与连接

FastAPI API、Taskiq Worker 和 Scheduler 是独立进程；应用层协调领域规则和供应商无关端口。
PostgreSQL 是连接、任务、审批、ToolExecution、审计、Outbox 与 Checkpoint 的唯一事实来源，Redis
仅承载队列、通知和可重建协调数据。Google 与 Microsoft OAuth 按连接渐进申请 `mail.read`、
`mail.send`、`calendar.read`、`calendar.write` 最小委托 scope，Token 以版本化 AEAD 密文保存。

## 前端技术栈

前端是 Vue 3 + TypeScript + Vite 单页应用，状态用 Pinia，路由用 vue-router，服务端通信统一经
`frontend/src/api/`。UI 技术栈固定为 PrimeVue 4.5.5（styled 模式 + 定制 Aura 预设）、
PrimeVue Forms + zod、Tailwind CSS 4 与 `tailwindcss-primeui`；设计常量集中在 `src/design/`。
许可证与版本门禁拒绝 PrimeVue 5、`@primeui/*` 和 `@primeuix/themes` 3.x，保留规格 §3.2
的精确版本基线许可例外。Forms 仅承担设置、邮件与日程格式校验，领域规则仍由服务端裁决。

浏览器基线为 Chrome 111、Safari 16.4、Firefox 128 及以上；本轮自动化在 Chromium 执行，
未把三种视口等同于三个浏览器引擎。`just check` 已包含许可证、版本及严格样式检查；
`just ci` 在构建后限制首屏 JS gzip 相对迁移前基线的增量不超过 200 KB，CSS gzip 不超过
60 KB，并执行七视图 axe 与 375／900／1400 像素布局验证。axe 门禁要求 serious／critical
为零；保留的 minor／moderate 项和测试覆盖边界见验收记录。

## M2 范围边界

M2 不是完整邮箱或日历客户端。供应商草稿箱同步、附件、HTML/富文本邮件、转发、通讯录、批量
操作、日程删除/取消、重复日程写入、会议链接自动创建、参会人 Free/Busy、完整收件箱/日历、
共享邮箱或代理发送、文件上传、RAG、长期记忆、通用 Planner、通用工具或工具市场、多用户、产品
多 Agent 和 Kubernetes 均不在当前里程碑内。
