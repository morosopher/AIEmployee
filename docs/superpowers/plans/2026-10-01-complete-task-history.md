# 完整任务历史列表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 PostgreSQL 提供用户隔离、用途分类、双向分页的真实任务历史，并接入 M2.1 Task 7，保留现有可信任务详情与动作。

**Architecture:** 新增只读摘要查询用例、Repository 和专用签名游标；前端新增独立列表客户端及状态，不将摘要写入完整 Task Store。必要索引通过原 Alembic/schema lifecycle 路径发布；此计划 Task 8 同时完成 M2.1 Task 7，不做两次页面迁移。

**Tech Stack:** Python 3.12、SQLAlchemy 2、PostgreSQL、Alembic、FastAPI、现有 cryptography、Vue 3、TypeScript strict、PrimeVue 4.5.5、Tailwind 4、Vitest、Testing Library、Playwright、uv、pnpm、just。不增加依赖或新 Secret 配置。

**Spec:** `docs/superpowers/specs/2026-10-01-complete-task-history-design.md`（用户已复核确认）。

## Global Constraints

- “完整”指可遍历当前用户保留中的真实 TaskRun 记录，不是浏览器曾访问的任务缓存，不包括没有 TaskRun 的内部队列作业，也不恢复已删除的数据。
- 默认“业务任务”，可切换“全部任务”。创建时间倒序，每页默认 20 条，上一页／下一页；并列时间使用任务 ID 确定顺序。
- 排序固定 `(created_at DESC, id DESC)`，seek 比较使用完整精度时间和 UUID。
- 所有查询强制注入当前认证用户；游标不能替代认证，输入／结果载荷、步骤、租约、幂等键和供应商身份不进入摘要。
- 游标最长 2048 字符，有效期 24 小时，签发时间超前超过 60 秒拒绝；HMAC-SHA256，HKDF-SHA256 用途 `ai-employee/task-history-cursor/v1`。
- 列表响应 `Cache-Control: no-store`。用户日期按当前 IANA 时区转换 UTC 半开区间，不按宿主时区或固定 24 小时计算。
- 列表仅在任务历史页挂载且文档可见时每 30 秒检查同一筛选的最新一页。同一时间最多一次探测；隐藏、卸载时停止，失败不快速重试。
- 列表不会覆盖 TaskSnapshot、steps、event_cursor 或 SSE 游标。详情只使用现有 getTask/useTaskEvents；同一任务不因列表新增第二条订阅。
- 禁止修改任务创建事务、TaskRun 状态机、Worker、Scheduler、Taskiq 投递、Outbox、Checkpoint、清理器、真实供应商调用、审批／幂等规则及现有 SSE 协议。
- 不增加 origin 字段、每行 SSE、批量动作、全文搜索、导出、永久归档、真实账户调用或 M2.2 运维功能。
- 逐任务串行，单元／集成测试先红后绿；每次提交前 `git diff --check` 和 `just check`。单元通过不能替代 PostgreSQL／浏览器证据。
- 注释使用详细中文原生 Docstring/TSDoc；所有 fixture 合成；不要输出 Secret、DSN、Cookie、正文或原始 cursor。
- 本计划的 Task N 与 M2.1 Task N 不是同一编号。唯一桥接点是本计划 Task 8＝原 M2.1 Task 7；完成本计划后续跑 M2.1 Task 8 前置确认修复及 Task 8～17。

## 文件地图与任务归属

| 职责 | 路径 | 任务 |
|---|---|---|
| 摘要／查询／分页值类型、分类和过滤 | `backend/src/ai_employee/application/task_history.py` | 1 |
| IANA 民用日期边界 | `backend/src/ai_employee/application/task_history_dates.py` | 1 |
| 用途隔离的游标签名 | `backend/src/ai_employee/infrastructure/security/task_history_cursor.py` | 2 |
| 索引及发布目录兼容 | `backend/migrations/versions/20261001_0020_task_history_index.py`、现有 tasks ORM、database_grants、env.py | 3 |
| 只读数据库摘要视图 | `backend/src/ai_employee/infrastructure/db/repositories/task_history_views.py` | 4 |
| 查询端口／游标端口／用例 | `backend/src/ai_employee/application/use_cases/task_history.py` | 4 |
| API／依赖注入 | `backend/src/ai_employee/api/routers/tasks.py`、`api/deps.py` | 5 |
| 前端摘要客户端及 fixture | `frontend/src/api/taskHistory.ts`、`frontend/src/test-support/taskHistoryFixtures.ts` | 6 |
| 路由查询适配／列表生命周期 | `frontend/src/features/tasks/historyRoute.ts`、`useTaskHistory.ts` | 7 |
| 历史筛选、行列表、详情组合 | `frontend/src/components/TaskHistoryList.vue`、`pages/TasksPage.vue`、`components/TaskTimeline.vue` | 8 |
| 综合浏览器／迁移／敏感字段门禁和交接证据 | `frontend/e2e/task-history.spec.ts`、任务历史验收记录 | 9 |

测试与被测模块对应，以下各任务列明精确路径。现有 `repositories/task_history.py` 是保留清理器，不修改。现有 `stores/tasks.ts`、`useTaskEvents.ts`、认证守卫、`task_views.py` 写操作保持只读。

## 开发与测试环境

工作目录均为仓库根目录。使用现有 Fake／合成 fixture 和受管临时 PostgreSQL；集成测试新模块复用 `cycle5_regular_database_url` 与 `cycle5_tracked_session_factories`，不要直接迁移共享 anchor。

- 单元：`uv run --project backend pytest <本任务文件> -q`。
- 集成：先在已配置的隔离环境 `source ~/.cache/ai-employee-m21-e2e/env.sh`，再运行聚焦 pytest；同一 anchor 同时只运行一个集成／E2E 任务。
- 浏览器：`bash ~/.cache/ai-employee-m21-e2e/run-e2e-netns.sh <spec>`；pnpm 11 的 spec 参数前不加 `--`。该外部脚本只用于本地环境，不加入仓库命令体系。
- 完整验证仍执行根 `just check`、`just ci`，不得以拆开的成功片段冒充完整 CI；容器启动无输出时观察同一个句柄，不能直接重启。
- 所有输出留在本计划自己的 `.superpowers/sdd/2026-10-01-complete-task-history/`，不提交日志、截图、缓存或 Secret。

