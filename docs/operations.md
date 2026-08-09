# AI Employee 运行手册

本手册覆盖可信任务中心与每日办公简报的 M1 基线，以及当前已落地的 M2 邮件与日历助手发布边界。生产环境不连接测试账号，不以 Redis 作为业务事实来源；M2 真实写入仍必须遵守三层开关、连接能力和精确人工审批。

## 发布与回滚

发布前为 `APP_IMAGE_TAG` 指定不可变镜像标签或摘要，严禁使用 `latest`。先执行一次独立、向前兼容的 `migration` 服务；它完成 Alembic upgrade 后会重新运行数据库角色授权，使新增表获得最小权限。确认 API、Worker、Scheduler、PostgreSQL、Redis 均为 healthy 后，才切换 Caddy 流量。

应用镜像只能回滚到与当前数据库 revision 兼容的不可变标签，且绝不对生产数据库执行破坏性 migration downgrade。除下述明确要求停机排空的 0018/0019 contract revision 外，迁移采用 expand/migrate/switch/contract：旧应用必须能读取扩展后的 schema，删除旧列或表只能在所有旧应用退出后的后续发布完成。一旦数据库应用 `20260809_0019`，应用回滚下限就是理解 0019 字段版本并且只写 v2 的 0019-compatible 镜像；无论 M2 Task 是否都已终态，都不得回滚到 pre-0019/M1、v1 reader 或旧 Calendar writer。切换到较旧但仍兼容 0019 的 M2 镜像前，仍必须确认所有 M2 Task 已终态；存在 `reconciling` 或 `needs_attention` 时只能部署保留核对能力的前滚修复镜像。

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

发布期间保持真实写入开关关闭，并严格按以下顺序执行：

1. 保持全局与供应商真实写开关关闭，关闭 Calendar 周期调度，并进入停止新入口流量的维护窗口。
2. 优雅排空并停止全部 pre-0019 CalendarEvent reader/writer：Caddy、API、Worker、Scheduler；确认没有旧 `sync_calendar`、事件读取或 upsert 仍在运行。PostgreSQL 与 Redis 全程保持运行。
3. 创建带固定 basename 的加密备份，再运行 `pre-migration` 只读审计。该审计必须确认 revision 为 0018、没有部分 AEAD 三元组，并且每个受影响的精确 `(connection_id, calendar_id)` pair 都存在非 `directory` Calendar 事件游标。
4. 运行独立 migration service 应用 0019。迁移自身必须在任何 DDL/DML 前重复相同 preflight；任一精确 pair 缺游标时 fail closed，Alembic revision 保持 0018，数据不变，且不得创建猜测 marker。
5. 运行 `post-migration` 只读审计，确认 Schema 只新增版本列与原子约束；历史完整三元组标记为 v1、全空组保持 NULL；事件身份/非 AAD 业务字段及 ciphertext/nonce/key-version 摘要不变；`directory` 完全不变；只有精确 affected pair 的 cursor/freshness/error marker 改变。两个 connection 即使复用同一 `calendar_id`，也禁止只按 `calendar_id` 扩大更新。
6. 部署 v2-only 的不可变 API、Worker、Scheduler 与 Caddy 镜像；禁止任何旧 reader/writer 在 0019 后回流。
7. 只通过 Task 27 定稿并测试的 v2-only Worker/scope 入口，对被标记的精确 pair 执行有界重同步，不扩大到整个连接或账户。
8. 运行 `post-resync` 只读审计，确认每个 affected pair 的 marker 已清除、cursor/freshness 已恢复、此次新写入的非空描述/地点均为 v2，并只报告未读取内容的剩余历史 v1 数量。
9. 运行健康检查；上述条件全部满足后才退出维护窗口并继续发布。

以下命令从 `/srv/ai-employee` 运行，不包含账号、DSN、正文、原始 `calendar_id` 或凭据。migration service 继续从 Compose Secret 读取数据库凭据。`BACKUP_ARTIFACT_BASENAME`、`just calendar-aad-audit phase artifact` 和其三阶段脚本属于 Task 27 要落地并通过合成数据库测试的运维契约；当前 recipe 尚未实现，未完成 Task 27 前不得执行生产 0019。示例 `2026.08.09-0019-v2` 必须替换为预先构建并固定的实际不可变镜像标签，不得使用 `latest`。

