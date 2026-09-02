# Task 19 最终修复报告 — 隐私删除幂等请求标识

## 结果

已修复全数据删除 API 在同一用户使用同一 `Idempotency-Key` 重试时无法复用原任务的
问题。`deletion_request_id` 现在由应用用例基于用户 UUID 和客户端幂等键稳定派生；路由
不再为每次请求生成随机 UUID。任务 Repository 的 `kind + 完整 input_payload` 严格绑定
保持不变，确认短语仍不会进入任务载荷。本轮未访问真实 Google/Microsoft 账户或供应商。

## 根因

`backend/src/ai_employee/api/routers/privacy.py` 原先在每次请求中调用 `uuid4().hex`。
因此，同一用户携带同一幂等键重试时，任务输入中的 `deletion_request_id` 发生变化；严格
任务 Repository 将其判定为 `idempotency_key_payload_mismatch`，响应没有原始 `task_id`。

## 修改文件

- `backend/src/ai_employee/application/use_cases/privacy.py`
  - 新增固定 domain-separated 的 SHA-256 派生函数。
  - 以 UUID 二进制值和幂等键 UTF-8 值分别使用 `uint32` 大端长度帧，避免分隔符边界碰撞。
  - `RequestAllDataDeletionUseCase.execute()` 自行派生请求 ID，移除随机/外部传入的
    `request_id`。
- `backend/src/ai_employee/api/routers/privacy.py`
  - 移除 `uuid4` 依赖及随机请求 ID 参数；保留认证、CSRF、精确确认和异步任务契约。
  - 同步执行 Ruff 格式化，未改变其他路由行为。
- `backend/tests/unit/application/test_privacy.py`
  - 新增用例级回归：同一用户/键产生完全相同的 64 位小写摘要；不同用户或键产生不同
    摘要；摘要不直接包含原始用户 UUID 或幂等键。
- `backend/tests/integration/api/test_privacy.py`
  - 扩展跨用户断言，验证两个任务的删除请求摘要不同，同时保留确认文本不入载荷断言。

## TDD 证据

### RED

先新增用例并调用新的窄 API（不再传入随机 `request_id`），旧实现因仍要求该参数而失败：

```text
uv run --project backend pytest backend/tests/unit/application/test_privacy.py -q
1 failed
TypeError: RequestAllDataDeletionUseCase.execute() missing 1 required keyword-only argument: 'request_id'
```

### GREEN

实现稳定派生后：

```text
uv run --project backend pytest backend/tests/unit/application/test_privacy.py -q
2 passed in 0.02s
```

## 验证

```text
uv run --project backend ruff check \
  backend/src/ai_employee/application/use_cases/privacy.py \
  backend/src/ai_employee/api/routers/privacy.py \
  backend/tests/unit/application/test_privacy.py \
  backend/tests/integration/api/test_privacy.py
All checks passed!

uv run --project backend ruff format --check \
  backend/src/ai_employee/application/use_cases/privacy.py \
  backend/src/ai_employee/api/routers/privacy.py \
  backend/tests/unit/application/test_privacy.py \
  backend/tests/integration/api/test_privacy.py
4 files already formatted

uv run --project backend mypy \
  backend/src/ai_employee/application/use_cases/privacy.py \
  backend/src/ai_employee/api/routers/privacy.py
Success: no issues found in 2 source files

git diff --check
通过
```

使用受控 Cycle 5 disposable regular 数据库运行官方 integration orchestrator；隐私 API
回归未出现在失败列表，regular child 汇总为：

```text
2 failed, 1107 passed, 2 deselected, 7 errors, 1 warning
```

失败/错误均来自当前环境未提供 `TEST_REDIS_URL` 的 Redis 故障演练和 SSE 测试（缺少 Redis
连接导致 setup/断言失败），不是本次隐私代码；PostgreSQL regular 数据库生命周期已由
orchestrator 完成清理。由于该环境没有 Redis，本轮不能把完整 integration suite 宣称为全绿。

另以同一官方 orchestrator 和 Cycle 5 disposable 生命周期，仅通过 `PYTEST_ADDOPTS` 选择
全数据删除 API 回归，得到：

```text
1 passed, 1117 deselected, 1 warning in 1.73s
```

该次 regular child 的目标测试通过；后续 lifecycle child 因过滤后无匹配测试返回 pytest
code 5，属于过滤命令的预期选择副作用，不代表目标测试失败。

## 剩余限制

- 需要在具备受控测试 Redis 的环境中重新运行完整 `scripts/run_integration_tests.py`，以
  取得包含 Redis 故障路径的全套绿色证据。
- 本修复未改变 TaskRun 的严格 `kind + input_payload` 校验，也未实施 Task 27E 的删除
  barrier/retention 改造。