## 共享接口约定

Task 1 定义以下不可变 dataclass；后续任务不得改名或使用无约束 dict 代替。

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID
from ai_employee.domain.tasks import TaskStatus

HistoryScope = Literal['business', 'all', 'background']
HistoryCategory = Literal['business', 'background', 'other']
HistoryDirection = Literal['older', 'newer']

@dataclass(frozen=True, slots=True, order=True)
class HistoryKey:
    created_at: datetime
    task_id: UUID

@dataclass(frozen=True, slots=True)
class TaskHistoryQuery:
    scope: str = 'business'
    kind: str | None = None
    status: str | None = None
    created_from_date: str | None = None
    created_to_date: str | None = None
    limit: int = 20
    cursor: str | None = None

@dataclass(frozen=True, slots=True)
class TaskHistoryFilters:
    user_id: UUID
    scope: HistoryScope
    kind: str | None
    status: TaskStatus | None
    created_from: datetime | None
    created_before: datetime | None
    timezone: str
    limit: int

@dataclass(frozen=True, slots=True)
class TaskHistoryItem:
    id: UUID
    kind: str
    category: HistoryCategory
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    retry_of_task_id: UUID | None

@dataclass(frozen=True, slots=True)
class HistoryCursorState:
    user_id: UUID
    filters_hash: str
    direction: HistoryDirection
    upper: HistoryKey
    anchor: HistoryKey
    issued_at: datetime

@dataclass(frozen=True, slots=True)
class HistoryReadPage:
    items: tuple[TaskHistoryItem, ...]
    upper: HistoryKey | None
    next_anchor: HistoryKey | None
    previous_anchor: HistoryKey | None
    background_failed_count: int

@dataclass(frozen=True, slots=True)
class TaskHistoryPage:
    items: tuple[TaskHistoryItem, ...]
    next_cursor: str | None
    previous_cursor: str | None
    server_time: datetime
    filter_timezone: str
    background_failed_count: int
```

- `normalize_history_filters(*, user_id: UUID, timezone: str, query: TaskHistoryQuery) -> TaskHistoryFilters`。
- `history_category(kind: str) -> HistoryCategory`。
- `history_filters_hash(filters: TaskHistoryFilters) -> str`：按固定字段 canonical JSON 的 SHA256，包括 user_id、scope、kind、status、UTC 边界、timezone、limit，不包括 cursor。
- `local_day_start(day: date, timezone: str) -> datetime`：该民用日的 UTC 起点，整日跳过时为下一存在日的起点。
- `canonical_utc_timestamp(value: datetime) -> str` 与 `parse_history_timestamp(value: str) -> datetime`：六位微秒Z的严格往返；naive或非法值拒绝。
- `TaskHistoryFilterError` 与 `TaskHistoryCursorError`：固定无载荷错误，路由统一映射 422。

所有公共函数和 dataclass 在实际实现中补中文业务、类型、异常与边界注释。上面的接口不表示可省略实现验证。

---

### Task 1: 摘要、分类、过滤与 IANA 日期边界

**Files:**
- Create: `backend/src/ai_employee/application/task_history.py`
- Create: `backend/src/ai_employee/application/task_history_dates.py`
- Create: `backend/tests/unit/application/test_task_history_filters.py`
- Create: `backend/tests/unit/application/test_task_history_dates.py`

**Interfaces:** 消费现有 `TaskStatus`；产出“共享接口约定”的全部值类型、两个固定异常及所列纯函数。

- [ ] **Step 1: 写失败的分类与真实日期用例**

```python
from datetime import UTC, date, datetime
from uuid import UUID
import pytest
from ai_employee.application.task_history import (
    TaskHistoryQuery, history_category, normalize_history_filters,
)
from ai_employee.application.task_history_dates import local_day_start

@pytest.mark.parametrize('kind', [
    'daily_brief', 'conversation.respond', 'mail_draft.generate',
    'trusted_action', 'calendar.restore.prepare',
])
def test_business_classification_does_not_depend_on_trigger(kind):
    assert history_category(kind) == 'business'

def test_calendar_day_uses_timezone_rules_not_24_hours():
    user = UUID('00000000-0000-0000-0000-000000000001')
    filters = normalize_history_filters(
        user_id=user, timezone='America/New_York',
        query=TaskHistoryQuery(created_from_date='2026-03-08', created_to_date='2026-03-08'),
    )
    assert filters.created_from == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert filters.created_before == datetime(2026, 3, 9, 4, tzinfo=UTC)

def test_skipped_civil_day_is_empty_not_another_days_records():
    assert local_day_start(date(2011, 12, 30), 'Pacific/Apia') == local_day_start(
        date(2011, 12, 31), 'Pacific/Apia',
    )
```

同文件参数化覆盖规格完整类型表、未知 kind、所有十个状态、默认 limit20、limit0/101/bool、未知 scope/status、business+sync_mail 和 business+other 冲突、非法／倒置日期、闰日、日期极值越界、25小时日期及午夜重叠／跳跃；非法输入统一固定异常，不暴露原输入。

- [ ] **Step 2: 观察 RED**

Run: `uv run --project backend pytest backend/tests/unit/application/test_task_history_filters.py backend/tests/unit/application/test_task_history_dates.py -q`
Expected: 新模块缺失导致失败。

- [ ] **Step 3: 实现纯值类型和规范化**

```python
BUSINESS_KINDS = frozenset({
    'daily_brief', 'conversation.respond', 'mail_draft.generate',
    'trusted_action', 'calendar.restore.prepare',
})
BACKGROUND_KINDS = frozenset({
    'sync_mail', 'sync_gmail', 'sync_calendar', 'brief.overdue_diagnostic',
    'privacy.clear_source_cache', 'privacy.delete_all_data',
})

def history_category(kind: str) -> HistoryCategory:
    if kind in BUSINESS_KINDS:
        return 'business'
    if kind in BACKGROUND_KINDS:
        return 'background'
    return 'other'
