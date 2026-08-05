# AI Employee

AI Employee 的 M1 是单管理员的可信任务中心与每日办公简报。它只连接 Gmail 和 Google
Calendar 的最小只读权限，使用 PostgreSQL 保存业务事实、任务、审计、Outbox 与 checkpoint；
Redis 仅用于队列、通知和可重建的短期协调数据。M1 不执行真实外部写操作，假写工具始终受
冻结载荷和人工审批保护。

## 快速开始

在仓库根目录复制 `.env.example` 为本地 `.env`，通过 Secret 文件提供开发密钥，然后执行：

```bash
just bootstrap
just infra-up
just db-upgrade
just create-admin admin@example.test
just dev
```

浏览器访问开发前端后登录。首次管理员由 `just create-admin` 创建；项目不提供公开注册。开发
环境使用合成数据时设置 `APP_ENV=test` 与 `APP_TEST_MODE=true`，二者缺一不可，且不得用于生产。

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
备份恢复与 observability profile 见 [运行手册](docs/operations.md)。生产镜像和 Compose 配置已
提供，但仍须按 [验收清单](docs/acceptance-checklist.md) 在目标环境取得运行态证据；本仓库不把
静态检查或本地构建描述为生产验证。

## 架构与 OAuth

FastAPI API、Taskiq Worker 和 Scheduler 是独立进程；应用层协调领域规则和基础设施端口。SSE
持久事件可从 PostgreSQL 重放，浏览器在终态时会以快照对账。Google OAuth 仅申请 Gmail/Calendar
读取所需 scope，Token 以版本化 AEAD 密文保存，断开连接会删除本地凭据并尽力撤销远端授权。

## M1 范围外

M1 不包含 Outlook、真实邮件或日历写入、文件上传、RAG、长期记忆、通用 Planner、工具市场、多
Agent、多用户注册或 Kubernetes。测试和演示不得连接个人 Google 帐户或模型服务。
