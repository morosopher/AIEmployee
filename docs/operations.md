# AI Employee 运行手册

本手册覆盖可信任务中心与每日办公简报的 M1 基线，以及当前已落地的 M2 邮件与日历助手发布边界。生产环境不连接测试账号，不以 Redis 作为业务事实来源；M2 真实写入仍必须遵守三层开关、连接能力和精确人工审批。

## 发布与回滚

发布前为 `APP_IMAGE_TAG` 指定不可变镜像标签或摘要，严禁使用 `latest`。先执行一次独立、向前兼容的 `migration` 服务；它完成 Alembic upgrade 后会重新运行数据库角色授权，使新增表获得最小权限。确认 API、Worker、Scheduler、PostgreSQL、Redis 均为 healthy 后，才切换 Caddy 流量。

应用镜像可回滚到上一个不可变标签，但绝不对生产数据库执行破坏性 migration downgrade。迁移采用 expand/migrate/switch/contract：旧应用必须能读取扩展后的 schema，删除旧列或表只能在所有旧应用退出后的后续发布完成。

### M2 CalendarEvent 0018 发布

`20260809_0018` 将 CalendarEvent 供应商身份从连接级二元组切换为 `connection_id + calendar_id + provider_event_id` 三元组。该 contract revision 不支持旧、新 Calendar writer 混跑，发布必须严格按以下顺序执行：

1. 关闭 Calendar 周期调度。
2. 排空并停止全部可能执行 `sync_calendar` 的旧 Worker，确认没有旧事件 upsert 事务仍在运行。
3. 应用 Alembic revision `20260809_0018`。
4. 部署引用三元约束的新 API、Worker 和 Scheduler，禁止旧 Worker 回流。
5. 恢复 Worker，再恢复 Calendar 调度。

生产环境不得对该迁移执行破坏性 downgrade。若迁移后已经存在跨日历相同 provider event ID，旧二元约束无法无损恢复，downgrade 必须 fail closed；不得删除、合并或改写事件行来强行回退，应停止发布并制定人工数据保留方案。

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