```

过滤用完整已知集合校验；`other` 是筛选哨兵，SQL 映射为不在两集合内。日期模块只抛固定 ValueError，不导入 task_history.py，避免循环依赖；规范化边界将其转为 TaskHistoryFilterError。UTC 时间编码为六位微秒、Z 后缀，供 hash 和 cursor 统一使用。

日期算法先验证两个 fold 的午夜 UTC 往返；有有效候选时取最早。午夜处于跳跃时，在两个候选 UTC 的有界区间内按微秒二分首次本地日期达到目标的瞬间；整日跳过自然得到与次日起点相同的值。不得按分钟扫全年、硬加24小时或静默使用UTC代替非法时区。所有 date/datetime 溢出转换为固定过滤错误。

- [ ] **Step 4: GREEN 与提交前检查**

Run: 上述聚焦命令，然后 `just check`、`git diff --check`。Expected: 全通过；现有状态机不改。

- [ ] **Step 5: 提交**

```bash
git add backend/src/ai_employee/application/task_history.py backend/src/ai_employee/application/task_history_dates.py backend/tests/unit/application/test_task_history_filters.py backend/tests/unit/application/test_task_history_dates.py
git commit -m "feat: define task history filters and summaries"
```

### Task 2: 用途隔离的有界签名游标

**Files:**
- Create: `backend/src/ai_employee/infrastructure/security/task_history_cursor.py`
- Create: `backend/tests/unit/infrastructure/test_task_history_cursor.py`

**Interfaces:** 消费 Task 1 `TaskHistoryFilters`、`HistoryCursorState`、`HistoryKey`、hash 与异常。产出 `TaskHistoryCursorCodec(master_key: bytes)`、`from_file(path: Path)`、`encode(state: HistoryCursorState) -> str`、`decode(token: str, *, filters: TaskHistoryFilters, now: datetime) -> HistoryCursorState`。

- [ ] **Step 1: 写错误绑定／时效失败测试**

```python
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID
import pytest
from ai_employee.application.task_history import (
    HistoryCursorState, HistoryKey, TaskHistoryCursorError,
    TaskHistoryQuery, history_filters_hash, normalize_history_filters,
)
from ai_employee.infrastructure.security.task_history_cursor import TaskHistoryCursorCodec

def test_cursor_is_bound_to_user_filter_and_exact_expiry():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    uid = UUID('00000000-0000-0000-0000-000000000001')
    key = HistoryKey(now, uid)
    filters = normalize_history_filters(user_id=uid, timezone='UTC', query=TaskHistoryQuery())
    state = HistoryCursorState(uid, history_filters_hash(filters), 'older', key, key, now)
    codec = TaskHistoryCursorCodec(bytes(range(32)))
    token = codec.encode(state)
    assert codec.decode(token, filters=filters, now=now) == state
    with pytest.raises(TaskHistoryCursorError):
        codec.decode(token, filters=replace(filters, limit=21), now=now)
    with pytest.raises(TaskHistoryCursorError):
        codec.decode(token, filters=filters, now=now + timedelta(hours=24))
```

增加独立固定向量、字节篡改、正确签名但错误schema、重复JSON键、NaN/Infinity、额外键、bool冒充版本/整数、超2048长度、非法base64url、非UTF8、错误签名长度、naive日期、anchor>upper、非canonical UUID、未来60秒边界、错误用户／时区／日期／scope／kind／status、密钥变化测试。测试错误消息不得包含 token 或 key。

- [ ] **Step 2: 运行观察 RED**

Run: `uv run --project backend pytest backend/tests/unit/infrastructure/test_task_history_cursor.py -q`
Expected: 新 codec 不存在。

- [ ] **Step 3: 实现编码规则**

```python
import hashlib
import hmac
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

signing_key = HKDF(
    algorithm=hashes.SHA256(), length=32, salt=None,
    info=b'ai-employee/task-history-cursor/v1',
).derive(master_key)
mac = hmac.new(signing_key, payload_bytes, hashlib.sha256).digest()
```

格式 `v1.<base64url-no-padding(canonical-json)>.<base64url-no-padding(mac)>`。JSON 固定键 `v,user_id,filters_hash,direction,upper,anchor,issued_at`；key 对象固定 `created_at,task_id`。JSON 使用 sort_keys、紧凑分隔符、UTF8；时间用 Task1 六位微秒 UTC 编码。解码前限长，验证版本/base64签名后严格JSON类型及重复键，再验证归属、筛选、排序界和时效。使用 compare_digest；捕获仅预期解码／验证异常，禁止吞掉未知系统异常。

from_file 兼容现有32字节标准Base64和Base64URL主密钥文件（CI使用openssl rand -base64）；Secret文件读取与外部游标严格URLsafe解码分开；不访问 AeadCipher 私有属性，不改变 encryption.py 或 OAuth/命令加密。文件读取只发生在受认证查询需要游标 codec 时，不在模块 import 或 create_app 时提前读取 Secret。

- [ ] **Step 4: GREEN、`just check`、diff检查**

Run: 聚焦命令、`just check`、`git diff --check`。Expected: 通过，无生产凭据或原始游标输出。

- [ ] **Step 5: 提交**

```bash
git add backend/src/ai_employee/infrastructure/security/task_history_cursor.py backend/tests/unit/infrastructure/test_task_history_cursor.py
git commit -m "feat: add signed task history cursors"
```

### Task 3: 查询索引与迁移发布链兼容

**Files:**
- Create: `backend/migrations/versions/20261001_0020_task_history_index.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/tasks.py`
- Modify: `backend/src/ai_employee/infrastructure/db/database_grants.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/src/ai_employee/infrastructure/db/alembic.py`（跨0019的双阶段守卫最终检查延至实际target callback）
- Test/Modify: `backend/tests/integration/db/test_migrations.py`（保留历史0019专项，增加0020最终守卫与根事务回滚证据）
- Test/Modify: `backend/tests/unit/infrastructure/db/test_alembic_migration_lifecycle.py`
- Test/Modify: `backend/tests/unit/cli/test_calendar_aad_revision_cli.py`（区分当前 head 与冻结 0019 历史场景，保留专用 CLI 零连接拒绝后继镜像）
- Create: `backend/tests/integration/db/test_task_history_index.py`

**Interfaces:** 新 revision=`20261001_0020`，down_revision=`20260809_0019`；索引名 `ix_task_runs_user_created_id`，列 `(user_id, created_at, id)`。不增加表／列／角色／grant，不修改旧 revision 文件。

- [ ] **Step 1: 写迁移准入与目录失败回归**

在现有 migration lifecycle 单测 fixture 中加入0020链，断言：published head由真实新脚本派生；0020两个phase的完整表／列／序列权限与0019完全相同；跨越0019仍绑定同一真实AAD guard并执行原before/after检查；0019→0020不重新执行AAD变更；缺显式guard且affected集合非空时不能从0018跨过0019；缺显式guard但affected为空时必须调用原内置strict-empty守卫的两个阶段，允许既有受管空库升级。显式注入错误类型／空值仍拒绝，不能把“没有显式attribute”误当成“没有守卫”。

集成测试复用 `cycle5_regular_database_url`／受管临时迁移 fixture，执行目录查询：

```python
from sqlalchemy import text

