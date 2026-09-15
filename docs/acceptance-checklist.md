# M2 验收清单

每项证据必须记录日期、执行人、目标环境、提交 SHA/不可变镜像标签、合成数据标识或本地哈希后的
专用测试账户标识、执行命令、预期与实际结果，以及截图、审计事件或受控产物链接。涉及真实供应商
写入时还必须记录冻结命令哈希、审批 ID、唯一 ToolExecution ID、只读核对结果和清理结果；不得记录
Token、Cookie、OAuth 凭据、Secret、原始允许列表身份键、完整邮件正文、日程敏感内容或真实个人
资料。失败或环境阻塞必须明确标记。

任务级验证不等于发布验收。本轮验收日期为 2026-09-15，结果与原件摘要见[正式发布证据](releases/2026-08-06-m2-release-evidence.md)。
勾选表示本轮 fresh/zero-bootstrap 分支已取得相应证据；非空生产 0019、sealed 回退及事故竞争分支
使用明确列出的合成自动化证据，生产部署未执行。专用 Google 与 Microsoft personal 账户的 12 项真实
操作由用户逐项批准，另一 Microsoft 账户类型完成契约验证。候选以验证基准 HEAD 与完整源码摘要共同
绑定，首四项历史候选执行不改写为最终候选结果。操作步骤以[运行手册](operations.md)为准。

## 范围、权限与默认关闭

- [x] 完成 API、Worker、前端入口、命令注册表和 OAuth scope 审计，证明真实写动作仅有 `mail.send`、`calendar.create`、`calendar.update`、`calendar.restore`，且未出现附件、HTML 邮件、转发、Contacts、日程删除/取消、重复日程写入、通用工具、多用户或产品多 Agent 能力。
- [x] 保存 `.env.example` 与生产/开发 Compose 渲染证据，确认全局、Google、Microsoft 写入开关均默认 `false`，非生产启用任一供应商写入时空 `WRITE_TEST_ACCOUNT_ALLOWLIST` 会拒绝启动；同时审计允许列表仅接受三段 canonical key、规范大写 `%HH` 与既定长度边界，所有异常格式 fail closed 且不输出完整身份键。
- [x] 分别审核 Google 与 Microsoft 的渐进委托 scope，证明每个连接只按能力申请 `mail.read`、`mail.send`、`calendar.read`、`calendar.write`，未申请 Contacts、Gmail Draft、Microsoft `Mail.ReadWrite`、应用权限或目录管理权限。

## 功能、审批与唯一执行

- [x] 以合成数据回归单管理员登录、每日简报、对话、任务历史、Outbox、Checkpoint、SSE 重放、部分失败和刷新恢复，附任务 ID、事件序列及 PostgreSQL 快照证据。
- [x] 保存 Google/Microsoft Fake 浏览器矩阵：新邮件/回复/全部回复、日程创建/修改/恢复、版本冲突、批准/拒绝/过期/失效、刷新/SSE 重连、部分可用、重新授权/管理员同意、只读核对及两种人工结果；同时核对键盘焦点、live region 和窄屏布局。测试支持只提供合成来源和场景，编辑、提交与审批必须走正式边界。
- [x] 使用另行授权的专用 Google 与 Microsoft personal 测试账户完成端到端验收；只记录本地哈希后的专用账户标识、三层开关状态、连接能力、允许列表命中结果和资源处置结果，不使用日常个人账户或生产账户。原始允许列表身份键只存在于带外受控配置，不进入提交的证据、文档、日志、审计、SSE 或 API。
- [x] 对四种写命令分别保存结构化审批预览、不可变版本、规范序列化哈希、批准/拒绝/过期/篡改结果，证明一次审批只授权一个冻结命令。
- [x] 对每次已批准写入记录唯一 ToolExecution ID、幂等键、认领事务、供应商结果、只读核对与追加审计事件，证明重复 API 请求、重复队列投递和 Worker 接管均不会产生第二次外部写入。
- [x] 在供应商调用成功后、结果持久化前注入进程崩溃，保存恢复时间线，证明任务进入 `reconciling` 并通过只读核对收敛，而不是盲目重放写请求。
- [x] 对两个 Fake 供应商的四种命令分别记录认领前、认领后/request-start 提交前、请求后/响应前、响应后/结果提交前、结果提交后/ACK 前及四次核对槽位的事务故障矩阵；每例验证唯一 ToolExecution、按边界为零或一次写入，最终只允许成功、确定失败或 `needs_attention`。
- [x] 单独保存真实 Taskiq `SIGKILL` 演练：预先配置测试时序参数，进程组退出后的独立快照到 replacement 启动前保持数据库期限/状态与原 pending ID/owner/交付次数/载荷不变，由真实时钟和 `XAUTOCLAIM` 恢复。确认同一 entry 再次交付并 ACK、请求时间与执行 ID 不变、实际写次数不增加；不得把 BaseException 注入或崩溃后改写时间事实作为该证据。
- [x] 注入超时、连接中断、语义不明 5xx、ETag 冲突和能力撤销，记录 `needs_attention`、人工结果确认竞争及恢复动作，证明未知结果不会被伪装成成功或可安全重试。

