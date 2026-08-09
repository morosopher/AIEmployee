# AI Employee 运行手册

本手册覆盖可信任务中心与每日办公简报的 M1 基线，以及当前已落地的 M2 邮件与日历助手发布边界。生产环境不连接测试账号，不以 Redis 作为业务事实来源；M2 真实写入仍必须遵守三层开关、连接能力和精确人工审批。

## 发布与回滚

发布前为 `APP_IMAGE_TAG` 指定不可变镜像标签或摘要，严禁使用 `latest`。先执行一次独立、向前兼容的 `migration` 服务；它完成 Alembic upgrade 后会重新运行数据库角色授权，使新增表获得最小权限。确认 API、Worker、Scheduler、PostgreSQL、Redis 均为 healthy 后，才切换 Caddy 流量。

应用镜像只能回滚到与当前数据库 revision 兼容的不可变标签，且绝不对生产数据库执行破坏性 migration downgrade。除下述明确要求停机排空的 0018/0019 contract revision 外，迁移采用 expand/migrate/switch/contract：旧应用必须能读取扩展后的 schema，删除旧列或表只能在所有旧应用退出后的后续发布完成。一旦 0019 完成恢复审计、启动任一通用服务或允许任一业务写入，应用回滚下限就是理解 0019 字段版本并且只写 v2 的 0019-compatible 镜像；无论 M2 Task 是否都已终态，都不得回滚到 pre-0019/M1、v1 reader 或旧 Calendar writer。只有下述尚未重开服务、无业务写入的 sealed maintenance window 才允许从本窗口迁移前整库备份恢复到 revision 0018；它不是 Alembic downgrade。切换到较旧但仍兼容 0019 的 M2 镜像前，仍必须确认所有 M2 Task 已终态；存在 `reconciling` 或 `needs_attention` 时只能部署保留核对能力的前滚修复镜像。

### M2 CalendarEvent 0018 发布

`20260809_0018` 将 CalendarEvent 供应商身份从连接级二元组切换为 `connection_id + calendar_id + provider_event_id` 三元组。该 contract revision 不支持旧、新 Calendar writer 混跑，发布必须严格按以下顺序执行：

1. 关闭 Calendar 周期调度。
2. 排空并停止全部可能执行 `sync_calendar` 的旧 Worker，确认没有旧事件 upsert 事务仍在运行。
3. 应用 Alembic revision `20260809_0018`。
4. 部署引用三元约束的新 API、Worker 和 Scheduler，禁止旧 Worker 回流。
5. 恢复 Worker，再恢复 Calendar 调度。

生产环境不得对该迁移执行破坏性 downgrade。若迁移后已经存在跨日历相同 provider event ID，旧二元约束无法无损恢复，downgrade 必须 fail closed；不得删除、合并或改写事件行来强行回退，应停止发布并制定人工数据保留方案。

### M2 CalendarEvent 字段 AAD 0019 发布

`20260809_0019_calendar_event_field_aad_v2.py` 的 `down_revision` 是 `20260809_0018`，因此必须在 0018 完成后应用。描述与地点分别维护独立的 AAD 版本：v1 仅用于标记历史三元组，v2 使用包含 `calendar_id` 的 `user_id:connection_id:calendar_id:provider_event_id:field`。新 Calendar writer 只能写 v2；reader 遇到不可用 cipher、v1、未知版本、key-version mismatch、明确的解密边界错误或 v2 `InvalidTag` 时，必须返回 `calendar_event_resync_required`，绝不尝试 v1、缺少 `calendar_id` 或未经认证明文等 legacy fallback。

0019 变更窗口还必须绑定一个由 preflight 生成的 content-free rollout state artifact。对每个受影响 connection 只执行一次主动 OAuth refresh（按 connection UUID 去重并串行），并以刷新后持久化的 `token_expires_at` 计算固定截止时间：
`rollout_deadline = min(token_expires_at) - 900 seconds`。900 秒是与现有 `task_timeout_seconds=900` 对齐的固定安全余量，不是可由运维输入的“足够新鲜”估计；所有时间使用 UTC。若不存在受影响 pair，artifact 必须记录可核验的零值并将 deadline 标为不适用；后续 guard 只能在重新证明 affected set 仍为空后走该显式 no-deadline 分支，不能把 `null` 当作可比较时间或跳过集合核验。

preflight 还必须持有 revision-global PostgreSQL session lease。固定锁调用是 `pg_try_advisory_lock(20260809, 19)`，不包含 backup basename，因此相同或不同 basename 的并发窗口都互斥。CLI 使用独立数据库连接，在检查 rollout artifact 是否已存在、调用 OAuth/Calendar provider 或写 credential 前先 try-lock；失败以 `calendar_aad_rollout_locked` 返回，必须保持零 refresh、零持久写入。该连接贯穿完整 preflight 网络阶段且不承载跨网络业务事务；每次 provider call、credential commit 和 artifact 原子发布前都确认同一 session 仍持锁，连接断开或锁丢失立即 fail closed。进程退出或连接关闭后由 PostgreSQL 自动释放锁。