index_rows = await session.execute(text("""
    SELECT i.indisvalid, pg_get_indexdef(i.indexrelid)
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = 'ix_task_runs_user_created_id'
"""))
row = index_rows.one()
assert row.indisvalid is True
assert '(user_id, created_at, id)' in row.pg_get_indexdef
```

测试中 `session` 必须由本模块已声明的、受管 `ManagedAsyncSessionMaker` fixture 创建；不直接连接任意环境URL。新测试模块不得用SQL补索引令旧代码通过。

- [ ] **Step 2: RED**

Run: `uv run --project backend pytest backend/tests/unit/infrastructure/db/test_alembic_migration_lifecycle.py backend/tests/integration/db/test_task_history_index.py -q`
Expected: 新revision／index／inventory尚不存在，明确失败。

- [ ] **Step 3: 实现事务型索引与严格后继目录**

```python
from alembic import op

revision = '20261001_0020'
down_revision = '20260809_0019'

def upgrade() -> None:
    op.create_index('ix_task_runs_user_created_id', 'task_runs', ['user_id', 'created_at', 'id'])

def downgrade() -> None:
    op.drop_index('ix_task_runs_user_created_id', table_name='task_runs')
```

使用普通事务型创建，避免引入新的并发索引部分提交协议；仅测试受管非生产升级，不运行生产迁移或破坏性回滚。ORM登记同名同列Index。权限registry增加0020，显式继承0019的最终权限策略；现有 `if revision == 0019` 分支不能令0020退回旧权限。

env.py原先只在expected_target_revision==0019时绑定guard。保留该既有条件，并额外按已验证的线性迁移路径是否跨过0019这一步判定（source位置 < 0019位置 <= target位置），不根据字符串大小／任意revision猜测；保留同一guard identity、锁顺序及原after-step断言。实际经过0019时，before_mutation仍由原revision执行，before_commit在最终target步骤完整权限核验后作为最终callback检查执行；总共两个阶段，目标0019保持原行为，0019→0020不重新触发AAD。真实artifact guard及专用CLI仍精确绑定0018/0019，不扩大准入。0019专用CLI仍保留其版本限制，不能借新增历史索引放宽恢复／重同步准入。若既有测试曾将发布head固定写成0019，只更新“当前head”断言；历史0019场景仍精确验证0019。

- [ ] **Step 4: GREEN 与迁移回归**

Run: 本任务单元／集成，再 `uv run --project backend pytest backend/tests/integration/db/test_migrations.py -q`、`just check`、`git diff --check`。用合成数据运行索引查询EXPLAIN，记录键序与无逐行明细加载；不以微秒计时作脆弱断言。只有数据证明需要第二索引时才提出精确查询计划证据，不提前新增。

- [ ] **Step 5: 提交**

```bash
git add docs/superpowers/plans/2026-10-01-complete-task-history.md backend/src/ai_employee/infrastructure/db/alembic.py backend/tests/integration/db/test_migrations.py backend/migrations/versions/20261001_0020_task_history_index.py backend/src/ai_employee/infrastructure/db/models/tasks.py backend/src/ai_employee/infrastructure/db/database_grants.py backend/migrations/env.py backend/tests/unit/infrastructure/db/test_alembic_migration_lifecycle.py backend/tests/integration/db/test_task_history_index.py backend/tests/unit/cli/test_calendar_aad_revision_cli.py
git commit -m "feat: index task history with guarded schema upgrade"
```

### Task 4: 短快照只读 Repository 与分页用例

**Files:**
- Create: `backend/src/ai_employee/infrastructure/db/repositories/task_history_views.py`
- Create: `backend/src/ai_employee/application/use_cases/task_history.py`
- Create: `backend/tests/unit/application/test_task_history.py`
- Create: `backend/tests/integration/tasks/test_task_history_views.py`
- Create: `backend/tests/integration/tasks/history_fixtures.py`

**Interfaces:**
- `TaskHistoryReader.read_page(*, filters: TaskHistoryFilters, cursor: HistoryCursorState | None) -> HistoryReadPage`（async Protocol）。
- `HistoryCursorCodec.encode(state)` / `decode(token, *, filters, now)` Protocol对应Task2。
- `ListTaskHistoryUseCase(reader, codec_factory: Callable[[], HistoryCursorCodec], clock: Clock)`；`execute(*, user_id: UUID, timezone: str, query: TaskHistoryQuery) -> TaskHistoryPage`（async）。
- `SqlAlchemyTaskHistoryReader(session_factory: ManagedAsyncSessionMaker)` 实现 reader；不导入旧TaskViewStore以免读取payload。

- [ ] **Step 1: 写失败的两个用户、排序和隔离测试**

`history_fixtures.py` 声明 `HistoryDataset(owner_id, other_id, owner_task_ids)` dataclass及 `seed_history_dataset(session_factory) -> HistoryDataset`，使用固定合成UUID、用户资料和精确UTC微秒，创建25条所属任务、另一用户任务及不同kind/status；不包含真实正文。

```python
@pytest.mark.asyncio
async def test_pages_are_user_scoped_and_bidirectional(history_reader, history_dataset):
    filters = normalize_history_filters(
        user_id=history_dataset.owner_id, timezone='UTC',
        query=TaskHistoryQuery(scope='all'),
    )
    first = await history_reader.read_page(filters=filters, cursor=None)
    assert len(first.items) == 20
    assert set(row.id for row in first.items) <= set(history_dataset.owner_task_ids)
    assert first.next_anchor is not None
    assert first.previous_anchor is None
