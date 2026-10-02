# 完整任务历史补充验收记录

日期：2026-10-02。范围为[补充规格](../superpowers/specs/2026-10-01-complete-task-history-design.md)及其[独立计划](../superpowers/plans/2026-10-01-complete-task-history.md)。验证基准为 `9aa1b3b`；本记录只验收完整任务历史与用户批准的 SSE 取消清理修复，同时交付原 M2.1 Task 7。M2.1 Tasks 1～6 保持已完成，Task 8 前置异步确认修复及 Tasks 8～17 尚未实施，整个 M2.1 尚未完成，M2.2 未获实施批准。

## 实现与验证基准

| 独立任务 | 实现提交 | 聚焦结果及独立审查 |
|---|---|---|
| 1 分类、筛选、日期及摘要 | `7244d82` | 62 单测通过，规格／质量审查通过 |
| 2 签名游标 | `7adc5b2`、`bd49a81` | 55 单测通过；标准 Base64 主密钥兼容问题修复并复审通过 |
| 3 索引及受守卫迁移 | `2a9bce7` | 旧迁移 92 项、索引／lifecycle／CLI 聚焦 36 项分别通过，审查通过 |
| 4 只读一致性查询 | `ae462cb` | 合计 136 项通过，其中本任务 5 单元＋14 PostgreSQL，审查通过 |
| 5 认证列表 API | `d9c138f` | 11 HTTP 单元＋12 PostgreSQL API 通过；旧 REST 16、原 SSE 12 通过，审查通过 |
| 6 独立前端客户端 | `79963d5` | 46 单测通过，审查通过 |
| 7 列表状态与路由 | `b451448` | 26 单测通过，审查通过 |
| 8 列表／时间线与原 M2.1 Task 7 | `addc635` | 页面、组件、状态及原 Store/SSE 聚焦 98 项；最终 E2E 12 项通过，审查通过 |
| 最终修复 | `1b66c5d`、`1315dab` | 导航修复及慢探测补强，SSE 清理单元 5 项、新 PostgreSQL 13＋原 SSE 12 项、最终相关 E2E 12 项通过；限定复审通过 |
| CI 兼容收尾 | `9f57d41`、`9aa1b3b` | 历史迁移测试兼容经独立复审；密码精确标签经控制器复核；最终完整 CI 通过 |

上述为各实现提交的聚焦证据，数字存在交叉覆盖，不相加为总测试数。最终完整门禁另列；早期中间失败不算绿色证据。Task 3 曾因把两个持有不同生命周期锁的 fixture 合并到同一 pytest 会话产生 90 个 setup errors，该次并非通过；正式分阶段入口下旧迁移 92 和聚焦 36 才是验收依据。Task 5 直接运行旧 SSE fixture 的准入错误随后通过原受管编排器运行原 12 项解除，未放宽 fixture 或迁移守卫。

## 完整门禁

工作目录为仓库根目录。使用 Python 3.12／uv、Node.js 24／pnpm，以及已配置的受管临时 PostgreSQL、Redis 和合成认证／Secret 环境；真实供应商写入关闭，不连接真实账户。数据库测试、E2E 与完整 CI 串行独占同一受管测试资源。

```bash
just check
just ci
bash scripts/report-frontend-bundle.sh --budget
```

本次宿主 8000 端口绑定受限，完整 `just ci` 在同一 Docker 网络 namespace 内执行，仍调用仓库根 `just` recipe 和原测试编排器；没有拆开若干成功命令冒充完整 CI，也未修改共享数据库或 fixture 准入。环境适配脚本只属于本地执行环境；复现可在满足仓库标准运行前提的环境直接使用上列正式命令。