```bash
cd /srv/ai-employee
export APP_IMAGE_TAG=2026.08.09-0019-v2

# 先在外部维护流程关闭 Calendar scheduling 和新入口，再优雅停止所有旧 reader/writer。
docker compose stop caddy api worker scheduler
docker compose ps
# 预期仅 postgres 与 redis 保持 running；若仍有旧 reader/writer，立即停止发布。

APP_ENV=production \
BACKUP_DIR=/var/backups/ai-employee \
BACKUP_ARTIFACT_BASENAME=ai_employee-0019-20260809 \
just backup
just calendar-aad-audit pre-migration /backups/ai_employee-0019-20260809

APP_ENV=production docker compose run --rm migration
just calendar-aad-audit post-migration /backups/ai_employee-0019-20260809

docker compose up -d api worker scheduler caddy

# 此处仅运行 Task 27 写回本文并经测试的 v2-only 精确 pair 重同步入口；当前仓库没有现成 recipe。
# 在该入口落地前必须停在这里，禁止用全量同步、直接 SQL 或临时命令替代。

just calendar-aad-audit post-resync /backups/ai_employee-0019-20260809
just health
```

`calendar-aad-audit` 只接受 `pre-migration`、`post-migration`、`post-resync` 三个 phase，并分别写入 `${artifact}.calendar-aad-${phase}.json`。每阶段 artifact 权限固定为 `0600`，只保存 revision、行/密文摘要、locally hashed scope IDs 和 affected count；不得保存或输出 DSN、描述/地点正文、供应商响应或 raw `calendar_id`。三个 artifact 必须与备份 basename、迁移标识和不可变镜像标签一起进入 Task 30 发布证据。

0019 只能通过新应用镜像前滚修复，其 downgrade 必须 fail closed；任何应用回滚目标也必须是只写 v2 的 0019-compatible 镜像。不得删除事件或游标、移除 AAD 版本列或约束、恢复 v1 reader，亦不得让旧 writer 回流；即使所有 Task 已终态，这一下限也不得放宽。若有 scope 的有界重同步失败，保持真实写入开关关闭，按该精确 scope 排查连接能力、同步错误和供应商读取状态；不得通过扩大同步范围、删除事实或启用 legacy fallback 绕过故障。

## 备份与恢复演练

备份命令需要显式 `BACKUP_DIR`、`BACKUP_PASSPHRASE_FILE` 与 PostgreSQL `PG*`/`PGPASSFILE` 连接环境：

```bash
cd /srv/ai-employee
APP_ENV=production BACKUP_DIR=/var/backups/ai-employee \
BACKUP_PASSPHRASE_FILE=/run/secrets/backup_passphrase \
BACKUP_RCLONE_REMOTE='encrypted-remote:ai-employee' just backup
```

脚本生成 PostgreSQL custom dump、AES-256-CBC PBKDF2 加密文件及 SHA-256 校验和，保留七份日备和四份周备，并复制 artifact 与校验和到异地 remote。生产缺失 remote 会拒绝执行。

每月在隔离、非生产数据库执行一次恢复演练。先校验 artifact 的来源和 checksum，再显式授权：

```bash
cd /srv/ai-employee
APP_ENV=production ALLOW_PRODUCTION_RESTORE=yes \
BACKUP_PASSPHRASE_FILE=/run/secrets/backup_passphrase \
scripts/restore-postgres.sh /var/backups/ai-employee/ai_employee-YYYYMMDDTHHMMSSZ.dump.enc
```

恢复会使用临时目录解密、执行 `pg_restore --clean --if-exists`，并通过 trap 清除明文。演练记录恢复时间点、校验结果、抽样任务和简报完整性验证，以及任何差异的处置结果。

## 主机与网络

VPS 的 PostgreSQL、Redis、Caddy、Prometheus 与 Grafana 持久卷必须使用主机或块设备加密。只有 Caddy 发布 80/443；数据库、Redis、API 指标和 Worker/Scheduler 指标均留在 Compose 内部网络。Caddy 负责 TLS 和压缩，公共 `/metrics` 固定返回 404。Grafana 仅在 `observability` profile 下启动，保留 Grafana 登录并使用 `/ops/grafana/` 子路径。

## Secret 与访问撤销

所有生产 Secret 由部署平台创建为 Docker Secret，再以 `/run/secrets/*` 文件挂载。不得把值写进 Compose、环境文件、日志、镜像层、备份名或 CI 输出。轮换数据库、应用主密钥、Grafana 管理员密码和备份口令时，先创建新版本、滚动重启依赖服务、验证健康和解密演练，最后撤销旧版本。

Google 连接撤销时，先在 Google 帐号安全页面撤销 AI Employee 的 OAuth 授权，再在应用连接页断开并执行对应的数据删除流程。模型 API Key 轮换时，更新 Secret、滚动 API/Worker/Scheduler，并确认结构化模型调用仍使用预期供应商与数据最小披露策略。