lease 只解决并发，不证明 OAuth refresh 的未知结果。每个 connection 在调用 provider 前，还必须在短事务内向既有 append-only `audit_events` 追加 content-free `calendar.aad_0019.refresh_started` fence；不新增 0018 Schema。事件使用新生成的 canonical lowercase `refresh_attempt_id`，并绑定 `fence_schema_version="calendar_aad_0019_refresh_fence.v1"`、`rollout_digest_v1`、`connection_digest`、发起时冻结的 `fence_generation=F`、`pre_credential_snapshot_digest_v1`、`pre_refresh_credential_snapshot_digest_v1` 和 `result_code`，但不保存 token、raw scope、响应、正文或 raw `calendar_id`。provider 返回后，只有旧 snapshot CAS、新 access/可选 refresh credential、精确 expiry 与复用相同 attempt/rollout/fence generation/两个 pre-digest，并强制增加 `post_credential_snapshot_digest_v1`、`post_refresh_credential_snapshot_digest_v1`、从同一事务实际持久化值读取的 `token_expires_at` 及其 `rollout_deadline_candidate = token_expires_at - 900 seconds` 的 `calendar.aad_0019.refresh_confirmed` 在同一事务提交，才算 refresh 完成。网络断开、调用后 lease 丢失、响应后 CAS/事务失败、malformed token/expiry/scope、`invalid_grant` 或 scope shrink 都留下 started-without-confirmed，或追加绑定同一 attempt 的稳定 needs-attention/`calendar.aad_0019.refresh_terminal` 结果；任何同名或不同 basename 的后续 preflight 都不得再次调用 provider。未消费 fence 只有精确匹配的 `oauth.refresh_credential_replaced` consumption fact 才能自动解除；普通 access-only rotation、generation-only、任意物理摘要变化或显式人工处置都不能代替首次消费。若 confirmed 已提交但 artifact 发布前崩溃，同一 rollout 必须从持久新 credential/expiry 继续 probe 和发布 artifact，provider refresh 调用数保持不变。

摘要协议不得由脚本或实现自行解释。`credential_snapshot_digest_v1` 是 SHA-256 小写十六进制，规范字节以 `b"AIEMPLOYEE/calendar-aad/credential-snapshot/v1\x00"` 开头，顶层固定为 access row、refresh row；`authorization_generation` 不进入摘要，而是 fence/consumption 的独立字段。`refresh_credential_snapshot_digest_v1` 使用 domain `b"AIEMPLOYEE/calendar-aad/refresh-credential-snapshot/v1\x00"`，只编码 refresh row。每行固定为 kind、row ID、user ID、connection ID、ciphertext、nonce、key version、expiry、updated-at。`rollout_digest_v1` 使用 domain `b"AIEMPLOYEE/calendar-aad/rollout/v1\x00"`，固定绑定 preflight schema、source/target revision、安全 basename、精确小写 `sha256:...` image ID 和 `900` 秒余量。三个协议的每个字段都以 `NULL => 0x00`、非 NULL `=> 0x01 || uint32_be(length) || raw_bytes` framing；UUID 为小写 canonical ASCII，整数为规范十进制 ASCII，时间为 UTC RFC3339 固定六位 microseconds 加 `Z`，ciphertext/nonce 使用原始 bytes。空 bytes 不等于 NULL。字段、顺序、domain 或编码变化必须新增版本；不得把 raw scope/calendar ID 放入摘要，或让 started/confirmed/terminal/consumption/retention 使用不同 helper。两个 credential 摘要都只证明数据库物理快照；ciphertext、nonce、key version、`updated_at` 或摘要变化均不能证明 refresh token 明文已替换。完整固定合成向量以 M2 规格 17.3 为准，Task 27 单测的 expected bytes/base64/hash 必须是独立常量。

`oauth.refresh_credential_replaced` 是某个 unresolved `refresh_started` 的 append-only、不可逆 terminal/consumption fact，不是要求提交后状态与后续状态永久相等的 lineage marker。首次成功消费必须锁定并绑定原 fence 的 `refresh_attempt_id`、`fence_generation=F`、`old_refresh_credential_snapshot_digest_v1`、来源与来源代际；供应商必须明确返回 non-empty 新 refresh token，并在受控内存中以 `secrets.compare_digest(old, new)` 证明 plaintext 不同。只有旧 refresh snapshot 与 connection generation 的精确 CAS 成功、写入新 refresh row 并在同一事务追加 consumption event，原 fence 才永久 resolved。之后正常 authorization generation、capability/scope、access token 或 refresh token 变化都不会使旧 fence 复活，也不要求后续 generation/digest 保持 event 提交时的值。若 scope 后来进入 `action_required`，后续授权走普通 existing-connection 代际协议，不再绑定旧 fence。preflight 只验证 matching consumption 的 schema、原 attempt/F/old digest 与来源代际不变量，再独立验证当前 connected connection、能力、scopes 和 credential；若要建立新 attempt，必须从通过校验的当前 generation 与两行 credential snapshot 重新冻结新 `F` 和两个 pre-digest，不得沿用已消费 fence 的旧 pre-state。若当前 refresh snapshot 恰好回到已消费的 old physical digest，则以 credential rollback fail closed。

consumption metadata 使用关闭的 source-tagged union，共同绑定 `proof_schema_version="oauth_refresh_credential_replaced.v1"`、`source`、`connection_digest`、原 `refresh_attempt_id`、`fence_generation=F`、old/new refresh digest、`source_generation`、`pre_generation`、`post_generation` 与稳定 `result_code`。`source="progressive_callback"` 时，仍连接的原 connection 按首次恢复协议从 `source_generation=S` 发起，既有 `set_capabilities_authorizing` 只递增一次到 `target_generation=T=S+1`，`OAuthAttempt` 只保存 target `T`，callback anti-replay 后保持 `pre_generation=post_generation=T`；`F <= S` 且发起时 refresh digest 必须等于 fence old digest。`source="provider_refresh"` 时，普通 Google/Microsoft mail/calendar refresh 在读取代际 `G` 下保持 `source_generation=pre_generation=post_generation=G`，绝不递增 generation。两个来源都必须 compare-digest、snapshot CAS，并把 credential 与 consumption 原子提交；锁住 started 后重新检查未消费状态，确保竞争 writer 最多只有一个成为 terminal consumer。

