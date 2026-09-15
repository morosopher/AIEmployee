#!/usr/bin/env python3
"""扫描已审合成 fixture，并以十类随机 canary 验证真实运行输出零泄漏。

静态 schema 与值检查相互独立。动态阶段直接保存子进程、浏览器、指标与真实 tracing
exporter 的原始字节，先扫描再生成不含 canary 的报告；不会读取控制器脱敏日志。
输入及原始输出保留在独立 0700 临时目录，文件默认 0600，供审查者复核。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

ROOT = Path(__file__).resolve().parent.parent
CANARY_FIELDS = ("address", "subject", "body", "title", "description", "location", "attendee", "token", "cookie", "prompt")

# 只冻结经审查的键、类型与数组成员形状；值改变仍须逐项扫描，新增 schema 必须代码审查。
FIXTURE_SCHEMAS = {
    "backend/tests/contract/fixtures/calendar_incremental.json": "696c51f2680f099cd2a3f969e12935f9060abb35416debfacfde7d882ea85060",
    "backend/tests/contract/fixtures/calendar_initial.json": "a31dfbb44b95ba2034d5f4b03b88e66e7dd1c9287b1ebab85f67043d5f99e24b",
    "backend/tests/contract/fixtures/gmail_history.json": "43257e9612355bdbb422345bd5d1ed716465b462190dc11b8dcc36c501e1dd3a",
    "backend/tests/contract/fixtures/gmail_initial.json": "9ebbe9ba735315b85d8e2455ca30e869adb604f4c5cac9406294ed1f41015d90",
    "backend/tests/contract/fixtures/google_calendar_list.json": "3eda33a1632375ccfd20f32ed74b9725da16990646219ad26f3cc021444ec4b0",
    "backend/tests/contract/fixtures/task_step_events.json": "66855de3744d93a4984c74f6fdf7f5bf9d7fe7daa255611d5c0b27d95ce2b49b",
    "backend/tests/contract/google/fixtures/calendar_event_write.json": "aa0d9e5aea95ad5a7cc6536589fee681a6af39930c1bf4bec9a16dd2c0aa72fa",
    "backend/tests/contract/google/fixtures/gmail_send_success.json": "65e95b6d4e2ba7838f93d9dc82d652c227f0cfe3522c724387d981a8a68e1de0",
    "backend/tests/contract/google/fixtures/gmail_sent_search.json": "477c970990684aca2747ee6d4fef87bbc8825f8a52e254ef45ba059283e36817",
    "backend/tests/contract/microsoft/fixtures/calendar_event_write.json": "393f78331d4edb97a8920d3f4b37a6a4c217b07e47431d092bdc87df1d78c4e5",
    "backend/tests/contract/microsoft/fixtures/calendar_view_delta_incremental.json": "c6a22317acbc2538385cce7f906053d794014b8234acb600d987f24ad3df31d3",
    "backend/tests/contract/microsoft/fixtures/calendar_view_delta_initial.json": "fc3703376d6f33500e3c4b190c29b3a7f23198e9025049850dafbad22bc03200",
    "backend/tests/contract/microsoft/fixtures/calendars.json": "414770568c1c88fbea5bc3d9374e30d939f35cc55a48e1bcdf3c9b57f65b5df9",
    "backend/tests/contract/microsoft/fixtures/jwks.json": "2aff96118d24ecc93f0329de3b7cf48332c0cc696e337b450d44801e22d7be43",
    "backend/tests/contract/microsoft/fixtures/mail_delta_incremental.json": "1c462b4f4ff7abace4b1e6119b5d2b0e60f1f66bafa62cf0ab4fe0e2cbded264",
    "backend/tests/contract/microsoft/fixtures/mail_delta_initial.json": "e7f573dda957bdfc7de85f661776109574934c5973ccb9f425e051d2690c841a",
    # Graph v1.0 目录仅保留已审合成 id/displayName；删除不存在的 wellKnownName 属性。
    "backend/tests/contract/microsoft/fixtures/mail_folders.json": "463d25b3825735fb4715f95ec9750256b7bb736c72126be1733684a981bc5720",
    "backend/tests/contract/microsoft/fixtures/openid_configuration.json": "66c548bc952d07c7860ec2ce27f43c2087ecf77f4f0b778e2bc36e9554d4a130",
    "backend/tests/contract/microsoft/fixtures/sent_message.json": "2faaee7b7c22c606ed803e7bfa98eee1b98e08d3f12f9aaaf248dd6f771b5014",
    "backend/tests/evals/daily_brief_cases.json": "67514e1c4ce06b6d786dfe9258f9053f8c28d29374446078148545c9e2ae1597",
    "backend/tests/evals/mail_draft_cases.json": "881712ec3cb5dc8ca6fdc2609f77708793f710737e3879f62d5ab344f6921a7c",
}
_FORBIDDEN_KEYS = {"accesstoken", "refreshtoken", "clientsecret", "authorization", "cookie", "setcookie", "password", "privatekey", "nationalid", "passport", "passportnumber", "ssn", "socialsecuritynumber", "bankaccount", "creditcard"}
_CREDENTIAL = re.compile(r"(?:ya29\.[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:Authorization|Cookie|Set-Cookie)\s*:)" , re.IGNORECASE)
_DOMAIN = re.compile(r"(?i)(?:[a-z0-9-]+\.)+[a-z]{2,63}\b")
_EMAIL = re.compile(r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")


@dataclass(frozen=True, slots=True)
class Finding:
    """只报告类别、文件和字段类别；匹配到的字节与值绝不进入 repr 或终端。"""

    category: str
    path: str
    field: str = ""


def _shape(value: object) -> object:
    """剥离所有具体值后生成稳定 schema；数组允许相同已审结构重复。"""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return sorted({json.dumps(_shape(item), sort_keys=True, separators=(",", ":")) for item in value})
    return type(value).__name__


def _metadata_domain_allowed(path: str, keys: tuple[str, ...], value: str, domain: str) -> bool:
    """仅放行原 Graph/OIDC 元数据精确字段与官方 HTTPS 主机，邮件字段无例外。"""
    if len(keys) != 1 or not path.startswith("backend/tests/contract/microsoft/fixtures/"):
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != domain or parsed.username or parsed.password:
        return False
    if keys[0] in {"@odata.context", "@odata.nextLink", "@odata.deltaLink"}:
        return domain == "graph.microsoft.com"
    return path.endswith("/openid_configuration.json") and keys[0] in {
        "issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"
    } and domain == "login.microsoftonline.com"


def _value_findings(value: object, path: str, keys: tuple[str, ...] = ()) -> list[Finding]:
    """递归检查值和禁止键；schema 摘要通过也不能豁免凭据或个人数据检查。"""
    findings: list[Finding] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z]", "", str(key).lower()) in _FORBIDDEN_KEYS:
                findings.append(Finding("forbidden_personal_or_credential_key", path))
            findings.extend(_value_findings(item, path, (*keys, str(key))))
        if str(value.get("name", "")).lower() in {"authorization", "cookie", "set-cookie"}:
            findings.append(Finding("credential_header", path))
    elif isinstance(value, list):
        for item in value:
            findings.extend(_value_findings(item, path, (*keys, "*")))
    elif isinstance(value, str):
        if _CREDENTIAL.search(value):
            findings.append(Finding("credential_format", path))
        domains = set(_DOMAIN.findall(value)) | set(_EMAIL.findall(value))
        for candidate in re.findall(r"https?://[^\s<>\"]+", value):
            host = urlsplit(candidate).hostname
            if host:
                domains.add(host)
        for domain in domains:
            domain = domain.lower()
            # RFC 2606 的 .test/.example/.invalid 都是保留合成域；两种 SSE 事件名则
            # 仅在既有精确 event 字段放行，不把整个 dotted 字符串类别误认为可公开数据。
            internal_event = path == "backend/tests/contract/fixtures/task_step_events.json" and keys == ("*", "event") and value in {"step.started", "step.completed"}
            if not domain.endswith((".test", ".example", ".invalid")) and not internal_event and not _metadata_domain_allowed(path, keys, value, domain):
                findings.append(Finding("real_domain", path))
        if keys[-2:] == ("body", "data") and value:
            # Gmail 的已审 fixture 含最小 base64url 正文；编码不能成为静态检查旁路。
            try:
                decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                findings.append(Finding("invalid_fixture_encoding", path))
            else:
                findings.extend(_value_findings(decoded, path, (*keys, "decoded")))
    return findings


def fixture_findings(root: Path) -> list[Finding]:
    """扫描当前候选全部 JSON fixture/eval，缺失、新文件或未知结构一律要求审查。"""
    findings: list[Finding] = []
    paths = {path.relative_to(root).as_posix(): path for path in (root / "backend/tests").rglob("*.json")}
    for relative in sorted(set(FIXTURE_SCHEMAS) | set(paths)):
        path = paths.get(relative)
        if path is None:
            findings.append(Finding("reviewed_fixture_missing", relative))
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError):
            findings.append(Finding("invalid_fixture_json", relative))
            continue
        digest = hashlib.sha256(json.dumps(_shape(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != FIXTURE_SCHEMAS.get(relative):
            findings.append(Finding("unreviewed_fixture_schema", relative))
        findings.extend(_value_findings(value, relative))
    return findings


def staged_findings(root: Path) -> list[Finding]:
    """只读当前 index 的文件名，拒绝私有配置、转储和生成物；不修改暂存区。"""
    result = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root, check=True, capture_output=True)
    findings: list[Finding] = []
    forbidden_parts = {"secrets", ".secrets", "backups", "playwright-report", "test-results", "coverage", "htmlcov", "dist", ".superpowers", "__pycache__", "node_modules"}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(os.fsdecode(raw))
        name = path.name.lower()
        # 运维产物可以被复制到任意目录；三件套的复合后缀不能只按最后的 .enc/.json 判断。
        encrypted_backup = name.endswith((".dump.enc", ".dump.enc.manifest.json", ".dump.enc.sha256"))
        if ((name == ".env" or name.startswith(".env.")) and name != ".env.example") or forbidden_parts.intersection(path.parts) or path.suffix.lower() in {".dump", ".backup", ".pem", ".key", ".p12", ".pfx"} or name.startswith(".coverage") or encrypted_backup:
            findings.append(Finding("staged_private_or_generated_artifact", path.as_posix()))
    return findings


def runtime_findings(paths: Sequence[Path], canaries: Mapping[str, str]) -> list[Finding]:
    """在原始字节上查找十类标记及常用传输编码；无脱敏、替换或输出正文步骤。"""
    findings: list[Finding] = []
    for path in paths:
        raw = path.read_bytes()
        for field, value in canaries.items():
            variants = {value.encode(), quote(value, safe="").encode(), quote_plus(value, safe="").encode(), json.dumps(value)[1:-1].encode(), base64.b64encode(value.encode()), base64.urlsafe_b64encode(value.encode()).rstrip(b"=")}
            if any(variant in raw for variant in variants):
                findings.append(Finding("runtime_canary", path.name, field))
    return findings


def _private_write(path: Path, data: bytes) -> None:
    """一次性创建 0600 文件；旧输入、旧报告和失败证据都不能覆盖。"""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)


def _playwright_config(root: Path, raw: Path) -> str:
    """继承真实浏览器配置；服务由同一 supervisor 拥有，Playwright 不另开 shell 服务。"""
    config = {
        "testDir": str(root / "frontend/e2e"), "testMatch": "m2-sensitive-output.flow.ts",
        "outputDir": str(raw / "browser-results"), "retries": 0, "workers": 1, "webServer": [],
        "reporter": [["list"], ["junit", {"outputFile": str(raw / "junit.xml")}], ["json", {"outputFile": str(raw / "playwright.json")}]],
    }
    return (
        f"import base from {json.dumps(str(root / 'frontend/playwright.config.ts'))}\n"
        f"const raw = {json.dumps(config)}\n"
        "export default { ...base, ...raw, use: { ...base.use, trace: 'off', screenshot: 'off', "
        "launchOptions: { env: { ...process.env, ...(process.env.M2_E2E_BROWSER_LIBRARY_PATH ? "
        "{ LD_LIBRARY_PATH: process.env.M2_E2E_BROWSER_LIBRARY_PATH } : {}) } } } }\n"
    )


def _run_browser(root: Path, raw: Path, input_path: Path) -> int:
    """等待拥有全部原始输出生产者的 supervisor；超时仍请求其完成自有资源清理。"""
    fd, name = tempfile.mkstemp(prefix=".m2-sensitive-", suffix=".config.ts", dir=root / "frontend")
    config_path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(_playwright_config(root, raw))
    environment = dict(os.environ, M2_SENSITIVE_OUTPUT_DIRECTORY=str(raw), M2_SENSITIVE_INPUT_FILE=str(input_path), M2_SENSITIVE_PLAYWRIGHT_CONFIG=str(config_path), METRICS_ENABLED="true", OTEL_ENABLED="false")
    try:
        with (raw / "supervisor.stdout.log").open("xb") as stdout, (raw / "supervisor.stderr.log").open("xb") as stderr:
            process = subprocess.Popen(["uv", "run", "--project", "backend", "python", "scripts/run_e2e_backend.py", "--sensitive-output"], cwd=root, env=environment, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                return process.wait(timeout=240)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=55)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                return 124
    finally:
        config_path.unlink()


def _runtime_channel_findings(raw: Path) -> list[Finding]:
    """要求字段到达回执、真实日志事件、非空指标及 HTTP/SQL span，禁止空文件绿灯。"""
    findings: list[Finding] = []
    required = ("services.stdout.log", "services.stderr.log", "web.stdout.log", "web.stderr.log", "playwright.stdout.log", "playwright.stderr.log", "browser.jsonl", "junit.xml", "playwright.json", "metrics.txt", "traces.jsonl", "reached.json", "producers-stopped.json")
    if any(not (raw / name).is_file() for name in required):
        return [Finding("runtime_channel_missing", "runtime")]
    stopped = json.loads((raw / "producers-stopped.json").read_text())
    if stopped.get("code") != 0 or {item.get("role") for item in stopped.get("producers", [])} != {"backend", "web", "playwright"} or not all(item.get("group_exited") is True for item in stopped["producers"]):
        findings.append(Finding("runtime_producer_exit_unproved", "producers-stopped.json"))
    reached = json.loads((raw / "reached.json").read_text())
    if reached != {field: True for field in CANARY_FIELDS}:
        findings.append(Finding("runtime_input_not_reached", "reached.json"))
    logs = b"\n".join((raw / name).read_bytes() for name in ("services.stdout.log", "services.stderr.log"))
    if b"E2E owned services stopped; disposable database cleaned; anchor unchanged" not in logs:
        findings.append(Finding("runtime_database_cleanup_unproved", "services"))
    if b'"event": "ai_employee.oauth.authorization_failed"' not in logs:
        findings.append(Finding("runtime_oauth_log_missing", "services"))
    if b"# HELP" not in (raw / "metrics.txt").read_bytes():
        findings.append(Finding("runtime_metrics_empty", "metrics.txt"))
    spans = [json.loads(line) for line in (raw / "traces.jsonl").read_text().splitlines() if line]
    if not any(span.get("kind") == "SpanKind.SERVER" for span in spans) or not any("db.statement" in span.get("attributes", {}) for span in spans):
        findings.append(Finding("runtime_http_or_sql_trace_missing", "traces.jsonl"))
    return findings


def main() -> int:
    """先执行静态/index 门禁，再执行真实 canary 流程并输出仅含指纹的审查入口。"""
    findings = fixture_findings(ROOT) + staged_findings(ROOT)
    if findings:
        print(json.dumps({"status": "failed", "phase": "static", "findings": [asdict(item) for item in findings]}))
        return 1
    os.umask(0o077)
    private = Path(tempfile.mkdtemp(prefix="ai-employee-m2-sensitive-"))
    raw = private / "raw"
    raw.mkdir(mode=0o700)
    canaries = {field: f"Synthetic M2 {field} {secrets.token_hex(24)}" for field in CANARY_FIELDS}
    for field in ("address", "attendee"):
        canaries[field] = f"m2-{secrets.token_hex(24)}@example.test"
    for field in ("token", "cookie"):
        canaries[field] = f"m2-{field}-{secrets.token_hex(24)}"
    input_path = private / "input.json"
    _private_write(input_path, json.dumps(canaries).encode())
    code = _run_browser(ROOT, raw, input_path)
    paths = tuple(sorted(path for path in raw.rglob("*") if path.is_file()))
    # 此处第一次读取的是进程直接产生的原始字节；后续任何错误报告都只含字段类别。
    findings = runtime_findings(paths, canaries)
    try:
        findings.extend(_runtime_channel_findings(raw))
    except (ValueError, OSError):
        findings.append(Finding("runtime_channel_malformed", "runtime"))
    if code:
        findings.append(Finding("runtime_process_failed", "playwright"))
    report = {
        "status": "failed" if findings else "passed", "runtime_exit_code": code,
        "fields": list(CANARY_FIELDS), "fixture_count": len(FIXTURE_SCHEMAS),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "raw_outputs": [{"path": str(path.relative_to(private)), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
        "findings": [asdict(item) for item in findings],
    }
    report_path = private / "report.json"
    _private_write(report_path, json.dumps(report, sort_keys=True, indent=2).encode())
    print(json.dumps({"status": report["status"], "phase": "runtime", "report": str(report_path), "raw_output_count": len(paths), "finding_count": len(findings)}))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
