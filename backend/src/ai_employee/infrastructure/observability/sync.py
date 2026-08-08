"""把供应商同步结果收敛为不含个人数据的 Prometheus 指标。"""

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel, SyncCursorModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics


async def observe_google_sync[ResultT](
    *,
    metrics: Metrics | None,
    resource: str,
    operation: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    """执行一次已认证的 Google 同步并记录聚合结果。

    Args:
        metrics: 当前 Worker 的进程级指标；禁用指标时为 ``None``。
        resource: 受控的 ``gmail`` 或 ``calendar`` 资源名称。
        operation: 不接收任何观测参数的实际同步协程。

    Returns:
        原样返回同步操作的结果。

    Raises:
        TransientProviderError: 记录稳定错误码后原样抛出，供耐久 Runner 安排重试。
        UserActionRequiredError: 记录稳定错误码后原样抛出，供上层提示重新授权。

    成功路径只将新鲜度重置为零。此处不能附带用户、连接、任务载荷或供应商响应，避免把
    个人数据引入 Prometheus 标签。新鲜度在下一次成功同步前由该 Gauge 的最后样本表示。
    """
    return await observe_provider_sync(
        provider="google",
        metrics=metrics,
        resource=resource,
        operation=operation,
    )


async def observe_provider_sync[ResultT](
    *,
    provider: str,
    metrics: Metrics | None,
    resource: str,
    operation: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    """执行任一已支持供应商同步并以真实 provider 标签记录指标。

    Args:
        provider: 固定的 google 或 microsoft provider 键；禁止使用用户输入。
        metrics: 当前 Worker 的进程级指标；禁用指标时为 None。
        resource: 受控的 mail 或 calendar 资源名称。
        operation: 不接收观测参数的实际同步协程。
    """
    if provider not in {"google", "microsoft"}:
        raise ValueError("provider is unsupported")
    try:
        result = await operation()
    except (TransientProviderError, UserActionRequiredError) as error:
        if metrics is not None:
            metrics.record_provider_error(provider=provider, error_code=error.error_code)
        raise
    if metrics is not None:
        metrics.record_sync_success(provider=provider, resource=resource)
    return result


async def refresh_sync_age_metrics(
    *, session_factory: ManagedAsyncSessionMaker, metrics: Metrics, now: datetime
) -> None:
    """从持久 ``last_success_at`` 恢复每种 Google 资源的最旧同步年龄。

    API 与 Worker 重启后内存指标为空，此只读探针以 PostgreSQL 事实重建 Gauge。同步失败只会
    更新 ``last_attempt_at`` 或错误码而不会触及 ``last_success_at``，故该查询不会虚假降低
    新鲜度。断开的连接不再代表可用的数据源，也不会输出为当前健康样本。
    """
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(SyncCursorModel.resource_kind, func.min(SyncCursorModel.last_success_at))
                    .join(
                        OAuthConnectionModel,
                        OAuthConnectionModel.id == SyncCursorModel.connection_id,
                    )
                    .where(
                        OAuthConnectionModel.provider == "google",
                        OAuthConnectionModel.status == "connected",
                        SyncCursorModel.last_success_at.is_not(None),
                    )
                    .group_by(SyncCursorModel.resource_kind)
                )
            ).all()
    except SQLAlchemyError:
        # 指标探测不能改变主流程错误语义；短暂数据库故障只缺少当前采样。
        return
    for resource, last_success_at in rows:
        if last_success_at is not None:
            metrics.record_sync_age(
                provider="google",
                resource=str(resource),
                seconds=(now - last_success_at).total_seconds(),
            )