所有既有 connection credential rotation writer 都必须复用同一共享 snapshot-CAS boundary：Google/Microsoft mail 与 calendar resolver 当前调用的 `rotate_access_token`、渐进 callback 和 0019 主动 preflight 都携带读取时 connection generation，以及 access/refresh 两行的 row ID、用户/连接/kind、`updated_at`、完整 AEAD 和 expiry。短事务统一按 connection → access row → refresh row → matching started audit 的锁序重检；CAS miss 不覆盖任何 credential，也不消费 fence。provider 未返回新 refresh token，或返回与旧 plaintext 相同的值时，只允许 CAS 更新 access row 并逐字节保留 refresh row，不生成 consumption。普通 Worker 在 unresolved fence 存在时若得到确定的不同 refresh token，可以用 `source="provider_refresh"` 消费；网络未知、响应无/同 refresh、事务失败或 stale CAS 都继续保留 unresolved fence。0019 preflight 对自己刚提交的 started 只写 matching `refresh_confirmed`，不把同一 attempt 标记为 consumption。

365 天审计清理对 refresh fence 有强制例外：unresolved started 没有 matching confirmed、可证明安全关闭的 known-terminal 或有效 terminal consumption 时必须跨 cutoff 保留。retention 只能在显式 `user_id` 范围内解析 content-free metadata；matching `oauth.refresh_credential_replaced` 只需作为该 started 的有效 terminal consumption，精确匹配原 attempt/F/old refresh digest 并满足对应 source generation 不变量，不再读取或约束后续 current credential/generation lineage。通用 audit delete 必须排除 started/confirmed/terminal 与 replacement consumption；专用清理只能在同一事务按 attempt 成组删除已证明安全的 started 及其 matching terminal result。started 与 consumption/result 每一行都必须早于 cutoff，等价要求组内最大 `created_at < cutoff`；旧 started 匹配较新的 result 时不得提前删除，也不能先删 result 再留下看似未决的 started。清理与所有 credential CAS 统一采用 connection → access credential → refresh credential → started audit 的锁序，锁后重查 matching result；另一用户与普通过期 audit 必须继续按正常 cutoff 处理。无需为了 retention 保留消费后的 credential lineage。

发布期间保持真实写入开关关闭，并严格按以下顺序执行：

