"""验证正文保留策略所需的 ORM 空值契约。"""

from ai_employee.infrastructure.db.models.sources import EmailMessageModel


def test_expired_email_body_encryption_triplet_can_be_cleared() -> None:
    """保留任务需要原子清空三元组，因此所有列必须允许 SQL NULL。"""
    table = EmailMessageModel.__table__

    assert table.c.body_ciphertext.nullable is True
    assert table.c.body_nonce.nullable is True
    assert table.c.body_key_version.nullable is True
