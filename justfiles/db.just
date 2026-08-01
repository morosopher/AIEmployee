# 数据库 recipe 统一从根目录运行，确保 Alembic 配置路径和项目依赖解析稳定。
[group('database')]
db-upgrade:
    # 清除可覆盖连接目标或 libpq service 的环境变量，保证迁移只消费应用显式提供的 DATABASE_URL。
    env -u PGHOST -u PGHOSTADDR -u PGPORT -u PGDATABASE -u PGUSER -u PGSERVICE -u PGSERVICEFILE uv run --project backend alembic -c backend/alembic.ini upgrade head

[group('database')]
db-revision name:
    uv run --project backend alembic -c backend/alembic.ini revision --autogenerate -m "$1"

[group('database')]
create-admin email password_file:
    uv run --project backend python -m ai_employee.cli.create_admin --email "$1" --password-file "$2"

[confirm]
[group('database')]
db-reset:
    #!/usr/bin/env bash
    set -euo pipefail

    # just 的 [confirm] 只能防止误触；脚本仍需独立验证环境、精确目标和人工输入。
    fail() {
      printf 'db-reset refused: %s\n' "$1" >&2
      exit 1
    }

    app_env="${APP_ENV-}"
    case "${app_env}" in
      development|test) ;;
      *) fail 'APP_ENV must be explicitly set to development or test' ;;
    esac

    database_name="${POSTGRES_DB-}"
    database_user="${POSTGRES_USER-}"
    database_url="${DATABASE_URL-}"
    [[ -n "${database_name}" ]] || fail 'POSTGRES_DB is required'
    [[ -n "${database_user}" ]] || fail 'POSTGRES_USER is required'
    [[ -n "${database_url}" ]] || fail 'DATABASE_URL is required'

    # 使用 Python 标准库解析 URL，避免 Bash 字符串切片漏掉 username、host 或 port。
    # 敏感 URL 通过临时环境变量传入，不作为参数或错误消息输出；脚本只打印静态拒绝原因。
    AI_EMPLOYEE_DB_RESET_ENV="${app_env}" \
    AI_EMPLOYEE_DB_RESET_NAME="${database_name}" \
    AI_EMPLOYEE_DB_RESET_USER="${database_user}" \
    AI_EMPLOYEE_DB_RESET_URL="${database_url}" \
    python3 - <<'PY'
    import os
    import re
    import sys
    from typing import NoReturn
    from urllib.parse import urlsplit


    def reject(reason: str) -> NoReturn:
        """以不泄露 URL、密码或完整标识符的方式终止数据库目标校验。"""

        print(f"db-reset refused: {reason}", file=sys.stderr)
        raise SystemExit(1)


    app_env = os.environ["AI_EMPLOYEE_DB_RESET_ENV"]
    database_name = os.environ["AI_EMPLOYEE_DB_RESET_NAME"]
    database_user = os.environ["AI_EMPLOYEE_DB_RESET_USER"]
    database_url = os.environ["AI_EMPLOYEE_DB_RESET_URL"]


    def validate_identifier(label: str, value: str) -> None:
        """验证 PostgreSQL 标识符的 ASCII 语法和 63 字节上限。"""

        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            reject(f"{label} must match [A-Za-z_][A-Za-z0-9_]*")
        if len(value.encode("utf-8")) > 63:
            reject(f"{label} must be at most 63 UTF-8 bytes")


    validate_identifier("POSTGRES_DB", database_name)
    validate_identifier("POSTGRES_USER", database_user)
    if database_name.casefold() in {"postgres", "template0", "template1"}:
        reject("system databases cannot be reset")
    if app_env == "test" and not database_name.endswith("_test"):
        reject("test database name must end with _test")

    # urlsplit 会规范化 scheme 大小写，并从任意位置静默删除 TAB、LF、CR；先校验原始
    # 字符串，确保安全 guard 与后续 SQLAlchemy 对 DATABASE_URL 的接受范围完全一致。
    if not database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        reject("DATABASE_URL must begin with an exact lowercase PostgreSQL scheme")
    if any(character in database_url for character in "\t\n\r"):
        reject("DATABASE_URL must not contain TAB, LF, or CR characters")

    try:
        parsed_url = urlsplit(database_url)
        url_scheme = parsed_url.scheme
        url_username = parsed_url.username
        url_hostname = parsed_url.hostname
        url_port = parsed_url.port
    except ValueError:
        # urlsplit 会对非法 IPv6 或 port 抛出 ValueError；不要把原始 URL 带入错误。
        reject("DATABASE_URL is malformed")

    if url_scheme not in {"postgresql", "postgresql+asyncpg"}:
        reject("DATABASE_URL must use a PostgreSQL scheme")
    if url_username != database_user:
        reject("DATABASE_URL username must exactly match POSTGRES_USER")
    if url_hostname is None or url_hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        reject("DATABASE_URL host must be localhost, 127.0.0.1, or ::1")
    if url_port != 5432:
        reject("DATABASE_URL port must be explicitly set to 5432")
    # SQLAlchemy 与底层驱动可能把 query 解释为 authority/path 的覆盖项；破坏性操作不接受任何 query。
    if "?" in database_url:
        reject("DATABASE_URL must not contain a query")
    if "#" in database_url:
        reject("DATABASE_URL must not contain a fragment")
    if parsed_url.path != f"/{database_name}":
        reject("DATABASE_URL database must exactly match POSTGRES_DB")

    # 仅输出完成全部校验后的规范化端点，不回显密码、query、fragment 或完整 URL。
    normalized_host = f"[{url_hostname.lower()}]" if ":" in url_hostname else url_hostname.lower()
    print(f"Verified endpoint: {normalized_host}:{url_port}", file=sys.stderr)
    PY

    printf 'APP_ENV=%s\nCompose service: postgres\nDatabase target: %s\nDatabase user: %s\n' \
      "${app_env}" "${database_name}" "${database_user}" >&2
    printf 'Type the exact database name to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${database_name}" ]] || fail 'confirmation did not match database target'

    # 只有全部验证与精确确认完成后才允许 Fake/真实命令接触目标；set -e 保证迁移严格后置。
    docker compose exec -T postgres dropdb --if-exists -U "${database_user}" "${database_name}"
    docker compose exec -T postgres createdb -U "${database_user}" "${database_name}"
    just db-upgrade
