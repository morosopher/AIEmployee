# M2 验收清单

每项证据必须记录日期、执行人、目标环境、提交 SHA/不可变镜像标签、合成数据标识或本地哈希后的
专用测试账户标识、执行命令、预期与实际结果，以及截图、审计事件或受控产物链接。涉及真实供应商
写入时还必须记录冻结命令哈希、审批 ID、唯一 ToolExecution ID、只读核对结果和清理结果；不得记录
Token、Cookie、OAuth 凭据、Secret、原始允许列表身份键、完整邮件正文、日程敏感内容或真实个人
资料。失败或环境阻塞必须明确标记。

任务级验证不等于发布验收。以下均为待收集/签署的目标证据；Task27E 的合成回归与文档检查不表示
生产 0019、真实供应商测试或发布门禁已经执行。操作步骤以[运行手册](operations.md)为准。

## 范围、权限与默认关闭

- [ ] 完成 API、Worker、前端入口、命令注册表和 OAuth scope 审计，证明真实写动作仅有 `mail.send`、`calendar.create`、`calendar.update`、`calendar.restore`，且未出现附件、HTML 邮件、转发、Contacts、日程删除/取消、重复日程写入、通用工具、多用户或产品多 Agent 能力。
- [ ] 保存 `.env.example` 与生产/开发 Compose 渲染证据，确认全局、Google、Microsoft 写入开关均默认 `false`，非生产启用任一供应商写入时空 `WRITE_TEST_ACCOUNT_ALLOWLIST` 会拒绝启动；同时审计允许列表仅接受三段 canonical key、规范大写 `%HH` 与既定长度边界，所有异常格式 fail closed 且不输出完整身份键。
- [ ] 分别审核 Google 与 Microsoft 的渐进委托 scope，证明每个连接只按能力申请 `mail.read`、`mail.send`、`calendar.read`、`calendar.write`，未申请 Contacts、Gmail Draft、Microsoft `Mail.ReadWrite`、应用权限或目录管理权限。

## 功能、审批与唯一执行

- [ ] 以合成数据回归单管理员登录、每日简报、对话、任务历史、Outbox、Checkpoint、SSE 重放、部分失败和刷新恢复，附任务 ID、事件序列及 PostgreSQL 快照证据。
- [ ] 使用另行授权的专用 Google 与 Microsoft 测试账户完成端到端验收；只记录本地哈希后的专用账户标识、三层开关状态、连接能力、允许列表命中结果和清理结果，不得使用个人或生产账户。原始允许列表身份键只存在于带外受控配置，不进入提交的证据、文档、日志、审计、SSE 或 API。
- [ ] 对四种写命令分别保存结构化审批预览、不可变版本、规范序列化哈希、批准/拒绝/过期/篡改结果，证明一次审批只授权一个冻结命令。
- [ ] 对每次已批准写入记录唯一 ToolExecution ID、幂等键、认领事务、供应商结果、只读核对与追加审计事件，证明重复 API 请求、重复队列投递和 Worker 接管均不会产生第二次外部写入。
- [ ] 在供应商调用成功后、结果持久化前注入进程崩溃，保存恢复时间线，证明任务进入 `reconciling` 并通过只读核对收敛，而不是盲目重放写请求。
- [ ] 注入超时、连接中断、语义不明 5xx、ETag 冲突和能力撤销，记录 `needs_attention`、人工结果确认竞争及恢复动作，证明未知结果不会被伪装成成功或可安全重试。

## 隐私、安全与恢复

- [ ] 对 JSON 日志、异常、Trace、指标、审计、SSE、API 响应和测试报告执行敏感输出扫描，附扫描命令、规则版本与零泄漏结果，确认不存在 Authorization、Cookie、Token、Secret、允许列表身份键、完整正文或敏感 Prompt。
- [ ] 在隔离环境完成一次加密 PostgreSQL 备份与恢复演练，记录备份哈希、恢复目标、行数/关键约束校验和销毁结果；确认连接、审批、ToolExecution、审计、Outbox 与 Checkpoint 可一致恢复。
- [ ] 验证 Redis 清空、Outbox 重投、SSE 断线和 Worker 请求前/请求后崩溃恢复，附故障注入时间、任务状态变化和无重复外部写入证据。

## 生命周期与删除恢复

