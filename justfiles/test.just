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

[group('quality')]
check: lint typecheck test

[group('quality')]
ci: check test-integration
    bash scripts/test-tooling.sh
    bash scripts/test-run-e2e-backend.sh
    bash scripts/test-ci-workflow.sh
    pnpm --dir frontend build
    pnpm --dir frontend test:e2e