| 基准 | 命令 | 实际结果 |
|---|---|---|
| `9aa1b3b` | `just check` | 退出 0；后端单元 2730、前端 41 文件／458 测试；Ruff、mypy、ESLint、vue-tsc、许可及版本检查通过 |
| `9aa1b3b` | `just ci` | 退出 0；单元 2730＋458；常规集成 2516（阶段排除2），生命周期 485；工具链契约4例、E2E后端脚本8例；生产构建及浏览器60例通过 |
| `9aa1b3b` | `bash scripts/report-frontend-bundle.sh --budget` | 退出 0，budget OK；JS gzip 284193 B，相对140373 B基线增加143820 B（143.82 KB＜200 KB）；CSS gzip 8900 B（8.90 KB＜60 KB） |

当前 `just ci` 报告构建体积；预算仍通过上述显式 `--budget` 命令验证，自动强制预算及遗留样式 strict 模式属于原 M2.1 Task 16，不能提前宣称已启用。现有弃用警告、设置页测试的空路由提示和未迁移页面样式 warning 保留；Vite仍提示单个压缩前chunk超过500 KB，未抑制该提示，实际gzip预算通过。

本轮基准CI日志的创建／最后写入窗口为2026-10-02 05:33:05～06:07:10 UTC（文件元数据，非逐命令计时）；完整进程退出码为0。文档提交后的复验另外记录明确的进程开始／结束UTC时间、提交SHA及退出码。

本文保留产品构建基准 `9aa1b3b`。本次验收文档提交后由控制器再运行完整 `just ci`，以该提交的 SHA、起止时间、退出码和输出另行留档；不为写入自身提交 SHA 循环提交。文档后的 CI 结果以对应提交的独立运行记录为准。

完整 CI 曾在 `1315dab` 得到常规集成2516通过、生命周期418通过／64失败，集中于历史0019发布head假设及全替身包装器继承外层数据库配置。`9f57d41` 仅调整测试：完整相邻迁移callback、历史版本属于真实发布链、原脚本副本构造真实0019发布状态，并继续验证当前0020的专用CLI拒绝；保留完整权限矩阵和三锁流程。当前0020筛查零连接拒绝，专用迁移按原流程取得lease并读工件后拒绝，不能称后者零连接。相关模块输出1常规＋348生命周期通过，但自动删除容器导致交接时未单独捕获退出码；最终 `9aa1b3b` 的完整CI退出0才是统一门禁证据。

`9f57d41` 完整CI随后在浏览器阶段得到58通过／2失败，原因均为密码标签同时匹配输入框和“显示密码”按钮。`9aa1b3b` 只把同文件三处选择器设为精确匹配，供应商故障与恢复断言不变。最终60项浏览器测试全部通过。平台在该小改动复审调度时返回“agent thread limit reached”，因此这一提交和本验收文档由控制器核对，未冒称独立复审；前面产品实现、SSE修复及迁移测试兼容的独立审查记录保持有效。

## 规格 §10 的 14 项验收映射

下列测试路径均为仓库跟踪文件；实现、测试断言、可复现命令与上方数值共同构成证据，不依赖将来可能删除的本地原始日志。单元文件可用 `uv run --project backend pytest <文件> -q` 或 `pnpm --dir frontend test:unit --run <src 路径>` 聚焦执行；全部 PostgreSQL／契约测试通过 `just test-integration` 的原受管分阶段入口复现，浏览器通过 `just test-e2e` 复现。

