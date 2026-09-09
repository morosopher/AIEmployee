# AI Employee 运行手册

本手册覆盖已交付的可信任务中心与每日办公简报 M1 基线，以及正在实施的 M2 邮件与日历助手目标发布契约。生产环境不连接测试账号，不以 Redis 作为业务事实来源；M2 真实写入仍必须遵守三层开关、连接能力和精确人工审批。

## 发布与回滚

发布前为 `APP_IMAGE_TAG` 指定不可变镜像标签或摘要，严禁使用 `latest`。M2 目标数据库生命周期顺序固定为
`role-bootstrap → typed migration`：前者先把唯一允许的 fresh/legacy candidate 原子收敛为可 admission 的
baseline posture，后者才运行普通 Alembic upgrade，并在每个 migration step 的同一事务内为本 step 新对象
或显式 policy delta 应用最小权限、核验完整 destination inventory。migration 不得在结束后重新运行
`init-db-roles.sh` 或任何权限“修复”脚本。确认该链路成功，且 API、Worker、Scheduler、PostgreSQL、Redis
均为 healthy 后，才切换 Caddy 流量。

应用镜像只能回滚到与当前数据库 revision 兼容的不可变标签，且绝不对生产数据库执行破坏性 migration downgrade。除下述明确要求停机排空的 0018/0019 contract revision 外，迁移采用 expand/migrate/switch/contract：旧应用必须能读取扩展后的 schema，删除旧列或表只能在所有旧应用退出后的后续发布完成。一旦 0019 完成恢复审计、启动任一通用服务或允许任一业务写入，应用回滚下限就是理解 0019 字段版本并且只写 v2 的 0019-compatible 镜像；无论 M2 Task 是否都已终态，都不得回滚到 pre-0019/M1、v1 reader 或旧 Calendar writer。只有下述尚未重开服务、无业务写入的 sealed maintenance window 才允许从本窗口迁移前整库备份恢复到 revision 0018；它不是 Alembic downgrade。切换到较旧但仍兼容 0019 的 M2 镜像前，仍必须确认所有 M2 Task 已终态；存在 `reconciling` 或 `needs_attention` 时只能部署保留核对能力的前滚修复镜像。

### M2 数据库 bootstrap、ACL 与 migration grant 契约

> **实施状态警告：** 仓库已提供 Task 16A/27D 的 typed CLI、共享 catalog helper 与 per-step migration
> grant guard。本小节同时保留发布验收约束；生产使用仍须完成当前里程碑发布门禁与目标环境演练，
> 本地合成测试不能替代生产证据，不得使用 shell SQL、migration 后置授权或人工 GRANT 绕过入口。

数据库 ACL 只能由共享 reader 从目标 `pg_database.datacl` 读取；`datacl IS NULL` 必须展开 PostgreSQL
默认 ACL，不能解释为“无权限”。规范 SQL 固定为：

```sql
SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid
```

令 `O=pg_database.datdba`、`P=PUBLIC/OID 0`、`A=ai_employee_app OID`、
`R=ai_employee_retention OID`。只允许以下四个完整排序 multiset；它们不是最低权限子集：

```text
fresh_default =
  (O, O, CREATE,    false)
  (O, O, CONNECT,   false)
  (O, O, TEMPORARY, false)
  (P, O, CONNECT,   false)
  (P, O, TEMPORARY, false)

pre_protocol_legacy = fresh_default +
  (A, O, CONNECT, false)
  (R, O, CONNECT, false)

baseline =
  (O, O, CREATE,    false)
  (O, O, CONNECT,   false)
  (O, O, TEMPORARY, false)
  (A, O, CONNECT,   false)
  (R, O, CONNECT,   false)

active =
  (O, O, CREATE,    false)
  (O, O, CONNECT,   false)
  (O, O, TEMPORARY, false)
```

四组中每个 tuple 的 `grantor` 都是 owner，`is_grantable` 都是 `false`。PostgreSQL 17 对 owner 的隐式
grant ability 不会把 catalog tuple 变成 `true`；owner 显式 `WITH GRANT OPTION` 同样是非法漂移。未知、
duplicate、额外或缺失 tuple、错误 grantor、`PUBLIC CREATE`、app/retention 的 CREATE/TEMPORARY、任一
其他 grantee 或任一 grant option 都 fail closed，禁止只看 effective CONNECT。

已存在的 app 与 retention 角色必须分别精确满足：`LOGIN=true`、`INHERIT=true`、
`SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS=false`、`CONNLIMIT=-1`、`VALID UNTIL NULL`、
`rolconfig NULL`；`pg_auth_members` 中不得存在以任一角色作为 `roleid` 或 `member` 的 row。密码不读取或
比较，只能从 Secret 在受控 transaction 内轮换。

`bootstrap_candidate` 只是持锁调用内的瞬态分类，不是第四个数据库状态。fresh/default 只允许两角色
同时缺失或同时已安全；pre-protocol legacy 要求两角色都已安全。mixed/missing/unsafe role、membership、
ACL/object-grant drift、任一 restore fact 或活动非 owner session 都在首笔写前拒绝。只有 typed
`role-bootstrap` 和受保护 `db-reset` 的 post-create 步骤可以执行 candidate→baseline；migration、restore、
owner one-off 与 0019 zero-bootstrap 都不能接受 candidate。

bootstrap 必须在同一 management→target→schema locks 与单一 owner transaction 内完成：仅在两个角色都
缺失时显式创建最小 LOGIN role；安全既有角色只轮换密码；执行 `REVOKE ALL PRIVILEGES ... FROM PUBLIC`；
只授予 app/retention owner-granted、non-grantable CONNECT；应用当前 revision 的完整最小 object grants；
提交前重读三项 restore facts 仍 absent、ACL 精确为 baseline、角色安全、双向无 membership、object grants
不多不少。任一 SQL、进程或最终核验失败都整体回滚。commit 后仍持锁并由普通 admission 重新读为
`pristine_idle`，随后才可迁移。

所有在线 Alembic 入口在 `context.run_migrations()` 前按 current revision 验证完整 schema/table/column/
sequence grant inventory；已有 missing/extra/wrong-grantor/grant-option drift 在首笔 DDL 前失败且不得修复。
同一个 typed migration grant guard 保存 current revision 与 catalog snapshot。每次 Alembic
`on_version_apply(*, ctx, step, heads, run_args)` 都在本 step operation 与 version-row mutation 后、事务提交前：

1. 验证 callback connection、upgrade/non-stamp、source/destination revision chain；
2. 比较本 step 前后 catalog，只对新对象或冻结的显式 policy delta 执行 grant/revoke；
3. 核验 destination revision 的完整 object-grant inventory；
4. destination 为 exact `20260809_0019` 时，最后才调用同一 Calendar guard 的 `before_commit`。

任一步失败都在同一 migration transaction 回滚 DDL/DML、Alembic version row 与 grant delta。非 0019
revision 也逐 step grant/verify；fresh upgrade 不需要第二个 owner session或 migration 后置修复。

首次安装与 Compose 顺序固定为 `role-bootstrap → typed migrate`；`scripts/init-db-roles.sh` 只能是
Secret-file-safe 的 typed CLI 薄 wrapper，不能包含 SQL、ACL parser 或 object-grant inventory。受保护
`db-reset` 的一个 live management lease 必须覆盖
`check → drop → create → candidate-to-baseline bootstrap → ordinary migration`；recipe 不得直接调用
`dropdb`/`createdb`，也不存在环境 skip lease。

### M2 CalendarEvent 0018 发布

`20260809_0018` 将 CalendarEvent 供应商身份从连接级二元组切换为 `connection_id + calendar_id + provider_event_id` 三元组。该 contract revision 不支持旧、新 Calendar writer 混跑，发布必须严格按以下顺序执行：

1. 关闭 Calendar 周期调度。
2. 排空并停止全部可能执行 `sync_calendar` 的旧 Worker，确认没有旧事件 upsert 事务仍在运行。
3. 应用 Alembic revision `20260809_0018`。
4. 部署引用三元约束的新 API、Worker 和 Scheduler，禁止旧 Worker 回流。
5. 恢复 Worker，再恢复 Calendar 调度。

生产环境不得对该迁移执行破坏性 downgrade。若迁移后已经存在跨日历相同 provider event ID，旧二元约束无法无损恢复，downgrade 必须 fail closed；不得删除、合并或改写事件行来强行回退，应停止发布并制定人工数据保留方案。

### M2 CalendarEvent 字段 AAD 0019 发布

> **实施状态：** 仓库已提供0019迁移、共享OAuth协调、真实artifact guard、无参数preflight/migration/
> resync入口，以及Task27D的完整manifest备份组、三阶段审计、独立generic/sealed restore和镜像内
> 固定脚本。下文保留完整目标发布顺序；Task27E、Task30与目标环境演练仍是发布前置条件。
> 单独dump/checksum或本地合成验证不能作为完整发布证据，不得用临时SQL或手工artifact替代门禁。

Task 16A 是仓库首次创建并发布 `20260809_0019` 的任务；在它之前不存在已发布的 0019。若旧
development/test 数据库曾被手工或实验性代码写入同名、未知或不完整的 obsolete 0019，先为该精确
非生产目标创建取证备份，然后只使用现有受保护的 `just db-reset`：按其要求设置
`APP_ENV=development|test`、精确本机数据库身份并输入完整数据库名确认。该 recipe 必须通过
`postgres` 管理库上的 target lifecycle advisory lock 包住 gate/authority 检查、drop、create 和普通
`upgrade head`；禁止继续直接调用未受锁保护的 `dropdb`/`createdb`。这里没有也不得新增“0019 repair”
recipe；禁止改写 migration 文件、静默
`stamp`、伪 downgrade 或直接修改 Alembic revision。生产发现任何未知、同名或 obsolete 0019 时立即
停止发布，保持 Caddy/API/Worker/Scheduler 与全部写入开关关闭；生产禁止 `db-reset`、revision/stamp
手术、直接 SQL 修复或 downgrade，必须等待新的已批准事故与数据处置方案。

`20260809_0019_calendar_event_field_aad_v2.py` 的 `down_revision` 是 `20260809_0018`，因此必须在 0018 完成后应用。描述与地点分别维护独立的 AAD 版本：v1 仅用于标记历史三元组；v2 只能通过共享纯函数
`calendar_event_field_aad_v2(...)` 生成以下 canonical bytes：

```text
b"AIEMPLOYEE/calendar-event-field-aad/v2\x00"
|| frame(user_id)
|| frame(connection_id)
|| frame(calendar_id)
|| frame(provider_event_id)
|| frame(field)

frame(raw_bytes) = uint32_be(len(raw_bytes)) || raw_bytes
```

domain 恰好 39 bytes。user/connection 必须是小写 canonical UUID ASCII；calendar/event ID 是不做 Unicode
normalization 的严格 UTF-8 原始标量字节，不要求 ASCII；field 只允许 `description | location`。五项各编码
一次，没有 delimiter、NULL tag、JSON 或旧冒号拼接 fallback。writer 与 reader 必须导入同一 helper，禁止
各自实现 framing、删除 `calendar_id`、正规化 opaque ID 或尝试 v1。