- [ ] 以合成数据验证 30/180/365 天边界、未来日程保留、邮件正文/审批与日程快照/审批同事务清空，CalendarEvent 描述/地点各四列同时清空；覆盖未认领取消、未知结果保留最小核对事实、终态结果不变及中途回滚。
- [ ] 验证邮件 source-thread/source-message 任一非空、日程 target-event 非空的精确来源绑定；独立草稿与 `calendar.create` 保留，跨用户不可读/不可删，缓存删除零供应商写入。
- [ ] 保存 app/retention 真实角色证据：普通历史连续持有 TaskRun→user 锁跨越 app 原生三表 checkpoint 清理及父任务删除，迟到 saver 拒绝；全数据恢复同样覆盖三表、保留 `checkpoint_migrations`，无新增角色/grant，失败保留父任务。
- [ ] 验证五种 OAuth refresh 审计被普通 cutoff 排除；confirmed、unsatisfied、replacement 各完整组仅在所有成员过期时原子删除；原 fence、畸形/重复组保留，后续 lineage 不参与清理。保存真实 writer 与 `EXCLUSIVE NOWAIT` 清理双方先提交、锁后完整重读、回滚和零额外 provider-call 证据。
- [ ] 验证 `privacy.deletion_started` 与 inactive CAS 原子绑定，严格完整集合解析、两删除任务竞争、TaskRun/ToolExecution/request-start 双顺序竞争及迟到 OAuth/设置/人工确认/对话拒绝。确认 Settings 在 `DELETE ALL DATA` 输入前说明外部邮件/日程及未知结果可能已经生效。
- [ ] 保存 `INACTIVE_ALL_DATA_RECOVERY` 的 acquire/renew、原 started_at 与新尝试预算、通用 timeout/error/retry 不改 RUNNING、各删除阶段崩溃及最终事务回滚证据；真实 Redis 丢失、Taskiq/Outbox 重投、五分钟桶并发去重及超过 limit 的零变更前缀不能饿死赢家。
- [ ] 验证 `privacy_reconciliation_started` 在 GET 前提交且崩溃不重复；缺定位/能力/access 零网络，实际 GET 仍为 UNKNOWN，不解密命令、不 refresh/交换 code/写入。Google/Microsoft 撤销矩阵证明至多解密一个 token、先提交凭据删除、远端失败/不支持仍完成，恢复不重取已删凭据。
- [ ] 断言最终只保留 inactive 匿名用户及冻结默认值、唯一无内容完成审计，赢家 TaskRun/authority/Outbox 与匿名化原子提交，ACK 丢失零重复审计。分别删除过期及隐私范围的 `database.restore.completed`，证明 completed call/completion zero-slot pair 不变且准入/ACK 仍成功，后续恢复能绑定唯一匿名用户。

## 迁移与灾备运维协议

- [ ] 核对 Task 16A/27C 的迁移与运维边界、framed AAD 三组合成向量、当前 access expiry 重算的 `effective_deadline`（较短收紧、较长不延长原 artifact，历史 candidate 不授权）；记录 pre/post-migration 和 post-resync 审计，审计失败时保持服务停止。
- [ ] 启动生产 Compose、开发 Compose、`just api`、E2E backend 四个 Uvicorn 入口的真实 access-log canary：synthetic code/state/error_description 在 stdout/stderr/JSON logs 零命中，脱敏 callback 审计与一次性消费正常。
- [ ] 保存 canonical ACL SQL、四个 exact ACL tuple multiset、PostgreSQL 17 owner `is_grantable=false`、app/retention 安全属性及双向零 membership 的 catalog 证据；只允许冻结 fresh/legacy candidate，由 role-bootstrap 先收敛再 typed migration，Compose 不得 post-repair。
- [ ] 证明 pre-step grant drift 在首笔 DDL 前拒绝、per-step new-object/policy delta 与 destination inventory 完整校验、0019-last-callback 失败全事务回滚。0016～0018 autocommit/concurrent-index 必须记录实际部分持久事实，只准精确 resume candidate，不伪称全回滚或引入 repair。
- [ ] 验证 Task27D schema/backup locks、pre/post revision CAS、manifest 三件套绑定与 manifest-last 发布；production management→target→schema 锁、三项 catalog facts、`.env.example`/独立 state-v4 配置和跨主机恢复均遵守手册，local projection 不能授权写入。
- [ ] 保存 DB 零写 already-applied 本地证据、psql backend 注册前 SQL-byte-zero、deferred guard 下 pg_restore stream 中断回滚、新 call/reopen ordinal、active 建立时 RESET completion、completed pair 的 zero-slot 核对及精确 gate/ACL/role/membership/object-grant 原子 reopen；audit 可被清理，completed call 不可缺失。
- [ ] 独立演练 registry/scavenger 管理的隔离 legacy conversion 与 artifact 独立的 sealed restore，后者沿用 generic/sealed 共享 primitive；obsolete 0019 仅非生产取证后受保护 reset，生产必须停止并等待已批准事故方案。不得混用 generic、sealed 或 legacy 证据。

## 发布门禁

- [ ] 保存本轮 `just ci` 的完整成功输出、退出码、提交 SHA 与不可变镜像标签；不得用历史输出或聚焦测试替代。
- [ ] 保存部署合约脚本、生产 Compose 渲染、进程健康检查、数据库迁移、备份/恢复和回滚演练的完整输出摘要。
- [ ] 汇总 scope 审计、敏感输出扫描、专用 Google/Microsoft 账户 E2E、唯一 ToolExecution 审计与调用后崩溃恢复证据，并由发布审批人逐项签署结果。