1. 保持全局与供应商真实写开关关闭，关闭 Calendar 周期调度，并进入停止新入口流量的维护窗口。
2. 优雅排空并停止全部 pre-0019 CalendarEvent reader/writer：Caddy、API、Worker、Scheduler；确认没有旧 `sync_calendar`、事件读取或 upsert 仍在运行。PostgreSQL 与 Redis 全程保持运行。
3. 运行无参数 `just calendar-aad-preflight-0019`。该 0018-compatible one-off 先取得上述 revision-global lease，再检查 revision、artifact 不存在/不冲突并从历史完整 AEAD 三元组推导精确 affected pairs。它按 connection UUID 去重并串行处理每个 connection：冻结 access/refresh 两行的 `id`、用户/连接/kind、完整 AEAD、`token_expires_at` 和 `updated_at`，以唯一 helper 计算当前 full 与 refresh credential digest，并重检授权代际与 refresh fence。若同一 rollout/attempt 已有与当前持久两个 post-digest、持久 expiry 及其 deadline candidate 匹配的 `refresh_confirmed`，则直接恢复后续 probe，禁止再次 refresh；若存在未确认/needs-attention fence，则先查找 matching `oauth.refresh_credential_replaced` consumption。consumption 必须精确匹配原 `refresh_attempt_id`、原 fence 自身的 generation、old refresh digest，并满足 `progressive_callback` 或 `provider_refresh` 的代际不变量；它一经有效即永久关闭旧 fence，不要求 event 提交后的 generation/refresh digest 在后续保持不变。若 current refresh digest 回到该 old digest，则以 credential rollback fail closed；否则重新独立验证当前 connection 仍 connected、`calendar.read` enabled、scopes 完整且 access/refresh credential 存在、归属正确并可解密，才能建立新 attempt。新 attempt 必须使用本轮当前 generation 和两行 snapshot 重新冻结的 `fence_generation=F` 与两个 pre-digest，不得复用已消费 fence 的旧 pre-state。没有 matching consumption 时，同名或不同 basename 都以零 provider call 失败。它先完成 access/refresh 存在性、归属和 AEAD 可解密性等本地校验；通过后再次核验 lease，在紧邻 provider 调用前提交绑定 attempt UUID、`rollout_digest_v1`、`fence_generation=F` 与两个 pre-digest 的 `refresh_started`，再使用已解密的 refresh credential 主动调用现有 Google/Microsoft OAuth refresh。access-only、缺失/不可解密 refresh 必须在 started/provider 前 fail closed；refresh 被拒绝或返回 malformed access/expiry/scope 则留下 durable fence，并在任何 `initial_pages` 前 fail closed。返回的 access token 必须非空且可用，`granted_scopes` 必须覆盖该 connection 保存的 canonical scopes 并包含 `calendar.read` 所需 provider scope。验证后只能通过所有 writer 共享的 snapshot-CAS repository，在短事务中精确匹配连接 generation 与两行旧 snapshot，并重检 owning connection、fence generation 与 `calendar.read` 能力，再把 credential rotation、实际持久化 expiry 和绑定同一 attempt/fence generation/两个 pre-digest、两个新 post-digest 及 `rollout_deadline_candidate = token_expires_at - 900 seconds` 的 `refresh_confirmed` 原子提交；响应没有新 refresh token，或新值与旧 plaintext 相同时，仍校验旧 refresh row snapshot 并逐字节保留。现有无条件 credential upsert 禁止用于该路径。任一未知结果或 CAS miss 不覆盖并发新 token、不执行后续 probe、不发布 artifact，revision 保持 0018，也不得对同 snapshot 重放 provider。每个 confirmed rotation 提交前先证明该 token 的 expiry 晚于当前 UTC 加 900 秒；全部 connection 已 confirmed 后以 confirmed 中持久化 deadline candidate 的最小值计算全局 `rollout_deadline`，再按确定性 pair 顺序调用与迁移后恢复相同的 `initial_pages(scope_key)`，要求最终 cursor。不等待资源 401，主动刷新后的资源 401、403、权限不足、malformed page 或无最终 cursor 都直接失败。它不得调用 `directory_pages()`、连接级/full-account sync、其他 pair 或 Calendar 写适配器，也不得写 CalendarEvent、cursor 或 marker；仅允许上述 fence 与 credential CAS/confirmed 事实。非空 set 在每个 pair probe 前及 rollout artifact 原子提交前检查 `now < rollout_deadline`，zero branch 则在 artifact 提交前重新证明 affected set 仍为空。任何失败都返回非零并停止变更窗口。
4. 只有 preflight 全部通过且统一 rollout guard 通过后，才创建带固定 basename 的加密备份并运行 `pre-migration` 只读审计：非空 affected set 要求当前 UTC 早于 deadline；零 affected pair 则要求 artifact 为规范 zero/no-deadline 形状并重新证明数据库 affected set 仍为空。backup、audit 和其 artifact 原子发布前都必须重复同一 guard；审计必须确认 revision 仍为 0018、没有部分 AEAD 三元组，并重复证明每个 affected pair 的本地可恢复性事实和与 preflight 完全一致的 locally hashed pair set。
5. 运行受统一 rollout guard 保护的独立 migration one-off 应用 0019。迁移自身必须在任何 DDL/DML 前、事务最终提交前重复本地 preflight/refresh-state、镜像绑定与非空 deadline/零集合分支检查；任一精确 pair 缺游标、连接非 connected、`calendar.read` 非 enabled、缺失 access 或 refresh credential、refresh scope 不足或缺失精确 `ProviderCalendar` 时都 fail closed，Alembic revision 保持 0018，全部数据不变，且不得创建猜测 marker。
6. 运行 `post-migration` 只读审计，确认 Schema 只新增版本列与原子约束；历史完整三元组标记为 v1、全空组保持 NULL；事件身份/非 AAD 业务字段及 ciphertext/nonce/key-version 摘要不变；`directory` 完全不变；只有精确 affected pair 的 cursor/freshness/error marker 改变。两个 connection 即使复用同一 `calendar_id`，也禁止只按 `calendar_id` 扩大更新。随后使 v2-only 不可变镜像可供 one-off 恢复命令使用，但继续保持 Caddy、API、普通 Worker 与 Scheduler 停止，禁止任何旧 reader/writer 回流。
7. 运行无参数 `just calendar-aad-resync-0019`。该入口只扫描 `calendar_event_resync_required` marker，在精确 cursor 锁下始终复用 `created`、`queued`、`running`、`retry_scheduled` 活动尝试；只有上一尝试已经 `failed` 或 `cancelled` 且 marker 仍存在时，下一次显式 CLI 调用才分配 `max_ordinal + 1`。每次 CLI 调用每个 pair 最多返回/创建一个 ordinal，并在开始、每个任务最终本地提交前检查同一 rollout guard；非空集合 deadline 已到、zero state 漂移、marker 仍存在或任务失败都必须停止，不得在同一次调用中追加 ordinal+1。随后只在进程内执行 planner 本次返回的 `calendar.aad_0019.resync` 任务；不得启动 Taskiq、扫描普通队列、运行 `directory` owner，或扩大到整个连接或账户。
8. 运行 `post-resync` 只读审计，并在开始及 artifact 原子提交前检查同一 rollout guard，确认每个 affected pair 的 marker 已清除、cursor/freshness 已恢复、此次新写入的非空描述/地点均为 v2，并只报告未读取内容的剩余历史 v1 数量。任一 marker 未恢复或 rollout guard 失败都必须停止发布，Caddy、API、普通 Worker 与 Scheduler 继续保持停止。若非空 deadline 尚未到且失败条件可在精确 scope 内修复，只能由运维人员再次显式运行 resync 复用活动 ordinal 或在上一尝试终态后分配下一 ordinal；一旦 deadline 已到、已经不能证明 artifact 会及时提交，才转入下述 sealed-window restore。zero-state 漂移或镜像/basename 不匹配先 fail closed 并调查，只有仍能独立证明维护窗口内无业务写入时才可能满足整库恢复前提。
9. 只有 `post-resync` 审计通过后，才启动 v2-only API、普通 Worker、Scheduler 与 Caddy。
10. 运行 `just health`；上述条件全部满足后才退出维护窗口并继续发布。

以下命令从 `/srv/ai-employee` 运行，不包含账号、DSN、正文、原始 `calendar_id` 或凭据。migration one-off 继续从 Compose Secret 读取数据库凭据。`BACKUP_ARTIFACT_BASENAME`、content-free preflight state、revision-global lease、dual credential digest、source-tagged refresh replacement consumption、共享 credential snapshot CAS、`just calendar-aad-preflight-0019`、`just calendar-aad-migrate-0019`、`just calendar-aad-audit phase artifact`、`just calendar-aad-resync-0019`、专用 owner-role restore service、`just calendar-aad-verify-restored-0018` 及其合成数据库测试属于 Task 27 要落地的运维契约；当前 0019 recipe/CLI 和 owner restore 边界尚未实现。现有 `just restore` 复用 app-role `backup` 服务，不能执行受控整库 DDL restore，也不得被当作 sealed-window 恢复证据。未完成 Task 27 前不得执行生产 0019 或整库恢复，也不得用临时命令、额外 DDL 授权或直接 SQL 替代。示例镜像标签必须替换为预先构建并固定的实际不可变标签，不得使用 `latest`。

