"""Microsoft Graph 委托 OAuth/OIDC 与后续邮件、日历适配器的隔离边界。"""

from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter

__all__ = ["FakeMicrosoftOAuthAdapter", "MicrosoftOAuthAdapter"]
