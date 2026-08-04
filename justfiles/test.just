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
    uv run --project backend pytest backend/tests/integration backend/tests/contract -q

[group('quality')]
test-e2e:
    pnpm --dir frontend test:e2e

[group('quality')]
e2e-backend:
    bash scripts/run-e2e-backend.sh

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
    pnpm --dir frontend build
    pnpm --dir frontend test:e2e