```bash
cd /srv/ai-employee
export PRE_0019_APP_IMAGE_TAG=2026.08.09-0018-compatible
export APP_IMAGE_TAG=2026.08.09-0019-v2
export APP_ENV=production
export BACKUP_DIR=/var/backups/ai-employee
export BACKUP_ARTIFACT_BASENAME=ai_employee-0019-20260809

# 先在外部维护流程关闭 Calendar scheduling 和新入口，再优雅停止所有旧 reader/writer。
docker compose stop caddy api worker scheduler
docker compose ps
# 预期仅 postgres 与 redis 保持 running；若仍有旧 reader/writer，立即停止发布。

# Task 27 落地前必须停在这里；禁止用临时 provider probe 或 SQL 替代。
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

`calendar-aad-preflight-0019`、`calendar-aad-migrate-0019` 与 `calendar-aad-resync-0019` 都不接受 user、connection 或 calendar 参数，避免运维输入把范围改写为任意对象；三者都要求预先设置安全 basename `BACKUP_ARTIFACT_BASENAME`，并使用 `${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}.calendar-aad-preflight.json` 作为同一 rollout state。recipe 在启动 one-off 容器前检查 Compose 状态；只要 `caddy`、`api`、`worker` 或 `scheduler` 任一仍在运行就 fail closed。recipe 还必须从 Compose 选中的 backend image 引用解析本机实际镜像内容 ID，并以内部环境变量传给固定程序；该值不得由运维人员另行输入。preflight artifact 绑定该内容 ID，后续 backup、audit、migration、resync 与 restored-0018 verifier 都必须重新解析并拒绝镜像不匹配，避免可变 tag 或切错镜像绕过同一窗口。宿主 recipe 只能验证 basename/path 形状和挂载边界，不能在 lease 之外检查 preflight artifact 是否存在；碰撞/存在性检查必须由 CLI 在成功取得 revision-global advisory lock 后完成。preflight 还要求数据库精确位于 0018，复用选定不可变镜像、`worker` 服务的 Secret/数据库配置和现有 Calendar credential/adapter resolver，以 `--no-deps --entrypoint /bin/sh` 运行固定程序 `export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_preflight_0019`。它只输出 affected/connection count、locally hashed pair/connection digest、earliest UTC deadline 和稳定结果码；不得输出 token、scope 原文、正文、描述/地点或 raw `calendar_id`。

preflight 的供应商表面严格只读，但 refresh 本身是可能轮换凭据的外部写效应，因此必须使用 durable fence。`refresh_started`、`refresh_confirmed` 和 `calendar.aad_0019.refresh_terminal` 使用既有 `audit_events`、`task_id=NULL`、`actor_type=system`；started/terminal metadata 只含 `fence_schema_version`、canonical lowercase `refresh_attempt_id`、`rollout_digest_v1`、`connection_digest`、`fence_generation`、对应 pre `credential_snapshot_digest_v1`/`refresh_credential_snapshot_digest_v1` 和稳定 `result_code`；confirmed 必须额外包含两个 post digest、实际持久化 `token_expires_at` 及由它减去 900 秒得到的 `rollout_deadline_candidate`。本地 credential 存在性、归属和可解密性校验必须在 started 前完成；紧邻 provider 的 started 事务与 provider 后的 credential CAS/confirmed 事务都很短，网络阶段不持有业务事务。已 confirmed 且当前 credential 与事件的两个 post-digest 匹配时，同一 rollout 从持久 expiry 继续，不再次 refresh；started-without-confirmed 或 needs-attention 对所有 basename 都是硬阻断。matching refresh replacement consumption 只验证原 attempt/F/old digest 与来源代际不变量，并永久关闭原 fence；preflight 随后独立验证 current connection/capability/scopes/credential，且 current refresh digest 回到已消费 old digest 时必须以 rollback fail closed。普通 access-only rotation、generation 或物理摘要变化和显式人工处置都不得成为 replay waiver；普通 Worker 只有确定提交不同 refresh plaintext 时，才可通过 `source="provider_refresh"` 成为 terminal consumer。

snapshot 对 access/refresh 每行都绑定 `id`、用户/连接/kind、ciphertext、nonce、key version、`token_expires_at` 与 `updated_at`；共享短事务还要重检 connection/capability/generation，并按 connection → access row → refresh row → matching started audit 锁序执行。响应省略新 refresh token 或返回相同 plaintext 时只更新 access row，精确匹配并逐字节保留旧 refresh 行；任一 CAS miss 都不会覆盖新 token、消费 fence 或发布 artifact。网络断开、锁连接丢失、响应后 commit 失败、malformed response、`invalid_grant` 与 scope shrink 都不得对同 snapshot 自动重试。它不等待资源 401，也不在 probe 阶段再刷新；主动刷新后的资源 401、403 或 scope 缺失均是失败。除 refresh fence 与成功的 credential CAS/confirmed 事实外，不得写连接、能力或其他业务事实，也不得写 CalendarEvent、SyncCursor 或 marker。revision-global lease 必须在每个 provider call、credential CAS 和 artifact publish 前仍可由同一 session 证明持有。每个 confirmed access token 的持久 `token_expires_at` 参与固定 `rollout_deadline = min(token_expires_at) - 900 seconds`；preflight state artifact 只保存 schema/revision、`rollout_digest_v1`、安全 basename、宿主机解析的 immutable image content ID、earliest expiry/deadline、固定 margin、affected/connection 计数、lowercase pair digest、hashed connection IDs 和稳定结果码，不保存 token、scope 原文、正文或 raw `calendar_id`。preflight、backup、migration、resync、audit 每个 CLI/recipe 都必须在启动和关键本地提交前读取并校验该 artifact。非空 affected set 的任何 `now >= rollout_deadline` 都 fail closed；zero/no-deadline state 只有在规范零值、镜像/basename 绑定有效且重新查询仍无 affected pair 时才通过，否则同样 fail closed。

任一 pair 或 connection 失败时不得继续备份、审计或 migration；数据库仍为 0018。若存在未确认/needs-attention refresh fence，后续同名或不同 basename invocation 都不得再次调用 preflight provider。保持写开关关闭；原 connection 仍为 `connected` 时，可以通过既有渐进重授权/重新同意 callback，或由正常 Google/Microsoft mail/calendar Worker refresh，取得并以共享 snapshot-CAS 原子提交真正不同的 non-empty refresh token。前者记录 `source="progressive_callback"`、`source_generation=S`、`pre_generation=post_generation=T=S+1`，后者记录 `source="provider_refresh"`、`source_generation=pre_generation=post_generation=G`；两者都只能让一个 matching writer 消费原 fence。不要为了消费 fence 启动临时 OAuth-only 服务、强制资源 401 或额外 provider 调用。**发现未知 fence 时不要先 disconnect**：断开会先删除旧 credential，之后无法比较 old/new plaintext；断开后重连、新建 connection 或映射新 token 都不能生成或替代 consumption。callback/Worker 未返回 refresh token、返回相同 plaintext、旧 plaintext 不可得、connection 已断开，或仅改变 access/generation/物理密文时仍保持阻断。若断开后 affected scope 仍存在，只能走既有明确用户授权的数据处置/删除历史敏感字段流程并重新评估 affected set；若该流程也不适用，必须停止并请求新的已批准设计。禁止 fabricated consumption、新连接映射、marker skip、retry waiver、直接 SQL、伪造 credential、access-only 迁移或先迁移再清 marker。显式人工调查结果不能授权同 snapshot 重放；除非另有独立持久的不可调用终态，否则也不能让 retention 删除唯一 started 防线。已 confirmed 但尚未发布 artifact 的崩溃恢复必须复用同一 rollout 与持久新 token。其他本地事实失败可在恢复精确目录事实后重新开启完整变更窗口。

resync recipe 要求数据库精确位于 0019，并以相同 wrapper 边界运行固定程序 `export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_0019`；该 wrapper 不打印 Secret，也不会启动 Taskiq Worker、Scheduler 或普通 Redis queue consumer。

resync CLI 只从 revision 0019 数据库中的精确 marker 推导用户与 pair。pair digest 固定为 `SHA-256(20260809_0019 + NUL + connection_id + NUL + calendar_id)` 的小写十六进制；planner 对精确 cursor 行执行 `FOR UPDATE` 或等价 CAS。每个 pair 的任务种类固定为 `calendar.aad_0019.resync`，首次 recovery ordinal 为 1，稳定幂等键为 `calendar-aad-0019:<pair_digest>:attempt:<ordinal>`，输入精确包含 `connection_id`、`scope_key`、`recovery_revision=20260809_0019`、`pair_digest` 和 `recovery_attempt_ordinal`。

planner 将 `created`、`queued`、`running`、`retry_scheduled` 视为活动尝试并复用；多个活动尝试属于不变量错误。若最新尝试为 `failed` 或 `cancelled` 且 marker 仍存在，只有下一次显式 CLI 调用才分配 `max_ordinal + 1`；同一次调用执行失败后不得再次规划 ordinal+1。`succeeded` 与 marker 并存必须 fail closed，`waiting_approval`、`reconciling`、`needs_attention` 对该只读恢复种类均不合法。旧终态 TaskRun、时间戳、`attempt_count` 和结果不得复活或改写；recovery ordinal 与单个 TaskRun 内由 `DurableTaskRunner` 管理的有界 `attempt_count` 完全分离。并发 planner 对同一 ordinal 只能产生一个 TaskRun 和一条初始 Outbox。

每个 ordinal 的 TaskRun、内容安全审计和初始 Outbox 在一个事务提交，随后仅通过精确 task ID 的内联 Outbox/`DurableTaskRunner` 路径执行，不发布或消费普通队列。Worker 在供应商访问前重新计算 pair digest，核对 exact input keys、ordinal、幂等键、用户归属、连接读取能力、必需 access/refresh AEAD credential、现存非 `directory` cursor、marker 与同一 ProviderCalendar，并只调用该 calendar 的事件页；目录 reader、其他 connection/calendar、ApprovalRequest、ToolExecution 和供应商写适配器调用次数都必须为零。

成功提交以 marker 仍匹配为 CAS 前提，只恢复同一 pair 的 cursor/freshness、写入 v2 字段并清除其错误；marker 已清除的重放是无供应商调用的 no-op。任何权限、供应商读取、租约或 CAS 失败都保留 marker、返回非零并阻止后续服务启动；修复条件后必须由运维人员再次显式运行同一无参数命令创建下一 ordinal，禁止改用直接 SQL、临时全量同步或 connection-level directory sync。零 marker 运行是可审计的成功 no-op。

`calendar-aad-audit` 只接受 `pre-migration`、`post-migration`、`post-resync` 三个 phase，并分别写入 `${artifact}.calendar-aad-${phase}.json`。每个 phase 在读取数据库前和以原子 rename 发布 artifact 前校验同一 rollout state/guard。`pre-migration` 还必须重复验证 connected connection、enabled `calendar.read`、必需 access/refresh credential 与精确 `ProviderCalendar`，并记录与 preflight 相同的 locally hashed pair set；运维证据必须比较两者完全一致。每阶段 artifact 权限固定为 `0600`，只保存 revision、行/密文摘要、locally hashed scope IDs、affected/connection count 和 earliest deadline；不得保存或输出 DSN、描述/地点正文、供应商响应、token、scope 原文或 raw `calendar_id`。三个 artifact、preflight 的 content-free hashed results 和每个恢复 ordinal 的结果必须与备份 basename、迁移标识和不可变镜像标签一起进入 Task 30 发布证据。

0019 进入正常运行后只能通过新应用镜像前滚修复，其 Alembic downgrade 必须 fail closed；任何应用回滚目标也必须是只写 v2 的 0019-compatible 镜像。唯一例外是：若仍处于本次已封锁的维护窗口、通用服务从未在 0019 后启动且没有业务写入，且非空 rollout deadline 已到或已经不能证明能在截止时间前清完 marker，可使用本窗口指定的迁移前加密整库备份恢复到 revision 0018；这不是 Alembic downgrade，也不是直接 SQL。恢复前后必须核对 backup checksum、原始与恢复后 `pre-migration` audit artifact 的无敏感摘要完全一致、Alembic revision=0018，再切回原记录的 0018-compatible immutable image 并运行健康检查；未完成这些核验不得启动任何通用服务。除此 sealed-window restore 外，不得删除事件或游标、移除 AAD 版本列或约束、恢复 v1 reader，亦不得让旧 writer 回流；禁止 OAuth-only 临时服务、跳过 marker 或迁移后直接清 marker。若有 scope 的有界重同步失败，保持真实写入开关关闭，按该精确 scope 排查连接能力、同步错误和供应商读取状态；deadline 前仍可通过下一次显式 CLI 按既定 ordinal 规则重试，但不得通过扩大同步范围、删除事实或启用 legacy fallback 绕过故障。

### 0019 deadline 失败后的 sealed-window 整库恢复

若 0019 已提交，且非空 rollout deadline 已到仍有 marker、任一 deadline guard 已失败，或运维人员已经不能证明 `post-resync` artifact 会在精确 deadline 前提交，则立即停止新的恢复 ordinal 和 provider probe。Caddy、API、普通 Worker、Scheduler 以及所有真实写入开关继续保持停止；恢复动作只要求 healthy PostgreSQL，Redis 即使继续运行也不是 restore service 的依赖。不得执行 `alembic downgrade`、直接 SQL、手工清 marker、跳过审计、临时 full-account sync，亦不得启动仅用于继续 refresh/probe 的 OAuth-only API 或 Worker。

本路径只允许使用当前窗口在主动 refresh 之后、0019 之前创建并记录 basename/checksum 的加密整库备份。维护窗口从停止新入口开始，到 `post-resync`、通用服务启动和健康检查全部通过才结束；期间没有业务写入，且 preflight credential rotation 已包含在该备份中。因此整库恢复只撤销失败的 0019 migration/resync 本地事实，不会丢失窗口后的用户业务写入，也不会退回到 refresh 前已失效的 credential。

Task 27 必须把 `just restore` 升级为 operations profile 的专用 `restore` one-off；在此之前，当前复用 app-role `backup` service 的 recipe 明确禁止用于本路径。任何 owner Secret 读取、owner DB connection 或 `pg_restore` 之前，宿主 recipe 先完成纯文件/本地镜像 guard：验证安全 basename、加密 dump 与相邻 checksum、preflight 和原 `pre-migration` artifact 的 schema/basename/digest、两者绑定的 immutable image content ID，并重新解析本机 Compose 选定镜像。缺失/错误镜像、移动 tag、checksum 或 artifact/image mismatch 都必须在 owner connection 与 `pg_restore` 调用数为零时失败。

guard 成功后，recipe 才把匹配 artifact 的精确 `sha256:...` content ID 作为内部 `CALENDAR_AAD_RESTORE_IMAGE_ID` 注入 Compose。新 `restore` service 的 `image:` 必须直接引用该 ID，不得引用 tag，不得包含 `build:`，并以 `docker compose --profile operations run --rm --pull never ... restore` 启动，禁止 pull/build。service 不得继承会等待/自动执行 migration 的 `backend-common`，`depends_on` 只能包含 healthy PostgreSQL；固定 `PGUSER=ai_employee_owner`，owner 密码只能读取 `/run/secrets/postgres_bootstrap_password`。容器还只读挂载 backup passphrase Secret、受控 backup volume、preflight/`pre-migration` artifacts，以及恢复成功后重授最小权限所需的 app/retention password Secrets。普通 API、Worker、Scheduler 与 backup service 均不得获得 owner/DDL 权限。

容器入口不得预先设置 `PGPASSWORD`。`scripts/restore-postgres.sh` 必须先在容器内再次验证 artifact、`CALENDAR_AAD_RESTORE_IMAGE_ID` 的 `sha256:` 形状及其与 preflight image binding 完全一致；通过后才读取 owner Secret、建立连接和解密，随后固定执行 `pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --single-transaction`。任一 restore 错误使整个事务回滚且不得留下部分 Schema/数据；只有 `pg_restore` 成功后，one-off 才以 owner session 运行现有 `scripts/init-db-roles.sh` 重建/重授 `ai_employee_app` 与 `ai_employee_retention` 最小权限。grant 失败仍视为恢复失败并保持通用服务停止。

最后必须由使用 `app_database_password` 的 `calendar-aad-verify-restored-0018` 做 revision/artifact digest 核验；它不挂载 owner Secret。recipe 固定设置 `PGOPTIONS=-c default_transaction_read_only=on`，CLI 的首个数据库动作是显式 `BEGIN READ ONLY`，且不得关闭只读模式。测试在同一 verifier connection 尝试 DML，PostgreSQL 必须以 read-only transaction、SQLSTATE `25006` 拒绝；仅靠“代码没有 UPDATE”不构成验收证据。

以下命令沿用上文已导出的 `PRE_0019_APP_IMAGE_TAG`、`APP_IMAGE_TAG`、`BACKUP_DIR` 与 `BACKUP_ARTIFACT_BASENAME`。Task 27 落地后的 `just restore` 保留 `[confirm]` 保护，并再次要求输入完整、精确相同的 restore path；生产环境还必须显式设置 `ALLOW_PRODUCTION_RESTORE=yes`：

```bash
cd /srv/ai-employee
export RESTORE_TARGET="${BACKUP_DIR}/${BACKUP_ARTIFACT_BASENAME}.dump.enc"

