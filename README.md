# AI Employee

AI Employee 是面向单管理员长期使用的可信办公助手。M1「可信任务中心 + 每日办公简报」已经
交付；当前已批准并正在实施的 M2 是「可执行邮件与日历助手」。M2 保留 PostgreSQL 业务事实、
任务审计、Outbox、Checkpoint、SSE 重放和崩溃恢复底座，并增加 Google 与 Microsoft 邮件/日历
连接、只读增量同步、本地草稿与提案，以及受控的真实写入。

真实外部写入默认关闭，且严格限于 `mail.send`、`calendar.create`、`calendar.update`、
`calendar.restore`。每次写入都必须通过全局开关、供应商开关、连接能力、适用环境的专用账户
白名单、冻结载荷哈希和单次人工审批；请求结果未知时只允许只读核对或人工确认，不得盲目重放。

## 快速开始

在仓库根目录复制 `.env.example` 为本地 `.env`，通过未提交的 Secret 文件提供开发密钥，然后执行：

```bash
just bootstrap
just infra-up
just db-upgrade
just create-admin admin@example.test
just dev
```

浏览器访问开发前端后登录。首次管理员由 `just create-admin` 创建；项目不提供公开注册。开发
环境使用合成数据时设置 `APP_ENV=test` 与 `APP_TEST_MODE=true`，二者缺一不可，且不得用于生产。
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
just lint
just typecheck
just check
just ci
just health
just backup
just restore backup-file
```

`just dev` 启动完整开发进程，`just infra-up` 只启动 PostgreSQL 和 Redis。部署构成、Caddy、
备份恢复与 observability profile 见 [运行手册](docs/operations.md)。生产镜像和 Compose 配置仍须
按 [验收清单](docs/acceptance-checklist.md) 在目标环境取得运行态证据；静态检查或本地构建不能替代
发布验证。

## 架构与连接

FastAPI API、Taskiq Worker 和 Scheduler 是独立进程；应用层协调领域规则和供应商无关端口。
PostgreSQL 是连接、任务、审批、ToolExecution、审计、Outbox 与 Checkpoint 的唯一事实来源，Redis
仅承载队列、通知和可重建协调数据。Google 与 Microsoft OAuth 按连接渐进申请 `mail.read`、
`mail.send`、`calendar.read`、`calendar.write` 最小委托 scope，Token 以版本化 AEAD 密文保存。

## M2 范围边界

M2 不是完整邮箱或日历客户端。供应商草稿箱同步、附件、HTML/富文本邮件、转发、通讯录、批量
操作、日程删除/取消、重复日程写入、会议链接自动创建、参会人 Free/Busy、完整收件箱/日历、
共享邮箱或代理发送、文件上传、RAG、长期记忆、通用 Planner、通用工具或工具市场、多用户、产品
多 Agent 和 Kubernetes 均不在当前里程碑内。