| 项 | 行为及证据 | 结果／提交 |
|---|---|---|
| 1 真实保留记录 | `backend/src/ai_employee/infrastructure/db/repositories/task_history_views.py` 从 PostgreSQL 查摘要；`frontend/e2e/task-history.spec.ts` 的 `retained database tasks are reachable across pages and browser reload without prior visits` 先在简报页通过既有认证 POST 创建 25 条合成任务，再从历史分页找回全体 ID、验证无重复、刷新后打开详情 | 真实 API 浏览器场景通过；`ae462cb`、`addc635` |
| 2 分类完整性 | `backend/src/ai_employee/application/task_history.py` 固定五种业务、六种后台，未知为 other；`backend/tests/unit/application/test_task_history_filters.py` 与 `backend/tests/integration/tasks/test_task_history_views.py::test_filters_and_independent_failed_count` 覆盖已知／未知 kind。仅查询真实 TaskRun，无 TaskRun 的作业和本地草稿不伪造列表行 | 规则 62 项及数据库 14 项选集中通过；`7244d82`、`ae462cb` |
| 3 日期／组合筛选 | `backend/src/ai_employee/application/task_history_dates.py`、`backend/tests/unit/application/test_task_history_dates.py` 覆盖纽约 23/25 小时、Havana 午夜重叠、São Paulo 午夜跳跃、Apia 整日跳过、Kathmandu 非整小时跳跃、上海闰日和越界；过滤单测及 API 非法参数矩阵覆盖倒置／空区间、默认无日期及 IANA；移动 E2E 保留 YYYY-MM-DD 民用日期 | 规则及 API 23 项选集中通过；`7244d82`、`d9c138f`、`addc635` |
| 4 精确双向分页 | `backend/tests/integration/tasks/test_task_history_views.py` 的 `test_pages_are_user_scoped_and_bidirectional`、`test_deleted_anchor_empty_window_and_no_recovery_loop`、`test_upper_and_newer_empty_window`、`test_page_limits_and_retry_metadata`、`test_new_commit_does_not_cross_upper_but_status_changes_are_visible` 覆盖 20＋5 往返、完整微秒／并列 UUID、1／100、插入、状态变化、边界删除和空窗口恢复 | 真实 PostgreSQL 14 项选集中通过；`ae462cb` |
| 5 游标安全 | `backend/tests/unit/infrastructure/test_task_history_cursor.py` 验证独立 HKDF/HMAC 向量、严格 schema／编码、版本、长度、篡改、24 小时／未来60秒、用户／筛选／时区／limit 绑定、轮换、既有标准及 URLsafe Base64 Secret 兼容；API 统一固定 422，不回显载荷 | 55 项通过，兼容修复 RED 为 2 failed／53 passed；`bd49a81` |
| 6 用户隔离 | reader 投影、两个方向 EXISTS 和 COUNT 都带 user_id；`backend/tests/integration/tasks/test_task_history_views.py` 双用户分页与计数、`backend/tests/integration/api/test_task_history.py::test_summary_pagination_is_isolated_and_read_only` 真实 Cookie 与跨用户 cursor、原详情 404 防线 | 数据库、API 及旧 REST 16 项通过；`ae462cb`、`d9c138f` |
| 7 白名单／只读／no-store | `test_snapshot_projection_and_closed_transaction` 精确断言八个数据库列、固定查询数和只读 RR；API 显式增加 category 成九字段，测试 GET 不新增 TaskRun／Outbox／audit，成功及 401／422 no-store；`frontend/src/api/taskHistory.spec.ts` 严格解析且丢弃额外敏感字段 | API 23 项及前端客户端 46 项通过；`ae462cb`、`d9c138f`、`79963d5` |
| 8 后台失败口径 | `test_filters_and_independent_failed_count` 验证 COUNT 仅受 user／日期／非 business／failed 限制，独立于列表筛选及 upper；`frontend/src/components/TaskHistoryList.spec.ts` 验证 background＋failed＋kind=null、保留日期及解释文案 | 数据库及组件选集中通过；`ae462cb`、`addc635` |
| 9 原动作／重试 | `test_page_limits_and_retry_metadata` 验证重试独立行和来源；`frontend/src/pages/TasksPage.spec.ts` 保留取消、重试运输失败复用幂等键、明确拒绝后的新意图、replacement 跟随及恢复提案审批入口；`frontend/e2e/action-workspace.spec.ts`、`frontend/e2e/calendar-restore.spec.ts` 保留未知请求、审批、执行断言 | 原动作、相关最终 E2E 12 项通过；`addc635`；完整 CI 覆盖既有幂等及唯一执行回归 |
| 10 列表／详情／SSE 隔离 | `frontend/src/features/tasks/useTaskHistory.ts` 只保存独立摘要，`frontend/src/pages/TasksPage.spec.ts` 确认摘要不写 Store、同任务单事件流、404 后不得复活旧详情；`frontend/src/stores/tasks.spec.ts`、`frontend/src/composables/useTaskEvents.spec.ts`、`frontend/e2e/reconnect.spec.ts` 保留快照／重放／游标／去重断言 | 原 Store/SSE、旧后端 SSE 12 及最终相关 E2E 通过；`addc635`、`1315dab`；取消清理另见下节 |
| 11 探测／迟到响应 | `frontend/src/features/tasks/useTaskHistory.spec.ts` 用 FakeTimer／deferred 覆盖 30 秒、隐藏／卸载、慢请求互斥、失败等待、旧世代隔离、深链接基线、新提示不替换快照；最终补充慢 probe 跨筛选及隐藏90秒／恢复90秒、完成后29999毫秒不探测／30000毫秒探测 | 最终前端 458 项中通过；`b451448`、`1b66c5d` |
| 12 浏览器／无障碍 | `frontend/e2e/task-history.spec.ts` 两例分别提供真实 API 桌面分页／刷新及受控移动键盘、日期、错误／404恢复、焦点；`frontend/src/pages/TasksPage.spec.ts`、`frontend/src/components/TaskTimeline.spec.ts`、`frontend/src/components/AppShell.spec.ts` 覆盖 live region 与三个断点的唯一时间线；`frontend/src/components/TaskHistoryList.spec.ts` 覆盖普通及修饰键点击 | 最终相关 E2E 12 项、前端 458 项中通过；`addc635`、`1b66c5d`；不冒称七视图 axe 或整个 M2.1 布局验收完成 |
| 13 索引／迁移／ACL | `backend/tests/integration/db/test_task_history_index.py` 2000 条合成记录与 `test_projected_seek_explain` 3000 条附加记录验证默认 planner 倒序索引扫描；`backend/tests/integration/db/test_migrations.py`、lifecycle 和 CLI 测试验证最终 guard、拒绝回滚与精确 ACL，见下节 | 旧迁移92／聚焦36通过；`2a9bce7`、`ae462cb` |
| 14 全部门禁 | 上方完整门禁记录及本次文档提交后独立 CI 留档；所有测试采用 Fake／合成数据，未访问真实供应商 | 产品验证基准 `9aa1b3b`；完整CI退出0、浏览器60项通过 |