## 隐私、安全与恢复

- [x] 对 JSON 日志、异常、Trace、指标、审计、SSE、API 响应和测试报告执行敏感输出扫描，附扫描命令、规则版本与零泄漏结果，确认不存在 Authorization、Cookie、Token、Secret、允许列表身份键、完整正文或敏感 Prompt。
- [x] 分别保存已审 fixture 的 schema/值静态扫描，以及地址、主题、正文、标题、描述、地点、参会人、Token、Cookie、Prompt 十类运行时 canary 的到达与输出证据。扫描真实子进程、浏览器、JUnit/Playwright、指标和 Trace 原始字节及常用编码；先证明所有输出生产者退出，再哈希绑定输入与完整输出清单，禁止用预先脱敏的日志冒充零泄漏。
- [x] 在隔离环境完成一次加密 PostgreSQL 备份与恢复演练，记录备份哈希、恢复目标、行数/关键约束校验和资源处置结果；确认连接、审批、ToolExecution、审计、Outbox 与 Checkpoint 可一致恢复。本轮合成服务停止，容器/卷/三件套保留用于审计，legacy 临时资源由正式转换入口精确清理。
- [x] 验证 Redis 清空、Outbox 重投、SSE 断线和 Worker 请求前/请求后崩溃恢复，附故障注入时间、任务状态变化和无重复外部写入证据。

## 生命周期与删除恢复

- [x] 以合成数据验证 30/180/365 天边界、未来日程保留、邮件正文/审批与日程快照/审批同事务清空，CalendarEvent 描述/地点各四列同时清空；覆盖未认领取消、未知结果保留最小核对事实、终态结果不变及中途回滚。
- [x] 验证邮件 source-thread/source-message 任一非空、日程 target-event 非空的精确来源绑定；独立草稿与 `calendar.create` 保留，跨用户不可读/不可删，缓存删除零供应商写入。
- [x] 保存 app/retention 真实角色证据：普通历史连续持有 TaskRun→user 锁跨越 app 原生三表 checkpoint 清理及父任务删除，迟到 saver 拒绝；全数据恢复同样覆盖三表、保留 `checkpoint_migrations`，无新增角色/grant，失败保留父任务。
- [x] 验证五种 OAuth refresh 审计被普通 cutoff 排除；confirmed、unsatisfied、replacement 各完整组仅在所有成员过期时原子删除；原 fence、畸形/重复组保留，后续 lineage 不参与清理。保存真实 writer 与 `EXCLUSIVE NOWAIT` 清理双方先提交、锁后完整重读、回滚和零额外 provider-call 证据。
- [x] 验证 `privacy.deletion_started` 与 inactive CAS 原子绑定，严格完整集合解析、两删除任务竞争、TaskRun/ToolExecution/request-start 双顺序竞争及迟到 OAuth/设置/人工确认/对话拒绝。确认 Settings 在 `DELETE ALL DATA` 输入前说明外部邮件/日程及未知结果可能已经生效。
- [x] 保存 `INACTIVE_ALL_DATA_RECOVERY` 的 acquire/renew、原 started_at 与新尝试预算、通用 timeout/error/retry 不改 RUNNING、各删除阶段崩溃及最终事务回滚证据；真实 Redis 丢失、Taskiq/Outbox 重投、五分钟桶并发去重及超过 limit 的零变更前缀不能饿死赢家。
- [x] 验证 `privacy_reconciliation_started` 在 GET 前提交且崩溃不重复；缺定位/能力/access 零网络，实际 GET 仍为 UNKNOWN，不解密命令、不 refresh/交换 code/写入。Google/Microsoft 撤销矩阵证明至多解密一个 token、先提交凭据删除、远端失败/不支持仍完成，恢复不重取已删凭据。
- [x] 断言最终只保留 inactive 匿名用户及冻结默认值、唯一无内容完成审计，赢家 TaskRun/authority/Outbox 与匿名化原子提交，ACK 丢失零重复审计。分别删除过期及隐私范围的 `database.restore.completed`，证明 completed call/completion zero-slot pair 不变且准入/ACK 仍成功，后续恢复能绑定唯一匿名用户。

## 迁移与灾备运维协议

