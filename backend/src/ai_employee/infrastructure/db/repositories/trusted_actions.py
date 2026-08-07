"""在 ApprovalRequest 上持久化严格验证且记录绑定的加密可信命令。"""

import json
from collections.abc import Mapping
from hmac import compare_digest
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import canonical_command_json, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, JsonValue
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, TaskRunModel
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

APPROVAL_COMMAND_CONTENT_KIND = "approval_command"


class SqlAlchemyTrustedActionRepository:
    """在调用方事务内写入和读取 ApprovalRequest 的真实 M2 命令。

    M2 路径只允许四种严格命令进入 AEAD 列，JSONB ``payload`` 始终只保存无敏感
    marker。读取按 ``schema_version`` 显式分支：``None`` 只能解释为 M1
    ``fake.write``，任何非空版本都必须具有完整 AEAD 三元组并重新通过严格命令及
    规范哈希验证。Repository 从不提交，也不会把内容异常转换成供应商错误。
    """

    def __init__(self, session: AsyncSession, cipher: ActionPayloadCipher) -> None:
        """绑定应用事务会话和 ApprovalRequest 记录级加密器。

        Args:
            session: 由上层用例负责提交或回滚的异步会话。
            cipher: 使用用户、Approval ID、动作和 Schema 构造 AAD 的加密器。
        """
        self._session = session
        self._cipher = cipher

    async def save_command(
        self,
        *,
        user_id: UUID,
        approval_id: UUID,
        command_payload: Mapping[str, object],
    ) -> dict[str, object] | None:
        """锁定用户拥有的审批并且只允许首次写入规范命令密文。

        命令先经过现有严格 Pydantic/领域边界规范化，再由现有
        ``trusted_command_hash`` 计算冻结哈希；动作和 Schema 只从规范命令中取得，
        不接受调用方提供第二套可能分叉的哈希或 AAD 元数据。精确 encrypted marker
        本身就是不可变冻结事实：完整 AEAD 存在时，同命令重放复用既有密文；保留任务
        已清除三元组时，任何命令都拒绝，不能旋转 nonce 或重新生成已丢失的冻结内容。
        首次冻结只接受 pending、完全未决定、无执行截止、空 JSONB、空 AEAD 且哈希已
        预绑定到当前规范命令的精确骨架；Repository 绝不把异常生命周期或旧哈希改写成
        新的冻结事实。

        Args:
            user_id: 当前认证用户，用于通过审批所属 TaskRun 验证所有权。
            approval_id: 待绑定真实命令的 ApprovalRequest ID。
            command_payload: 四种 M2 可信命令之一的标准 JSON object。

        Returns:
            已规范化并写入的命令副本；审批不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: 审批行预声明的 action/schema 与命令不一致。
            TypeError: 命令包含非标准 JSON Python 值。
            ValueError: 命令违反严格 Schema 或领域不变量。
        """
        canonical = _canonical_command_object(command_payload)
        action = canonical.get("action")
        schema_version = canonical.get("schema_version")
        if type(action) is not str or type(schema_version) is not str:
            # 正常严格边界不可能到达这里；保留固定失败避免未来 Schema 漏掉绑定字段。
            raise _trusted_action_unavailable()
        payload_hash = trusted_command_hash(canonical)
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                ApprovalRequestModel.id == approval_id,
                TaskRunModel.user_id == user_id,
            )
            .with_for_update()
        )
        if approval is None:
            return None
        if approval.action != action or approval.schema_version != schema_version:
            raise _trusted_action_unavailable()

        marker: dict[str, JsonValue] = {
            "storage": "encrypted",
            "schema_version": schema_version,
        }
        stored_aead = (
            approval.payload_ciphertext,
            approval.payload_nonce,
            approval.payload_key_version,
        )
        if approval.payload == marker:
            if not all(value is not None for value in stored_aead):
                # 精确 marker 即使在保留清理后也持续证明载荷已冻结，不能退回首次写入分支。
                raise _trusted_action_unavailable()
            existing = self._load_m2_command(approval=approval, user_id=user_id)
            if compare_digest(trusted_command_hash(existing), payload_hash):
                # 同命令重放只返回既有冻结事实，绝不旋转 nonce 或改写已批准载荷。
                return existing
            raise _trusted_action_unavailable()
        if (
            approval.status != ApprovalStatus.PENDING.value
            or approval.decided_at is not None
            or approval.decided_by_user_id is not None
            or approval.approved_execution_deadline_at is not None
            or approval.payload != {}
            or any(value is not None for value in stored_aead)
            or not compare_digest(approval.payload_hash, payload_hash)
        ):
            # 非精确 pending 骨架可能是已决定、被替换或旧命令事实，必须整体 fail closed。
            raise _trusted_action_unavailable()

        encrypted = self._cipher.encrypt_json(
            canonical,
            user_id=user_id,
            record_id=approval.id,
            content_kind=APPROVAL_COMMAND_CONTENT_KIND,
            action=action,
            schema_version=schema_version,
        )
        # JSONB 只留下明确存储协议 marker；地址、正文和日程字段全部只进入 AEAD 列。
        approval.payload = marker
        approval.payload_ciphertext = encrypted.ciphertext
        approval.payload_nonce = encrypted.nonce
        approval.payload_key_version = encrypted.key_version
        approval.payload_hash = payload_hash
        await self._session.flush()
        return canonical

    async def load_command(
        self,
        *,
        user_id: UUID,
        approval_id: UUID,
    ) -> dict[str, object] | None:
        """按用户读取并验证 legacy fake.write 或完整 M2 加密命令。

        Args:
            user_id: 当前认证用户，用于通过 TaskRun 显式隔离审批。
            approval_id: 待读取 ApprovalRequest ID。

        Returns:
            已复制的 legacy fake payload 或重新规范化的 M2 命令；不存在、跨用户时
            返回 ``None``。

        Raises:
            StateConflictError: marker、AEAD 列、action/schema、规范哈希或 legacy
                分支不符合冻结协议。
            cryptography.exceptions.InvalidTag: 密文或任一 AAD 维度被替换。
        """
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                ApprovalRequestModel.id == approval_id,
                TaskRunModel.user_id == user_id,
            )
        )
        if approval is None:
            return None
        if approval.schema_version is None:
            return self._load_legacy_fake_write(approval)
        return self._load_m2_command(approval=approval, user_id=user_id)

    def _load_legacy_fake_write(
        self,
        approval: ApprovalRequestModel,
    ) -> dict[str, object]:
        """只允许无 AEAD 列的 M1 fake.write 使用历史 JSONB payload。"""
        if approval.action != "fake.write" or any(
            value is not None
            for value in (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            )
        ):
            raise _trusted_action_unavailable()
        proposal = ApprovalProposal.create(approval.action, approval.payload)
        if not compare_digest(proposal.payload_hash, approval.payload_hash):
            raise _trusted_action_unavailable()
        return dict(approval.payload)

    def _load_m2_command(
        self,
        *,
        approval: ApprovalRequestModel,
        user_id: UUID,
    ) -> dict[str, object]:
        """要求精确 marker/AEAD 三元组并重新验证完整可信命令与哈希。"""
        schema_version = approval.schema_version
        if schema_version is None:
            # 调用方已经分支；这里保留局部防线，也让类型系统证明 AAD 不接收空版本。
            raise _trusted_action_unavailable()
        marker = {
            "storage": "encrypted",
            "schema_version": schema_version,
        }
        if approval.payload != marker or (
            approval.payload_ciphertext is None
            or approval.payload_nonce is None
            or approval.payload_key_version is None
        ):
            raise _trusted_action_unavailable()
        payload = self._cipher.decrypt_json(
            EncryptedValue(
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            ),
            user_id=user_id,
            record_id=approval.id,
            content_kind=APPROVAL_COMMAND_CONTENT_KIND,
            action=approval.action,
            schema_version=schema_version,
        )
        if (
            payload.get("action") != approval.action
            or payload.get("schema_version") != schema_version
        ):
            raise _trusted_action_unavailable()
        try:
            canonical = _canonical_command_object(payload)
            canonical_hash = trusted_command_hash(canonical)
        except (TypeError, ValueError):
            # 严格命令边界异常不能携带解密内容跨出 Repository，也不能变成 provider retry。
            raise _trusted_action_unavailable() from None
        if (
            canonical.get("action") != approval.action
            or canonical.get("schema_version") != schema_version
            or not compare_digest(canonical_hash, approval.payload_hash)
        ):
            raise _trusted_action_unavailable()
        return canonical


def _canonical_command_object(payload: Mapping[str, object]) -> dict[str, object]:
    """复用现有严格边界并把规范 UTF-8 JSON 解码为独立 object。"""
    decoded: object = json.loads(canonical_command_json(payload).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise _trusted_action_unavailable()
    return dict(cast(dict[str, object], decoded))


def _trusted_action_unavailable() -> StateConflictError:
    """构造不泄露命令、审批存在性或篡改细节的 fail-closed 错误。"""
    return StateConflictError(
        error_code="trusted_action_unavailable",
        message="trusted action is unavailable",
    )


__all__ = [
    "APPROVAL_COMMAND_CONTENT_KIND",
    "SqlAlchemyTrustedActionRepository",
]