APP_ENV=production \
ALLOW_PRODUCTION_RESTORE=yes \
BACKUP_DIR="${BACKUP_DIR}" \
just restore "${RESTORE_TARGET}"

# 在 recipe 的精确路径确认提示中再次输入 ${RESTORE_TARGET}；任一字符不符都会拒绝。
# recipe 先完成 artifact/image guard，再以精确 sha256 image、--pull never 启动 PostgreSQL-only restore。
# 继续保持通用服务停止；verifier 以 app role + PGOPTIONS/BEGIN READ ONLY 核验。
just calendar-aad-verify-restored-0018
docker compose ps

# 上述 audit 已 fail-closed 证明 revision 精确为 0018 且摘要与迁移前一致后，才切回原镜像。
export APP_IMAGE_TAG="${PRE_0019_APP_IMAGE_TAG}"
docker compose up -d api worker scheduler caddy
just health
```

`calendar-aad-verify-restored-0018` 只在四个通用服务都停止时运行，固定通过 `ai_employee_app` 的 Secret 连接并同时启用 server-default 与 transaction-level read-only，要求数据库 revision 精确为 0018，验证本窗口 backup/checksum 与 preflight/pre-migration artifact 的 basename/image 绑定，并以同一确定性查询比较恢复后的 affected pair/connection count、locally hashed pair set、事件/游标/密文摘要和本地可恢复性事实；它不得因 deadline 已过而跳过任何数据比较，也不得关闭只读模式、切换到 owner、挂载 owner Secret 或触发 migration。任一差异或 PostgreSQL 未拒绝同连接 DML 都必须保持服务停止并人工调查。只有原 0018-compatible immutable image 启动且 `just health` 通过后，才可退出本次失败的维护窗口；新的 0019 尝试必须从新的完整 preflight/refresh/deadline/backup 窗口重新开始。

## 备份与恢复演练

备份命令需要显式 `BACKUP_DIR`、`BACKUP_PASSPHRASE_FILE` 与 PostgreSQL `PG*`/`PGPASSFILE` 连接环境：

```bash
cd /srv/ai-employee
APP_ENV=production BACKUP_DIR=/var/backups/ai-employee \
BACKUP_PASSPHRASE_FILE=/run/secrets/backup_passphrase \
BACKUP_RCLONE_REMOTE='encrypted-remote:ai-employee' just backup
```

脚本生成 PostgreSQL custom dump、AES-256-CBC PBKDF2 加密文件及 SHA-256 校验和，保留七份日备和四份周备，并复制 artifact 与校验和到异地 remote。生产缺失 remote 会拒绝执行。

每月在隔离、非生产数据库执行一次恢复演练。演练 fixture 必须包含与备份匹配的合成 preflight/`pre-migration` artifacts 和本地不可变镜像，先校验来源、checksum 与 image binding，再通过 Task 27 的专用 owner-role Compose 边界显式授权；不要直接为 app role 增加 DDL，也不要在宿主机拼接 owner DSN：

```bash
cd /srv/ai-employee
APP_ENV=test BACKUP_DIR=/var/backups/ai-employee \
just restore /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc
```

恢复会在任何 owner connection 前验证 artifact/image，使用精确 `sha256:` image 与 `--pull never`，再以 owner 执行单事务且遇错退出的固定 `pg_restore` 参数，并通过 trap 清除明文；成功后重授 app/retention 权限，再用 app role、server-default read-only 和显式 `BEGIN READ ONLY` 完成 revision/摘要验证。演练必须同时记录：moved tag/缺失 image/artifact mismatch 时 owner/`pg_restore` 调用为零、app-role restore 被拒绝、owner restore 原子成功、注入中途错误无部分状态、grant 恢复、同 verifier connection 的 DML 被 PostgreSQL 以 SQLSTATE `25006` 拒绝，以及抽样任务和简报完整性；任何差异都保持服务停止并记录处置结果。

## 主机与网络

VPS 的 PostgreSQL、Redis、Caddy、Prometheus 与 Grafana 持久卷必须使用主机或块设备加密。只有 Caddy 发布 80/443；数据库、Redis、API 指标和 Worker/Scheduler 指标均留在 Compose 内部网络。Caddy 负责 TLS 和压缩，公共 `/metrics` 固定返回 404。Grafana 仅在 `observability` profile 下启动，保留 Grafana 登录并使用 `/ops/grafana/` 子路径。

## Secret 与访问撤销

所有生产 Secret 由部署平台创建为 Docker Secret，再以 `/run/secrets/*` 文件挂载。不得把值写进 Compose、环境文件、日志、镜像层、备份名或 CI 输出。轮换数据库、应用主密钥、Grafana 管理员密码和备份口令时，先创建新版本、滚动重启依赖服务、验证健康和解密演练，最后撤销旧版本。

Google 连接撤销时，先在 Google 帐号安全页面撤销 AI Employee 的 OAuth 授权，再在应用连接页断开并执行对应的数据删除流程。模型 API Key 轮换时，更新 Secret、滚动 API/Worker/Scheduler，并确认结构化模型调用仍使用预期供应商与数据最小披露策略。
