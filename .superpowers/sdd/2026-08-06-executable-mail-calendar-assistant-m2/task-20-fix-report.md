# Task 20 Fix Round 1 修复报告 — 核对 Worker 组合与损坏租约恢复

日期：2026-09-04
基线：`1daf552 feat: reconcile unknown action outcomes`
范围：仅修复 Task 20 复审指出的两个 Important finding；未接入真实 Google/Microsoft 账户，也未执行任何真实供应商写入。

## 结果

可信动作的只读核对入口现在由 Worker 固定组合根显式提供 adapter registry。普通可信动作执行与
`reconciling` 专用入口使用同一组合约束；当前 Task 21–24 的真实 provider adapter 尚未落地，
因此组合根仍返回空 registry 并保持 fail-closed。

`reconciling` TaskRun 若出现 `lease_owner IS NOT NULL` 且 `lease_expires_at IS NULL` 的损坏
租约，现在会被 PostgreSQL-only recovery 扫描识别。恢复只清理本地 owner/expiry 协调事实，
不会调用 provider；已有未发布 `task.execute` Outbox 时也不会重复插入事件。正常活动租约和已过期
租约的既有规则保持不变。

## Finding 1：reconciliation 缺少 Worker adapter registry 注入

### 根因

`execute_task.py` 的 `RECONCILING` 分支直接调用 `execute_reconciliation_task()`，没有传入生产
Worker 组合的 `TrustedActionAdapterRegistry`。`reconcile_actions.py` 随后以空 registry 构造
用例，导致生产中的 UNKNOWN 结果无法找到只读 adapter，最终被分类为
`provider_action_unavailable` 并重新排队，而不是执行 `adapter.reconcile()`。原有直接调用测试
可以注入 registry，但没有覆盖 Taskiq 组合路径。

### 修复

- 在 `workers/trusted_actions.py` 增加
  `build_worker_trusted_action_registry()`，作为唯一静态 Worker 组合根。
- `execute_task.py` 在普通可信动作和 `RECONCILING` 专用路径均从该组合根构造 registry，
  并显式传入 runner/use case。
- `build_trusted_action_task_step()` 与 `execute_reconciliation_task()` 使用显式
  `is not None` 判断，保留调用方注入的 registry，不以布尔值误替换。
- 组合根不支持运行时动态注册，不从 Taskiq payload 读取 adapter、命令或 token；registry 只在
  当前消息生命周期内组装，已有 `session_factory` 仍由入口的 `finally` 释放。
- 后续 Task 21–24 的 Gmail、Google Calendar、Microsoft mail/calendar adapter 必须在该
  固定组合根接入。接入前返回空 registry 是有意的安全默认值。

## Finding 2：malformed lease 会永久卡住 reconciliation

### 根因

`_recover_due_reconciliations_in_session()` 只选择无 owner 或已过期的非空 lease。owner 非空、
expiry 为 NULL 的异常形状既不满足查询条件，也不能通过 claim CAS，因此任务会一直停留在
`reconciling`，无法被恢复或重新认领。

### 修复

- recovery 查询将精确的 `lease_owner IS NOT NULL AND lease_expires_at IS NULL` 形状纳入候选。
- 对该形状仅在事务内清除 `lease_owner` 与 `lease_expires_at`，不读取或调用 provider，随后
  允许下一次只读 claim。
- 若当前 task 已有未发布的 `task.execute` Outbox，只清理损坏租约，不再追加恢复事件；没有
  pending event 时沿用原有幂等恢复投递。
- 活动 lease（未来 expiry）仍不会被恢复；已过期 lease 仍按既有恢复路径处理。

## TDD 证据

修复前在新增回归测试上观察到预期 RED：

```text
... pytest ... -k 'worker_registry or malformed_reconciliation_lease or preserves_active'
FF..
```

失败分别表现为：Taskiq 组合 factory 未被调用、fake adapter 的 reconcile 次数为 0；损坏租约
recovery 返回 0。活动/过期 lease 的既有断言通过。

修复后新增回归的窄矩阵通过：

```text
2 passed, 7 deselected
```

## 验证

### Task 20 Fix Round 1 聚焦矩阵

命令：

```bash
TEST_DATABASE_URL='postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test' \
uv run --project backend pytest \
  backend/tests/integration/m2/test_action_reconciliation.py \
  backend/tests/integration/m2/test_manual_resolution_race.py \
  backend/tests/unit/application/test_reconciliation_policy.py \
  backend/tests/integration/m2/test_tool_execution_claim.py \
  backend/tests/integration/agents/test_trusted_action_resume.py -q
```

输出：

```text
150 passed in 62.03s (0:01:02)
```

### Retry recovery / due schedule 相邻检查

命令：

```bash
TEST_DATABASE_URL='postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test' \
uv run --project backend pytest \
  backend/tests/integration/workers/test_retry_recovery.py \
  backend/tests/unit/workers/test_due_schedule.py -q
```

输出：

```text
8 passed, 9 errors in 1.15s
```

9 个错误全部发生在 `migrated_database` fixture setup 的
`database maintenance invariant violation`，测试体未执行；8 个 `due_schedule` 单测通过。
这是共享测试数据库当前 admission/maintenance 状态造成的环境 setup error，不是本轮代码断言
失败，也不把该 suite 宣称为全绿。需要在受控 disposable 数据库生命周期下重新运行 retry
recovery 集合。

### 静态检查

```text
uv run --project backend ruff check \
  backend/src/ai_employee/workers/trusted_actions.py \
  backend/src/ai_employee/workers/reconcile_actions.py \
  backend/src/ai_employee/workers/execute_task.py \
  backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py \
  backend/tests/integration/m2/test_action_reconciliation.py
All checks passed!

uv run --project backend mypy \
  backend/src/ai_employee/workers/trusted_actions.py \
  backend/src/ai_employee/workers/reconcile_actions.py \
  backend/src/ai_employee/workers/execute_task.py \
  backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py
Success: no issues found in 4 source files

git diff --check
退出码 0
```

所有测试均使用合成数据、Fake adapter 和受控 PostgreSQL；本轮没有访问真实供应商或持久化
token、完整邮件正文、原始 provider 响应。

## 修改文件

- `backend/src/ai_employee/workers/trusted_actions.py`
- `backend/src/ai_employee/workers/reconcile_actions.py`
- `backend/src/ai_employee/workers/execute_task.py`
- `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- `backend/tests/integration/m2/test_action_reconciliation.py`
