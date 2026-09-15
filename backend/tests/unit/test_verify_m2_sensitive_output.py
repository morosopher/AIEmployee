"""用独立污染样本验证静态与原始运行输出扫描，失败消息不携带敏感值。"""

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIELDS = ("address", "subject", "body", "title", "description", "location", "attendee", "token", "cookie", "prompt")


def _scanner() -> ModuleType:
    """从真实 CLI 脚本加载纯扫描函数；未实现时明确失败而不跳过。"""
    path = ROOT / "scripts/verify-m2-sensitive-output.py"
    assert path.is_file(), "sensitive output scanner is absent"
    name = "m2_sensitive_scanner_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_reviewed_synthetic_fixtures_and_public_jwks_are_allowed() -> None:
    """当前已审 fixture 与公钥不是私钥；扫描仍独立检查所有字符串值。"""
    assert _scanner().fixture_findings(ROOT) == []


@pytest.mark.parametrize("pollution", ["domain", "credential", "authorization", "cookie", "personal_key", "raw_payload", "metadata_domain"])
def test_fixture_values_and_unreviewed_payloads_are_rejected(tmp_path: Path, pollution: str) -> None:
    """修改既有 schema 的值或添加未审字段都必须拒绝，不能仅凭结构摘要放行。"""
    module = _scanner()
    shutil.copytree(ROOT / "backend/tests/contract", tmp_path / "backend/tests/contract")
    shutil.copytree(ROOT / "backend/tests/evals", tmp_path / "backend/tests/evals")
    path = tmp_path / "backend/tests/contract/google/fixtures/gmail_send_success.json"
    value = json.loads(path.read_text())
    forbidden = "Synthetic scan input"
    if pollution == "domain":
        forbidden = "synthetic-person@real-domain.com"
        value["id"] = forbidden
    elif pollution == "credential":
        forbidden = "ya29." + "A" * 45
        value["id"] = forbidden
    elif pollution in {"authorization", "cookie"}:
        forbidden = pollution.title() + ": Synthetic-private-value"
        value["id"] = forbidden
    elif pollution == "personal_key":
        value["national_id"] = forbidden
    elif pollution == "raw_payload":
        value["unreviewed_provider_response"] = {"message": "Synthetic new raw payload"}
    else:
        path = tmp_path / "backend/tests/contract/microsoft/fixtures/mail_delta_initial.json"
        value = json.loads(path.read_text())
        forbidden = "https://attacker-domain.com/v1.0/$metadata"
        value["@odata.context"] = forbidden
    path.write_text(json.dumps(value), encoding="utf-8")
    findings = module.fixture_findings(tmp_path)
    assert findings, "polluted fixture was accepted"
    assert forbidden not in repr(findings)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("encoding", ["raw", "url", "base64"])
def test_each_runtime_field_is_detected_before_any_redaction(tmp_path: Path, field: str, encoding: str) -> None:
    """十类 canary 在原文、URL 编码和 base64 中的泄漏都必须失败且不回显。"""
    module = _scanner()
    canaries = {name: f"Synthetic {name} private marker 32a8d4" for name in FIELDS}
    value = canaries[field]
    raw = {"raw": value, "url": quote(value, safe=""), "base64": base64.b64encode(value.encode()).decode()}[encoding]
    output = tmp_path / "raw-observable.log"
    output.write_text("real output prefix " + raw)
    findings = module.runtime_findings((output,), canaries)
    assert any(finding.category == "runtime_canary" and finding.field == field for finding in findings)
    assert value not in repr(findings)


@pytest.mark.parametrize("name", [".env", "secrets/master-key", "backups/private.dump", "private.dump.enc", "private.dump.enc.manifest.json", "private.dump.enc.sha256", "frontend/playwright-report/index.html", "frontend/coverage/data.json", "frontend/dist/index.html", ".superpowers/sdd/private-report.md"])
def test_staged_private_configuration_and_generated_artifacts_are_rejected(tmp_path: Path, name: str) -> None:
    """只在临时 Git 仓库建 index；产品加密备份三件套移出 backups 目录后仍须拒绝。"""
    module = _scanner()
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Synthetic private staging sentinel")
    subprocess.run(["git", "add", "--", name], cwd=tmp_path, check=True)
    assert module.staged_findings(tmp_path)


def test_staged_documentation_and_environment_example_remain_allowed(tmp_path: Path) -> None:
    """文档与无秘密的示例配置可以审查提交；禁止项不靠扩展名一刀切。"""
    module = _scanner()
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    for name in ("README.md", ".env.example"):
        (tmp_path / name).write_text("Synthetic public example")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert module.staged_findings(tmp_path) == []