发布证据使用三组完全合成向量独立复算：delimiter A/B 总长均为 `146`，SHA-256 分别为
`67ba9e40f2157a49de2d987e1f4d8e61c2a78a7d71d4da7ce13421fde58a8e70` 与
`52e707318449a55779a023931f5914909d662944f5364545e1795edbcf555dad`；Unicode 向量总长 `157`，SHA-256 为
`0bdf656c1b037423e71c950df9f6bb5c56a737bcef8fe7d6e58e26d7dee04370`。对应 base64 固定为：

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAADYTpiAAAAAWMAAAALZGVzY3JpcHRpb24=
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAABYQAAAANiOmMAAAALZGVzY3JpcHRpb24=
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAAJ5pel5Y6GL86xAAAACeS6i+S7tjrDqQAAAAhsb2NhdGlvbg==
```

新 Calendar writer 只能写 v2；reader 遇到不可用 cipher、v1、未知版本、key-version mismatch、明确的
解密边界错误、无效 UTF-8 或 v2 `InvalidTag` 时，必须返回 `calendar_event_resync_required`，绝不尝试旧
冒号 bytes、v1、缺少 `calendar_id` 或未经认证明文等 fallback。

0019 变更窗口还必须绑定一个由 preflight 生成的 content-free rollout state artifact。对每个受影响 connection 只执行一次主动 OAuth refresh（按 connection UUID 去重并串行），并以刷新后持久化的 access credential `token_expires_at` 计算原始截止时间：
`rollout_deadline = min(token_expires_at) - 900 seconds`。900 秒是与现有 `task_timeout_seconds=900` 对齐的固定安全余量，不是可由运维输入的“足够新鲜”估计；所有时间使用 UTC。ACK-lost closure 与 current readiness 后，以及 backup、audit、migration、resync、restore eligibility 和 post-resync 的每个后续 guard，都必须重新读取每个 affected connection 当前持久化 access credential 的 expiry，计算 `current_deadline`，并使用 `effective_deadline = min(original_artifact_deadline, current_deadline)`。更短 expiry 必须立即收紧，更长 expiry 不得延长原窗口；历史 `oauth.refresh_confirmed.rollout_deadline_candidate` 只用于验证历史 result schema，绝不能作为当前 deadline 输入。若不存在受影响 pair，artifact 必须记录可核验的零值并将 deadline 标为不适用；后续 guard 只能在重新证明 affected set 仍为空后走该显式 no-deadline 分支，不能把 `null` 当作可比较时间或跳过集合核验。

preflight 还必须持有 revision-global PostgreSQL session lease。固定锁调用是 `pg_try_advisory_lock(20260809, 19)`，不包含 backup basename，因此相同或不同 basename 的并发窗口都互斥。CLI 使用独立数据库连接，在检查 rollout artifact 是否已存在、调用 OAuth/Calendar provider 或写 credential 前先 try-lock；失败以 `calendar_aad_rollout_locked` 返回，必须保持零 refresh、零持久写入。该连接贯穿完整 preflight 网络阶段且不承载跨网络业务事务；每次 provider call、credential commit 和 artifact 原子发布前都确认同一 session 仍持锁，连接断开或锁丢失立即 fail closed。进程退出或连接关闭后由 PostgreSQL 自动释放锁。

#### OAuth refresh/exchange 共享协调协议

revision-global lease 只解决 0019 窗口并发，不证明 OAuth refresh 的未知结果。automatic refresh 只有两类来源：0019 preflight 的 `calendar_aad_preflight`，以及 Google/Microsoft mail/calendar Worker 的 `provider_refresh`。它们必须通过供应商中立的 `OAuthRefreshCoordinator` 取得按 `connection_id` 隔离的 PostgreSQL session advisory lease；带 `connection_id` 的 explicit progressive recovery 使用同一 connection lease，但属于一次性 authorization-code 通道，不创建第二个 automatic started。无 target 的 `/provider/start` 不属于恢复通道。任何路径都不得调用 `ensure_connection` 覆盖既有行、无条件 upsert，或让适配器自行 refresh/retry。

每次 automatic claim 在短事务中按 connection → access credential → refresh credential → matching started audit 的顺序锁定并冻结 generation `G`、两行完整物理 snapshot、old `refresh_token_identity_v1` 与固定 identity key version。若存在没有被合法 matching confirmed 或 replacement result 关闭的 `oauth.refresh_started`，provider call 必须为零；否则提交 content-free `oauth.refresh_started`，事务提交成功后才允许进入 provider。started metadata 使用 `fence_schema_version="oauth_refresh_fence.v1"`，并精确绑定 `source`、canonical lowercase `refresh_attempt_id`、`connection_digest`、适用的 source/target revision 与 `rollout_digest_v1`、`fence_generation=G`、两个 pre-digest、old identity/key version 和稳定 `result_code`。网络阶段只保持 session lease，不持有业务事务；provider 前、响应后、CAS 前和结果提交前都由同一 session 证明 lease 仍在。资源 401 最多触发一次新的 automatic coordinator claim；重复 Taskiq delivery、`TransientProviderError` 或进程恢复不能绕过已提交 fence。

provider token response 只有通过 non-empty access token、正数 expiry、canonical scope 覆盖等完整校验后才是 known-valid。随后在另一短事务重检 lease、connection/capability/generation、两行旧 snapshot 与 old identity，并把 credential CAS 与 matching `oauth.refresh_confirmed` 原子提交。known-valid missing、same、different refresh token 都必须 confirmed：missing/same 只更新 access row并逐字节保留 refresh row，要求 old == new 且 `refresh_identity_changed=false`；different non-empty refresh 更新两行，要求 old != new 且 `refresh_identity_changed=true`，但仍只关闭本次 automatic started，不能追加 `oauth.refresh_credential_replaced` 或消费更早 fence。confirmed metadata 使用 `result_schema_version="oauth_refresh_confirmed.v1"`，精确包含 source/started_source、matching attempt、connection digest、适用 revision/rollout 字段、G/G/G、两个 pre-digest、两个 required post-digest、old/new identity 与各自固定 key version、实际持久化 expiry、`refresh_token_disposition`、`refresh_identity_changed` 和稳定 result code；0019 还包含由该 expiry 得出的 deadline candidate，普通 Worker 使用规范 NULL。candidate 只证明历史 result schema，不授权后续 deadline。confirmed 的 `created_at` 必须严格晚于 matching started。confirmed 后崩溃只从持久 credential/result 恢复，不再次调用 provider。

network/response unknown、`invalid_grant`、malformed token/expiry/scope、scope shrink、响应后 lease loss、CAS miss 或数据库明确 rollback 都不产生 confirmed，原 `oauth.refresh_started` 保持 unresolved。provider 一旦已调用就绝不重放。

ACK-lost reconcile 必须返回版本化 `OAuthRefreshResultV1` union：automatic started 只接受 matching `oauth.refresh_confirmed` 或 `oauth.refresh_credential_replaced`；explicit recovery authorization started 只接受 matching `oauth.refresh_recovery_unsatisfied` 或同一个 replacement consumption。unsatisfied 只关闭 recovery attempt，replacement 同时关闭 recovery attempt 并消费 original automatic fence。每个 member 必须校验自己的 event type、result/proof schema version、user/connection/attempt、source/original-attempt 关联和严格时序；confirmed parser 还必须拒绝 disposition、`refresh_identity_changed` 与 old/new identity equality 不一致的 metadata，replacement 只能是 old != new 且 changed=true。不一致 event 不能关闭 attempt。

若 result commit ACK 丢失或异常使 rollback 无法证明，必须丢弃旧 session/lease，使用新的数据库 session 按 `user_id + connection_id + attempt_id` 只读查询该 union；connection/lease 丢失不影响此核对。合法 append-only matching result 一旦存在，对应 provider/authorization attempt 永久 closed，reconcile 不增加 provider call，也不因后续 credential/generation/capability 变化而复活。没有合法 result 才保持 started unresolved、进入 `needs_attention`，禁止补写猜测 result且后续 provider/code 调用为零。

关闭证明与 current readiness 分开：confirmed/replacement 的 current credential 精确匹配历史 post-state 时从持久 result 恢复；ACK 丢失后另一合法 refresh/reauth 已先提交时，按 current generation、credential、scope、capability 继续 preflight，不要求历史 post 等于 current。old-identity rollback guard 只用于 `refresh_identity_changed=true` 的合法 result：只有 current identity 回到该 result 的 old identity 才返回稳定 `oauth_credential_state_conflict`/`needs_attention`。changed=false confirmed 的 old == new；后续合法 access-only refresh 或同 plaintext refresh-token 重加密即使改变完整物理 snapshot，current identity 仍为 old == new 也是正常状态。无论 flag 为何，必需 credential 行缺失、AEAD/归属无效或 current facts 无法自洽都必须进入相同冲突并阻断 rollout，但旧 attempt 仍 closed且 `provider_calls == 0`。unsatisfied 没有 credential post/expiry proof，只按 current capability facts 做 readiness。防重放无需构造完整 credential lineage。不要重放 Taskiq，也不要把普通 retry 次数视为调用授权。只有 started 尚未提交且 provider call 尚未开始的纯本地失败可以重新 claim。

explicit progressive recovery 必须从带 `connection_id` 的授权入口启动。start 事务锁定仍为 `connected` 的原 connection，读取 `source_generation=S`、current credential snapshot/identity 和唯一 unresolved automatic fence；只有 current identity 等于 fence old identity 且 `S >= F` 才继续。现有 `set_capabilities_authorizing` 只递增一次到 `target_generation=T=S+1`，同一事务创建一次性 `OAuthAttempt` 并追加 `oauth.refresh_recovery_authorization_started`。该事件绑定 recovery schema/source、OAuthAttempt ID、connection digest、原 refresh attempt/started source、F/S/T、两个 frozen pre-digest、old identity/key version 和稳定 result code；其 `created_at` 必须严格晚于原 automatic started。它不是 refresh grant started。

callback 一次性消费 state/code，按 `OAuthAttempt.id` 找到上述关联，取得同一 connection lease并在 exchange 前重检 target `T`、原 fence、identity 与 snapshot。authorization-code exchange 保持在业务事务外；同一 code 永不重放。只有 provider 明确返回 different non-empty refresh plaintext，且 normalized identity、scope、expiry 校验通过时，callback 才在一个短事务以 target-`T` CAS 原子写 credential/scopes/capabilities，并追加 `oauth.refresh_credential_replaced`。replacement proof 必须包含 `source="progressive_recovery"`、started_source、connection digest、原 refresh attempt、recovery OAuthAttempt/event IDs、F/S/T、`pre_generation=post_generation=T`、两个原 pre-digest、两个 required post-digest、old/new identity 与各自固定 key version、`refresh_identity_changed=true`、persisted expiry 和稳定 result code，并要求 old != new；其 `created_at` 严格晚于原 automatic started 与 matching recovery started。

missing/empty/same refresh、用户拒绝、管理员同意缺失、已知 provider/network failure 或 identity mismatch 都不消费原 fence。start 的 S→T 已把本次 requested capabilities 置为 `authorizing`；当 connection 仍为 `connected` 且 generation 仍为 `T` 时，复用并扩展现有 `mark_progressive_authorization_failed`，在同一短事务把这些 capabilities 收敛为 `action_required`、写稳定 `error_code`，保留原 `actual_scopes` 与 last-verified facts，并追加 `oauth.refresh_recovery_unsatisfied`。该 result 使用 `result_schema_version="oauth_refresh_recovery_unsatisfied.v1"`，精确绑定 matching recovery event/`OAuthAttempt.id`、原 fence refresh attempt/started source/connection digest、F/S/T、两个 frozen pre-digest、old identity/key version、规范 requested capabilities、`capability_transition="action_required"|"stale_target_noop"`、稳定 error/result code 与严格时序；不得包含 credential post snapshot/digest、new identity、persisted expiry 或 deadline。若 `T` 已过时，capability 更新为 no-op，只允许不会覆盖较新授权状态的安全审计。unsatisfied 只关闭本次 OAuthAttempt，原 automatic fence 继续 unresolved；用户可以显式创建新的 OAuthAttempt 再尝试，但任何旧 code 都不得重放。lease/CAS/rollback 与 commit-result-unknown 按上一段 result union 核对，不能 fabricated result/consumption。

Google callback 与 Microsoft 一样要求 non-empty state，并且只能接受互斥的 `code` 或 `error`。缺 state、同时 code+error、两者都缺失，或 error 为空/超出现有输入边界时必须在消费前拒绝。任何合法 non-empty `error+state`，包括未知 error 名称，都必须先一次性消费 state；target-`T` recovery 随后走上述 unsatisfied 收敛。统一的是消费、收敛、脱敏与 replay 拒绝，不是 Problem 分类：Microsoft consent evidence（包括 `AADSTS65001`）返回既有 `microsoft_admin_consent_required` 与管理员指引，用户拒绝返回 `oauth_authorization_failed`，普通 Microsoft `interaction_required` 保留 `microsoft_reauthorization_required`，只有未知名称回退 `oauth_authorization_failed`。raw error/description/error codes 不得成为持久错误码，也不得入库、日志或 Trace。首次未知 error 已消费后，同一 state 以相同或不同未知 error 重放都必须拒绝。两家入口都不得绕过 targetless fenced-identity 保存前阻断。

无 target 的 callback 若规范化 provider identity 命中已有 connected connection 且该行有 unresolved automatic fence，必须在保存任何 credential、scope 或 capability 前返回 `oauth_refresh_recovery_requires_connection_start`。可追加 content-free blocked audit，但它不关闭 fence；禁止 candidate snapshot、targetless consumption、第二 connection 或无条件 upsert。

identity 与摘要协议不得由脚本或实现自行解释。API、mail Worker、calendar Worker 与 0019 CLI composition root 必须从同一 `APP_MASTER_KEY_FILE` bytes 和同一个固定 `AeadCipher.key_version` 同时构造 AEAD cipher 与 identity service。M2 不更换 root key/version，不新增 identity Secret 或 multi-key lookup；root key 缺失或变化一律 fail closed。运行顺序固定为：在受控内存按精确 credential AAD 解密 refresh token，验证 non-empty、合法 UTF-8 和现有长度边界，然后才按规格 17.3 的 HKDF-SHA256/HMAC-SHA256 算法计算 `refresh_token_identity_v1`；完成 started claim、提交 durable fence并重检所需 session lease 后才允许 provider access。不得对未解密/未验证的 ciphertext 猜测 identity。明文、root key、派生 key 和 identity fingerprint 不得进入日志、Trace、SSE、API、fixture 或 release evidence。`credential_snapshot_digest_v1`、`refresh_credential_snapshot_digest_v1` 与 `rollout_digest_v1` 的 domain、framing、字段顺序和四个固定 synthetic vectors 也以规格 17.3 为唯一事实来源。两个 credential digest 只证明数据库物理 snapshot/CAS；同 plaintext 重加密保持相同 identity、只能是 changed=false，不能伪造 rotation。只有 changed=true 的 A→B proof 后 current identity 回到 A 才由固定-version identity guard 检出为 rollback。

365 天 generic AuditEvent cleanup 必须排除 `oauth.refresh_started`、`oauth.refresh_confirmed`、`oauth.refresh_recovery_authorization_started`、`oauth.refresh_recovery_unsatisfied` 与 `oauth.refresh_credential_replaced`。专用 user-scoped cleanup 使用与 reconcile 相同的 versioned result-union parser，只按三类完整组删除：automatic started + confirmed；recovery started + unsatisfied；原 automatic started + successful recovery started + replacement consumption。matching confirmed 只关闭自己的 automatic started，unsatisfied 只关闭自己的 recovery attempt，replacement 同时关闭 matching recovery attempt 并消费原 unknown fence。parser 必须拒绝 confirmed disposition/flag/equality 不一致，以及 replacement 非 old != new、changed=true 的 metadata。unsatisfied 组必须核对其 result schema、matching attempt/event、F/S/T、requested capabilities、`capability_transition` 和稳定 error/result code，但不要求当前 capability 仍保持旧状态；`action_required` 与 `stale_target_noop` 都不能消费原 fence。组内每一事件都必须早于 cutoff，等价于 `max(created_at) < cutoff`；旧 started 配较新 result 不得提前删除。unresolved fence 跨 cutoff 保留并继续阻断 automatic provider call，多次 closed unsatisfied recovery pair 不改变该事实。cleanup 只核对 append-only result closure与时序，不要求 historical post 等于 current credential，也不构造完整 lineage；与 credential CAS 使用统一锁序并锁后重查 matching metadata/F/S/T/identity/version/`refresh_identity_changed`，但不读取后续 credential lineage，也不维护 multi-key state。

`database.restore.completed` 不享有 retention 或隐私删除豁免。database-wide
completed call + `ai_employee.restore_completion` 互绑 pair 是 completed admission/ACK 的持久 catalog
authority，不依赖 AuditEvent 存续，且 call 不可缺失。generic cleanup 按普通 365 天 cutoff 删除 completion audit；全数据删除也必须删除该用户的 completion
audit，不能为 restore proof 绕过隐私删除。audit 后续缺失不使已提交 restore 失效，也不阻止下一次合法
restore。AuditEvent 的普通 BigInteger ID、event 数量与 metadata matcher 都不进入 admission/ACK predicate；
exact audit 只在最终提交事务当刻作为原子审计证据校验。

发布期间保持真实写入开关关闭，并严格按以下顺序执行：

1. 保持全局与供应商真实写开关关闭，关闭 Calendar 周期调度，并进入停止新入口流量的维护窗口。
2. 优雅排空并停止全部 pre-0019 CalendarEvent reader/writer：Caddy、API、Worker、Scheduler；确认没有旧 `sync_calendar`、事件读取或 upsert 仍在运行。PostgreSQL 与 Redis 全程保持运行。
3. 运行无参数 `just calendar-aad-preflight-0019`。该 0018-compatible one-off 先取得上述 revision-global lease，再检查 revision、artifact 不存在/不冲突并从历史完整 AEAD 三元组推导精确 affected pairs。它按 connection UUID 去重并串行处理；每次 provider call 前还必须取得同一 connection 的 coordinator lease，使两层 lease 同时有效。随后冻结 generation、access/refresh 完整 snapshot 与两个 physical digest，在受控内存解密并验证 non-empty/UTF-8/边界后才由固定 APP root key/version 计算 current identity。若同一 rollout 的 `oauth.refresh_confirmed` 与 current post-snapshot 匹配，则其 persisted expiry/deadline candidate 只验证历史 result closure，随后仍从当前持久化 access credential 重读 expiry；若存在 unresolved `oauth.refresh_started`，相同或不同 basename 都保持零 provider call，除非已有 explicit progressive recovery 的 matching `oauth.refresh_credential_replaced`，且其原 attempt/started source/F、两个原 pre-digest、old identity/version、recovery OAuthAttempt/event、F/S/T、两个 post-digest、different-token new identity/version、`refresh_identity_changed=true`、persisted expiry、old != new 与严格时序全部有效。automatic Worker rotation、targetless callback、generation/access-only/physical digest 变化都不能消费旧 fence；只有 changed=true 的 consumption 后 current identity 回到 consumed old identity 才按 A→B→A rollback fail closed，changed=false confirmed 后的 access-only refresh 或同 plaintext 重加密不冲突。通过 current connection/capability/scope/credential 本地校验后，preflight 在紧邻 provider 的短事务提交 generic `oauth.refresh_started`，绑定 source、attempt、revision/rollout、G、两个 pre-digest、old identity/version 与稳定 result code，重检两层 lease后再调用 OAuth refresh。known-valid missing/same/different refresh 都通过共享 snapshot-CAS repository 与 generic `oauth.refresh_confirmed` 原子提交；missing/same 逐字节保留 refresh row并要求 old == new、changed=false，different 更新 refresh row并要求 old != new、changed=true，但只关闭本次 started，不生成 replacement consumption。confirmed 必须绑定 matching attempt/source、G/G/G、pre/post digests、old/new identity/version、persisted expiry、disposition、`refresh_identity_changed`、deadline candidate 与严格晚于 started 的 `created_at`，不一致 schema 不能关闭 attempt。unknown、`invalid_grant`、malformed/scope shrink、响应后 lease loss、CAS miss 或已确认 rollback 都不产生 confirmed、不 probe、不发布 artifact；commit ACK 丢失则用新 session 做只读 reconcile，实际 commit 恢复 confirmed且总调用数为 1，实际 rollback 保持 unresolved且以后调用数为零。后续 Taskiq/`TransientProviderError` 只读 fence。每个 current access expiry 必须晚于 current UTC 加 900 秒；全部 connection closure/readiness 通过后从 current access rows 计算 current deadline，并与原 artifact deadline 取最小值，再按确定性 pair 顺序只调用 `initial_pages(scope_key)` 并要求最终 cursor。不等待资源 401，主动 refresh 后的 401/403、权限不足、malformed page 或无最终 cursor 都失败。它不得调用 `directory_pages()`、connection-level/full-account sync、其他 pair 或 Calendar 写适配器，也不得写 CalendarEvent、cursor 或 marker；非空 set 在每个 pair probe 前及 artifact 原子提交前检查 `now < effective_deadline`，zero branch 在发布前重新证明 affected set 仍为空。任何失败都返回非零并停止变更窗口。
4. 只有 preflight 全部通过且统一 rollout guard 通过后，才创建带固定 basename 的加密备份
   dump/checksum/versioned-manifest 三件套并运行 `pre-migration` 只读审计：非空 affected set 必须从当前
   access rows 重算并要求当前 UTC 早于 `effective_deadline`；零 affected pair 则要求 artifact 为规范
   zero/no-deadline 形状并重新证明数据库 affected set 仍为空。backup、audit 和其 artifact 原子发布前
   都必须重复同一 guard。backup 还必须持有固定数据库级 backup lock 与 `${BACKUP_DIR}` 本地 `flock`，
   并在固定 `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` shared session lock 内执行 pre-revision → 完整
   `pg_dump` → post-revision CAS；只有 revision 相等且同一 session 仍持锁，才可 manifest-last 发布完整
   三件套作为本窗口恢复事实。审计必须确认
   revision 仍为 0018、没有部分 AEAD 三元组，并重复证明每个 affected pair 的本地可恢复性事实和与
   preflight 完全一致的 locally hashed pair set。
5. 运行受统一 rollout guard 保护的独立 migration one-off 应用 0019。所有在线 migration 入口都通过
   Task 16A 的 Alembic 外层边界持有 `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` exclusive session lock，
   直到外层 migration transaction commit/rollback 后才释放；backup 持 shared lock 时 migration 不得
   commit。迁移自身仍必须在任何 DDL/DML 前、事务最终提交前重复本地 preflight/refresh-state、镜像绑定
   与非空集合 current-expiry `effective_deadline`/零集合分支检查；任一精确 pair 缺游标、连接非
   connected、`calendar.read` 非 enabled、缺失 access 或 refresh credential、refresh scope 不足或缺失
   精确 `ProviderCalendar` 时都 fail closed，Alembic revision 保持 0018，全部数据不变，且不得创建猜测
   marker。schema-lifecycle lock 不改变 typed guard 语义，也不替代 `(20260809, 19)` rollout lease。
6. 运行 `post-migration` 只读审计，确认 Schema 只新增版本列与原子约束；历史完整三元组标记为 v1、全空组保持 NULL；事件身份/非 AAD 业务字段及 ciphertext/nonce/key-version 摘要不变；`directory` 完全不变；只有精确 affected pair 的 cursor/freshness/error marker 改变。两个 connection 即使复用同一 `calendar_id`，也禁止只按 `calendar_id` 扩大更新。随后使 v2-only 不可变镜像可供 one-off 恢复命令使用，但继续保持 Caddy、API、普通 Worker 与 Scheduler 停止，禁止任何旧 reader/writer 回流。
7. 运行无参数 `just calendar-aad-resync-0019`。该入口只扫描 `calendar_event_resync_required` marker，在精确 cursor 锁下始终复用 `created`、`queued`、`running`、`retry_scheduled` 活动尝试；只有上一尝试已经 `failed` 或 `cancelled` 且 marker 仍存在时，下一次显式 CLI 调用才分配 `max_ordinal + 1`。每次 CLI 调用每个 pair 最多返回/创建一个 ordinal，并在开始、每个任务最终本地提交前检查同一 rollout guard；非空集合当前重算的 `effective_deadline` 已到、zero state 漂移、marker 仍存在或任务失败都必须停止，不得在同一次调用中追加 ordinal+1。随后只在进程内执行 planner 本次返回的 `calendar.aad_0019.resync` 任务；不得启动 Taskiq、扫描普通队列、运行 `directory` owner，或扩大到整个连接或账户。
8. 运行 `post-resync` 只读审计，并在开始及 artifact 原子提交前检查同一 rollout guard，确认每个 affected pair 的 marker 已清除、cursor/freshness 已恢复、此次新写入的非空描述/地点均为 v2，并只报告未读取内容的剩余历史 v1 数量。任一 marker 未恢复或 rollout guard 失败都必须停止发布，Caddy、API、普通 Worker 与 Scheduler 继续保持停止。若非空集合当前重算的 `effective_deadline` 尚未到且失败条件可在精确 scope 内修复，只能由运维人员再次显式运行 resync 复用活动 ordinal 或在上一尝试终态后分配下一 ordinal；一旦该 `effective_deadline` 已到、已经不能证明 artifact 会及时提交，才转入下述 sealed-window restore。zero-state 漂移或镜像/basename 不匹配先 fail closed 并调查，只有仍能独立证明维护窗口内无业务写入时才可能满足整库恢复前提。
9. 只有 `post-resync` 审计通过后，才启动 v2-only API、普通 Worker、Scheduler 与 Caddy。
10. 运行 `just health`；上述条件全部满足后才退出维护窗口并继续发布。

以下命令从 `/srv/ai-employee` 运行，不包含账号、DSN、正文、原始 `calendar_id` 或凭据。migration one-off
继续从 Compose Secret 读取数据库凭据。`BACKUP_ARTIFACT_BASENAME`、content-free preflight state、
revision-global lease、共享 `OAuthRefreshCoordinator` connection lease、dual credential digest、固定 APP
root-key identity、automatic started/confirmed 与 explicit recovery replacement proof、共享 credential
snapshot CAS、`just calendar-aad-preflight-0019`、`just calendar-aad-migrate-0019`、
`just calendar-aad-audit phase artifact`、`just calendar-aad-resync-0019`、独立
`just calendar-aad-restore-0018 file` 及其 executor 内部 sealed verifier、合成数据库测试属于
Task 27C/27D 的运维契约。Task16A、27A–27C已提供0019迁移、共享coordinator、typed guard及
preflight/migration/resync组合；versioned通用备份manifest、durable restore attempt、恢复专用gate、
通用owner-session role-switched verifier与两条owner restore边界由Task27D交付。Task 27D 必须把现有通用 `just backup`/`just restore file` 修正为
manifest-bound 三件套与 owner-role、生产停写、`--exit-on-error --single-transaction`、crash-surviving gate、
恢复后同 session `SET ROLE ai_employee_app` read-only verifier 和 atomic reopen 的灾备入口；它不要求 0019 artifacts，
且不能被当作 sealed-window 恢复证据。未完成相应任务前不得执行生产 0019 或生产整库恢复，也不得用
临时命令、额外 DDL 授权或直接 SQL 替代。示例镜像标签必须替换为预先构建并固定的实际不可变标签，
不得使用 `latest`。

```bash
cd /srv/ai-employee
export PRE_0019_APP_IMAGE_TAG=2026.08.09-0018-compatible
export APP_IMAGE_TAG=2026.08.09-0019-v2
export APP_ENV=production
export EXTERNAL_WRITES_ENABLED=false
export GOOGLE_WRITES_ENABLED=false
export MICROSOFT_WRITES_ENABLED=false
export BACKUP_DIR=/var/backups/ai-employee
export BACKUP_ARTIFACT_BASENAME=ai_employee-0019-20260809
install -d -m 0700 "${BACKUP_DIR}"