```

在该模块明确定义 `history_reader`、`history_dataset` fixture，依赖受管regular数据库fixture，调用上述seed方法。补齐下一页5条、回上一页20条、同微秒UUID排序、每个筛选、background含other、失败计数只受user/date限制、空列表、边界行删除、全部窗口消失、并发提交／状态更新的一致性读取测试。单元FakeReader/FakeCodec/FakeClock验证无效游标零reader调用、正常读零dispatcher／provider调用、空页方向cursor必须由仍存方向记录的EXISTS支持。

- [ ] **Step 2: RED**

Run: `uv run --project backend pytest backend/tests/unit/application/test_task_history.py backend/tests/integration/tasks/test_task_history_views.py -q`
Expected: 模块缺失。

- [ ] **Step 3: 实现显式列投影及keyset**

```python
columns = (
    TaskRunModel.id, TaskRunModel.kind, TaskRunModel.status,
    TaskRunModel.created_at, TaskRunModel.started_at, TaskRunModel.finished_at,
    TaskRunModel.error_code, TaskRunModel.retry_of_task_id,
)
statement = select(*columns).where(TaskRunModel.user_id == filters.user_id)
# 规范化过滤条件由固定列比较构建；禁止动态SQL列名或原始字符串插值。
```

先 `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`，沿用现有TaskView/ActionView的受管session模式。first页从匹配集合取最大key作为upper；older使用key<anchor倒序，newer使用key>anchor正序取limit+1再反转可见limit条。所有窗口受key<=upper限制。

用同一事务的EXISTS判断可见页首之前／页末之后是否还有记录，生成previous/next anchor。COUNT只对当前用户、日期区间、kind不在BUSINESS_KINDS、status=failed，不套用业务kind/status或翻页upper。禁止SELECT整个ORM或N+1。

当前窗口为空时返回空items，检查输入anchor的反方向是否仍有记录：older空页且有key>anchor时以原anchor返回previous；newer空页且有key<anchor时以原anchor返回next；对应seek方向确实不存在才返回null。不依赖被删除边界行，不制造虚构anchor；前端始终提供重新加载首页，方向查询不得返回同一空窗口造成自动循环。保留期／状态变化不是500，不能为了固定分页去锁业务行。用例用同一clock时间验证／签发cursor；需要解码或有方向anchor需签发时，通过 asyncio.to_thread 调用同步codec_factory，每次execute最多构造一次并复用，避免Secret文件I/O阻塞事件循环；分别编码older/newer，仅有anchor时编码；所有字段都从固定DTO构造。

- [ ] **Step 4: GREEN、SQL证据与提交检查**

Run: 聚焦、`just check`、`git diff --check`。记录SQL列清单和合成EXPLAIN；同一只读事务中并发更改记录，证明摘要／count／方向判断来自相同快照；事务随请求结束关闭。

- [ ] **Step 5: 提交**

```bash
git add backend/src/ai_employee/infrastructure/db/repositories/task_history_views.py backend/src/ai_employee/application/use_cases/task_history.py backend/tests/unit/application/test_task_history.py backend/tests/integration/tasks/test_task_history_views.py backend/tests/integration/tasks/history_fixtures.py
git commit -m "feat: query task history from consistent database snapshots"
```

### Task 5: 认证列表端点与完整API契约

**Files:**
- Modify: `backend/src/ai_employee/api/routers/tasks.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Create: `backend/tests/unit/api/test_task_history_contract.py`
- Create: `backend/tests/integration/api/test_task_history.py`

**Interfaces:** 新 `get_task_history_use_case(request: Request) -> ListTaskHistoryUseCase`；HTTP字段／错误严格按规格§5，无修改现有TaskResponse、POST／detail／SSE。

- [ ] **Step 1: 注册、401、摘要白名单和原端点回归RED**

```python
@pytest.mark.asyncio
async def test_list_exposes_only_summary_fields(history_client):
    response = await history_client.get('/api/v1/tasks', params={'scope': 'all'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert set(response.json()) == {
        'items', 'next_cursor', 'previous_cursor', 'server_time',
        'filter_timezone', 'background_failed_count',
    }
    assert set(response.json()['items'][0]) == {
        'id', 'kind', 'category', 'status', 'created_at', 'started_at',
        'finished_at', 'error_code', 'retry_of_task_id',
    }
```

本模块定义 `history_client` fixture，仿照 `integration/api/test_tasks.py` 的真实Cookie登录、受管session生命周期，调用Task4 seed；设置合成32字节Secret临时文件并在退出清理，禁止写仓库.env。测试用户timezone非UTC，另一客户端访问别人的cursor统一422且无计数泄漏。参数错scope/kind/status/date/limit/cursor均断言稳定错误码和无输入回显；增加GET零TaskRun/Outbox/审计创建和未登录不读取Secret的断言。

- [ ] **Step 2: RED**

Run: `uv run --project backend pytest backend/tests/unit/api/test_task_history_contract.py backend/tests/integration/api/test_task_history.py -q`
Expected: GET返回405／依赖不存在／契约不符。

- [ ] **Step 3: 实现只读路由和惰性注入**

```python
@router.get('', response_model=TaskHistoryPageResponse)
async def list_task_history(
    authenticated: CurrentSession,
    response: Response,
    use_case: Annotated[ListTaskHistoryUseCase, Depends(get_task_history_use_case)],
    scope: str = 'business', kind: str | None = None,
    status: str | None = None, created_from_date: str | None = None,
    created_to_date: str | None = None, limit: str = '20',
    cursor: str | None = None,
) -> TaskHistoryPageResponse:
    response.headers['Cache-Control'] = 'no-store'
    query = parse_task_history_query(
        scope=scope, kind=kind, status=status, created_from_date=created_from_date,
        created_to_date=created_to_date, limit=limit, cursor=cursor,
    )
    page = await use_case.execute(
        user_id=authenticated.user.id, timezone=authenticated.user.timezone, query=query,
    )
    return task_history_response(page)
```

