# 质量门禁从聚焦测试逐步扩展；ci 在 check 之后补充集成、构建与浏览器验证。
[group('quality')]
test: test-backend test-frontend

[group('quality')]
test-backend:
    uv run --project backend pytest backend/tests/unit -q

[group('quality')]
test-frontend:
    pnpm --dir frontend test:unit --run

[group('quality')]
test-integration:
    # parent orchestrator 用两个独立 pytest session 隔离 roles-present 与 roles-absent 状态。
    uv run --project backend python scripts/run_integration_tests.py

[group('quality')]
test-e2e:
    pnpm --dir frontend test:e2e

[group('quality')]
e2e-backend:
    bash scripts/run-e2e-backend.sh

# 仅供新 CI job 显式准备其已登记 service；普通测试/产品启动不隐式 seed 或修补数据库。
[group('quality')]
prepare-ci-database:
    uv run --project backend python scripts/prepare_ci_database.py

[group('quality')]
lint:
    uv run --project backend ruff check backend
    pnpm --dir frontend lint

[group('quality')]
format:
    uv run --project backend ruff format backend
    pnpm --dir frontend format

[group('quality')]
typecheck:
    uv run --project backend mypy backend/src
    pnpm --dir frontend type-check

# 样式字面量检查在 M2.1 迁移期间只告警，由实施计划 Task 16 改为 --strict。
# 本地提交前门禁：lint、typecheck、单测通过后核对前端许可证与 PrimeVue 主版本，并告警样式字面量。
[group('quality')]
check: lint typecheck test
    bash scripts/check-frontend-licenses.sh
    bash scripts/check-frontend-styles.sh

# 构建后立即报告首屏 gzip 体积并写入 CI 日志；预算强制（--budget）由 Task 16 开启。
[group('quality')]
ci: check test-integration
    bash scripts/test-tooling.sh
    bash scripts/test-run-e2e-backend.sh
    bash scripts/test-ci-workflow.sh
    pnpm --dir frontend build
    bash scripts/report-frontend-bundle.sh
    pnpm --dir frontend test:e2e
