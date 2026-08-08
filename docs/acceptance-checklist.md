# M2 验收清单

每项证据必须记录日期、执行人、目标环境、提交 SHA/不可变镜像标签、合成数据标识或本地哈希后的
专用测试账户标识、执行命令、预期与实际结果，以及截图、审计事件或受控产物链接。涉及真实供应商
写入时还必须记录冻结命令哈希、审批 ID、唯一 ToolExecution ID、只读核对结果和清理结果；不得记录
Token、Cookie、OAuth 凭据、Secret、原始允许列表身份键、完整邮件正文、日程敏感内容或真实个人
资料。失败或环境阻塞必须明确标记。

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

## 发布门禁

- [ ] 保存本轮 `just ci` 的完整成功输出、退出码、提交 SHA 与不可变镜像标签；不得用历史输出或聚焦测试替代。
- [ ] 保存部署合约脚本、生产 Compose 渲染、进程健康检查、数据库迁移、备份/恢复和回滚演练的完整输出摘要。
- [ ] 汇总 scope 审计、敏感输出扫描、专用 Google/Microsoft 账户 E2E、唯一 ToolExecution 审计与调用后崩溃恢复证据，并由发布审批人逐项签署结果。