`parse_task_history_query`、`task_history_response`和显式Pydantic response models在本路由文件定义并测试；limit只接受canonical整数文本再验证1..100，cursor限2048；日期转换属于用例。参数异常统一 ApiProblem(422,task_history_filter_invalid)，cursor统一task_history_cursor_invalid；异常响应同样no-store，不以局部通用异常吞掉500。`deps.py::_problem_response` 现有no-store条件新增仅 `request.method == "GET" and request.url.path == "/api/v1/tasks"` 的精确分支，使认证前401和过滤422也生效，不改变其他任务端点响应。

依赖从 `request.app.state.auth_session_factory` 构造stateless reader，Clock用既有get_auth_clock；codec_factory闭包按当前配置读取主密钥，非列表请求不受影响。响应datetime全部转UTC，用六位微秒Z字符串，不丢失分页比较精度；不改变其它API的时间序列化。

- [ ] **Step 4: GREEN与旧REST/SSE回归**

Run: 本任务聚焦，再 `uv run --project backend pytest backend/tests/integration/api/test_tasks.py backend/tests/integration/api/test_task_sse.py -q`、`just check`、diff检查。
Expected: 新GET和既有POST/detail/cancel/retry/SSE同时通过，四种真实写命令无变化。

- [ ] **Step 5: 提交**

```bash
git add backend/src/ai_employee/api/routers/tasks.py backend/src/ai_employee/api/deps.py backend/tests/unit/api/test_task_history_contract.py backend/tests/integration/api/test_task_history.py
git commit -m "feat: expose authenticated task history listing"
```

### Task 6: 前端摘要客户端与严格解析

**Files:**
- Create: `frontend/src/api/taskHistory.ts`
- Create: `frontend/src/api/taskHistory.spec.ts`
- Create: `frontend/src/test-support/taskHistoryFixtures.ts`

**Interfaces:** 输出 `TaskHistoryFilters`（scope/kind/status/created_from_date/created_to_date）、`TaskHistoryItem`、`TaskHistoryPage`、`listTaskHistory(filters, cursor: string | null = null): Promise<TaskHistoryPage>`、`historyItemKey(item): string`。limit固定20；列表独立于现有TaskSnapshot。

- [ ] **Step 1: 写摘要解析／请求参数RED**

fixture导出 `historyItem(overrides?: Partial<TaskHistoryItem>)`、`historyPage(overrides?: Partial<TaskHistoryPage>)`，全部合成固定UUID、时间、无内容数据。

```typescript
it('reads summaries without replacing task snapshots', async () => {
  const payload = historyPage()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
  const page = await listTaskHistory({
    scope: 'business', kind: null, status: null,
    created_from_date: null, created_to_date: null,
  })
  expect(page.items[0]).not.toHaveProperty('steps')
  expect(page.items[0]).not.toHaveProperty('event_cursor')
  expect(page.items[0]?.id).toBe(payload.items[0]?.id)
})
```

补齐null字段、非法UUID／时间／状态／category、额外敏感字段不进入返回值、数组类型、count非负整数、cursor长度、已知kind和other、错误ProblemDetails/trace、GET携带Cookie的真实统一client行为测试。测试保持全文件Vitest风格，afterEach恢复fetch，不能借导入stores产生初始化副作用。

- [ ] **Step 2: RED**

Run: `pnpm --dir frontend test:unit --run src/api/taskHistory.spec.ts`
Expected: 模块缺失。

- [ ] **Step 3: 实现类型与明确投影**

```typescript
export interface TaskHistoryFilters {
  scope: 'business' | 'all' | 'background'
  kind: string | null
  status: TaskStatus | null
  created_from_date: string | null
  created_to_date: string | null
}
export function listTaskHistory(filters: TaskHistoryFilters, cursor: string | null = null) {
  const query = new URLSearchParams({ scope: filters.scope, limit: '20' })
  for (const key of ['kind', 'status', 'created_from_date', 'created_to_date'] as const) {
    const value = filters[key]
    if (value !== null) query.set(key, value)
  }
  if (cursor !== null) query.set('cursor', cursor)
  return requestJson(`/tasks?${query.toString()}`, parseTaskHistoryPage)
}
```

在本文件定义 `parseTaskHistoryPage(value: unknown): TaskHistoryPage`，使用统一requestJson与现有类型收窄模式；不更改client.ts、TaskSnapshot或Store。时间必须匹配服务端六位微秒UTC契约并验证真实日历日期；historyItemKey使用规范UTC原文+canonical UUID比较，不用JS毫秒精度丢失后三位微秒。未知kind可显示“其他任务”，不能因未来kind就丢掉整页；未知状态/category拒绝并显示读取失败，不猜成功。

- [ ] **Step 4: GREEN、类型、lint、check**