- [x] 核对 Task 16A/27C 的迁移与运维边界、framed AAD 三组合成向量、当前 access expiry 重算的 `effective_deadline`（较短收紧、较长不延长原 artifact，历史 candidate 不授权）；合成矩阵验证 pre/post-migration 和 post-resync 审计及失败时服务保持停止。本次实际安装为 affected=0/no-deadline，非空生产维护窗口不适用。
- [x] 保存三个独立固定 framed v2 bytes/base64/长度/SHA 向量、三个 v1 digest 向量及 `refresh_token_identity_v1` HKDF/HMAC 向量；验证共享 helper、严格 UTF-8/不正规化、A/B 分隔不碰撞、拒绝旧 framing，以及解密→验证→identity→已提交 started/持有 lease→provider 顺序。
- [x] 保存 confirmed/unsatisfied/replacement 严格结果联合的真实提交与真实回滚、完整旧行 CAS、双 Worker 单调用、ACK 丢失后新会话核对与后续合法刷新/重新授权证据；自动与恢复 attempt 的闭合独立于当前 readiness。验证 disposition/flag/identity 不一致拒绝、仅 replacement 消费原 fence、changed=false 重加密不冲突、changed=true A→B→A 阻断且零额外 provider/code 调用。
- [x] 核对生产 Compose、开发 Compose、`just api`、E2E backend 四个 Uvicorn 入口的启动配置，并用真实子进程执行四组 access-log canary：synthetic code/state/error_description 在 stdout/stderr/JSON logs 零命中，脱敏 callback 审计与一次性消费正常；这不表示启动了生产部署。
- [x] 保存 canonical ACL SQL、四个 exact ACL tuple multiset、PostgreSQL 17 owner `is_grantable=false`、app/retention 安全属性及双向零 membership 的 catalog 证据；只允许冻结 fresh/legacy candidate，由 role-bootstrap 先收敛再 typed migration，Compose 不得 post-repair。
- [x] 证明 pre-step grant drift 在首笔 DDL 前拒绝、per-step new-object/policy delta 与 destination inventory 完整校验、0019-last-callback 失败全事务回滚。0016～0018 autocommit/concurrent-index 必须记录实际部分持久事实，只准精确 resume candidate，不伪称全回滚或引入 repair。
- [x] 明确记录 candidate 拒绝、fresh/zero-bootstrap、affected-without-guard、Fake guard 与 final 0019 guard 路径；所有入口持同一外层 schema-lifecycle exclusive lock，Compose 先 bootstrap 再 migration，禁止事后修复权限来补做准入。
- [x] 验证 Task27D schema/backup locks、pre/post revision CAS、manifest 三件套绑定与 manifest-last 发布；production management→target→schema 锁、三项 catalog facts、`.env.example`/独立 state-v4 配置和跨主机恢复均遵守手册，local projection 不能授权写入。
- [x] 保存 DB 零写 already-applied 本地证据、psql backend 注册前 SQL-byte-zero、deferred guard 下 pg_restore stream 中断回滚、新 call/reopen ordinal、active 建立时 RESET completion、completed pair 的 zero-slot 核对及精确 gate/ACL/role/membership/object-grant 原子 reopen；audit 可被清理，completed call 不可缺失。
- [x] 同一 ordinal 恢复时保持既有 call-start/pre/backend 事实；只有确定 `restore_not_applied` 才可新建 ordinal，重新冻结 exact-current pre、清空旧 backend 身份并记录新 call-start。保存管理→目标→schema 锁的低/高位固定向量、read-only artifact/独立 state-v4、真实 backend 退出和 owner-session `SET ROLE` 只读校验；completion-GUC、精确元数据及 BASELINE 权限恢复必须同事务提交。
- [x] 独立演练 registry/scavenger 管理的隔离 legacy conversion 与 artifact 独立的 sealed restore，后者沿用 generic/sealed 共享 primitive；obsolete 0019 仅非生产取证后受保护 reset，生产必须停止并等待已批准事故方案。不得混用 generic、sealed 或 legacy 证据。

## 发布门禁

- [x] 保存本轮 `just ci` 的完整成功输出、退出码、提交 SHA 与不可变镜像标签；不得用历史输出或聚焦测试替代。
- [x] 保存同候选 `just check` 和完整 `bash scripts/test-m2-release.sh` 的原始退出码、完整日志与前后源码清单。确认固定 Task13 URL 在首个子命令前单次 export，缺省/精确外部值被接受、其他地址零子调用；严格执行脚本中的 13 条命令，显式 AAD audit 与来源清理/删除赢家/恢复预算/无饥饿矩阵不得只由间接 CI 覆盖。
- [x] 保存部署合约脚本、生产 Compose 渲染、进程健康检查、数据库迁移、备份/恢复和回滚演练的完整输出摘要。
- [x] 对本轮实际构建的不可变后端镜像核对受管运维脚本及全部运行源码摘要；原生部署和备份/恢复证据绑定该镜像，历史镜像的通过结果保留各自范围。暂存区不得包含 `.env`、Secret、dump、coverage、浏览器报告或生成的私有发布产物。
- [x] 汇总 scope 审计、敏感输出扫描、专用 Google/Microsoft 账户 E2E、唯一 ToolExecution 审计与调用后崩溃恢复证据；保留用户逐命令应用内批准及控制器逐项只读核对结果，不把控制器核对写成用户代批。
- [x] 自动化和独立审查通过后，才请求专用 Google/Microsoft 账户及带外配置授权；真实新邮件/回复/全部回复/创建/修改/恢复矩阵和 Microsoft 另一账户类型的合同证据齐全后，才创建正式 release-evidence 文档并作发布决定。未授权时保持 Task30/M2 未完成。
