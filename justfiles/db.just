# 数据库 recipe 统一从根目录运行，确保 Alembic 配置路径和项目依赖解析稳定。
[group('database')]
db-upgrade:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${DATABASE_URL:?DATABASE_URL is required}"
    : "${POSTGRES_BOOTSTRAP_PASSWORD_FILE:?POSTGRES_BOOTSTRAP_PASSWORD_FILE is required}"

    # 应用 DATABASE_URL 在本地开发可能仍含 app/owner 密码；lifecycle 子进程只继承同一
    # endpoint 的 passwordless URL，owner Secret 始终从显式文件读取。
    lifecycle_url="$(AI_EMPLOYEE_LIFECYCLE_URL="${DATABASE_URL}" python3 - <<'PY'
    import os
    import re
    from urllib.parse import quote, unquote, urlsplit


    def strict_unquote_url_component(value: str) -> str:
        """拒绝 malformed percent triplet，并且只执行一次严格 UTF-8 解码。"""

        if re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None:
            raise ValueError("malformed percent triplet")
        return unquote(value, encoding="utf-8", errors="strict")


    value = os.environ["AI_EMPLOYEE_LIFECYCLE_URL"]
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SystemExit("db-upgrade refused: DATABASE_URL is invalid")
    if not value.startswith(("postgresql://", "postgresql+asyncpg://", "postgresql+psycopg://")):
        raise SystemExit("db-upgrade refused: DATABASE_URL is invalid")
    if "?" in value or "#" in value:
        raise SystemExit("db-upgrade refused: DATABASE_URL is invalid")
    try:
        parsed = urlsplit(value)
        encoded_username = parsed.username
        hostname = parsed.hostname
        port = parsed.port
        username = (
            strict_unquote_url_component(encoded_username)
            if encoded_username is not None
            else None
        )
        # URL path 是传输编码，不是 PostgreSQL identity；strict unquote 后才可校验
        # quoted database name 的真实 UTF-8 字节数，且不得 normalization 或截断。
        database_name = strict_unquote_url_component(parsed.path.removeprefix("/"))
        database_name_utf8 = database_name.encode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        raise SystemExit("db-upgrade refused: DATABASE_URL is invalid") from None
    if (
        username is None
        or hostname is None
        or hostname == ""
        or port is None
        or not 1 <= port <= 65535
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", username) is None
        or len(username.encode("utf-8")) > 63
        or database_name == ""
        or "\0" in database_name
        or not 1 <= len(database_name_utf8) <= 63
    ):
        raise SystemExit("db-upgrade refused: DATABASE_URL is invalid")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    # lifecycle URL 只携带同一 decoded identity 的规范 percent-encoding；原密码及原始
    # encoding 形式均不继续传播，encoded delimiter 也不会变成 query/fragment 覆盖项。
    canonical_username = quote(username, safe="")
    canonical_database_path = quote(database_name, safe="")
    print(
        f"postgresql+psycopg://{canonical_username}@{normalized_host}:{port}/"
        f"{canonical_database_path}"
    )
    PY
    )"
    env -u PGHOST -u PGHOSTADDR -u PGPORT -u PGDATABASE -u PGUSER \
      -u PGSERVICE -u PGSERVICEFILE DATABASE_URL="${lifecycle_url}" \
      uv run --project backend python -m ai_employee.cli.database_maintenance migrate \
      --owner-password-file "${POSTGRES_BOOTSTRAP_PASSWORD_FILE}"

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
    owner_password_file="${POSTGRES_BOOTSTRAP_PASSWORD_FILE-}"
    app_password_file="${APP_DATABASE_PASSWORD_FILE-}"
    retention_password_file="${RETENTION_DATABASE_PASSWORD_FILE-}"
    [[ -n "${database_name}" ]] || fail 'POSTGRES_DB is required'
    [[ -n "${database_user}" ]] || fail 'POSTGRES_USER is required'
    [[ -n "${database_url}" ]] || fail 'DATABASE_URL is required'
    [[ -n "${owner_password_file}" ]] || fail 'POSTGRES_BOOTSTRAP_PASSWORD_FILE is required'
    [[ -n "${app_password_file}" ]] || fail 'APP_DATABASE_PASSWORD_FILE is required'
    [[ -n "${retention_password_file}" ]] || fail 'RETENTION_DATABASE_PASSWORD_FILE is required'

    # 使用 Python 标准库解析 URL，避免 Bash 字符串切片漏掉 username、host 或 port。
    # 敏感 URL 通过临时环境变量传入，不作为参数或错误消息输出；脚本只打印静态拒绝原因。
    lifecycle_url="$(AI_EMPLOYEE_DB_RESET_ENV="${app_env}" \
    AI_EMPLOYEE_DB_RESET_NAME="${database_name}" \
    AI_EMPLOYEE_DB_RESET_USER="${database_user}" \
    AI_EMPLOYEE_DB_RESET_URL="${database_url}" \
    python3 - <<'PY'
    import os
    import re
    import sys
    from typing import NoReturn
    from urllib.parse import quote, unquote, urlsplit


    def reject(reason: str) -> NoReturn:
        """以不泄露 URL、密码或完整标识符的方式终止数据库目标校验。"""

        print(f"db-reset refused: {reason}", file=sys.stderr)
        raise SystemExit(1)


    def strict_unquote_url_component(value: str) -> str:
        """拒绝 malformed percent triplet，并且只执行一次严格 UTF-8 解码。"""

        if re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None:
            raise ValueError("malformed percent triplet")
        return unquote(value, encoding="utf-8", errors="strict")


    app_env = os.environ["AI_EMPLOYEE_DB_RESET_ENV"]
    database_name = os.environ["AI_EMPLOYEE_DB_RESET_NAME"]
    database_user = os.environ["AI_EMPLOYEE_DB_RESET_USER"]
    database_url = os.environ["AI_EMPLOYEE_DB_RESET_URL"]


    def validate_role_identifier(label: str, value: str) -> None:
        """验证未加引号 runtime/owner role 的 ASCII 语法和 63 字节上限。"""

        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            reject(f"{label} must match [A-Za-z_][A-Za-z0-9_]*")
        if len(value.encode("utf-8")) > 63:
            reject(f"{label} must be at most 63 UTF-8 bytes")


    def validate_database_name(value: str) -> None:
        """验证 quoted database name 的严格 UTF-8 字节边界并保持原始 code points。"""

        if value == "" or "\0" in value:
            reject("POSTGRES_DB must be non-empty and contain no NUL")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            reject("POSTGRES_DB must be valid UTF-8")
        if not 1 <= len(encoded) <= 63:
            reject("POSTGRES_DB must be at most 63 UTF-8 bytes")


    validate_database_name(database_name)
    validate_role_identifier("POSTGRES_USER", database_user)
    if database_name in {"postgres", "template0", "template1"}:
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
        if url_username is not None:
            strict_unquote_url_component(url_username)
        url_database_name = strict_unquote_url_component(parsed_url.path.removeprefix("/"))
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
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
    if url_database_name != database_name:
        reject("DATABASE_URL database must exactly match POSTGRES_DB")

    # 仅输出完成全部校验后的规范化端点，不回显密码、query、fragment 或完整 URL。
    normalized_host = f"[{url_hostname.lower()}]" if ":" in url_hostname else url_hostname.lower()
    print(f"Verified endpoint: {normalized_host}:{url_port}", file=sys.stderr)
    canonical_database_path = quote(database_name, safe="")
    print(
        f"postgresql+psycopg://{database_user}@{normalized_host}:{url_port}/"
        f"{canonical_database_path}"
    )
    PY
    )"

    printf 'APP_ENV=%s\nCompose service: postgres\nDatabase target: %s\nDatabase user: %s\n' \
      "${app_env}" "${database_name}" "${database_user}" >&2
    printf 'Type the exact database name to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${database_name}" ]] || fail 'confirmation did not match database target'

    # 只有全部验证与逐字节确认完成后，单一 typed CLI 才能在同一 management lease 内执行
    # authority→drop/create→post-create bootstrap→ordinary migrate；recipe 没有 destructive SQL。
    env -u PGHOST -u PGHOSTADDR -u PGPORT -u PGDATABASE -u PGUSER \
      -u PGSERVICE -u PGSERVICEFILE DATABASE_URL="${lifecycle_url}" \
      uv run --project backend python -m ai_employee.cli.database_maintenance db-reset \
      --owner-password-file "${owner_password_file}" \
      --app-password-file "${app_password_file}" \
      --retention-password-file "${retention_password_file}" \
      --app-env "${app_env}" \
      --confirmed-database-name "${confirmation}"