Run: 聚焦、`pnpm --dir frontend type-check`、`pnpm --dir frontend lint`、`just check`、diff检查。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/api/taskHistory.ts frontend/src/api/taskHistory.spec.ts frontend/src/test-support/taskHistoryFixtures.ts
git commit -m "feat: add typed task history client"
```

### Task 7: 独立列表状态、路由筛选与可见性探测

**Files:**
- Create: `frontend/src/features/tasks/historyRoute.ts`
- Create: `frontend/src/features/tasks/historyRoute.spec.ts`
- Create: `frontend/src/features/tasks/useTaskHistory.ts`
- Create: `frontend/src/features/tasks/useTaskHistory.spec.ts`

**Interfaces:**
- `parseHistoryRoute(query: LocationQuery)`返回明确filters/cursor或本地固定校验错误；`historyRouteQuery(query, filters, cursor)`保留task_id及非历史公开参数，替换本功能参数。
- `useTaskHistory({ timezone: Readonly<Ref<string | null>> })`在Vue组件setup内调用，内部使用现有router；返回 `page, filters, cursor, loading, error, hasNewTasks, setFilters, nextPage, previousPage, refreshCurrent, refreshFirst`。`setFilters(next: TaskHistoryFilters): Promise<void>`，其余四个导航／刷新方法均无参数并返回 `Promise<void>`；内部导航失败不得伪造成功。page为独立摘要ref；error区分本地过滤错误与真实ProblemError，不伪造服务端异常。

- [ ] **Step 1: 写乱序和轮询RED**

测试用 `renderWithPlugins` 挂载合成探针组件，在setup调用composable并暴露返回接口给测试；mock仅Task6 API函数与受控时间，不mock生产状态机。

```typescript
it('does not replace the visible page when a probe finds a newer task', async () => {
  vi.useFakeTimers()
  vi.mocked(listTaskHistory)
    .mockResolvedValueOnce(historyPage())
    .mockResolvedValueOnce(historyPage({ items: [historyItem({
      created_at: '2026-10-02T00:00:00.000000Z',
    })] }))
  const view = await renderWithPlugins(HistoryProbe, { route: '/tasks' })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(30_000)
  await flushPromises()
  expect(view.getByText('有新任务')).toBeVisible()
  expect(view.getByText('2026-10-01T00:00:00.000000Z')).toBeVisible()
})
```

`HistoryProbe`在该测试文件中完整定义，仅模板打印 page首项created_at及hasNewTasks，不创建产品组件。补齐：首次空页→新任务、深链接cursor初始探测只建baseline不误报旧任务、隐藏/卸载停止、慢探测不并发、失败等到下个30秒、切回读取当前页、手刷/筛选从首页、旧响应丢弃、task_id变化不重载列表、无local/sessionStorage写入、服务端cursor_invalid不静默重置、timezone变化重置。

- [ ] **Step 2: RED**

Run: `pnpm --dir frontend test:unit --run src/features/tasks`
Expected: 新模块缺失。

- [ ] **Step 3: 实现有界生命周期**

```typescript
let generation = 0
let probing = false
let disposed = false
async function readCurrentPage(): Promise<void> {
  const requestGeneration = ++generation
  loading.value = true
  try {
    const result = await listTaskHistory(filters.value, cursor.value)
    if (disposed || requestGeneration !== generation) return
    page.value = result
    error.value = null
  } catch (cause) {
    if (!disposed && requestGeneration === generation) error.value = toHistoryError(cause)
  } finally {
    if (!disposed && requestGeneration === generation) loading.value = false
  }
}
```

`toHistoryError`在同文件定义，ProblemError保持公开对象，本地意外错误只固定说明不透传。watch只依赖规范化历史query/cursor，忽略task_id。每次filter/cursor/可见性生命周期变更使旧probe失效；timer由onMounted/onUnmounted和visibilitychange清理，探测期间probing=true，finally释放，但不得释放更新一代的状态。

首页读取时记录首项精确key或已知空标记；从非首页URL初次进入时，额外读取最新页建立baseline但不替换当前页／不立即把既有任务报为新任务。随后探测比较创建顺序，只有更新的匹配记录设置hasNewTasks；不更新可见items/count/server_time，不制造混合快照。30秒常量与清理规则均有FakeTimer测试。

filter表单先验证再写URL；非法URL不静默当默认条件执行。旧数据在刷新失败时保留且显示失败。游标失效提供refreshFirst显式恢复，不能自动降级过滤。用户timezone ref初次从null变为已知不误清深链接，之后真实变更重置cursor；无用户时区时不推断本机时区。

- [ ] **Step 4: GREEN与check**

Run: 本任务聚焦、类型、lint、`just check`、diff检查；原Store/SSE测试不变。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/features/tasks/historyRoute.ts frontend/src/features/tasks/historyRoute.spec.ts frontend/src/features/tasks/useTaskHistory.ts frontend/src/features/tasks/useTaskHistory.spec.ts
git commit -m "feat: manage task history navigation and refresh"
```

### Task 8: 接入真实列表并完成 M2.1 Task 7

**Files:**
- Modify: `frontend/src/pages/TasksPage.vue`、`TasksPage.spec.ts`
- Modify: `frontend/src/components/TaskTimeline.vue`、`TaskTimeline.spec.ts`
- Create: `frontend/src/components/TaskHistoryList.vue`、`TaskHistoryList.spec.ts`
- Modify（任务页自有详情去重）: `frontend/src/components/AppShell.vue`、`AppShell.spec.ts`
- Modify（对应布局事实）: `docs/superpowers/specs/2026-10-01-complete-task-history-design.md`、`docs/superpowers/specs/2026-09-19-frontend-component-refactor-m2-1-design.md`
- Create: `frontend/e2e/task-history.spec.ts`
- Modify（仅必要语义选择器与新增只读 GET 的精确契约 fixture）: `frontend/e2e/reconnect.spec.ts`、`action-workspace.spec.ts`、`calendar-restore.spec.ts`；原未知请求、写入、审批和 SSE 断言保留
- Modify: `docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md`（Task7完成记录/交接，不更改其他任务）

**Interfaces:** TaskHistoryList接收page/filters/loading/error/newTaskHint；只emit过滤、翻页、刷新和select(id)，不调用API。TasksPage组合useTaskHistory和原选中任务详情；TaskTimeline保持既有task/retry/follow Props。本任务就是原M2.1 Task7，不另派第二个Task7实现者。

- [ ] **Step 1: 先建立旧单测／E2E安全网**

将既有TasksPage/TaskTimeline测试必要选择器转为角色／名称；保留原cancel/retry/replacement、步骤摘要、安全错误及恢复提案断言。运行旧页面聚焦和 `reconnect/action-workspace/calendar-restore`；不得先改SSE协议fixture或降低请求次数。

- [ ] **Step 2: 写新列表与UI RED**

```typescript
it('keeps filtered history visible while opening a task detail', async () => {
  const item = historyItem({ status: 'failed' })
  vi.mocked(listTaskHistory).mockResolvedValue(historyPage({ items: [item] }))
  const view = await renderWithPlugins(TasksPage, { route: '/tasks?scope=all&status=failed' })
  await fireEvent.click(await view.findByRole('link', { name: `查看任务 ${item.id.slice(0, 8)}` }))
  expect(view.router.currentRoute.value.query.status).toBe('failed')
  expect(view.router.currentRoute.value.query.task_id).toBe(item.id)
})
```

fixture应与failed过滤相符，测试使用 `historyItem({status:'failed'})` 和对应page，不能用不匹配成功记录伪造结果。补默认业务/全部/后台跳转、日期筛选、准确失败口径、上一页/下一页、空页恢复、单次选中SSE、敏感字段不存在、pending无重试、failed重试关联、清理404、keyboard/focus/narrow布局和所有旧live文案。