# 先在外部维护流程关闭 Calendar scheduling 和新入口，再优雅停止所有旧 reader/writer。
docker compose stop caddy api worker scheduler migration backup
docker compose ps
# 预期仅 postgres 与 redis 保持 running；若仍有旧 reader/writer，立即停止发布。

# Task27D完整备份/审计/restore与镜像验证门禁通过前必须停在这里。
just calendar-aad-preflight-0019

just backup
just calendar-aad-audit pre-migration "${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}"

just calendar-aad-migrate-0019
just calendar-aad-audit post-migration "${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}"

# 此时仍只允许 postgres 与 redis 运行。
docker compose ps
just calendar-aad-resync-0019

just calendar-aad-audit post-resync "${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}"

# post-resync 审计通过后，才允许启动通用服务。
docker compose up -d api worker scheduler caddy
just health
```

`calendar-aad-preflight-0019`、`calendar-aad-migrate-0019` 与 `calendar-aad-resync-0019` 都不接受 user、connection 或 calendar 参数，避免运维输入把范围改写为任意对象；三者都要求预先设置安全 basename `BACKUP_ARTIFACT_BASENAME`，并使用 `${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json` 作为同一 rollout state。`BACKUP_DIR`必须是已有、非根的绝对目录，basename使用`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`。recipe 在启动 one-off 容器前检查 Compose 状态；`caddy`、`api`、`worker`、`scheduler`、`migration`或`backup`仍为running/paused/restarting时都fail closed，同时拒绝已开启的三层写开关。宿主从Compose实际worker/migration镜像执行本地`docker image inspect`，要求内容ID一致，并固定one-off的`image=sha256:...`和`--pull never`；不会build/pull。所选service不得带覆盖镜像代码的额外挂载；迁移与resync的`/backups`只读。该ID通过内部环境变量传给固定程序，运维不得另行输入。preflight artifact 绑定该内容 ID，后续 backup、audit、migration、resync 与 restored-0018 verifier 都必须重新解析并拒绝镜像不匹配。宿主 recipe 只能验证 basename/path 形状和挂载边界，不能在 lease 之外检查 preflight artifact 是否存在；碰撞/存在性检查必须由 CLI 在成功取得 revision-global advisory lock 后完成。preflight 还要求数据库精确位于 0018，复用选定不可变镜像、`worker` 服务的 Secret/数据库配置和现有 Calendar credential/adapter resolver，以 `--no-deps --entrypoint /bin/sh` 运行固定程序 `export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_preflight_0019`。它只输出 affected/connection count、locally hashed pair/connection digest、earliest UTC deadline 和稳定结果码；不得输出 token、scope 原文、正文、描述/地点或 raw `calendar_id`。

宿主在任何 Docker 查询前解析并冻结 `BACKUP_DIR` 的实际物理目录，再检查存在性与非根边界；
`/tmp/..` 或指向根目录的符号链接会被拒绝，挂载复用同一个已验证的物理路径。
三个正式 invocation 都传入固定
`CALENDAR_AAD_ROLLOUT_STATE=/backups/<basename>.calendar-aad-preflight.json` 与实际 image ID。
CLI 从该路径派生目录和 basename；migration/resync 不要求额外的 `BACKUP_DIR` 或
`BACKUP_ARTIFACT_BASENAME` 环境字段，显式提供时必须与固定 state 绑定一致。

preflight/resync 的业务事务、凭据协调和供应商访问始终使用 app 身份。普通 baseline 不授予 app 对
`alembic_version` 的 SELECT；受控 one-off 通过固定私有 owner facts reader 在一个新 RR/RO 事务中同时
读取 revision、完整 affected set 和当前 expiry。audit 借用已持锁的同一 owner holder 做此读取，不能让
caller 提供 revision 或历史 deadline 作为授权。owner Secret 只挂载到这些固定短命令，不进入普通
API/Worker/Scheduler，也不交给业务 repository 或 provider。

在正式 preflight、migration、resync 或维护窗口 backup 容器启动前，宿主先用同一已 pin 的
`migration` 镜像与既有 owner Secret 运行无参数 `ai_employee.cli.calendar_aad_revision_0019`。
这个短 screening 容器没有 backup/source 挂载，只以单个目标连接、固定超时和只读事务读取
当前发布版本；preflight/migration/backup 要求 0018，resync 要求 0019。筛查成功后再次检查
停服状态和实际镜像，再启动正式命令。筛查不读取 artifact、不创建维护事实，正式命令仍独立
执行当前 revision、revision lease、artifact guard 和冻结迁移 lifecycle 的全部检查。
Migration 的固定 wrapper 为
`export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_migrate_0019`；
目标连接继续由既有安全 Secret 文件读取器与 typed endpoint 构造。Revision body 成功后的
policy/grant delta 在同一个 Task16A 外层迁移事务内应用并核对，然后才允许最终提交。

显式设置`BACKUP_ARTIFACT_BASENAME`时，`just backup`使用相同宿主校验，并由
`calendar_aad_backup_0019`持同一revision session lease完成入口guard、既有shell dump/encrypt
producer、当前期限重读和最终发布。两次guard绑定同一原artifact；暂存目录为0700，发布文件为0600，
失败或取消先等待自身producer退出再删除暂存内容，已有basename产物绝不覆盖。未设置basename时保持
原普通备份入口。此hook仅交接dump/checksum；完整manifest组发布、保留、remote及restore证据仍按
Task27D门禁验收，不能据此宣布0019生产窗口可执行。

preflight、resync 与此 backup hook 在主体执行阶段收到超时或重复取消后，仍持有同一 revision lease，
等待本次同步校验、artifact I/O、子进程获取与停止、文件和目录清理全部收敛，再传播原取消。
取消期间不会继续进入新的 provider 或业务步骤；本次新发布的 artifact/dump/checksum 必须撤回，
已有成功文件保留。取消不表示同步 I/O 已经停止，应等维护命令退出后再开始下一次操作。
发布及内部清理成功后，父调用仍保留本次文件的精确身份，直到最终 lease exit 成功。如果首次失败
发生在这个最终退出阶段，先等退出线程收敛，再条件撤回仍属于本次的文件；保留碰撞文件及后来
替换或改写的文件。此前已知的内部补偿仍在 lease 释放前完成。物理释放到条件补偿之间有短窗口，
因此维护命令实际成功退出前不能把刚出现的文件当作可继续操作的成功结果；失败后的替换文件
不属于本次成功产物。补偿自身再次收到取消也要完整收尾，并保留首次退出失败。
补偿先把候选目录项原子移入同一备份目录下本次独占的 `.calendar-aad-custody-*` 0700 目录，
以目录 fd 固定该私有命名空间，再核对文件身份；只在私有目录内删除本次文件，不能核对公开
路径后再按该路径删除。公开 basename 因此会短暂缺位。已知外来文件在捕获前直接保留；捕获时
遇到的外来 regular/FIFO/symlink 不读取或跟随，以 Linux `renameat2(RENAME_NOREPLACE)` 归还。
若原位置已有新占用者、系统不支持该原子返还或任一保管步骤失败，命令失败；外来对象、未知
捕获结果与当前公开占用者保守保留。保管目录内使用原 basename，便于定位需要人工处置的对象；
不得把该目录中的文件当成功产物，或由普通备份/retention 自动清除。命令失败或进程中止后先保留
这些目录，待维护操作全部退出、停止该目录的其他写入者后核对原 basename 与文件归属；公开位置
已被占用时不得覆盖，只能在人工确认后分别安置。普通备份的根目录匹配规则不会回收这些私有目录。
其他写入者可替换公开 basename，但不得进入、改名或改写本次私有保管树；0700 不防御同 UID 或
特权进程主动侵入。目录项保管也不锁住其他进程已经持有 fd 的原地写入；身份核对时已经可见的
改写仍会保留，不能据此宣称提供通用内容锁。空占位文件阻止捕获整个外来目录；任何失败清理都
不得递归删除未知保管内容。backup 的某个成员补偿失败时仍会处理另一个本次成员。
artifact 和 backup checksum 输入使用非阻塞 no-follow 打开，再对同一 fd 校验文件类型等约束；
无 writer 的 FIFO、目录等非法对象会直接拒绝，不会等待 FIFO writer 才进入校验。

preflight 的供应商表面严格只读，但 refresh 本身可能轮换 credential，因此必须使用 durable fence 和共享 coordinator。`oauth.refresh_started` 与 `oauth.refresh_confirmed` 使用既有 `audit_events`、`task_id=NULL`、`actor_type=system`；started 精确绑定 source、canonical attempt UUID、source/target revision、`rollout_digest_v1`、connection digest、G、两个 pre-digest、old identity/fixed key version 和稳定 result code。confirmed 必须复用同一 attempt/source，并包含 G/G/G、两个 pre-digest、两个 required post-digest、old/new identity/version、实际持久化 expiry、missing/same/different disposition、`refresh_identity_changed`、deadline candidate 与稳定 result code；missing/same 只能 old == new、changed=false，different 只能 old != new、changed=true，其 `created_at` 严格晚于 started。本地 credential 校验必须依次完成精确 AAD 解密、non-empty/UTF-8/边界验证和 identity 计算，随后提交 started 并在 provider 前重检两层 lease；网络阶段不持有业务事务。合法 matching confirmed 已永久关闭本次 automatic attempt：current credential 精确匹配 post-snapshot 时从持久 expiry 继续，后续合法 refresh/reauth 已改变 current facts 时按 current readiness 继续。没有合法 result-union member 的 started 对所有 basename 都是硬阻断；explicit progressive recovery 的 `oauth.refresh_credential_replaced` 在原 automatic attempt、recovery OAuthAttempt/event、F/S/T、pre/post digests、old/new identity/version、different-token decision、`refresh_identity_changed=true`、persisted expiry、old != new 与严格时序全部匹配时，同时关闭 recovery attempt并消费原 fence。automatic Worker refresh、targetless callback、access-only/generation/physical digest 变化和人工处置都不是 original-fence consumption。

snapshot 对 access/refresh 每行都绑定 `id`、用户/连接/kind、ciphertext、nonce、key version、`token_expires_at` 与 `updated_at`，并另行绑定 old refresh identity/fixed key version；共享短事务还要重检 connection/capability/generation，并按 connection → access row → refresh row → matching started audit 锁序执行。known-valid missing/same refresh 只更新 access row并逐字节保留旧 refresh 行，confirmed 为 old == new、changed=false；known-valid different refresh 更新两行，confirmed 为 old != new、changed=true，但同样只提交本次 `oauth.refresh_confirmed`。任一 CAS miss 都不会覆盖新 token或发布 artifact。网络断开、任一 lease 丢失、malformed response、`invalid_grant`、scope shrink 或数据库明确 rollback 都不得对同 snapshot 自动重试；同 attempt 的 Taskiq/`TransientProviderError` 再进入时 provider call 必须为零。若 credential/result commit ACK 丢失，新的数据库 session 必须查询 versioned result union：实际 commit 的合法 matching result 永久关闭 attempt且不增加调用；schema flag/equality 不一致的 event 不是合法 result。实际 rollback 没有 result时保持 unresolved/`needs_attention`，后续 provider call 为零。关闭后独立核对 current readiness；ACK 丢失后另一合法 refresh/reauth 先提交可按 current facts继续。changed=false 后续 access-only refresh 或同 plaintext 重加密即使 physical snapshot 改变也不冲突；changed=true 后 current identity 回到 old 的 A→B→A、缺行、AEAD/归属无效或其他不一致返回 `oauth_credential_state_conflict`并阻断 artifact，但绝不重开 attempt。不得因旧 coordinator session/lease 丢失跳过核对，也不得补写 result或构造完整 lineage。它不等待资源 401，也不在 probe 阶段再刷新；主动刷新后的资源 401、403 或 scope 缺失均是失败。除 refresh fence 与成功的 credential CAS/confirmed 事实外，不得写连接、能力或其他业务事实，也不得写 CalendarEvent、SyncCursor 或 marker。revision-global lease 与当前 connection coordinator lease 必须在每个 provider call 和 credential CAS 前仍可由各自同一 session 证明持有，artifact publish 前还必须重检 revision-global lease 和所有 connection result 已稳定确认。preflight 首次用 current-ready access rows 计算原始 `rollout_deadline`；此后每个 CLI/recipe guard 都重读 current access expiry，计算只能收紧的 effective deadline。preflight state artifact 只保存 schema/revision、`rollout_digest_v1`、安全 basename、宿主机解析的 immutable image content ID、earliest expiry/deadline、固定 margin、affected/connection 计数、lowercase pair digest、hashed connection IDs 和稳定结果码，不保存 token、scope 原文、正文、raw `calendar_id`、HMAC key 或真实 credential identity。preflight、backup、migration、resync、audit 每个 CLI/recipe 都必须在启动和关键本地提交前读取并校验该 artifact，同时从当前 access rows 重算 effective deadline。非空 affected set 的任何 `now >= effective_deadline` 都 fail closed；zero/no-deadline state 只有在规范零值、镜像/basename 绑定有效且重新查询仍无 affected pair 时才通过，否则同样 fail closed。

任一 pair 或 connection 失败时不得继续备份、审计或 migration；数据库仍为 0018。若存在 unresolved refresh fence，后续同名或不同 basename invocation、Worker、Taskiq 与 `TransientProviderError` 重入都保持 zero provider calls。保持写开关关闭；原 connection 仍为 `connected` 时，只能由用户从带 `connection_id` 的 progressive authorization start 创建新的 OAuthAttempt。start 同事务记录 recovery authorization event并把 requested capabilities 置为 `authorizing`；callback 不创建第二个 automatic started，且只有 different non-empty refresh token 才能把 credential/scopes/capabilities 与 old != new、`refresh_identity_changed=true` 的完整 F/S/T replacement proof 原子提交并消费旧 fence。missing/same、用户拒绝或已知 provider/network failure 必须在 target `T` 仍匹配时把本次 capabilities 与 `oauth.refresh_recovery_unsatisfied` 同事务收敛为 `action_required`，保留 actual scopes/last-verified facts且不保存新 credential/scope/account/post/expiry；stale `T` 只做 capability no-op与安全 audit。matching unsatisfied 只关闭本次 OAuthAttempt，原 fence 继续存在；matching replacement 同时关闭本次 OAuthAttempt并消费原 fence。用户可再显式创建下一 OAuthAttempt，但不得复用旧 code。automatic Worker refresh 和 targetless callback 都不能消费旧 fence；不得为消费 fence 强制资源 401、启动临时 OAuth-only 服务或增加额外 provider call。

targetless callback 若规范化 identity 命中带 unresolved fence 的既有 connection，必须保存前返回 `oauth_refresh_recovery_requires_connection_start`；不得冻结 candidate snapshot、交换后合并 credential、调用 `ensure_connection`/无条件 upsert或创建第二个 connection。事故调查只读核对 OAuthAttempt/state consumption、provider identity mapping、原 connection 与 fence，并确认本地 credential/scope/capability 写入次数为零。恢复只能由新的、明确用户发起且带该 connection ID 的 progressive OAuthAttempt 完成；同一 code/attempt 不得重放。

**发现未知 fence 时不要先 disconnect**：断开会先删除旧 credential，之后显式 recovery 无法比较 old/new plaintext；断开后重连、新建 connection 或映射新 token 都不能生成 replacement consumption。recovery callback 未返回 refresh token、返回相同 plaintext、旧 plaintext 不可得、connection 已断开，或仅改变 access/generation/物理密文时仍保持 original fence 阻断。合法 closing result 不因这些后续 current facts 变化而失效。current plaintext identity 按 M2 固定 key version 重算后，只有 closed result 为 `refresh_identity_changed=true` 且 identity 回到 old（A→B→A）时才是 rollback；changed=false confirmed 后的同 plaintext 重加密即使 physical digest 不同也不是冲突。必需 credential 行缺失、AEAD/归属无效或其他不一致则始终以 `oauth_credential_state_conflict` fail closed并阻断 rollout，旧 attempt 仍 closed且 provider call 为零。若断开后 affected scope 仍存在，只能走既有明确用户授权的数据处置/删除历史敏感字段流程并重新评估 affected set；若该流程也不适用，必须停止并请求新的已批准设计。禁止 fabricated consumption、新连接映射、marker skip、retry waiver、直接 SQL、伪造 credential、access-only 迁移或先迁移再清 marker。人工调查不能授权 automatic 同 snapshot 重放；已 confirmed 但尚未发布 artifact 的崩溃恢复必须复用同一 rollout 的 closing result并独立核对 current readiness。其他本地事实失败可在恢复精确目录事实后重新开启完整变更窗口。

resync recipe 要求数据库精确位于 0019，并以相同 wrapper 边界运行固定程序 `export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_0019`；该 wrapper 不打印 Secret，也不会启动 Taskiq Worker、Scheduler 或普通 Redis queue consumer。

resync CLI 只从 revision 0019 数据库中的精确 marker 推导用户与 pair。pair digest 固定为 `SHA-256(20260809_0019 + NUL + connection_id + NUL + calendar_id)` 的小写十六进制；planner 对精确 cursor 行执行 `FOR UPDATE` 或等价 CAS。每个 pair 的任务种类固定为 `calendar.aad_0019.resync`，首次 recovery ordinal 为 1，稳定幂等键为 `calendar-aad-0019:<pair_digest>:attempt:<ordinal>`，输入精确包含 `connection_id`、`scope_key`、`recovery_revision=20260809_0019`、`pair_digest` 和 `recovery_attempt_ordinal`。

planner 将 `created`、`queued`、`running`、`retry_scheduled` 视为活动尝试并复用；多个活动尝试属于不变量错误。若最新尝试为 `failed` 或 `cancelled` 且 marker 仍存在，只有下一次显式 CLI 调用才分配 `max_ordinal + 1`；同一次调用执行失败后不得再次规划 ordinal+1。`succeeded` 与 marker 并存必须 fail closed，`waiting_approval`、`reconciling`、`needs_attention` 对该只读恢复种类均不合法。旧终态 TaskRun、时间戳、`attempt_count` 和结果不得复活或改写；recovery ordinal 与单个 TaskRun 内由 `DurableTaskRunner` 管理的有界 `attempt_count` 完全分离。并发 planner 对同一 ordinal 只能产生一个 TaskRun 和一条初始 Outbox。

每个 ordinal 的 TaskRun、内容安全审计和初始 Outbox 在一个事务提交，随后仅通过精确 task ID 的内联 Outbox/`DurableTaskRunner` 路径执行，不发布或消费普通队列。Worker 在供应商访问前重新计算 pair digest，核对 exact input keys、ordinal、幂等键、用户归属、连接读取能力、必需 access/refresh AEAD credential、现存非 `directory` cursor、marker 与同一 ProviderCalendar，并只调用该 calendar 的事件页；目录 reader、其他 connection/calendar、ApprovalRequest、ToolExecution 和供应商写适配器调用次数都必须为零。

成功提交以 marker 仍匹配为 CAS 前提，只恢复同一 pair 的 cursor/freshness、写入 v2 字段并清除其错误；marker 已清除的重放是无供应商调用的 no-op。任何权限、供应商读取、租约或 CAS 失败都保留 marker、返回非零并阻止后续服务启动；修复条件后必须由运维人员再次显式运行同一无参数命令创建下一 ordinal，禁止改用直接 SQL、临时全量同步或 connection-level directory sync。零 marker 运行是可审计的成功 no-op。

`calendar-aad-audit` 只接受 `pre-migration`、`post-migration`、`post-resync` 三个 phase，并分别写入 `${artifact}.calendar-aad-${phase}.json`。每个 phase 在读取数据库前和以原子 rename 发布 artifact 前校验同一 rollout state/guard。`pre-migration` 还必须重复验证 connected connection、enabled `calendar.read`、必需 access/refresh credential 与精确 `ProviderCalendar`，并记录与 preflight 相同的 locally hashed pair set；运维证据必须比较两者完全一致。每阶段 artifact 权限固定为 `0600`，只保存 revision、行/密文摘要、locally hashed scope IDs、affected/connection count 和 earliest deadline；不得保存或输出 DSN、描述/地点正文、供应商响应、token、scope 原文或 raw `calendar_id`。三个 artifact、preflight 的 content-free hashed results 和每个恢复 ordinal 的结果必须与备份 basename、迁移标识和不可变镜像标签一起进入 Task 30 发布证据。

Task 30 release evidence 还必须记录共享 coordinator 的 content-free 证明：0019 preflight 与 Google/Microsoft mail/calendar automatic refresh 都经过同一 application port；0019 每个 connection 在 provider 前同时持有 revision-global 与 connection-scoped lease；refresh token 按“解密/验证/identity/started/lease/provider”顺序处理；`oauth.refresh_started` 已在网络前提交；known-valid missing/same/different response 都由 credential CAS + `oauth.refresh_confirmed` 原子提交，Google access-only 路径也必须 confirmed 并逐字节保留 refresh row。证据必须断言 missing/same 为 old == new、`refresh_identity_changed=false`，different 为 old != new、changed=true，replacement 为 old != new、changed=true，并覆盖 schema mismatch 被 versioned union/retention parser 拒绝。两个 Worker 竞争时仅一次 provider call；响应后 lease 丢失、网络 unknown、CAS miss、已确认 rollback、Taskiq 重投和 `TransientProviderError` 后续调用均为零。commit-result-unknown 证据必须覆盖 versioned result union 的实际 commit/rollback：confirmed、unsatisfied、replacement 每种合法 matching result actual commit 都永久关闭其对应 attempt，unsatisfied 不消费 original fence、replacement 才消费；actual rollback 没有 result时保持对应 started unresolved/`needs_attention`，且无 guessed result或额外 provider/code call。另证明 closure 与 current readiness 分离：ACK 丢失后另一合法 refresh/reauth 先提交时旧 attempt 仍 closed并按 current facts 继续；随后从 current access rows 重算 effective deadline，较短 expiry 收紧且较长 expiry 不延长原 artifact 窗口，历史 deadline candidate 不能替代该读取。changed=false 后续 access-only refresh 或同 plaintext 重加密不冲突，changed=true 的 A→B→A、缺行、AEAD/归属无效或其他不一致返回 `oauth_credential_state_conflict`、阻断 rollout且 `provider_calls == 0`。explicit recovery 证据必须覆盖同一 original fence 的多次用户尝试：denial、missing、same 或已知 provider failure 在 current `T` 上写 `oauth.refresh_recovery_unsatisfied`并把 requested capabilities 从 `authorizing` 收敛为 `action_required`，保留 actual scopes/last-verified facts且 result 无 credential post/expiry；stale `T` capability no-op；下一次新 OAuthAttempt 的 different token 才以完整 F/S/T proof消费。Google/Microsoft callback 还要证明 `code+state`/任意合法 `error+state` 互斥、state 一次性消费、shape 错误在消费前拒绝、未知 error 首次消费后 replay 拒绝，以及分类矩阵：用户拒绝与未知名称为 `oauth_authorization_failed`，Microsoft consent evidence 为 `microsoft_admin_consent_required`，普通 `interaction_required` 为 `microsoft_reauthorization_required`；所有 Problem 均脱敏且 raw error不入库/日志/持久错误码。真实 Uvicorn callback canary 还必须证明 synthetic `code`、`state`、`error_description` 不出现在 stdout、stderr 或 JSON logs，而脱敏审计仍正常持久化。targetless identity 命中 fence 时保存前阻断且 credential/scope/capability 写入为零。所有 started/result/consumption 必填 proof 字段和严格 `created_at` 顺序都要核验。四个固定 synthetic vector 必须独立重算；M2 只证明固定 APP root key/version 的 immutable-key 行为。证据只能包含 attempt/source、稳定结果码、hashed connection、generation tuple、CAS/lease/provider-call 计数和 synthetic vector 标识；不得包含真实 token、identity fingerprint、root key、派生 HMAC key、raw scope 或 provider response。

`scripts/test-m2-release.sh` 还必须在任何子命令前固定测试数据库边界。外部未设置
`TEST_DATABASE_URL` 时，脚本设置并导出
`postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test`；外部已
设置时，只有逐字节精确相同才允许继续。任何其他或 production-like DSN 都必须在 `just ci`、pytest、
audit shell、deployment/tooling shell 等第一个 child process 前拒绝。随后所有子命令继承同一 export，
禁止只给某一条 pytest/audit 命令添加前缀。自动化证据必须覆盖缺失环境的确定性 Task13 选择、精确外部
值接受、每个 child 的继承，以及错误 DSN 下零 child calls。

0019 进入正常运行后只能通过新应用镜像前滚修复，其 Alembic downgrade 必须 fail closed；任何应用回滚目标也必须是只写 v2 的 0019-compatible 镜像。唯一例外是：若仍处于本次已封锁的维护窗口、通用服务从未在 0019 后启动且没有业务写入，且非空集合当前重算的 `effective_deadline` 已到或已经不能证明能在该截止时间前清完 marker，可使用本窗口指定的迁移前加密整库备份恢复到 revision 0018；这不是 Alembic downgrade，也不是直接 SQL。恢复前后必须核对 backup dump/checksum/manifest 三件套、原始与恢复后 `pre-migration` audit artifact 的无敏感摘要完全一致、Alembic revision=0018，再切回原记录的 0018-compatible immutable image 并运行健康检查；未完成这些核验不得启动任何通用服务。除此 sealed-window restore 外，不得删除事件或游标、移除 AAD 版本列或约束、恢复 v1 reader，亦不得让旧 writer 回流；禁止 OAuth-only 临时服务、跳过 marker 或迁移后直接清 marker。若有 scope 的有界重同步失败，保持真实写入开关关闭，按该精确 scope 排查连接能力、同步错误和供应商读取状态；当前重算的 `effective_deadline` 前仍可通过下一次显式 CLI 按既定 ordinal 规则重试，但不得通过扩大同步范围、删除事实或启用 legacy fallback 绕过故障。

### 0019 effective deadline 失败后的 sealed-window 整库恢复

若 0019 已提交，且非空集合当前重算的 `effective_deadline` 已到仍有 marker、任一 deadline guard 已失败，或运维人员已经不能证明 `post-resync` artifact 会在该 `effective_deadline` 前提交，则立即停止新的恢复 ordinal 和 provider probe。Caddy、API、普通 Worker、Scheduler 以及所有真实写入开关继续保持停止；恢复动作只要求 healthy PostgreSQL，Redis 即使继续运行也不是 sealed restore service 的依赖。不得执行 `alembic downgrade`、直接 SQL、手工清 marker、跳过审计、临时 full-account sync，亦不得启动仅用于继续 refresh/probe 的 OAuth-only API 或 Worker。

本路径只允许使用当前窗口在主动 refresh 之后、0019 之前创建并记录完整
dump/checksum/versioned-manifest 三件套的加密整库备份。维护窗口从停止新入口开始，到 `post-resync`、
通用服务启动和健康检查全部通过才结束；期间没有业务写入，且 preflight credential rotation 已包含在
该备份中。因此整库恢复只撤销失败的 0019 migration/resync 本地事实，不会丢失窗口后的用户业务写入，
也不会退回到 refresh 前已失效的 credential。

独立 `just calendar-aad-restore-0018 file`、operations-profile `calendar-aad-restore-0018` service 与
sealed validator 承接本路径；通用 `just restore file` 保持独立的输入规则，两条 restore 复用下文同一
database maintenance admission primitive。
任何 owner Secret 读取、owner DB connection 或 `pg_restore` 之前，sealed 宿主 recipe 先完成纯文件/本地
镜像 guard：独立验证安全 basename、加密 dump、相邻 checksum 与 versioned manifest 的完整绑定，
preflight 和原 `pre-migration` artifact 的 schema/basename/digest、两者绑定的 immutable image content ID，
并重新解析本机 Compose 选定镜像。sealed validator 与回调独立校验原artifact；回调先复用完整manifest
fingerprint基础核验，再比较0018专属快照，不调用通用restore入口，也不把preflight/exact-image条件施加到通用灾备。
缺失/错误镜像、移动 tag、checksum 或 artifact/image mismatch 都必须在 owner connection 与
`pg_restore` 调用数为零时失败。

guard 成功后，recipe 才把匹配 artifact 的精确 `sha256:...` content ID 作为内部
`CALENDAR_AAD_RESTORE_IMAGE_ID` 注入 Compose。新 `calendar-aad-restore-0018` service 的 `image:`
必须直接引用该 ID，不得引用 tag，不得包含 `build:`，并以
`docker compose --profile operations run --rm --pull never ... calendar-aad-restore-0018` 启动，禁止
pull/build。service 不得继承会等待/自动执行 migration 的 `backend-common`，`depends_on` 只能包含
healthy PostgreSQL；service 级环境只能固定 `PGHOST`/`PGDATABASE`，禁止预设
`PGUSER=ai_employee_owner` 或 `PGPASSWORD`。只有通过容器内 guard 的 controller 才可显式读取
`/run/secrets/postgres_bootstrap_password` 并建立 bootstrap owner connection。容器还只读挂载 backup passphrase Secret、受控 backup
volume、preflight/`pre-migration` artifacts，以及恢复成功后重授最小权限所需的 app/retention password
Secrets；这些 bootstrap/app/retention Secret 文件必须只对 controller 的 OS 身份可读。普通 API、Worker、
Scheduler 不挂载 owner Secret；generic restore、backup/audit 与 sealed 的 owner 仅存在于受控one-off生命周期，
不运行通用业务consumer，也不把owner密码放入service环境或SQL generator。

`backend/Dockerfile` 必须从 repository root build context 将
`backup-postgres.sh`、`restore-postgres.sh`、`restore-calendar-aad-0018.sh`、`init-db-roles.sh`、
`convert-legacy-backup.sh`、`scavenge-legacy-backups.sh`、`audit-calendar-aad-0019.sh`，以及 generic/sealed
verifier、maintenance-gate CLI、`ai_employee.cli.postgres_restore` 与
`ai_employee.infrastructure.db.postgres_restore_stream` COPY/安装到镜像内固定只读路径。sealed host
guard 与容器内 guard 都比较 image content ID、manifest/artifact 绑定和镜像内 allowlisted script digest；
backup/artifact volume 可以只读 mount，但禁止用 host bind mount 覆盖 executable。镜像缺脚本、digest
不符或出现 executable bind mount 时，owner Secret/connection 与 `pg_restore_calls` 都必须为零。

service 级环境不得预先设置 `PGUSER=ai_employee_owner` 或 `PGPASSWORD`。独立 sealed validator 必须先在
容器内再次验证备份 manifest 三件套、preflight/`pre-migration` artifacts、revision 0018 与 image/script
binding；通过后 controller 才读取 owner Secret，并且只把 target-scoped connection facts 与 credential
交给一个受监督 `psql` child。随后 sealed restore 以 `kind=sealed_0018` 进入下文统一 Python stream
executor：依序取得 management/target/exclusive-schema locks；completed 来源先验证并 fsync 旧 pair archive，
再由同一 owner transaction 重检该 pair、RESET completion GUC、建立 crash-surviving gate/active call authority
并只撤销 app/retention CONNECT；PUBLIC 在 baseline 已无 database privilege。事务提交前必须通过共享
`database_access.py`/`database_grants.py` 重读 exact active ACL、两角色安全、双向无 membership、当前 revision
active object-grant inventory；任一既有 drift 都在 mutation 前失败；
该 pre-restore 阶段不写 AuditEvent。随后按
`restore_backend_starting → restore_backend_ready → restore_started` 注册 exact backend。
`psql` 固定连接目标数据库，环境固定
`PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>`，命令固定
`psql --set=ON_ERROR_STOP=1 --single-transaction`；
`pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>` 仅生成 SQL，完全
没有数据库 credential 或连接能力。controller 逐块转发 SQL，不使用 shell pipeline，并核对两个 child
的退出状态。unknown outcome 必须先证明 authority 记录的 exact psql backend 已退出，之后才可按 pre/post
fingerprint reconcile。object/schema grants 与 verifier 由
同一持锁 owner session 完成。holder 必须先验证 restored object set 精确匹配 revision 0018，且 grant catalog
仅处于冻结的 `pg_restore --no-privileges` pre-grant shape；unexpected object、已有 extra/missing grant、错误
grantor 或 grant option 都 fail closed，不得修复。只有随后受控阶段可由共享 grant helper 应用 exact active
inventory并完整重读；verifier 再在该 connection 上 `SET ROLE ai_employee_app`、`BEGIN READ ONLY` 并
要求 DML 以 SQLSTATE `25006` 拒绝。不得启动独立 app-only verifier connection，也不得在 CONNECT reopen
后再运行会改变成功判定的验证。通用 `scripts/restore-postgres.sh` 不解析 sealed artifacts。

以下命令沿用上文已导出的 `PRE_0019_APP_IMAGE_TAG`、`APP_IMAGE_TAG`、`BACKUP_DIR` 与
`BACKUP_ARTIFACT_BASENAME`。`just calendar-aad-restore-0018` 保留 `[confirm]`
保护，并再次要求输入完整、精确相同的 restore path；生产环境还必须显式设置
`ALLOW_PRODUCTION_RESTORE=yes`：

```bash
cd /srv/ai-employee
export RESTORE_TARGET="${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}.dump.enc"

APP_ENV=production \
ALLOW_PRODUCTION_RESTORE=yes \
BACKUP_DIR="${BACKUP_DIR}" \
RESTORE_STATE_DIR=/var/lib/ai-employee/restore-state \
just calendar-aad-restore-0018 "${RESTORE_TARGET}"

# 在 recipe 的精确路径确认提示中再次输入 ${RESTORE_TARGET}；任一字符不符都会拒绝。
# recipe 先完成 artifact/image/script guard，再以精确 sha256 image、--pull never 启动 PostgreSQL-only restore。
# maintenance executor 在 gate/call/completion catalog boundary + CONNECT-revoked window 内完成 verifier 与 ordinal-aware atomic reopen；这里不另起 verifier。
docker compose ps

# 上述 audit 已 fail-closed 证明 revision 精确为 0018 且摘要与迁移前一致后，才切回原镜像。
export APP_IMAGE_TAG="${PRE_0019_APP_IMAGE_TAG}"
docker compose up -d api worker scheduler caddy
just health
```

sealed-specific verifier 是 restore executor 内部回调，没有独立 CLI 或 reopen 后补跑入口。它要求
revision 精确为 0018，验证本窗口 backup/checksum/manifest 与 preflight/`pre-migration` artifact 的
basename/image/script 绑定，并以同一确定性查询比较恢复后的 affected pair/connection count、locally hashed
pair set、事件/游标/密文摘要和本地可恢复性事实；它不得因 `effective_deadline` 已过而跳过任何数据比较。
任一差异或 PostgreSQL 未拒绝同连接 DML 都保持 database gate、CONNECT-revoked ACL 与服务停止。只有最终原子
reopen 已由不可缺失的 completed call、expected `ai_employee.restore_completion` 的 zero-slot digest 互绑、
gate absent、exact baseline ACL、两角色安全、双向无 membership 与完整 revision-0018 baseline object grants
核对为 committed，operator 才可
切回原 0018-compatible immutable image并运行 `just health`；新的 0019 尝试必须从新的完整
preflight/refresh/deadline/backup 窗口重新开始。completion audit 可作为诊断证据且可按 retention/privacy
删除，但 completed call 是 catalog predicate 的必需成员。

### OAuth refresh/exchange 协调事故处置

出现 `oauth_refresh_result_unknown`、`oauth_refresh_claim_lost`、`oauth_credential_state_conflict`、同一 automatic attempt 第二次 provider-call 企图、targetless fenced-identity 保存企图、Google/Microsoft callback 歧义/replay，或 APP root key/version 与事件不一致时，立即关闭受影响 provider/connection 的新写入与会触发 refresh 的同步入口；0019 窗口则继续保持全部通用服务停止。不要 disconnect、重放 Taskiq、复用 OAuth code、手工改 credential、删除 audit fence 或调用供应商“探测”。先以只读方式收集 connection digest、automatic/recovery attempt、source、F/S/T 或 G/G/G、lease/CAS result、versioned result-union member、capability transition、current-readiness disposition 和 provider-call count；日志与工单不得包含 token、scope 原文、raw provider error、identity fingerprint 或密钥材料。

若 result commit ACK 丢失或 rollback 不可证明，先关闭旧 session，使用新的数据库 session 按 `user_id + connection_id + attempt_id` 查询 versioned result union；即使 coordinator connection/lease 已丢失也不得跳过。matching confirmed/unsatisfied/replacement 只有在 schema、attempt/source 关联、时序和 identity-change 关系都合法时，才永久关闭各自对应的 attempt；confirmed disposition/flag/equality 不一致或 replacement 非 old != new、changed=true 都按无合法 result 处理。unsatisfied 不关闭 original automatic fence，replacement 才关闭。没有合法 result 才保留对应 started unresolved、`needs_attention`，且不补写 result、不重放 provider/code。关闭后单独核对 current readiness：精确 post 可恢复；后续合法 refresh/reauth 用 current facts；changed=false 后续 access-only refresh 或同 plaintext 重加密不冲突，changed=true 后 current identity 回到 old 的 A→B→A、缺行、AEAD/归属无效或其他不一致进入 `oauth_credential_state_conflict`，阻断 rollout但 provider call 为零且旧 attempt 不复活。解除 original fence 只能由用户新建带 connection ID 的 progressive OAuthAttempt：该 code 仅交换一次；denial、missing/same 或已知 failure 在 target `T` 仍匹配时把 requested capabilities 与 `oauth.refresh_recovery_unsatisfied` 同事务收敛为 `action_required`，保留 actual scopes/last-verified facts；stale `T` 只允许 capability no-op，用户需要时可再创建下一 OAuthAttempt；different token 才能原子提交 old != new、changed=true 的 replacement proof。Google/Microsoft 任意合法 error 必须先消费 state并走既定 unsatisfied/no-op 路径；Problem 按安全分类矩阵返回，保留 `microsoft_admin_consent_required`/`microsoft_reauthorization_required`，用户拒绝与未知名称使用 `oauth_authorization_failed`。shape 错误消费前拒绝，raw error不记录也不作持久错误码，replay fail closed。targetless callback 在 fenced identity 上必须保持本地 credential/scope/capability 零写入。只有 content-free 审计证明 original fence 已被完整 replacement proof 消费、automatic duplicate delivery 为零调用、连接能力与 scopes 重新有效后，才能恢复该连接；0019 还必须重新开始完整 preflight/deadline/backup 窗口。

## 备份与恢复演练

> **实施状态警告：** 仓库已提供Task27D的versioned manifest、整组发布/保留、独立generic/sealed
> service、数据库catalog恢复协议、受监督SQL stream、同一owner会话切换app的只读验证，以及legacy
> registry/scavenger。远端适配使用既有rclone；本任务不连接真实remote或生产数据库。
> 下文既说明实现行为，也保留目标发布验收要求。生产启用仍须Task30完整门禁、备份介质与目标环境演练；
> 不得用手工manifest、临时owner DSN、直接pg_restore或修改本地projection绕过固定入口。

备份命令使用Compose中的无密码`DATABASE_URL`、固定owner/passphrase Secret和宿主`BACKUP_DIR`。
宿主不读取Secret值；owner密码仅在受控镜像进程内交给固定目标的dump子进程，不接受PGUSER/PGPASSWORD覆盖。
生产还必须配置独立rclone remote子目录及其既有凭据配置：

```bash
cd /srv/ai-employee
APP_ENV=production BACKUP_DIR=/var/backups/ai-employee \
BACKUP_RCLONE_REMOTE='encrypted-remote:ai-employee' just backup
```

只有`BACKUP_ARTIFACT_BASENAME`未设置时才走普通时间戳备份；显式空值非法。显式安全basename选择
0019窗口备份，必须先完成对应preflight并满足当前guard，不能借此给普通备份指定窗口身份。
每个新普通备份发布相邻、作为一组验证的三件套：AES-256-CBC PBKDF2 加密的
PostgreSQL custom dump、`${dump}.sha256` 与 `${dump}.manifest.json`。manifest schema 固定为
`ai_employee.postgres_backup_manifest.v1`，至少包含：

- 安全 dump basename 与严格 UTC RFC 3339 `created_at`；
- PostgreSQL server version 与 Alembic revision；
- 加密 dump 的小写 SHA-256 和 `dump_size`；
- 精确 checksum filename；
- 宿主解析得到的 immutable backend image `sha256:` content ID，以及允许列表内、无内容的
  build/release metadata；
- 用于 unknown-outcome reconcile 的 content-free `restore_fingerprint_v1`；
- 镜像内 generic/sealed/legacy/audit/maintenance-guard scripts 与 CLI 的 allowlisted digest map。

三件套权限都是 `0600`，不得包含 DSN、主机/数据库凭据、Token、Cookie、邮件/日程内容、raw provider
ID 或真实个人数据。checksum 只使用安全相对 basename，同时校验最终 dump 与 manifest；manifest 反向
绑定 checksum filename 并重复 dump digest/size。

每次backup先取得管理库target lifecycle lock，再取得目标maintenance lock并证明合法idle；随后在目标数据库取得固定session advisory
`BACKUP_LIFECYCLE_LOCK=(20260806, 274)`，即执行 `pg_advisory_lock(20260806, 274)`，再对
`${BACKUP_DIR}/.ai-employee-backup.lock` 取得本地 exclusive `flock`；两把锁从本 run 唯一
`.partial.<uuid>` staging 创建、最终名冲突检查、dump/checksum/manifest 发布、remote 上传、日/周保留到
孤儿清理结束前都保持。PostgreSQL lock 负责同一数据库不同主机，`flock` 负责同一目录；固定顺序不得
反转，也不得用进程内 mutex 替代。

锁内还要由同一数据库 session 取得
`SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` shared advisory lock，读取 pre-Alembic revision，保持 shared
lock 覆盖完整 `pg_dump`，再读取 post-revision 并逐字节比较。只有 pre/post 相等且 session 仍存活并持锁，
才可把该 revision 写入 manifest、完成本地 manifest-last 发布并释放 shared lock。所有 migration 入口持
对应 exclusive lock直到 transaction commit/rollback，因此 backup 期间 migration 可以等待但不能提交。
完整fingerprint与`pg_dump --snapshot`必须共享同一owner导出的`REPEATABLE READ READ ONLY`快照；
并发已提交的业务写不能让manifest绑定与dump不一致。快照事务结束后，post-revision、完整schema摘要及
会话/lease证明都使用新读取，不能借旧MVCC快照隐藏schema漂移。shared/exclusive lock与fresh pre/post CAS缺一不可。

`restore_fingerprint_v1`覆盖支持revision的全部注册表、结构与数据库内计算的逐行hash聚合，不能只比较
行数。结构按schema/对象名与定义规范化，不绑定物理OID；原始行或正文不离开数据库。仅完整符合固定
completion-audit shape的历史`database.restore.completed`行被归一化；同名但形状不合法的行直接拒绝。
数据库gate/ACL/GUC由独立精确校验器验证，不纳入内容摘要。sequence结构、归属与安全后继必须有效，
当前计数不作为内容相等条件：安全ahead可以接受，落后、耗尽、方向/cycle不支持或不可读都拒绝。
唯一never-called非空例外是固定`checkpoint_migrations.v`序列且版本集合精确为0..9；不执行setval修复。
因此该摘要证明的是上述逻辑等价，不能解释为整库字节或sequence计数完全相同；长快照会延长锁与旧行版本保留。

脚本只在本 run staging 内生成三成员，完成交叉验证和权限设置后，以 no-clobber 方式发布 dump/
checksum，并以同文件系统原子no-clobber link发布manifest作为最后marker。任一最终成员已存在都按整组冲突
拒绝，不能覆盖、拼接或删除冲突成员。失败只处理本run staging与精确receipt证明的本次发布；
最终guard/lease失败可先撤回本次manifest再补偿其成员，绝不清理外来替换。缺final manifest的残留不是可恢复备份。

rclone使用`<target_identity_digest_v1>/<canonical-run-uuid>/`私有前缀，以immutable copy先上传dump/checksum，最后发布remote manifest；远端整组
完成前命令不得成功。local/remote orphan grace 固定为 3600 秒，且清理必须在两把串行锁内、删除前重新
读取 mtime 与 final manifest，只处理超时 `.partial.<uuid>` 或缺 final manifest 的超时组。live/recent run、
完整组或另一 run 不得被删除。七份日备、四份周备、冲突检测和清理都按完整 basename 组执行；
production缺失remote仍拒绝执行。跨主机竞争依赖同一数据库锁与协作方独占namespace；remote inventory
重读和manifest-last不构成远端CAS，也不保护不遵守协议的远端写入者。

本地保留/孤儿清理先证明完整组或固定grace资格，删除前重查mtime、身份、marker和闭合成员集合，再将
选中的全部成员捕获到本次私有0700`.postgres-backup-custody-*` custody；完整组先撤下manifest。
只删除custody中身份仍匹配的已捕获对象，不在公开路径上做stat后直接unlink。身份漂移、未知文件、
新marker、capture/fsync失败或取消时保留首错，按原basename无覆盖返还；公开名字已被占用时保留custody。
所有锁保持到补偿收尾。公开名字可能短暂缺失，逐成员删除不具备整组原子性，已经删除的旧组也不能因
后续备份失败恢复；同UID/特权进程与预先打开的fd不在隔离保证内。运维应停下相关写者，检查保留的
custody、manifest/checksum及身份后人工恢复，禁止自动覆盖或递归清空。calendar custody、restore state、
legacy registry与未知目录不参与普通孤儿清理。

每月在隔离、非生产数据库执行一次通用灾备恢复演练。通用 `just restore file` 保留原命令名与灾备用途：
它只接受已发布的 manifest-bound 三件套，并在任何 owner Secret、owner service、owner connection 或
`pg_restore` 前校验精确路径、安全 basename、manifest schema、dump/checksum/manifest digest 互绑、
size、revision、checksum filename、content-free image metadata、`restore_fingerprint_v1` 与镜像内脚本/
CLI digest map。它不要求 0019 preflight/`pre-migration` artifacts，不要求 revision 0018；普通包含 0019
的备份必须可恢复。generic允许当前canonical镜像ID与历史ID不同，但支持的revision和完整受管可执行
digest map必须精确相等，任何脚本变化都在Secret前拒绝；历史manifest/source/claim不重写。sealed与audit
仍要求原始artifact/manifest/current image ID一致。generic重建兼容不证明base image或依赖完全相同，
选择支持的immutable operations image与实际运行验证仍是运维责任。
`backend/Dockerfile` 必须把 generic/sealed restore、maintenance guard、legacy conversion/scavenger 与 audit
所需 executable COPY 到 immutable image；recipe 只执行镜像内固定路径，禁止 host bind mount executable。

development/test 恢复至少要先停止同一 workspace 的 Caddy、API、Worker、Scheduler、migration 与所有
会持有业务写凭据的 consumer/one-off；如果不能停止，只允许恢复到明确隔离、不会共享 workspace 网络、
volume 或数据库目标的一次性 PostgreSQL：

```bash
cd /srv/ai-employee
APP_ENV=test BACKUP_DIR=/var/backups/ai-employee \
RESTORE_STATE_DIR=/var/lib/ai-employee/restore-state \
just restore /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc
```

两条 restore recipe 都接受可选的 call/reopen ordinal，但没有自动重试授权。只有 catalog 明确处于
`restore_not_applied` 或 `reopen_not_applied`，并再次验证精确前态和下一个序号时，以下显式调用才可能继续：

```bash
# 同样从 /srv/ai-employee 执行；先沿用以上非生产环境、独立state和精确backup路径设置。
just restore /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc 2
just restore /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc "" 2
just calendar-aad-restore-0018 /var/backups/ai-employee/calendar-aad-0019-window.dump.enc 2
just calendar-aad-restore-0018 /var/backups/ai-employee/calendar-aad-0019-window.dump.enc "" 2
```

第二位置是 call，第三位置是 reopen；只批准 reopen 必须保留空的 call 位置。只接受无前导零的 ASCII
十进制 `1..999999`；同时传入两种序号、重复或未知选项都拒绝。recipe 的原 `[confirm]`、精确路径确认和
production 第二次确认继续有效。示例中的 `2` 不能覆盖数据库实际要求的序号，也不能授权未知结果重放。

production 通用恢复与 sealed 0018 恢复是不同流程，但同样必须先停写。有效配置必须明确为
`EXTERNAL_WRITES_ENABLED=false`、`GOOGLE_WRITES_ENABLED=false`、
`MICROSOFT_WRITES_ENABLED=false`；停止 Caddy、API、普通 Worker、Scheduler、migration、role bootstrap、
全部 general consumer、backup/retention job、任何持有 app/retention/owner 写凭据的 one-off，以及所有
owner maintenance/general consumer，只保留 PostgreSQL 和必要的非业务写基础设施。`just restore file` 在
启动 owner service 前检查 Compose/host/container 状态与 manifest，并在已有精确路径确认之外，再要求
production-only 的第二次精确确认。

最后 host/container 检查后，所有会连接、替换或删除目标数据库的维护入口先连接固定管理库 `postgres`，
取得该 cluster/target 的 session-level lifecycle advisory lock；generic/sealed restore 必须依次取得
management lifecycle、target 与 exclusive schema lifecycle lock，并从 attempt admission 一直持有到
reconcile/reopen 终态；`db-reset` 必须从环境与精确目标复核一直持有 management lock，create 后的 migration
再依序取得 target/schema lock。锁顺序固定为 `management lifecycle lock → target lock → schema lifecycle
lock`，不得倒序。
holder crash 后，新 `db-reset` 即使能重新取得 lifecycle lock，仍必须从目标 catalog 读取 active gate/call
authority；任一活动或 `needs_attention` 状态都必须保持 drop/create/migration 为零。普通 drop/create 只能经
同一 wrapper；`db-reset` 的 migration 由同一进程内不可伪造的 live lifecycle lease 调用，环境变量不能绕过。

target identity 的输入字节冻结为：
`SHA-256(b"ai_employee.restore_target.v1" || 0x00 || system_identifier_ascii || 0x00 || database_name_utf8)`，
末尾没有 NUL。`pg_control_system().system_identifier` 的 SQL `bigint` 输入只接受
`-9223372036854775808..9223372036854775807`：值大于等于零时直接作为 uint64；值小于零时先以无溢出的
numeric/host integer 加 `18446744073709551616` 恢复原始 uint64，再序列化为无符号十进制 ASCII。
最终 `system_identifier_ascii` 数值范围固定 `1..18446744073709551615`，不得有符号、空白或前导零；
`database_name_utf8` 是从 `pg_database.datname` 取得的精确名称，必须是合法
UTF-8、1–63 bytes、不得含 NUL，不做 Unicode normalization、大小写折叠或截断，并在目标存在时逐字节
等于 `current_database()` 的 UTF-8。目标暂不存在时，只有持 lifecycle lock 的 create/reset wrapper 可使用
已经通过完整名称确认的同一 UTF-8 bytes，并必须在 create 后从 `pg_database.datname` 逐字节复核。固定合成向量为 system identifier `72623859790382856`、database
`ai_employee_restore_test`，其 `target_identity_digest_v1` 必须等于
`71f328c1e8eb5d9cd2f8e1f815afac4b723f94a274900853c895fd9d72e60e49`。高位固定向量中 SQL value `-1`
必须先恢复为 uint64 `18446744073709551615`；同一 database 的 target digest 必须等于
`ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b`。测试 expected 使用独立标准库实现，
不得调用生产 encoder。lifecycle/target lock key 分别对
`b"ai_employee.database_lifecycle_lock.v1\0"`/`b"ai_employee.database_maintenance_lock.v1\0"` 加上该
32-byte raw digest 再做 SHA-256，取前 8 bytes 作为 signed big-endian bigint；上述向量分别为
`-9116408252019116299` 与 `5971001870339114140`。lifecycle lock 只从管理库 `postgres` 获取；target lock
由目标 owner control session nonblocking 获取。

取得 target lock 后必须直接从 `pg_db_role_setting` 读取三个 database-wide catalog fact：
`ai_employee.maintenance_gate` 保存稳定 attempt admission identity，
`ai_employee.restore_call_authority` 保存 manifest-bound generic/sealed 的调用与阶段事实，
`ai_employee.restore_completion` 保存最后一次已提交 completion digest；completed admission/ACK 必须由
completed call 与该 completion GUC 共同证明。三者都只接受当前
database、`setrole=0` 的唯一 setting；role override、重复 key/row、malformed array、session-local override
或只读缓存值都 fail closed。`current_setting(..., true)` 仅用于证明新 session 看见同一 catalog value。
随后 private holder 将初始观察绑定到不可替换的物理连接，跨事务逐项复核；不能按新catalog值重建
session anchor。唯一注册例外是该holder自己成功执行某个canonical `ALTER DATABASE SET` 后，PostgreSQL
将该key的session观察从NULL注册为空字符串；只允许该key的确切变化，其余slot必须不变。注册可能在
transaction rollback后仍留在session中，仍须验证归因；RESET没有此例外，连接或归因不确定立即拒绝。
gate 格式保持
`restore:v1:<attempt_uuid>:<generic|sealed_0018>:<target_digest>:<source_digest>`；generic 值恰好 185 ASCII
bytes，sealed 值恰好 189 ASCII bytes，source digest 等于 manifest SHA-256。legacy conversion 只在 disposable
target 上使用 durable registry/scavenger，不写 workspace/production target 的 gate/call/completion facts。

`ai_employee.restore_call_authority` 的规范格式升级为
`restore-call:v4|<attempt_uuid>|<generic|sealed_0018>|<target_digest>|<source_digest>|<manifest_digest>|<previous_completion_digest_or_dash>|<call_ordinal>|<reopen_ordinal>|<phase>|<gate_established_at>|<call_started_at_or_dash>|<backend_pid_or_dash>|<backend_start_or_dash>|<pre_revision_or_dash>|<pre_fingerprint_or_dash>|<expected_revision>|<expected_fingerprint>|<observed_revision_or_dash>|<observed_fingerprint_or_dash>|<completed_at_or_dash>|<completion_authority_digest_or_dash>`。
parser 只接受 ASCII、恰好 21 个 `|`、总长不超过 1133 bytes、上述字段顺序与 phase-dependent presence；
禁止 NUL/CR/LF、空字段、escaping、未知版本或额外字段。UUID 必须是小写规范文本，digest/fingerprint 是
64 位小写十六进制；revision 是 1–128 个 ASCII alphanumeric/underscore。ordinal 是无前导零的非负十进制
且范围固定 `0..999999`，使 exact `PGAPPNAME` 最长 63 bytes；backend PID 是正 int32 十进制，所有时间是
数据库 UTC RFC3339 六位微秒 `Z`。source 与 manifest digest 必须相同。

pristine 来源 attempt 的 previous completion digest 为 `-`；从 completed idle 开始时必须等于被验证旧 pair
的 digest并全程不变。`gate_established_at` 在 gate transaction 内冻结；每个新的 real-call ordinal 都填写
本次数据库 `call_started_at`，同一 ordinal 后继保持不变；exact-post direct edge 为 `-`。
`restore_backend_starting` 的 backend 两字段为 `-`；
`restore_backend_ready`、`restore_started` 与其结果分支保存经 `pg_stat_activity` 验证的 PID/start。pre facts
在 gate-established 可为 `-`，每个 real-call ordinal 都从本次 exact current 重新冻结；expected facts 从
admission 起冻结，observed facts 在 exact-post 判定后必填并保持到 completed。非 completed phase 的
completed_at/digest 为 `-`；completed call 必须包含
observed facts、completed_at 和合法 digest。

字段单调性只约束同一 `call_ordinal`：starting 建立的 call-start/pre 全程保留；starting→ready 只允许首次
填入 backend pair，ready/started/outcome 及其后继必须保留；exact-post 只允许首次填入 observed pair，后继
必须保留。任何已填事实都不得在同一 ordinal 内清空或改写。唯一允许跨 ordinal 清除已填字段的边界是合法
`gate_established|restore_not_applied → restore_backend_starting` CAS；它必须递增且不复用 ordinal，写入新的
call-start，把 backend PID/start 清为 `-`，从刚验证的 exact current 重新冻结 pre pair，并让
observed/completed 字段回到 starting shape，禁止继承上一 ordinal 的 backend facts。direct exact-post 保持
ordinal 0 的独立 shape，不执行该重置。

`ai_employee.restore_completion` 只接受
恰好 86 ASCII bytes 的 `restore_completion:v1:<completion_authority_digest_v1>`。writer 只能在持锁 owner
transaction 内用 `ALTER DATABASE SET/RESET` 并直接重读 `pg_db_role_setting`；每个 present key 必须在
当前 database、`setrole=0` 的 `setconfig` 中恰好出现一次。重复 key/row、role override、malformed array、
非 canonical encoding、未知 schema 或 last-write-wins 解析都 fail closed。

owner admission 必须复用本手册前述四-profile canonical ACL reader、safe runtime-role/membership reader 和
revision/phase object-grant verifier；稳定状态仍只接受以下三态闭集：

| 状态 | gate | call | completion | Access posture | Object grants | 允许动作 |
|---|---|---|---|---|---|---|
| `pristine_idle` | absent | absent | absent | exact baseline ACL + 两角色安全 + 双向无 membership | current revision exact baseline inventory | migration/role-bootstrap/ordinary lifecycle 或新 restore |
| `active` | present | 同 attempt、合法非 completed | absent | exact active ACL + 同一安全角色条件 | manifest revision exact active inventory | 仅 matching executor/reconcile |
| `completed_idle` | absent | 不可缺失的合法 completed call | present 且与 call zero-slot digest 互绑 | exact baseline ACL + 同一安全角色条件 | restored revision exact baseline inventory | ordinary lifecycle 或归档旧 pair 后的新 restore |

fresh/default 与 pre-protocol legacy 只可能被识别为 transient candidate，不能作为 restore state；只有 typed
role-bootstrap/protected reset post-create 可以在零 restore fact、零 drift 条件下收敛到 baseline。其他组合
全部 fail closed：gate-only、call-only、completion-only、completed call 缺失或 digest mismatch、cross-attempt
pair、active gate + completion/baseline posture、completed call + absent completion、gate reset + active posture、
unsafe/missing/mixed role、任一方向 membership 或任一 object-grant drift。`completion absent + exact baseline
posture/inventory` 不能单独判非法，因为 gate/call 同时 absent 时正是 `pristine_idle`。active
`needs_attention` 只允许事故处置，不能被 ordinary lifecycle 或新 attempt 覆盖。

无 active gate 的新 generic/sealed 请求在持有 management lifecycle + target lock、且建立 gate 或撤销任何
CONNECT 之前，必须先以只读 verifier 证明 current revision 与完整 `restore_fingerprint_v1` 逐字节等于
manifest expected post。证据相等时返回稳定结果 `restore_already_applied`、`psql_calls=0`、
`pg_restore_calls=0`，数据库写入、audit、gate/call/completion GUC mutation 全为零，既有 completed authority
保持不变。controller 只把 canonical content-free evidence 以 schema
`ai_employee.postgres_restore_already_applied.v1` 发布到
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>.<kind>.<source_binding_digest_v1>.already-applied.json`；exact fields
为 schema/kind/target/source/manifest/expected revision+fingerprint/observed revision+fingerprint/result，且
observed 必须等于 expected，且 canonical content 的 target/kind/source 与 filename 逐字节一致，使 generic
与 sealed 互不覆盖。文件通过同目录 temp、mode `0600`、file/directory fsync 与 no-clobber publication 发布，
不含时间戳、随机 ID 或 Secret；目标不存在时原子创建，已存在且 canonical bytes 完全相同才是幂等成功，
已存在但不同则 fail closed，禁止 overwrite。只有当前数据库的revision/fingerprint与manifest不同才继续
正常gate admission；本地证据冲突本身不授权新恢复，也不能跳过数据库判断。

新 attempt 只能从 `pristine_idle` 或 `completed_idle` 进入虚拟 `new`，冻结新的 attempt UUID、
`call_ordinal=0`、`reopen_ordinal=0`、database `gate_established_at` 和 exact kind/target/source/manifest。
pristine 的 previous completion digest 为 `-`。completed 来源必须先校验 old completed call 的 zero-slot
digest、matching completion GUC、gate absent、exact baseline ACL、两角色安全、双向无 membership 与完整
restored-revision baseline object grants，再把 exact old pair 重建/固化为旧 attempt 的 mode-`0600` final
state-v4 no-clobber archive并 fsync；已存在文件只接受 byte-identical。新 active call 的 previous completion
digest 记录该已验证 pair digest。

随后一个 owner transaction 再比较 exact old three-fact tuple 与 archive digest：从 pristine 要求三者 absent；
从 completed 要求 call/completion 仍互绑。事务按固定顺序 RESET completion、以新 `gate_established` active
call 替换 absent/旧 completed call、设置 gate并只撤销 app/retention CONNECT；PUBLIC 在 baseline 已无 tuple，
不能作为宽泛修复目标。active阶段仅在既有baseline之上增加app对`alembic_version`的SELECT，以便同holder
的app只读verifier读取revision；baseline及普通运行权限保持不变。commit 前直接重读 exact active ACL、
两角色安全、双向无 membership 与完整current-revision active object grants。该阶段不向 `audit_events` 写 gate/failure row；commit 后只 fsync
state-v4。若在 fsync 前 crash，另一主机从 active
gate + active call 重建。随后终止既有非 owner session并证明新 app/retention connection 被拒绝。
archive 后、catalog CAS 前 crash 仍保持原 completed idle；catalog commit 必须原子完成旧 completion RESET、
新 active call replacement、gate 与 ACL 变更，不能出现 gate-only 或 missing-call 中间态。
`needs_attention` 不允许被新 attempt 覆盖。gate 与 call authority 都是 crash-surviving database setting；
所有 migration、role-bootstrap/init-db-roles、generic/sealed restore/verifier、`db-reset` 和 owner one-off 必须
在首笔写或 drop/create 前调用同一 guard。legacy conversion 不调用或改写这组三态 catalog facts。
matching grants/verifier 只能作为持锁 executor 的函数使用同一 connection；环境变量或第二条 owner
connection 不能冒充 lock owner，active gate 下独立 role-bootstrap 绝不授 CONNECT。`init-db-roles.sh` 始终
是 typed CLI 薄 wrapper，不向 restore 暴露 shell grant SQL；restore 只能调用共享 `database_grants.py`。

database catalog 是跨主机唯一调用权威；本地文件只是 projection。restore service 必须把 backup 与 sealed
artifact mount 为只读，并把独立绝对路径 `RESTORE_STATE_DIR` 作为唯一可写 state volume；该目录不得等于
或位于 backup/artifact tree 下，host/container 都必须按无 symlink 的 resolved path 复核 mount source。
Task 27D 的 `.env.example` 只提供 development/test host 默认 `RESTORE_STATE_DIR=./var/restore-state`；Compose
把它 read-write mount 到 container 固定绝对路径 `/var/lib/ai-employee/restore-state` 并把容器内同名变量设为
该固定路径。production 禁止继承相对默认值，必须显式配置专用绝对 host directory/volume；启动前创建为
`0700`、验证 owner/mode、拒绝 symlink、world/group writable 与 backup/artifact resolved-path 重叠。该目录
只含 content-free projection，不能存数据库 credential、生成 SQL 或其他 Secret，也不得提交到仓库。
attempt projection 固定为
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>/<attempt_uuid>.json`，schema
`ai_employee.postgres_restore_state.v4`，目录 `0700`、文件 `0600`，通过同目录 temp、mode check、文件与目录
`fsync`、atomic rename 发布。它可记录 authority 的完整 content-free 镜像和稳定 result code，但不能授权
transition、spawn 或 reopen。completed projection 含 exact completed call + completion GUC，并在下一
attempt 前转为 immutable no-clobber archive。另一主机或 projection 丢失/损坏时，必须从 gate/call authority
与 completion GUC 重建；`completed_idle` 要求不可缺失的 completed call、matching completion GUC、gate
absent、exact baseline ACL、两角色安全、双向无 membership 与完整 baseline object grants；completion audit
可存在或已按 retention/privacy 删除但不能补足 catalog pair。catalog authority 或 access/grant posture 信息
不足、漂移或相互矛盾时返回 `needs_attention` disposition并停止。只有当前 phase 在
下述 graph 中存在到 `needs_attention` 的边才可持久化该 phase，否则保持 authority 不变交由事故处置；
不得把“缺 local state”解释成“从未 spawn”。legacy conversion 只使用独立 durable registry/disposable target。

完整 phase graph 逐字冻结为：

```text
new → gate_established
gate_established → restore_backend_starting | restore_succeeded  # 后者只允许 exact post evidence，零 psql/pg_restore
restore_backend_starting → restore_backend_ready | needs_attention
restore_backend_ready → restore_started | restore_not_applied | needs_attention
restore_started → restore_succeeded | restore_outcome_unknown | restore_not_applied | needs_attention
restore_outcome_unknown → restore_succeeded | restore_not_applied | needs_attention
restore_not_applied → restore_backend_starting # 显式新 call_ordinal，且先证明 exact pre-state
restore_succeeded → grants_succeeded
grants_succeeded → verified
verified → reopen_committing
reopen_committing → completed | reopen_not_applied | needs_attention
reopen_not_applied → reopen_committing         # 新 reopen_ordinal
```

`completed` 与 `needs_attention` 是终态；禁止自动跳跃、倒退、非法 self-transition。每次 catalog 更新都必须
在 owner transaction 中以 exact authority value 做 CAS，并至少比较 attempt UUID、expected phase、
`call_ordinal`；reopen 分支还比较 `reopen_ordinal`。CAS miss 不得继续外部调用或写阶段。grant transaction
明确 rollback 时 authority 保持 `restore_succeeded`，verifier 失败时保持 `grants_succeeded`，仅允许有界
重试，不用 self-transition 伪造进度。

matching gate 在 `gate_established` 恢复时，若同一 holder 的只读 verifier 证明 current 完整等于 expected
post，则直接 CAS `gate_established → restore_succeeded`；不得启动 psql/pg_restore，也不写单独的
already-applied audit。该 attempt 随后仍必须经过 grants、verifier 与统一 completion transaction。无 exact
evidence 时只能继续正常 call；不存在“人工标记已应用”分支。

每个真实 call 必须先在 holder transaction 证明 current 完整等于冻结 pre-state，再以 exact old authority
CAS `gate_established|restore_not_applied → restore_backend_starting`。该 transaction 必须把
`call_ordinal` 恰好递增 1 且从不复用，保持 `reopen_ordinal`，写入本次新的数据库 `call_started_at`，把
backend PID/start 清为 `-`，从刚验证的 exact current 重新冻结 pre revision/fingerprint，并把
observed/completed 字段置为 starting shape 的 `-`；从 `restore_not_applied` 重试不得沿用上一 ordinal 的
backend facts。同一 ordinal 后续 ready/started/outcome 必须保留本次 call-start/pre/backend facts，direct
exact-post ordinal-0 shape 不受此规则影响；retry 在 exact-pre 证明前不得分配新 ordinal。受控 Python executor
随后读取长期 bootstrap owner Secret，只用它启动一个固定目标数据库、固定 `ai_employee_owner`、固定
`PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>` 的 `psql --set=ON_ERROR_STOP=1
--single-transaction` child。psql 的 stdin 是 controller 独占 write end 的匿名 pipe；child 连接后等待
stdin，尚未收到任何 SQL。controller 从独立持锁 session 查询 `pg_stat_activity`，要求 exact database、role、
application name 只有一个 backend，验证 PID 与 `backend_start` 晚于本次 spawn fence，再 CAS 为
`restore_backend_ready` 并持久化 PID/start。

只有 gate、authority、backend identity 与 controller-owned pipe 仍逐字节一致时，controller 才 CAS
`restore_backend_ready → restore_started` 并 fsync v4 projection；这两步完成前写入 psql stdin 的 SQL bytes
必须为零，也不得 spawn `pg_restore`。随后 controller 先发送 transaction-local deferred completion guard
prelude，再启动无任何 `PG*` credential/DSN/Secret 的
`pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>`。它逐块读取 generator
stdout 并写入已登记 psql pipe，禁止 shell/uncontrolled pipeline、整段 SQL 落盘或日志输出；两边 stderr
只允许稳定脱敏摘要。只有 pg_restore EOF 且 exit=0 时才发送 completion-guard trailer并正常关闭 pipe。
缺 trailer 的 EOF、controller crash、任一 child 非零或中途 pipe failure 必须令 psql 的 deferred guard/
single transaction rollback；controller 要终止另一 child、关闭所有 FD并记录两个 exit/status。

controller 在 psql backend visible 前 crash 时，唯一 pipe write end 随父进程关闭；psql 即使稍后连接也只
读取 EOF，且 phase 未到 `restore_started`，不得执行 SQL。`restore_backend_ready` 后、feed 前 crash 同样
零 SQL；mid-stream crash 因 completion trailer 缺失而 rollback。`restore_backend_ready`/`restore_started`/
unknown 的 reconcile 必须先按 authority 中 PID+backend_start+database+role+application name 证明 exact
backend 已退出，再读取 revision/fingerprint。处于 `restore_backend_starting` 时，只有原 controller 仍在本机
存活、仍持有可验证的唯一 child/pipe identity，且能证明 SQL-byte-zero，才可继续登记 backend 并 CAS 到
`restore_backend_ready`。另一 controller/主机或缺失、损坏、不可验证的 local projection 不得用
pipe-owner-dead/SQL-byte-zero 猜测 not-applied，不得分配新 ordinal；必须 CAS `needs_attention`（CAS 不安全时
只持久化同 disposition）并保持 `pg_restore_calls=0`。ready/started/unknown 在 backend 退出后，exact post
转 `restore_succeeded`，exact pre 转 `restore_not_applied`，其他/不可区分转 `needs_attention`。starting 的
人工处置必须先核对并终止可能存在的 child/backend，再显式 forward-fix，不能声称自动恢复。只有 operator
明确授权且再次证明 exact pre，才可从合法 `restore_not_applied` 为相同 attempt 分配新 ordinal；新 ordinal
必须记录新的 call-start、重新冻结 exact-current pre pair、清除旧 PID/backend-start，并让
observed/completed 符合 starting shape；演练同时拒绝旧 backend facts 继承、ordinal 复用及同一 ordinal 内
清空 call-start/pre/backend facts；禁止盲目 replay。故障演练覆盖 spawn-before-visible crash、
backend-ready-before-feed、mid-stream crash、generator/
consumer 任一失败、commit ACK unknown、两 host overlap、stale PID/backend_start、跨主机无 local projection
时 `pg_restore_calls=0`/`needs_attention`，以及原 controller 持有 local identity 的 starting→ready 路径。

object/schema grants 与 manifest-selected verifier 全部在 gate active、CONNECT revoked且持锁 owner session
中完成。grant helper 先证明 restored catalog 的 object set 精确匹配 manifest revision，并且实际 grants
只处于冻结的 `pg_restore --no-privileges` pre-grant shape；unexpected object、pre-existing extra/missing grant、
错误 grantor 或 grant option 都在任何 grant/ACL/authority/audit 写前失败。只有该阶段可应用 exact active
inventory并完整重读。随后 verifier `SET ROLE ai_employee_app`、`BEGIN READ ONLY`，断言 session/current user
并要求 DML SQLSTATE `25006`；所有可失败检查都在 reopen 前完成。之后 CAS
`verified → reopen_committing` 并递增 `reopen_ordinal`。completion event type 固定为
`database.restore.completed`，metadata schema 固定
`ai_employee.database_restore_completed.v1`，exact metadata keys（禁止额外 key）为 `schema`、`attempt_id`、
`kind`、`target_identity_digest_v1`、`source_binding_digest_v1`、`manifest_sha256`、
`previous_completion_authority_digest_v1`（pristine 来源为 JSON `null`）、`final_call_ordinal`、
`final_reopen_ordinal`、`gate_established_at`、`final_call_started_at`（direct exact-post 为 JSON `null`）、
`final_backend_start`（未启动 psql 为 JSON `null`）、`expected_post_revision`、
`expected_post_restore_fingerprint_v1`、`observed_post_revision`、
`observed_post_restore_fingerprint_v1`、`maintenance_gate_version`（固定整数 `1`）、
`maintenance_gate_value`、`completed_at`、`completion_authority_digest_v1`。这些安全 timeline/fingerprint
字段来自 active call 与最终 verifier；`completed_at` 必须是数据库在最终 transaction 内冻结的 UTC
RFC3339、恰好六位微秒和 `Z`，不接受客户端本地时间。gate/call/failure/needs-attention 阶段不预写
AuditEvent；事故稳定后可人工追加 content-free incident audit，但它不是 restore authority。

completion digest 覆盖完整 completed-call schema：先构造 exact `restore-call:v4` completed value，把最后
digest slot 固定为 64 个 ASCII `0`，得到 `canonical_completed_call_bytes`；
`completion_authority_digest_v1 = SHA-256(b"ai_employee.restore_completion_authority.v1\0" || canonical_completed_call_bytes)`。
stored completed call 只把该 zero slot 替换为所得小写 digest，其他 byte 不变。核对时必须重新归零并重算，
要求 call digest、completion GUC digest 与重算值三者相等。最终 owner transaction 必须
先锁定并读取 `users` 全表且要求恰好一行；该唯一管理员可为 active，也可为 M1 全数据删除后保留的 inactive
anonymized row，零行或多行都 fail closed。随后同一 transaction 同时把 call authority CAS 为带该 digest 的
`completed`、插入一条普通 BigInteger-ID completion audit、
`ALTER DATABASE ... SET ai_employee.restore_completion='restore_completion:v1:<digest>'`、reset maintenance
gate，通过 shared helper 应用唯一允许的 active→baseline object-grant policy delta，并只恢复 app/retention
最小 CONNECT；`PUBLIC` 保持 revoked。commit 前必须重读 exact baseline ACL、两角色安全、双向无 membership
与完整 restored-revision baseline inventory。AuditEvent ID 不进入 canonical bytes、
call authority、GUC 或 ACK predicate。completion audit 外层字段固定为：`user_id` 是上述唯一管理员 ID，
`task_id=NULL`，`event_type=database.restore.completed`，`actor_type=system`，
`actor_id=database_restore`，`created_at` 使用数据库时间；metadata 使用上述 exact versioned key set且禁止
额外 key。最终事务当刻已有 same-attempt/digest audit、INSERT 数量不是一、schema/key set/值/digest 不匹配
或 access/grant 最终核验失败，都使 call/audit/GUC/gate/ACL/object grants 在一个 transaction 内整体 rollback；
这是原子提交证据，不是 admission/ACK authority。audit 后续被普通 retention 或全数据删除移除不影响
completed call + completion GUC pair。

ACK unknown 时关闭旧 session并从新 owner session同时读取 call 与 completion。只有 gate absent、exact
baseline ACL、两角色安全、双向无 membership、完整 baseline object grants、合法 completed call、expected
`restore_completion:v1:<digest>`，且 zero-slot 重算证明二者互绑，才证明 committed 并只收敛 projection；
audit/local projection 可缺失，completed call 不可缺失。active attempt 建 gate 时已 RESET completion，因此
exact `reopen_committing` active call + absent completion + matching gate + exact active ACL/safe roles/zero
membership/active object grants 才证明 final transaction 未应用，并以 exact old call value CAS 到
`reopen_not_applied`。completed call 缺失/digest mismatch、completion + active gate、absent completion +
completed call、unsafe role/membership、grant drift或其他非三态组合进入 `needs_attention`。
`pristine_idle` 的 absent completion + exact baseline posture/inventory 合法。只有 operator 显式授权才以新
`reopen_ordinal` 再次进入 `reopen_committing`。commit 后 host crash 不是 reopen failure，服务仍由 operator
显式启动。

目标操作顺序如下；只有 `just restore` 已完成 owner restore、重授权限和通用 manifest verifier 后，才可
重启服务：

```bash
cd /srv/ai-employee
export APP_ENV=production
export ALLOW_PRODUCTION_RESTORE=yes
export RESTORE_STATE_DIR=/var/lib/ai-employee/restore-state
export EXTERNAL_WRITES_ENABLED=false
export GOOGLE_WRITES_ENABLED=false
export MICROSOFT_WRITES_ENABLED=false

docker compose stop caddy api worker scheduler migration role-bootstrap
docker compose ps --all
# 另外停止当前部署启用的全部 general consumer、backup/retention job 与 owner maintenance one-off；
# ps/主机进程核验必须证明它们均未运行。仅 PostgreSQL 和必要的非业务写基础设施可保留。
just restore /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc

# 仅在新 session 按 completed call + expected restore_completion GUC 的 digest 互绑、gate absent、exact baseline ACL、safe roles、zero membership 与完整 baseline object grants 核对后显式执行；audit 可缺，catalog call 不可缺。
docker compose up -d api worker scheduler caddy
just health
```

演练必须记录 manifest 三件套与权限、global backup lock/`${BACKUP_DIR}` flock、unique staging、管理库
lifecycle lock 与固定锁序、schema-lifecycle shared/exclusive lock/pre-post revision CAS、固定 target digest/
lock vectors、app-role restore/DDL 被拒绝、三个 database-wide catalog fact 在新 session 可见、PUBLIC/app/
retention CONNECT revocation、既有 writer termination、新 writer connection denial，以及 active authority 下
reset/migration/role-bootstrap 零 drop/create/write。还必须记录四个 exact ACL tuple multiset、PostgreSQL 17
owner `is_grantable=false`、两角色全部冻结属性、双向无 membership、candidate 只由 bootstrap 接受、Compose
role-bootstrap→typed migration 且无 post-repair、pre-step grant drift 零写、per-step new-object/policy delta 与
0019-last-callback 全 rollback，以及 restore active/baseline phase inventories。还必须记录只读 backup/artifact mount、独立
`RESTORE_STATE_DIR` v4 projection、跨主机无 local state 重建、逐 transition CAS、exact `PGAPPNAME` backend
注册前 SQL-byte-zero、backend-ready-before-feed、mid-stream EOF rollback、exact PID/backend_start 退出证明、
跨主机 starting 无 local child/pipe identity 时 fail closed 且 `pg_restore_calls=0`、两 host overlap、
`pristine → active → completed → next active` 三态矩阵、第二次恢复较旧 backup 时验证/归档旧 completed pair并
在 gate transaction 替换为新 active call且清除旧 completion GUC、commit 后 host crash 只读收敛、commit/rollback/
inconsistent 三支、owner
session `SET ROLE ai_employee_app`/`BEGIN READ ONLY`/SQLSTATE `25006`，以及 ACK-lost reopen 的 completed 与
`reopen_not_applied`/新 `reopen_ordinal` 分支；两支都必须同时核对 exact ACL、safe roles、zero membership 与
对应 phase object grants。演练证据包括 catalog snapshots、state-v4 checksums 与恢复后
唯一 completion audit，不要求会被 restore 覆盖的 gate audit。任一差异都保持服务停止；若 final transaction 已提交，host
crash 只需 reconcile，不得重做 reopen。

Task 27D 以前产生的 pre-manifest legacy backup 禁止直接恢复 production，也不得给旧加密文件补写或
伪造 manifest。唯一入口固定为：

```bash
cd /srv/ai-employee
APP_ENV=test BACKUP_DIR=/var/backups/ai-employee \
just restore-legacy-to-isolated \
  /var/backups/ai-employee/legacy-ai_employee.dump.enc \
  ai_employee-converted-20260810T000000Z
```

recipe 签名是 `just restore-legacy-to-isolated file output_basename`，只允许
`APP_ENV=development|test`。专用 `scripts/convert-legacy-backup.sh` 执行固定流程：

1. 验证 `file` 是显式受控的普通非符号链接文件、相邻 legacy checksum 正确；源目录只读挂载，允许与
   输出 `BACKUP_DIR` 分离。验证
   `output_basename` 使用普通 safe-basename 规则且新三件套任何成员均不存在。
2. 生成 attempt UUID，并在创建 Docker/temp 资源前原子+fsync 发布
   `${BACKUP_DIR}/.legacy-conversion-registry/<attempt_uuid>.json`；record mode `0600`、目录 `0700`，登记
   project/container/network/volume 名、UTC `created_at`、kind/source binding、统一 labels 与受控临时 Secret
   path。随后创建 `aiemployee-legacy-<32位小写十六进制 UUID>` Compose project，只启动 profile
   `legacy-conversion` 下的 `legacy-conversion-postgres` 与 `legacy-backup-converter`，使用 project-private
   `internal: true` network、ephemeral PostgreSQL volume，以及登记路径中的 mode `0600` 临时数据库
   Secret。全部资源使用精确labels `com.ai-employee.maintenance.attempt`、
   `com.ai-employee.maintenance.kind`、`com.ai-employee.maintenance.created_at`；不得输出 Secret，也不得连接 workspace/default/
   production network、volume、hostname 或数据库。
   仅该专用PostgreSQL以`0:0`启动官方初始化入口，初始化后server必须降为非root的PostgreSQL OS用户；
   converter仍以宿主UID:GID运行，不改变普通PostgreSQL服务或全局engine配置。
3. 只向 isolated PostgreSQL 恢复 legacy dump；在同一个owner RR/RO事务中读取完整fingerprint与真实revision，
   随后`SET LOCAL ROLE ai_employee_app`检查固定业务读取、sequence健康及真实SQLSTATE`25006`。
   不为健康检查扩充baseline grants，不写restore catalog事实；未知或不受支持revision立即失败。
4. 以 isolated 数据库为 source 调用上述普通锁定 backup 路径，在受控 `${BACKUP_DIR}` 下用精确
   `output_basename` no-clobber 发布新的 dump/checksum/versioned-manifest 三件套。不得给旧字节追加
   manifest、stamp/repair/downgrade revision或调用 generic target/sealed restore。
5. 每次 conversion 开头先运行 `just legacy-backup-scavenge`。成功只输出 content-free 新组路径/稳定
   结果码；失败只输出稳定错误与清理结果，不得输出 DSN、Secret 或业务内容。trap 在成功、错误和可捕获
   signal 上执行 `docker compose -p <project> --profile legacy-conversion down --volumes --remove-orphans`，
   再删除 temp Secret/registry并确认 project resources 均不存在。
6. SIGKILL、宿主 crash 或 Docker daemon crash 由独立 `scripts/scavenge-legacy-backups.sh` 收敛。scavenger
   扫描 registry 与精确 labels，固定 grace 3600 秒；只在 record 超时、per-attempt nonblocking lock 可取得
   且无 active labeled container 时，删除该 attempt 的 container/network/volume、temp Secret 与 registry。
   Docker 不可用、labels 不匹配、仍有 lock/container 或尚在 grace 内一律保留并返回稳定结果；另一 active
   run 不得被影响。registry/Secret 路径不上传 remote，也不进入普通 backup orphan cleanup。
   旧标签、缺失或未知registry、未知custody一律保留供人工核对，不能重标资源后自动认领。

新三件套通过普通验证后才可能进入后续灾备演练。

0019 sealed-window 恢复另行按上一节使用 `just calendar-aad-restore-0018 file` 演练。它必须使用带
完整合成 backup manifest 三件套、preflight/`pre-migration` artifacts 与精确本地 image binding 的专用
fixture，覆盖 owner-before-secret、精确 `sha256:`/`--pull never`、image-internal script digest、revision
0018、共享 management/target/schema locks、database gate/call/completion facts、read-only artifacts、独立 state-v4/
Python psql/pg_restore stream、backend-ready/SQL-byte-zero/ordinal reconcile 与 completion-GUC atomic reopen，
四个 exact ACL profiles、safe roles/zero membership、revision-0018 active↔baseline object-grant inventory，
以及同一 owner session 的 `SET ROLE + BEGIN READ ONLY`/SQLSTATE `25006`。generic/sealed/legacy recipe、service、
validator、artifact、fixture 和 tests 互不调用或混用；generic/sealed 共享同一 admission/access/grant
primitive，legacy 只使用 disposable-target lifecycle。

## 主机与网络

VPS 的 PostgreSQL、Redis、Caddy、Prometheus 与 Grafana 持久卷必须使用主机或块设备加密。只有 Caddy 发布 80/443；数据库、Redis、API 指标和 Worker/Scheduler 指标均留在 Compose 内部网络。Caddy 负责 TLS 和压缩，公共 `/metrics` 固定返回 404。Grafana 仅在 `observability` profile 下启动，保留 Grafana 登录并使用 `/ops/grafana/` 子路径。

生产 Compose、开发 Compose、`just api` 与 E2E backend 脚本四个 Uvicorn 入口都必须关闭默认 access
log；应用 JSON 日志不允许 raw URL、query string 或 request target。发布 canary 必须启动真实 Uvicorn
子进程，请求带 synthetic `code`、`state`、`error_description` 的 OAuth callback，并确认 stdout、
stderr、JSON logs 中三个值零命中，同时从 PostgreSQL 读取脱敏 callback-error 审计，证明一次性 state
消费没有因日志收口而失效。

## Secret 与访问撤销

所有生产 Secret 由部署平台创建为 Docker Secret，再以 `/run/secrets/*` 文件挂载。不得把值写进 Compose、环境文件、日志、镜像层、备份名或 CI 输出。数据库、Grafana 管理员密码和备份口令轮换时，先创建新版本、滚动重启依赖服务、验证健康和恢复演练，最后撤销旧版本。M2 的 `APP_MASTER_KEY_FILE` root key 与 `AeadCipher.key_version` 固定，禁止按普通 Secret 流程轮换；未来变更必须先有新的 ADR/里程碑，覆盖现有 ciphertext 重加密、refresh identity 重算和 durable fence 迁移。

在应用中断开 Google 或 Microsoft 连接时，系统先提交本地凭据密文删除、连接断开、能力 revoked
和未认领审批失效，再尽力调用供应商窄撤销端点。远端失败不能恢复本地 Token，也不会产生持有
Token 的重试任务。Microsoft 没有可用的窄撤销端点时同样记录 `oauth.revoke_unresolved`；其元数据
只有 provider、稳定错误码和 connection ID，时间由审计事件保存。

Scheduler 复用每分钟 `expire-approvals` 入口，扫描已关闭全局/供应商写开关下仍未认领的审批，
并统计未被补救的 OAuth 撤销事实。调整部署开关后应使 API、Worker、Scheduler 使用同一份新配置。
积压通过 `ai_employee.oauth.revoke_backlog` JSON 告警公开 provider、`oauth_revoke_unresolved`
错误码和整数 `unresolved_count`，不会访问供应商或重建已删 Token。管理员须在对应供应商的账号
安全页面移除 AI Employee 授权；完成后可通过应用维护用例
`OAuthRevokeMaintenanceUseCase.record_remediation(user_id=..., connection_id=..., unresolved_event_id=...)`
记录精确审计事件的补救确认。该入口校验连接归属、拒绝自由文本和重复确认，仅追加
`oauth.revoke_remediated`，不修改原始审计或执行网络请求。

能力关闭或连接断开只把没有 ToolExecution 的动作取消并退回编辑态。已认领动作保留 approved
审批和冻结命令，进入只读核对；后续核对可根据零 request-start 事实确认未应用。已有 request-start
但断开后无法取得只读 Token 的动作进入 `needs_attention/connection_scope_missing`，提供供应商
检查入口和人工确认，不能把删除凭据解释成未执行或自动重发。Worker 已在内存持有短期 Token
时可以完成当前只读核对。

模型 API Key 轮换时，更新 Secret、滚动 API/Worker/Scheduler，并确认结构化模型调用仍使用预期
供应商与数据最小披露策略。

## M2 操作快照与监控

`GET /api/v1/actions` 按 `limit`（1～100）、`offset`、`status`、`item_kind`、`provider`、
`action` 筛选当前用户的统一操作列表，按更新时间倒序及种类/本地 ID 稳定排序。
`mail_draft`、`calendar_proposal` 项使用自身编辑 ID，`task_id` 为 null；冻结后的
`trusted_task` 项携带真实 TaskRun ID。列表不读取正文、地址或日程标题。

`GET /api/v1/actions/{task_id}` 在同一 PostgreSQL 可重复读视图中返回本地对象摘要、冻结
审批、执行记录和有序时间线。`task_version` 与 `event_cursor` 都是相同的规范十进制字符串；
客户端不得先转换为 JavaScript number。`approval.preview.kind` 为 `mail` 或 `calendar`，
分别包含精确冻结的邮件字段或日程前后字段、通知策略与本人日历冲突。内容 AEAD 清除后返回
`content_status=redacted`、`preview=null`，没有旧明文回退。成功和错误响应均使用 `no-store`。
冲突查询覆盖冻结日程的完整时间范围及会议缓冲；修改和恢复排除目标事件自身。该读取不改变
候选时间建议的原有窗口，也不会查询参会人的 Free/Busy。

未知结果只能使用以下两个受 Cookie 会话和 CSRF 保护的入口：

- `POST /api/v1/actions/{task_id}/reconcile` 返回 202 与原 TaskRun ID，通过 Outbox 重开只读核对。
- `POST /api/v1/actions/{task_id}/manual-resolution` 只接收 `resolution` 和 `task_version`。
  resolution 仅允许 `confirmed_executed`、`confirmed_not_executed`；成功返回 200、新审计版本和
  TaskRun ID。旧版本或并发收敛返回 `409/manual_resolution_conflict`。人工确认不会调用供应商，
  确认未执行也不会自动重发；再次写入需要新草稿/提案版本和新的精确审批。

现有 `/api/v1/tasks/{task_id}/events` 是唯一任务 SSE。六种 M2 事件及可信任务缺口快照都过滤
敏感元数据；未知事件仍保留游标，客户端忽略其业务载荷并重读动作快照。未提交草稿、提案和
连接能力继续以 REST 为准，在页面聚焦或重新连接后刷新列表。

M1 的 `step.started`、`step.completed`、`step.failed` 保留受限英文步骤标识 `name`、非负
32 位步骤 `sequence`，以及显式 null 或经过同一标量白名单过滤的 `output_summary`。
步骤序号与信封中的审计游标含义不同；名称、摘要和步骤序号不会因该兼容规则进入 M2 或未知事件。

九个 M2 Prometheus 指标使用 `ai_employee_` 前缀，标签分别为：

| 指标 | 标签 |
| --- | --- |
| `provider_write_requests_total` | provider、action、outcome |
| `approval_decisions_total` | action、decision |
| `approval_expired_total` | action |
| `tool_reconciliation_total` | provider、action、outcome |
| `tool_reconciliation_age_seconds` | provider、action |
| `needs_attention_tasks` | provider、action |
| `calendar_version_conflicts_total` | provider |
| `connection_capability_state` | provider、capability、state |
| `write_kill_switch_state` | provider |

写与只读核对计数来自通过可信门禁后的 adapter 调用边界，异常结果计为 unknown；审批计数只在
事务提交后增加。状态 Gauge 由 Worker 每三十秒从 PostgreSQL 聚合恢复，能力 Gauge 表达各状态
的连接数量。核对年龄从最老未决请求开始计算；心跳与年龄在 Worker/Scheduler 直接抓取 registry
时也会随单调时钟增长。计数器是进程内观测，重启后归零，不能代替持久审计证明执行次数。

`ops/observability/alerts.yml` 已通过 Compose 只读挂载并由 Prometheus 加载。五类告警为：人工
确认等待超过十五分钟、被 request-start CAS 阻止的重复写企图、有效写开关与适配器缺失或各进程
配置漂移、撤销失败持续十五分钟积压、核对扫描器或 Outbox relay 缺失/停止心跳。后两项复用既有
`stuck_tasks` 和 `process_heartbeat_age_seconds` 指标，使用固定维护标签，不含账户或任务 ID。
cron 消息由 Worker 实际执行，只有工作完成后才刷新 `reconciliation_scanner`/`outbox_relay`
心跳；普通 Scheduler/Worker 进程心跳不表示维护任务正常。告警规则只产生 Prometheus 告警状态，
外部通知渠道仍由部署环境配置。

重复写企图的规则覆盖首次抓取即为非零的计数器，避免新进程的第一条异常因缺少零基线而漏报。
在仓库根目录、已安装 Docker 且可取得 Compose 使用的 Prometheus 镜像时，可运行以下无网络、
只读挂载的合成规则验证；命令只创建退出后自动删除的测试容器，不访问业务数据库或供应商：

```sh
docker run --rm --network none --entrypoint /bin/promtool \
  -v "$PWD/ops/observability:/etc/prometheus:ro" \
  prom/prometheus:v3.3.1 test rules /etc/prometheus/alerts.test.yml
```
