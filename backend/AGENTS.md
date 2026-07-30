# 后端协作指南

## 适用范围

本文件适用于 `backend/` 下所有代码、迁移和测试，并继承根目录 `AGENTS.md`。本文件只细化后端规则；根文件中的 M1 范围、人工审批、数据真实性、安全和 Git 约束不可放宽。

## 后端定位与目录边界

后端负责身份与会话、REST/SSE 接口、可信任务状态、领域规则、Google 只读同步、模型调用、任务编排、持久化和后台进程。目标结构：

```text
backend/
├── pyproject.toml
├── alembic.ini
├── migrations/
├── src/ai_employee/
│   ├── api/                 # FastAPI 路由、依赖、Problem Details、SSE
│   ├── application/         # 用例和端口
│   ├── domain/              # 纯领域类型、状态机和确定性规则
│   ├── agents/              # LangGraph 图、状态和节点
│   ├── integrations/        # Google 与模型供应商适配器
│   ├── infrastructure/      # 数据库、安全、队列、可观测性
│   ├── workers/             # Taskiq 任务、调度、Outbox 和维护任务
│   ├── prompts/             # 有版本的 Prompt
│   ├── config.py
│   └── main.py
└── tests/
    ├── unit/
    ├── integration/
    └── contract/
```

## 开发与验证命令

优先从仓库根目录使用统一入口：

- `just api`、`just worker`、`just scheduler`
- `just test-backend`
- `just test-integration`
- `just lint`
- `just typecheck`
- `just db-upgrade`
- `just db-revision name`
- `just check`、`just ci`

聚焦调试可以运行 `uv run --project backend pytest <path> -q`、`uv run --project backend ruff check <path>` 或 `uv run --project backend mypy backend/src`，但最终验证仍以根 `just` recipe 为准。不得绕过 `uv.lock` 临时安装未记录依赖。

## Python 编码规范

- 使用 Python 3.12、完整类型注解、Ruff 和严格 mypy；公共函数、端口、领域类型和返回值必须显式标注。
- Python 模块、公共类与函数、应用用例、端口、LangGraph 节点及复杂私有函数必须提供详细中文 Docstring，并采用兼容 PEP 257 的三引号格式。涉及参数、返回值和异常时使用 `Args`、`Returns`、`Raises` 等固定分段，字段说明使用中文；复杂事务、重试、幂等、状态转换和安全判断还必须添加中文行内注释解释设计原因。
- `Any` 只能出现在无法控制的第三方解析边界，并应尽快验证、收窄为 Pydantic 或领域类型。
- I/O 路径使用 `async`；不得在事件循环中直接执行阻塞网络、文件或 CPU 密集操作。
- 优先绝对包导入 `ai_employee...`，避免跨层相对导入和循环依赖。
- 领域值使用清晰的枚举、值对象或 dataclass；不要用字符串常量和松散字典承载状态机或审批协议。
- Pydantic 用于配置、API、模型输出和适配器边界；SQLAlchemy ORM 类型只存在于基础设施层，不直接作为 API 或领域模型。
- 捕获最窄异常，保留异常链；禁止裸 `except`、静默吞错和把未知异常伪装为可重试错误。
- 日志使用结构化字段和稳定 `trace_id`，不得拼接或序列化完整正文、凭据、Header 或 Prompt。

## 分层与依赖规则

### Domain

- `domain/` 不得导入 FastAPI、Pydantic API Schema、SQLAlchemy、LangGraph、Taskiq、Redis、Google SDK、HTTP 客户端或模型 SDK。
- 任务状态机、审批不变量、日程冲突、邮件确定性分类、幂等规则属于 Domain，并应能在无数据库和网络时测试。
- 领域函数接收显式输入并返回确定结果；当前时间、ID 和随机性由调用方注入。

### Application

- `application/` 定义用例和端口，协调领域对象、事务与外部能力，但不依赖具体供应商实现。
- 一个用例表达一个业务意图，明确输入、输出、授权、事务边界和错误；路由与 Worker 只调用用例。
- Repository、Clock、ID、Queue、Model、Encryption、Gmail 和 Calendar 能力通过端口注入，测试使用 Fake。

### API

- API 统一使用 `/api/v1`；JSON 字段保持 `snake_case`。
- 错误响应遵循 RFC 9457 Problem Details，包含稳定 `error_code` 和 `trace_id`，不泄露内部堆栈或供应商敏感载荷。
- 路由只做认证授权、输入验证、调用用例和响应映射；不得放置领域判断或长任务。
- 长任务创建返回 202 与 `task_id`。修改类请求必须验证会话和 CSRF。

### Agents 与 Worker

- LangGraph 节点应小、可重入、可独立测试；Graph 负责顺序和中断，不把业务规则藏在 Prompt 或路由条件中。
- 使用 `task_run_id` 作为主要 `thread_id`，Checkpoint 保存在 PostgreSQL。
- 中断恢复可能重复执行前置节点；任何副作用必须位于批准节点之后，并拥有稳定幂等键。
- Taskiq 按至少一次投递设计。Worker 必须通过租约、心跳和状态检查安全处理重复消息及进程崩溃。
- API 或 Scheduler 在同一事务中写 `TaskRun` 与 `OutboxEvent`；只有 Outbox relay 可以把任务标识投递到 Redis Stream。
- Scheduler 每分钟扫描 PostgreSQL 中的到期计划并幂等创建任务；动态计划不得只存在于 Redis。