- [ ] **Step 3: 实现DataView、Timeline与明确状态**

```vue
<DataView :value="page?.items ?? []" data-key="id">
  <template #list="slotProps">
    <ul aria-label="任务历史">
      <li v-for="item in slotProps.items" :key="item.id">
        <RouterLink :to="taskLocation(item.id)" :aria-label="`查看任务 ${item.id.slice(0, 8)}`">
          {{ taskKindLabel(item.kind) }}
        </RouterLink>
        <StatusTag kind="task" :value="item.status" />
      </li>
    </ul>
  </template>
</DataView>
```

`taskLocation`只合并当前公开route query和完整id；`taskKindLabel`在TaskHistoryList本地固定映射，不解析正文。所有DataView slot数据显式类型化，不能any。过滤控件有label；日期筛选优先用 PrimeVue InputText type=date 保持 YYYY-MM-DD 字符串，避免 Date 对象隐式宿主时区转换。耗时按page.server_time，不对历史行设每秒计时器。

无任务、无筛选结果、加载中和错误分别呈现；后台失败提示点击明确设scope=background/status=failed/kind=null、保留日期、清cursor。详情保留原查询与mutations，仅在原操作结束后通知history.refreshCurrent；不把列表返回写入TaskStore。Timeline保留原步骤名、状态、耗时和摘要文本，重试只在原允许状态可触发。删除旧scoped布局，以Tailwind/PrimeVue实现；不要复制全局AppShell时间线。

- [ ] **Step 4: GREEN与浏览器证据**

Run: 目标组件/页面/feature单测与未改Store/SSE单测、type-check、lint、相关E2E、`just check`。新增浏览器场景使用真实任务列表API与合成数据库，覆盖刷新后找回未曾访问任务；不能全部用page.route模拟列表后声称数据库联通。使用已认证 `page.request` 调用现有 POST /api/v1/tasks 与合成已支持的 kind、精确幂等键创建可见任务；默认关闭真实写入且工具为Fake，不新增生产seed端点。先停留在其他页面创建至少25条任务，再进入历史，证明此前未访问的ID可找到。共享测试管理员可能有其他测试记录，按本例ID集合验证分页可达与不重复，不断言全库仅有25条。

- [ ] **Step 5: 提交与双计划记账**

```bash
git add frontend/src/pages/TasksPage.vue frontend/src/pages/TasksPage.spec.ts frontend/src/components/TaskTimeline.vue frontend/src/components/TaskTimeline.spec.ts frontend/src/components/TaskHistoryList.vue frontend/src/components/TaskHistoryList.spec.ts frontend/e2e/task-history.spec.ts frontend/e2e/reconnect.spec.ts frontend/e2e/action-workspace.spec.ts frontend/e2e/calendar-restore.spec.ts docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md
git commit -m "feat: migrate task history and timeline to PrimeVue"
```

只暂存实际必要变更，未改E2E文件不制造格式噪声。提交正文记录静态与运行时live region差异、请求范围和原安全断言保留。独立审查通过后，将本计划Task8及M2.1 Task7都标为complete。

### Task 9: 完整门禁、证据与 M2.1 交接

**Files:**
- Create: `docs/releases/2026-10-01-task-history-evidence.md`
- Modify: `docs/acceptance-checklist.md`
- Modify: 本计划及 `docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md`（进度/交接）
- Modify: `AGENTS.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`（把设计阶段措辞改为已交付限定例外）

**Interfaces:** 本补充全部14条规格验收证据；交接到M2.1 Task8前置异步确认修复，保留原Task1～6提交和所有deferred findings。

- [ ] **Step 1: 逐项审计规格§10**

对14项逐一写出代码、测试文件、命令、输出与commit依据。不能以“单测全绿”代替双向分页／跨用户／迁移／真实API浏览器证据。补充所有发现的失败重现和最小修复，仍按独立fix+复审，不改范围以求通过。

- [ ] **Step 2: 执行完整门禁**

```bash
just check
just ci
bash scripts/report-frontend-bundle.sh --budget
```

在同一个已验证临时环境串行执行并完整读取输出。若宿主8000端口不可绑定，使用同一Docker网络namespace执行完整 `just ci`；挂载所需just/Docker/uv/pnpm和受管测试配置，不能拆开若干命令冒充完整ci。任何环境适配保持仓库just为唯一正式入口、不连接真实账号或手工修共享库。

- [ ] **Step 3: 写证据与同步完成状态**

证据记录实际验证HEAD、后端/前端/E2E数、分页/权限/日期/签名/索引矩阵、原SSE与幂等回归、体积和已知限制，链接实际留档摘要；不提交Secret、完整cursor、截图原始敏感数据或测试报告。确认新索引不改变0019最终ACL，TaskHistory清理行为未改。

- [ ] **Step 4: 提交并复验最终HEAD**

```bash
git add docs/releases/2026-10-01-task-history-evidence.md docs/acceptance-checklist.md docs/superpowers/plans/2026-10-01-complete-task-history.md docs/superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md
git commit -m "docs: record task history acceptance and M2.1 handoff"
just ci
```

最终提交CI输出独立留档，不循环改写自身SHA。之后继续原M2.1 Task8前置修复/Task8～17；本补充完成不等于整个M2.1完成。

## 规格覆盖与自审

| 规格 | 实施任务 |
|---|---|
| §1～3真实保留记录、用途分类、全部任务 | 1、4、5、8 |
| §4字段、详情、重试关联、失败记录提示 | 4～8 |
| §5HTTP、日期、错误和no-store | 1、5、6 |
| §6游标、分页、短快照和并发边界 | 2、3、4、5 |
| §7刷新、迟到响应、无每行SSE、URL | 6、7、8 |
| §8分层、索引、权限和只读路径 | 1～5、9 |
| §9M2.1交接 | 8、9 |
| §10全部14条验收 | 各任务聚焦证据＋9汇总 |

执行前控制器按以上接口核对相邻任务brief，不重做已完成的M2.1任务。本文示例中的test fixtures由指定任务定义，不引用假想服务端字段；任何实现导致规格/API/可信执行边界冲突时先报告，不从“独立补充”推导更宽的权限。
