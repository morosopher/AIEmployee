"""提供 OIDC、读取及可信写合同共用的两类合法 Microsoft 合成身份。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class MicrosoftAccountCase:
    """绑定真实消费者 tenant 语义或合成组织 tenant 与不同形状的 Graph 稳定 ID。

    两类使用相同主邮箱以证明账户分类来自经过验签的 tenant/issuer，而非邮箱后缀；
    access 值只是本地 HTTP mock 的合成标识，不能用于任何真实账户。
    """

    account_type: Literal["personal", "work_school"]
    tenant: str
    graph_id: str

    @property
    def provider_account_id(self) -> str:
        """返回 OIDC tenant 与 Graph `/me` ID 共同构成的预期持久身份。"""
        return f"{self.tenant}:{self.graph_id}"

    @property
    def access_token(self) -> str:
        """区分两类合同请求归属的不可用于真实服务的合成字符串。"""
        return f"synthetic-{self.account_type}-contract-access"


MICROSOFT_ACCOUNT_CASES = (
    MicrosoftAccountCase("personal", "9188040d-6c67-4c5b-b112-36a304b66dad", "0000000000000701"),
    MicrosoftAccountCase(
        "work_school",
        "11111111-2222-3333-4444-555555555555",
        "00000000-0000-0000-0000-000000000701",
    ),
)