### Integrations

- 第三方 SDK、HTTP 请求和供应商字段只能存在于 `integrations/` 或相应基础设施适配器。
- 所有外部请求设置明确连接/读取/总超时，遵守 `Retry-After`，只对 429、5xx、超时等已分类临时错误重试。
- 供应商错误必须转换为内部错误类别，同时保存可审计但已脱敏的错误码。
- Gmail 与 Calendar 只申请和调用只读能力；测试中禁止访问真实账号。
- 模型输出先按版本化 Pydantic Schema 验证；只允许一次结构化修复重试，失败后保留确定性结果并标记部分成功。

## 数据库与事务规则

- 使用 SQLAlchemy 2 异步 API、显式 Session/事务和类型化 Mapping；避免隐式 I/O、跨事务 ORM 对象和不可见的 lazy load。
- 事务由应用用例控制，Repository 不自行提交。事务内不执行耗时模型或供应商网络调用。
- 每个用户域查询显式携带 `user_id`；任何新增 Repository 都必须有跨用户不可见的集成测试。
- 所有可重复触发的创建操作设计唯一约束或幂等键，并把重复冲突解释为已有结果，而不是制造第二份事实。
- 时间戳以带时区 UTC 保存；用户本地日期必须由显式 IANA 时区计算，并覆盖夏令时测试。
- Schema 变更必须附 Alembic migration、模型更新和迁移测试。生产迁移使用 expand/contract，避免长锁、立即删除列和破坏性 downgrade 假设。
- 任务状态迁移、业务结果、`AuditEvent` 和持久前端事件需要一致时，应在同一数据库事务中写入。
- `AuditEvent` 追加写；OAuth Token、清洗后正文和敏感事件字段按规格加密。原始 MIME、附件和完整供应商响应不得落库。
- Redis key 或 Stream entry 只能保存标识符、短期协调信息或可重建数据，不能保存唯一业务副本。

## 后端安全规则

- 配置通过 `pydantic-settings` 验证；生产密钥从 Secret 文件读取，禁止提供不安全的生产默认值。
- 密码使用 Argon2id；OAuth Token 使用带 `key_version` 的 AEAD；比较审批哈希和安全令牌时使用合适的安全比较。
- 认证、授权和 Repository 用户过滤是独立防线，不得只依赖前端隐藏资源。
- 对模型和日志执行最小披露与字段级脱敏；垃圾邮件正文不发送给模型。
- 不在普通异常、健康检查、指标或测试快照中输出 DSN 密码、Token、Cookie、正文和个人数据。

## 后端测试要求

- `tests/unit/` 不依赖网络、PostgreSQL 或 Redis，覆盖领域状态、审批、分类、冲突、用例和 Graph 路由。
- `tests/integration/` 使用真实 PostgreSQL/Redis 测试事务、迁移、Repository、Outbox、Taskiq、Checkpoint、SSE 重放和恢复。
- `tests/contract/` 使用脱敏固定 Gmail/Calendar JSON 与 HTTP mock，覆盖分页、游标失效、429、5xx、撤销授权和异常字段。
- 每个缺陷修复必须新增能在修复前失败的回归测试；涉及重试/幂等时至少模拟重复请求或重复队列投递。
- 关键必测不变量：非法状态迁移被拒绝、批准载荷不可篡改、跨用户不可见、Redis 清空可恢复、Worker 崩溃可续跑、部分失败不伪装为成功。
- Prompt 或模型变更必须运行脱敏/合成评测集，记录模型名、Prompt 版本和结构化输出差异；不得只凭主观样例判断质量。

## 修改与文档策略

- 新增端点时同步更新请求/响应 Schema、应用用例、权限、错误码、前端类型和契约测试。
- 新增持久字段时同步更新 ORM、Migration、Repository、保留/删除策略、备份影响和测试 fixture。
- 新增任务节点时明确输入/输出、超时、重试类别、幂等键、审计事件、用户时间线文本和恢复语义。
- Prompt 文件必须版本化，代码记录版本；不要在 Python 字符串中散落长 Prompt。
- 后端运行、迁移、凭据轮换、数据保留或恢复方式变化时更新 `docs/operations.md` 和 `.env.example`。

## 后端 Agent 行为约束

- 修改前先确认目标属于 Domain、Application 还是 Adapter，不得以“更快”为由跨层直连。
- 诊断失败时先复现并区分业务错误、临时供应商错误、基础设施错误和程序不变量错误，再提出修复。
- 不执行真实邮件发送、日程创建、OAuth scope 提升或生产数据库破坏性操作。
- 不以单元测试替代数据库/队列边界验证；声称迁移、恢复或幂等正确前必须运行对应集成测试。