## 数据库与 SSE 关键边界

迁移 `20261001_0020_task_history_index.py` 仅增加普通事务型 B-tree `ix_task_runs_user_created_id (user_id, created_at, id)`。没有并发索引、第二索引、新列、新角色或新授权；0020 的 baseline／active 完整 ACL 与0019最终策略相同。跨0019升级时，原 AAD `before_mutation` 不变，`before_commit` 保持在实际最终目标 callback 的完整权限核验之后；拒绝会回滚同根事务的索引、字段、版本行及 ACL。source0019→0020不重新执行AAD处理；历史专用 artifact／CLI 仍精确绑定0018／0019，未开放非空0018直接升级0020的生产准入。未实施生产迁移。

列表在单次 REPEATABLE READ READ ONLY 事务内执行摘要、两个方向 EXISTS 和独立失败 COUNT。非空页固定四次 SELECT，不逐行查详情；投影仅八列，无正文／步骤／租约。合成 seek EXPLAIN 为 Limit → Backward Index Scan；它证明查询形态与索引使用，不承诺生产数据量下 COUNT 的延迟。原 `repositories/task_history.py` 保留清理器及数据保留规则未修改。

Task 8 浏览器运行曾观察到 SQLAlchemy 非归还连接 GC warning 和 termination error。后续真实 PostgreSQL／EventSourceResponse 实验精确复现了 SQLAlchemy pre_ping 期间 AnyIO 重复取消中断失效归还的机制；这证明一个真实缺陷，不能直接追溯并断言原先那一次日志必然来自同一调用。用户于2026-10-01批准限定修复，生产改动只在 `api/sse.py` 三种有限数据库读取及收尾边界。

正式 RED 在旧生产代码上使用 pre_ping 探针，正常退出1：1 failed，GC前 checkedout=1、GC warning=1，并出现 terminate CancelledError。早期诊断 SIGINT／退出2不算正式回归。最终13项 PostgreSQL 测试并非全部跑过 RED；正式 RED 的根因场景和失败断言被纳入最终回归。测试在断言后 finally 才精确回收本轮 PID，避免失败后的后置 TRUNCATE 等待；不以测试兜底清理后的状态证明产品成功。

`1315dab` 由独立 asyncio 子任务拥有会话和确切公开 AsyncConnection，外层断线只转发一次取消，并等待该所有者结束。关闭 Session 五秒预算，超时／异常则 invalidate 确切连接，finally 再以五秒预算尝试 close；锁定 asyncpg 适配器的失效关闭有两秒优雅关闭及强制关闭回退。正常读取的收尾异常传播；断线后的收尾异常只记固定安全日志并保留原 CancelledError。未关闭 pre_ping、屏蔽 warning、吞取消、修改依赖／全局 Session 工厂或丢弃后台清理任务。该有界策略限于支持的驱动取消／关闭路径，不声称任意驱动故障都保证归还。

`backend/tests/integration/api/test_task_connection_cleanup.py` 新13项包括三读取×query/pre_ping×disconnect/single_cancel共12项，以及 SessionTransaction移除后真实 rollback 超时关闭1项；与原 `test_task_sse.py` 的12项共25 passed。断言 GC前池归零、真实checkin、无idle事务／残留读取任务／终止错误。`backend/tests/unit/api/test_sse_database_cleanup.py` 五个单测覆盖重复外层取消、关闭预算、正常返回、取消期间收尾错误及invalidate失败仍close。最终相关浏览器12项及 `just check`2730／458通过，无该GC／termination告警；不是宣称整个工程零警告。

## Live region 与交接限制

`addc635` 提交正文记录：TasksPage 静态旧／新均3 status、2 alert，TaskTimeline均1 status、2 alert；旧触发语义／文案保留。新 TaskHistoryList 增加2 status（加载／新任务）和1 alert（安全错误）；DataView／Timeline不额外增加 live region。`/tasks` 去掉 AppShell 重复全局时间线及第三列，改为自有唯一可操作详情；其他页面抽屉保持。组件运行时测试验证上述变化，不用空role补数。完整 M2.1 清单及七视图axe留给原Task16。

已知限制及历史交接：

- 完整历史只包含当前用户仍保留的TaskRun；不恢复已删除记录、来源ID或没有TaskRun的作业。分页上界不冻结任务状态／清理／迟提交成员；新任务提示只比较创建顺序更高记录。后台失败数不是未处理故障数。
- 依赖审计沿用 M2.1 Task1核对的23项基线（9高、11中、3低），与`4ce84ae`的module＋GHSA＋受影响版本集合相同；本补充未升级依赖或修复这些漏洞。不得称audit clean。可用 `pnpm --dir frontend audit --json` 复核当前公告，网络公告更新可能改变计数。
- 原 M2.1 Task1首次E2E曾55／56，后续原聚焦2／2及完整56／56通过；即时请求计数竞态保留为历史观察，未声称已修复或当前仍失败。
- 原 M2.1 Task2移动导航点击当前路由不关闭Drawer的低优先级问题，留给原计划后续最终审查修复；Task5历史版本界面可补“零POST”明确断言的测试建议继续保留。原依赖弃用和待迁移样式warning继续保留。
- AppShell错误accessible-name排除断言已在`addc635`纠正；本补充Task7慢探测组合建议及Task8修饰键链接问题已在`1b66c5d`关闭，不再列为开放项。SSE资源清理缺陷已在`1315dab`修复并限定复审通过。
- 后续从原M2.1 Task8前置异步确认修复接续，再依序Task8～17。保留Task1～6提交及上述历史发现；无M2.2部署、CSP、TLS或新产品能力授权。
