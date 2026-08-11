# AI Employee M2：可执行邮件与日历助手设计

> 日期：2026-08-06
>
> 状态：已批准
>
> 前置里程碑：M1「可信任务中心 + 每日办公简报」

## 1. 背景与设计结论

M1 已建立单管理员登录、Google 只读同步、每日简报、持久任务、人工审批、
LangGraph Checkpoint、Taskiq、Outbox、SSE、审计和故障恢复底座。M2 在不改变这些可信
执行不变量的前提下，引入真实邮件和日历写操作，并把 Google 与 Microsoft 作为一次完整
里程碑统一交付。

M2 的产品定位仍是「助手工作台」，不是新的邮箱或日历客户端。系统可以在对话和简报中
提出行动建议，但只有用户明确点击或下达命令后，才会创建可编辑提案和人工审批。任何真实
外部写入都必须绑定一个类型化命令、一个冻结版本、一个审批决定和一个幂等工具执行事实。

设计结论如下：

- M2 一次发布 Google 和 Microsoft 邮件、日历能力，不拆分对外发布版本。
- 采用仅面向邮件和日历的供应商无关「可信操作内核」，不引入通用 Planner 或工具平台。
- 邮件草稿只在本地加密保存，批准后直接发送；不写入供应商草稿箱。
- 每项审批只授权一个精确外部操作，有效决定窗口为 10 分钟。
- 审批使用当前安全 Cookie 会话和 CSRF，不增加密码二次验证。
- 日程修改使用供应商版本条件，补偿永远是需要新审批的新操作。
- 外部调用采用「至多产生一次业务副作用、结果必须核对」策略；只有能够证明先前写入未应用时
  才能重试调用，不确定结果不得盲目重放。
- Google 和 Microsoft 使用最小、按能力渐进取得的 OAuth 委托权限。
- M2 仍是单管理员产品，不增加多用户、租户或企业权限体系。

## 2. 范围

### 2.1 M2 包含

- Google Gmail 与 Microsoft Graph 邮件的新建、回复和全部回复。
- 本地加密邮件草稿、不可变草稿版本、编辑冲突和 10 分钟审批。
- Google Calendar 与 Microsoft Graph Calendar 非重复日程的创建和修改。
- 日程标题、时间、时区、描述、地点、参会人及邀请通知策略。
- 基于用户全部已连接日历、统一工作时间和会议缓冲的确定性冲突建议。
- 日程修改前快照，以及需要再次审批的显式恢复操作。
- Microsoft 个人账户和 Microsoft 365 工作/学校账户；不支持本地 Exchange。
- 按连接区分 `mail.read`、`mail.send`、`calendar.read`、`calendar.write` 的渐进授权。
- 连接级默认发送账户、默认日历、每周工作时间和会议缓冲设置。
- 统一操作中心、结构化审批预览、结果核对、人工结果确认和完整执行时间线。
- Microsoft 邮件、日历只读增量同步，与现有 Google 规范化模型对齐。
- Google 与 Microsoft 日历目录同步，以及每个可见日历的权限、时区和独立增量游标。
- 真实写入的幂等认领、结果核对、未知结果安全停机、审计和运维开关。

### 2.2 M2 不包含

- 供应商草稿箱同步或把未批准草稿写入 Gmail、Outlook。
- 邮件转发、附件、原始 MIME 持久化、HTML 邮件或富文本编辑器。
- Google Contacts、Microsoft Contacts 或其他通讯录同步。
- 批量邮件发送、批量日程修改或时间段授权。
- 日程删除、取消、重复日程创建或编辑、会议链接自动创建。
- 查询参会人的 Free/Busy；冲突建议只说明当前用户自己的可用性。
- 完整收件箱、邮件搜索产品、完整日历网格或供应商客户端替代品。
- Outlook 本地 Exchange、共享邮箱、Microsoft 365 Group Calendar 或应用权限访问。
- Gmail Send As 别名、Microsoft 代理发送、共享发件地址或跨邮箱委托发送。
- 文件上传、Qdrant、RAG、长期记忆、通用 Planner、工具市场、多 Agent、多用户注册或
  Kubernetes。

### 2.3 M2 发布边界

M2 必须在同一个发布版本中提供 Google 与 Microsoft 的核心能力对等。内部开发可以按可信
操作内核、Google 适配器、Microsoft 适配器和前端体验分阶段落地，但不能把只完成单一供应商
描述为 M2 发布完成。

M2 开始实施前应将根 `AGENTS.md`、README 和验收文档中的「当前只实现 M1」表述更新为
已批准的 M2 范围。该文档同步不重新打开 M1 功能范围，也不能把尚无证据的 M1 验收项标记为
已通过。

## 3. 用户控制模型

### 3.1 受控混合发起

允许以下入口生成建议：

- 每日简报中的「准备回复」或「建议调整日程」。
- 对话中的明确自然语言请求。
- 操作中心中的新邮件和新日程按钮。
- 邮件线程或日程来源详情中的显式操作按钮。

建议本身不是审批请求，也不能让任务进入 `WAITING_APPROVAL`。只有用户明确点击「准备草稿」
「创建提案」或提交自然语言命令后，应用层才创建本地草稿或日程提案。用户完成编辑并点击
「提交审批」后，系统才冻结精确命令。

### 3.2 单操作审批

一次审批只能包含以下一种操作：

- 发送一封邮件。
- 创建一个日程。
- 修改一个日程。
- 恢复一个日程到先前状态。

不允许一个审批包含多封邮件、多个日程或未来一段时间的概括授权。一个邮件可以包含多个
收件人，但总收件人数量不得超过 50 个去重地址；一个日程的参会人不得超过 50 个去重地址。
这些上限用于阻止 M2 演变为批量外联工具。

### 3.3 多账户选择

- 回复和全部回复必须绑定原始邮件连接，不能跨账户发送。
- 日程修改和恢复必须绑定原始日历连接与日历 ID。
- 新邮件使用用户默认发送账户，冻结前允许显式切换。
- 新日程使用用户默认日历，冻结前允许显式切换。
- 冻结命令必须包含精确 `connection_id` 和目标日历，执行时不得重新自动选择。
- 默认连接或默认日历已禁用、断开或失去写权限时，创建操作必须要求用户重新选择；禁止静默
  回退到另一个账户或 primary calendar。

## 4. 总体架构

M2 延续模块化单体和独立运行单元：

- FastAPI API：认证、授权、Schema、短事务和 HTTP 202 接受。
- Taskiq Worker：长任务、供应商调用、核对和补偿执行。
- Scheduler：到期审批、重试、核对扫描、保留清理和同步计划。
- PostgreSQL：连接、草稿、提案、任务、审批、工具执行、审计、Outbox 和 Checkpoint 的唯一
  真实来源。
- Redis：队列、通知、短期缓存和协调；丢失后必须从 PostgreSQL 恢复。

依赖方向保持不变：

```text
api / agents / integrations / workers
                  ↓
             application
                  ↓
               domain
```

可信操作数据流如下：

```mermaid
flowchart LR
    U[用户明确发起] --> D[本地草稿或日程提案]
    D --> V[确定性校验与冻结命令]
    V --> A[ApprovalRequest + Checkpoint]
    A -->|批准| Q[Outbox + Taskiq]
    A -->|拒绝或过期| X[安全终止]
    Q --> C[幂等认领 ToolExecution]
    C --> P[Google 或 Microsoft 适配器]
    P --> R[结果核对]
    R --> S[成功或明确失败]
    R --> N[needs_attention]
    S --> E[审计 + SSE + 本地同步]
    N --> E
```

## 5. 模块与职责

### 5.1 Connections

负责：

- Google 与 Microsoft OAuth 生命周期。
- 供应商账户身份规范化。
- 实际 scope 与本地能力状态。
- 默认发送账户和默认日历。
- 能力启用、关闭和整个连接断开。
- 现有连接的 OAuth refresh/exchange 统一通过供应商中立的 `OAuthRefreshCoordinator`；不得直接
  `ensure_connection`、无条件 refresh upsert 或绕过 connection claim。

该模块不执行邮件或日历业务规则。

### 5.2 Mail

负责：

- 本地加密草稿和不可变版本。
- 新邮件、回复、全部回复的地址与线程规则。
- 纯文本正文和模型最小披露。
- 构建 `MailSendCommand`。

模型只生成正文候选，不能决定发件账户、收件人、主题、回复线程或是否发送。

### 5.3 Calendar

负责：

- 日程创建、修改和恢复提案。
- 用户本人冲突检测与候选时间生成。
- ETag、通知策略和补偿快照。
- 构建 `CalendarCreateCommand`、`CalendarUpdateCommand` 和
  `CalendarRestoreCommand`。

模型只能解释冲突与建议，不能决定候选时间是否合法。

### 5.4 Trusted Actions

负责：

- 类型化命令验证与规范序列化。
- 冻结命令、AEAD 加密和哈希绑定。
- 人工审批中断与恢复。
- 工具执行认领、结果核对和人工结果确认。
- 任务、步骤、审计、Outbox、Checkpoint 和 SSE 的一致状态变化。

该模块只接受邮件和日历命令联合，不提供任意 Tool Manifest 或动态工具注册。

### 5.5 Provider Adapters

Google 与 Microsoft 适配器分别负责：

- OAuth URL、Token 交换、刷新和撤销。
- 只读增量同步。
- 邮件发送和回复。
- 日程创建、更新及结果核对。
- 供应商错误到内部错误类别的映射。

供应商 SDK、HTTP 字段和原始响应不得离开适配器边界。
适配器不持有业务事务、不自行决定 refresh retry；所有现有 connection 的 token exchange/refresh
必须由 application 层 `OAuthRefreshCoordinator` 先 claim，再由 coordinator 统一提交结果。

## 6. 类型化命令

### 6.1 通用要求

所有命令必须：

- 使用固定英文 `action` 和显式 `schema_version`。
- 只包含标准 JSON 类型，时间使用带时区 RFC 3339，业务日期携带 IANA 时区。
- 包含精确 `connection_id`，不得只依赖账户邮箱。
- 包含稳定 `operation_id`，供幂等、核对和供应商关联使用。
- 在进入领域层前完成 Pydantic 边界验证，再映射为不可变领域值对象。
- 规范化后计算 SHA-256；动作名、Schema 版本和完整载荷都进入哈希。

### 6.2 `MailSendCommand`

字段至少包括：

| 字段 | 约束 |
|---|---|
| `schema_version` | 固定为 `mail_send.v1` |
| `action` | 固定为 `mail.send` |
| `operation_id` | 稳定 UUID |
| `connection_id` | 已启用 `mail.send` 的连接 |
| `draft_id` / `draft_version` | 已冻结的本地草稿版本 |
| `mode` | `new`、`reply` 或 `reply_all` |
| `source_thread_id` | 回复类必填，新邮件为空 |
| `source_message_id` | 回复类必填，新邮件为空 |
| `to` / `cc` / `bcc` | 规范化、去重地址；合计不超过 50 |
| `subject` | 用户可见纯文本主题，最大 255 个 Unicode 字符 |
| `body_text` | 纯文本，最大 100,000 个 Unicode 字符 |
| `thread_headers` | 回复所需的内部规范化引用，不接受用户任意原始 Header |

发件地址从连接事实确定，不能由用户或模型在命令中伪造。地址去重忽略域名大小写，并按供应商
规范保留本地部分；系统不擅自执行 Gmail 点号或加号地址归并。

### 6.3 `CalendarCreateCommand`

字段至少包括：

| 字段 | 约束 |
|---|---|
| `schema_version` | 固定为 `calendar_create.v1` |
| `action` | 固定为 `calendar.create` |
| `operation_id` | 稳定 UUID |
| `connection_id` / `calendar_id` | 精确目标 |
| `client_event_id` | 供应商允许时使用的稳定创建标识 |
| `title` | 必填纯文本 |
| `description` / `location` | 可空纯文本 |
| `starts_at` / `ends_at` | 明确 UTC 瞬间或全天日期 |
| `timezone` | IANA 时区 |
| `all_day` | 显式布尔值 |
| `attendees` | 规范化去重地址，不超过 50 |
| `notification_policy` | `all` 或 `none` |

不允许 recurrence、conference、attachments 或任意供应商扩展字段。

### 6.4 `CalendarUpdateCommand`

在创建命令字段基础上增加：

- `provider_event_id`：目标供应商事件 ID。
- `base_etag`：生成提案时的供应商版本。
- `before_snapshot_id`：加密修改前快照。
- `changed_fields`：确定性生成的字段名集合，仅用于预览和审计，不代替完整期望状态。

执行时必须用 `base_etag` 做条件更新。供应商当前版本不同则不执行写入。

### 6.5 `CalendarRestoreCommand`

恢复命令由历史 `before_snapshot` 生成，但必须：

- 读取当前供应商事件并取得新的 `base_etag`。
- 显示当前状态与待恢复状态的完整差异。
- 冻结新的通知策略。
- 生成新的 `operation_id`、ApprovalRequest 和 ToolExecution。

恢复不能复用旧审批，也不能绕过当前能力状态。

## 7. 状态模型

### 7.1 草稿和提案

邮件草稿状态：

```text
editing → awaiting_approval → executing → sent
   ↓             ↓          ↙          ↘
cancelled      editing   editing   needs_attention
```

- `editing`：允许基于当前版本继续编辑。
- `awaiting_approval`：某一不可变版本已生成审批，草稿被锁定；继续编辑必须先撤回审批。
- `executing`：审批已通过且 Worker 已认领，不能再取消或编辑冻结版本。
- `sent`：对应发送命令已确认成功。
- `cancelled`：用户删除本地草稿，没有外部副作用。
- `needs_attention`：发送结果未知；只能核对或人工确认，不能编辑后直接重发。

拒绝、过期或显式撤回会使草稿返回 `editing`。此后首次保存产生新版本；旧 ApprovalRequest
状态变为 `invalidated`，其冻结版本永久不可再次批准。明确确认邮件未发送后，用户也必须先
产生新草稿版本并重新审批。

日程提案使用 `editing`、`awaiting_approval`、`executing`、`applied`、`stale`、
`needs_attention` 和 `cancelled`。ETag 冲突会进入 `stale`，必须基于供应商最新事件重新生成
版本，旧提案不能继续执行。

ApprovalRequest 在 M1 状态基础上增加 `invalidated`，仅用于草稿或提案版本变化、能力关闭、
连接断开或执行认领截止时间到期。`invalidated` 是终态，不能改回 pending。

### 7.2 TaskRun

M2 在 M1 状态基础上增加：

- `reconciling`：供应商可能已接收写请求，系统只执行只读核对。
- `needs_attention`：自动核对无法确定结果，禁止继续写入。

主要迁移：

```text
running → waiting_approval → queued → running
running → retry_scheduled → queued
running → reconciling → succeeded
running → reconciling → failed
running → reconciling → needs_attention
needs_attention → reconciling
needs_attention → succeeded
needs_attention → failed
```

`needs_attention → succeeded/failed` 只允许经过用户人工结果确认用例，并追加明确 actor、时间和
确认来源。它不能重新调用供应商。

### 7.3 ToolExecution

工具执行状态：

```text
claimed → executing → succeeded
                    → confirmed_failed
                    → retryable_failed → executing
                    → reconciling → succeeded
                                  → confirmed_failed
                                  → needs_attention
```

- `claimed` 已阻止第二个 Worker 创建同一执行事实。
- `executing` 表示请求即将发送或正在发送，不能据此推断供应商结果。
- `retryable_failed` 只用于适配器能够证明供应商未接受写入的失败。
- `reconciling` 和 `needs_attention` 期间不得重发写请求。

## 8. 审批协议

### 8.1 冻结与存储

真实命令不得明文存入 `ApprovalRequest.payload`。ApprovalRequest 扩展为：

- `action`、`schema_version`、风险等级和不含敏感内容的预览元数据。
- 完整规范命令的 AEAD `ciphertext`、`nonce` 和 `key_version`。
- 规范明文命令的 `payload_hash`。
- 草稿或日程提案 ID 与版本。
- `expires_at`、`approved_execution_deadline_at` 和现有决定字段。

审批预览和 Worker 执行时才在受控内存中解密完整命令。日志、SSE、审计和普通错误不得输出
解密命令。

### 8.2 决定规则

- 待审批决定窗口为创建后 10 分钟。
- 当前有效登录会话和 CSRF 即可决定，不要求密码二次验证。
- 决定必须携带当前 `version` 和 `payload_hash`。
- 编辑已提交草稿或提案必须先使旧审批失效，再产生新版本。
- 已批准操作必须在批准后 5 分钟内由 Worker 完成幂等认领；超时则安全失败并要求重新审批。
- 认领后即使超过执行截止时间，也必须完成结果核对，不能把可能已执行的调用当作未执行。
- 连接断开、能力关闭或载荷变化会阻止尚未认领的操作。

### 8.3 审批预览

邮件预览必须展示：

- 供应商、账户、全部 To/CC/BCC。
- 新邮件、回复或全部回复模式。
- 完整主题和纯文本正文。
- 不可撤销提示、版本、到期时间和风险等级。

日程预览必须展示：

- 供应商、账户、目标日历。
- 创建、修改或恢复类型。
- 修改前后字段差异、时区、参会人和通知策略。
- 当前检测到的冲突、ETag 版本和可补偿性说明。

前端不能只显示原始 JSON。

### 8.4 风险等级

M2 风险等级只影响预览、审计和指标，不改变「每次写入都要审批」的规则：

- `high`：所有邮件发送；带参会人或发送通知的日程创建、修改和恢复。
- `medium`：不含参会人且 `notification_policy=none` 的个人日程创建、修改和恢复。

M2 没有可以免审批的低风险外部写操作。用户已选择不进行密码二次验证，因此两类风险都使用
当前安全会话、CSRF、10 分钟决定窗口和精确载荷校验。

## 9. 邮件助手

### 9.1 草稿生成

模型输入只包含：

- 用户本次指令。
- M1 已有线程摘要。
- 最近最多 3 封与回复直接相关的已清洗正文。
- 清洗后正文合计最多 12,000 个 Unicode 字符。
- 明确的语气、语言和长度要求。

模型输入继续删除签名、历史重复引用、跟踪内容和明显敏感模式。垃圾邮件正文不发送给模型。
Prompt 使用版本文件 `mail_draft_v1.md`，模型输出只包含正文候选；地址、主题和线程字段由应用层
确定性处理。

模型不可用或输出验证失败时，用户仍可创建空白本地草稿并手工编辑。系统不得把模型失败伪装成
发送失败。

### 9.2 草稿编辑

- 正文仅支持纯文本和换行，不解释 Markdown 或 HTML。
- 每次成功保存生成单调递增版本。
- PATCH 必须携带当前版本，陈旧版本返回 409。
- 自动补全只使用本地已同步历史参与者，不调用供应商 Contacts API。
- 用户可以输入任意语法合法地址；地址验证失败时不能提交审批。
- 回复和全部回复的原始线程、发送连接和确定性回复主题不可改变；需要更改主题时必须转换为
  新邮件。
- 发件地址固定为连接主账户；不解析或使用 Gmail Send As、Microsoft 代理发送或共享邮箱别名。
- 回复全部默认排除用户自己的连接地址并对地址去重，用户冻结前可以调整 To/CC/BCC。

### 9.3 Gmail 发送

- 使用 `gmail.send` scope 和 `users.messages.send`。
- 生成 RFC 2822 纯文本 MIME，并进行 Base64URL 编码。
- 回复必须提供原始 `threadId`、匹配主题，以及符合 RFC 2822 的 `In-Reply-To` 和
  `References`。
- 不调用 Gmail Draft API。
- 使用稳定 Message-ID 或等价关联信息进行 Sent 结果核对；若供应商无法证明结果，进入
  `needs_attention`。

### 9.4 Microsoft Graph 发送

- 使用委托 `Mail.Send`，不为本地草稿申请 `Mail.ReadWrite`。
- 新邮件使用直接发送接口；回复和全部回复使用对应的直接发送语义。
- 正文 ContentType 固定为 Text，并保存到 Sent Items。
- 使用 Graph 请求标识、支持的自定义关联信息和 Sent Items 只读查询进行核对。
- 若直接回复接口不能无损表达冻结命令，适配器必须在审批前拒绝，不得创建隐藏供应商草稿或
  丢弃收件人字段。

### 9.5 邮件结果

成功结果至少保存：

- 供应商消息 ID、线程或会话 ID。
- 供应商请求关联标识。
- 发送确认时间。
- 不含正文和完整地址的结果摘要。
- 可打开的供应商 Sent Items 链接；无法生成时明确为空。

邮件发送成功后不可补偿。界面不得提供「撤回」或暗示可撤回的按钮。

## 10. 日历助手

### 10.1 工作时间与冲突建议

用户设置新增：

- 每周工作时间：一周中的每一天可包含零个或多个不重叠时间段。
- 工作时区：复用用户 IANA 时区。
- 会议缓冲：0～120 分钟，默认 10 分钟。

迁移默认工作时间为周一至周五 09:00～18:00。用户修改后立即用于新建议，不追溯修改已冻结
提案。

确定性建议算法：

1. 读取用户全部已连接且同步新鲜的日历事件。
2. 把事件按用户 IANA 时区映射到工作时间。
3. 在事件前后加入配置的缓冲。
4. 按 15 分钟网格搜索未来 14 天。
5. 返回最多 3 个满足原始时长的候选时间。
6. 不查询参会人 Free/Busy，也不声称参会人可用。

若任一连接同步陈旧或失败，候选结果必须标记完整性为 partial，并展示缺失账户。模型只生成
解释文字，不能修改候选时间。

### 10.2 创建日程

- 支持定时和全天非重复事件。
- 有参会人的事件默认 `notification_policy=all`。
- 无参会人的个人事件默认 `notification_policy=none`。
- 用户冻结前可以显式更改通知策略。
- Google `sendUpdates=none` 可能影响外部日历同步，审批预览必须显示供应商警告。
- 创建使用稳定 `operation_id` 映射到供应商支持的事件关联标识，降低重复 POST 风险。
- 目标日历必须来自已同步日历目录，且供应商访问角色明确允许创建事件。

### 10.3 修改日程

- 只允许修改非重复事件；发现 recurring master 或 instance 时返回稳定不支持错误。
- 提案生成时保存 `base_etag` 和加密 before snapshot。
- 目标事件和日历必须在供应商目录中明确标记为当前账户可修改。
- 执行前读取当前事件并比较版本；不同则返回冲突，不调用更新接口。
- 时间、地点或参会人变化默认发送更新通知；纯个人字段变化默认不发送，冻结前可调整。
- 更新必须提交完整期望状态或供应商安全 Patch，不能依赖未展示的供应商默认值。

### 10.4 恢复日程

恢复是新提案而不是数据库回滚：

1. 读取历史修改前快照。
2. 读取供应商当前事件与 ETag。
3. 计算当前状态到历史状态的差异。
4. 用户确认通知策略并提交新审批。
5. 使用新 ETag 条件更新。

若事件已删除、成为重复事件或用户不再拥有修改权限，恢复提案不可执行并显示明确原因。

## 11. OAuth、连接与权限

### 11.1 能力模型

每个连接为下列能力保存独立状态：

- `mail.read`
- `mail.send`
- `calendar.read`
- `calendar.write`

能力状态为：

- `disabled`：用户未启用或已本地关闭。
- `authorizing`：正在进行渐进 OAuth。
- `enabled`：本地启用且最近一次验证存在所需 scope。
- `degraded`：Token 可用但供应商调用持续失败。
- `action_required`：需要重新授权或管理员同意。
- `revoked`：供应商明确撤销。

创建提案、提交审批和 Worker 执行三个边界都必须检查能力；前端隐藏按钮不是授权。

能力依赖固定为：

- `mail.send` 依赖 `mail.read`，因为回复线程绑定和 Sent 结果核对需要读取能力。
- `calendar.write` 依赖 `calendar.read`，因为冲突、ETag 和写后核对需要读取能力。
- 对应写能力启用期间不能单独关闭其读取能力；必须先关闭写能力。

### 11.2 Google scope

基础身份：

- `openid`
- `email`

邮件读取能力：

- `https://www.googleapis.com/auth/gmail.readonly`

日历读取能力：

- `https://www.googleapis.com/auth/calendar.readonly`

邮件发送能力追加：

- `https://www.googleapis.com/auth/gmail.send`

日历写能力追加：

- `https://www.googleapis.com/auth/calendar.events`

### 11.3 Microsoft delegated scope

基础身份和离线访问：

- `openid`
- `profile`
- `email`
- `User.Read`：Microsoft Graph `/me` 获取当前用户稳定 ID 和邮箱地址所需的最小
  delegated permission；个人账户与工作/学校账户均适用。
- `offline_access`

读取能力：

- `Mail.Read`
- `Calendars.Read`

邮件发送能力：

- `Mail.Send`

日历写能力：

- `Calendars.ReadWrite`

Microsoft OAuth 使用允许个人 Microsoft 账户和工作/学校账户的端点。`User.Read` 仅用于
Graph `/me` 的当前用户身份投影，不是目录权限或应用权限；不得借此申请 Contacts、
`Mail.ReadWrite` 或任何超出 M2 写动作边界的权限。连接唯一身份由供应商、tenant/account
类型和 Graph 用户 ID 共同规范化，不能只使用邮箱。

Trusted Action 允许列表与认领边界使用供应商固定为小写 `google` 或 `microsoft` 的精确三段
canonical key：
`provider:encoded_tenant:encoded_account`。Google 的 tenant 是空段，形成
`google::<encoded_account>`；Microsoft 保留现有 `provider_account_id=<tenant>:<graph_user_id>`
持久格式，但 canonical key 去除重复 tenant，形成
`microsoft:<encoded_tenant>:<encoded_graph_user_id>`。这只是物理编码，逻辑身份仍是供应商、
tenant 与稳定 provider account ID 三部分。

tenant/account 都按 opaque 文本处理：RFC 3986 unreserved 字符
`A-Z a-z 0-9 - . _ ~` 保持原样，其他字符先编码为 UTF-8 字节，再使用大写 `%HH`。因此普通
合成键仍保持 `google::synthetic-account` 或
`microsoft:synthetic-tenant:synthetic-graph-user`，而 `@`、`:`、`%` 与非 ASCII 字符必须编码。
每个解码后的 raw component 最长 255 个 Unicode 字符，每个 encoded segment 最长 3060 个
ASCII 字符，以覆盖 255 个四字节 Unicode 字符。Microsoft 现有
`<tenant>:<graph_user_id>` 持久复合值总长仍不得超过 255，内嵌 tenant 必须一致；Graph 用户
ID 含原始 `:` 时因旧复合格式存在歧义，OAuth 边界必须 fail closed。

parser 必须严格拒绝 malformed escape、小写 hex、对 unreserved 字符的过度编码、未编码保留
字符、空白/控制字符、非法 UTF-8、长度越界和任何非 canonical 表示。完整 raw 或 canonical
身份键不得进入日志、异常、指标、审计、SSE、API 响应或提交的验收证据。

### 11.4 渐进授权

- 首次连接允许只申请读取能力。
- 新连接由用户选择邮件读取、日历读取或两者；不得强制申请未选择的数据源权限。
- 用户启用某一写能力时重新发起 OAuth，并请求当前已启用能力的并集。
- 回调后必须以实际返回 scope 更新能力；缺失 scope 的能力进入 `action_required`。
- Token 响应没有新 refresh token 时保留仍有效的既有加密 refresh token，不能写空覆盖。
- Microsoft 租户要求管理员同意时，界面显示稳定错误和管理员操作说明。

Google 与 Microsoft callback 都必须要求 non-empty `state`，并且只接受互斥的 `code` 或 `error`。缺少
state、同时出现 `code+error`、两者都缺失，或 error 为空/超出现有输入边界时，必须在消费 state 前拒绝。
任何通过基本输入边界的 non-empty `error+state`——包括供应商返回但本地尚未识别的 error code——都必须
先按 state 解析并一次性消费 OAuthAttempt；若它是 target-`T` recovery attempt，则在同一失败收敛路径提交
matching `oauth.refresh_recovery_unsatisfied`，把仍匹配 `T` 的 requested capabilities 收敛为
`action_required`，stale `T` 则明确 no-op。callback 的一次性消费、target-`T` 收敛、raw error 脱敏和 replay
拒绝是统一不变量，但公开 Problem 与持久稳定 `error_code` 必须走安全分类矩阵，不得抹平为单一 Problem：

- Microsoft 的 `AADSTS65001` 或其他明确 consent evidence 映射为既有
  `microsoft_admin_consent_required`，并保留既有管理员操作指引；
- 明确的用户拒绝映射为 `oauth_authorization_failed`；
- 其他已知且可安全分类的错误保留既有稳定协议，例如普通 Microsoft `interaction_required` 映射为
  `microsoft_reauthorization_required`；
- 只有名称未知但输入合法的 error 才回退为通用 `oauth_authorization_failed`。

供应商 raw error、description、error codes 或正文不得作为持久错误码，也不得持久化、写日志或进入 Trace。
首次未知 error 不能因名称未知而跳过 state 消费；同一 state 随后以相同或不同未知 error 重放都必须因 state
已消费而拒绝。Google 与 Microsoft 保持相同的一次性消费、target-`T` 收敛、脱敏与 replay 语义，但允许按
上述矩阵返回不同的稳定 Problem。两家 callback 都不得让错误入口绕过下述 targetless fenced-identity 保存前
阻断。

已有 `connected` connection 的渐进授权和重新同意继续使用现有 connection-bound start：start 事务锁定
原 connection，读取 `source_generation=S`，按现有规则只递增一次到
`target_generation=T=S+1`，并把 connection ID 与 target `T` 写入 `OAuthAttempt`。若该 connection 恰有一个
未解决的自动 refresh fence，且 current `refresh_token_identity_v1` 等于 fence 的 old identity，则同一
事务允许用户显式发起恢复，并追加 content-free
`oauth.refresh_recovery_authorization_started`。该事件精确绑定 recovery schema/source、
`OAuthAttempt.id`、connection digest、原 fence 的 `refresh_attempt_id`/started source、`F/S/T`、两个
frozen pre-digest、old identity/固定 identity key version 与稳定 result code；其 `created_at` 严格晚于原
automatic started。它不是第二个 `oauth.refresh_started`。callback 通过 `OAuthAttempt.id` 查询该关联，在
交换授权码前取得同一 connection 的 coordinator lease，并继续使用现有 target-`T` anti-replay/CAS。

unknown fence 只阻断使用旧 refresh token 的自动 OAuth refresh grant；它不阻断用户明确发起、由一次性
state/PKCE/OAuthAttempt 保护的 authorization-code exchange。每个 code 最多交换一次。missing/same refresh
token、用户拒绝、管理员同意缺失或可安全确认的供应商/网络失败属于 unsatisfied recovery：connection
保持 `connected`、authorization generation 保持 target `T`，仍匹配 `T` 的本次 requested capabilities
在与 `oauth.refresh_recovery_unsatisfied` 相同的短事务中从 `authorizing` 收敛为
`action_required` 并写稳定 `error_code`；实际 scopes、last-verified facts 和既有 credential 保持不变。
若 `T` 已过时，能力更新为 no-op，只允许追加不会覆盖较新授权事实的安全审计。无论本地结果事务是否
成功，同一 state/code 都不得重放；只有新的用户操作才能创建下一 OAuthAttempt。只有 callback 明确得到
non-empty、与旧 plaintext 不同的新 refresh token，并把 credential、实际 scopes、capabilities 与
`oauth.refresh_credential_replaced` 在同一 CAS 事务提交时，才消费原 automatic fence。

无 connection target 的 `/provider/start` 保持普通首次连接/身份合并语义，不承担 fence 恢复。callback
完成一次性 exchange 并规范化身份后，若命中已有 `connected` connection 且发现 unresolved refresh fence，
必须在保存任何 credential、scope 或 capability 前返回稳定阻断结果；不得消费 fence、创建第二个
connection 或调用无条件 upsert。恢复该 connection 只能由带 `connection_id` 的显式渐进授权 start 发起，
无需给 targetless OAuthAttempt 增加 candidate snapshot、迁移或 API 参数。

Google `calendar.events` 和 Microsoft `Calendars.ReadWrite` 都是比 M2 动作集合更粗的供应商
权限，技术上可能允许删除事件。应用层端口、类型化命令联合和审批 Schema 不暴露删除动作；
供应商 scope 较粗不能成为扩大产品权限的理由。

### 11.5 关闭能力与断开连接

关闭单项能力：

- 本地立即阻止新提案。
- 取消尚未认领的相关审批和任务。
- 已认领执行进入核对，不能伪装为已取消。
- 不承诺供应商远端已删除单一 scope；彻底缩减权限需要断开并重新连接。

断开整个连接：

- 先删除本地 Token 密文并标记连接断开。
- 尽力调用供应商 revoke。
- 取消尚未认领的所有相关任务。
- 保留符合周期的历史结果和不含敏感内容的审计。

## 12. Microsoft 增量同步

### 12.1 邮件

- Microsoft 的 `mailbox` scope 是目录发现触发器，不是 Graph folder 或 Delta scope；周期
  Scheduler 与每日简报对每个 connection 只触发一次 mailbox owner。owner 成功发现目录后，
  按稳定 `scope_key` 顺序串行调用每个真实 folder 的独立同步；folder task 只用于显式恢复或
  人工维修。每个真实 folder 的 cursor 仍是增量同步事实，mailbox placeholder 不保存 Delta URL。
- 初始读取最近 7 天的可访问邮件，排除 Deleted Items 和 Junk Email，并确保覆盖 Sent Items。
- 使用 Microsoft Graph Delta 保存每个真实 folder 的 `deltaLink`，不得用 connection 级游标覆盖多个
  folder；目录发现成功时间单独记录在 mailbox placeholder 的 `last_success_at`。
- 规范化 Graph message ID、conversation ID、internetMessageId、参与者、主题、正文、时间、
  分类和 webLink；Graph `lastModifiedDateTime` 必须规范化为可选的 `provider_updated_at`。
  较旧的 provider projection 不得覆盖较新的版本；缺失或畸形的 Microsoft 版本字段按永久响应
  错误处理，Google/Fake 可保持 `NULL` 兼容。
- `@removed` tombstone 不伪造版本，只按 `(user_id, connection_id, mailbox_scope_key,
  provider_message_id)` 精确删除；旧 folder 的移动墓碑不得删除新 folder projection。
- 原始 Graph JSON、附件和完整 MIME 不持久化。
- Delta 失效时清除游标并执行受限 7 天重新同步。
- collection 与 Delta 的单次 HTTP 响应上限为 5 MiB；同步链还必须有固定总 wire/规范化字节预算，
  并同时受页数和 item 数上限约束。必须流式读取、超限立即中止并关闭响应；不得把超限数据带入
  持久化事务或推进 cursor。`@odata.nextLink` 即使同主机也必须精确匹配预期 collection/folder
  path，错误 path 在发请求前拒绝，opaque query 原样转发。

### 12.2 日历

- 先同步用户可见日历目录，规范化日历 ID、名称、时区、primary 标记、访问角色、可写能力和
  provider URL。Google CalendarList 目录使用供应商 sync token 增量读取；Microsoft Graph
  v1.0 `GET /me/calendars` 不提供目录 Delta，每轮都必须从固定 collection URL 开始执行有界
  完整快照，只跟随严格绑定 `graph.microsoft.com/v1.0/me/calendars` path 的
  `@odata.nextLink`。Microsoft 最终目录页既不要求也不接受 `@odata.deltaLink`，目录响应也不
  接受 `@removed` tombstone。
- 供应商中立目录分页必须显式携带 `full_snapshot`（或等价强类型）事实，且同一分页链的语义
  必须一致。仓储只有在 `full_snapshot=true` 时才可把快照中缺席的日历解释为删除；增量目录
  只能应用供应商明确返回的 tombstone，不能再从 cursor 是否为空推断完整性。
- Microsoft directory 的 provider cursor 永远保持 `NULL`；本地并发版本不得伪装成普通字符串
  cursor、Delta token 或 Graph URL。并发陈旧快照使用与 provider cursor 分离的本地持久 revision
  做 CAS；当前 Schema 复用 directory cursor 行的 `last_success_at` 作为观察 revision，无需新增
  Microsoft 表或迁移。两个从同一 revision 开始的完整快照最多只有一个可以提交。
- 初始读取用户时区下过去 1 天至未来 30 天的事件窗口，与 M1 Google 行为保持可比。
- Google Sync Token 和 Microsoft Calendar View Delta 都按单个日历保存，不能用连接级游标覆盖
  多个日历。Microsoft CalendarView Delta 的初始请求、后续 `@odata.nextLink` 和最终
  `@odata.deltaLink` 必须始终绑定同一个真实 calendar ID；opaque query 原样保存和转发。
- Microsoft directory 的 404/410 是固定永久供应商错误，不能伪装成不存在的目录 cursor
  expiry；精确事件 GET 的 404 返回 `None`；已持久 CalendarView deltaLink 的后续请求遇到
  404/410 或 `syncStateNotFound` 时，只使对应 calendar scope 的 cursor 失效并执行受限窗口重建。
- 规范化事件 ID、日历 ID、ETag/changeKey、时间、时区、参会人、状态和 webLink。适配器在
  持久化前必须执行与共享列长度一致的边界检查，拒绝 C0/DEL、NUL、非法邮箱和不安全 URL；
  任何畸形供应商 item 都转换为不回显原值的固定永久错误，不能下沉为数据库 DataError。
- Graph 显式 offset 必须与声明时区在该本地时刻一致，事件必须满足 `end > start`；全天事件还
  必须在同一时区的本地午夜边界开始和结束。重复投影按 Graph event `type` 验证：series master、
  occurrence、exception 和 single instance 的 recurrence/seriesMasterId 组合不得矛盾或降级为
  看似可写的非重复事件。
- 重复和会议字段可以读取并展示，但 M2 写提案必须拒绝不支持的重复事件修改。Microsoft 目录
  或任一 CalendarView 请求返回 403 时立即停止该连接剩余日历读取，仅把 `calendar.read` 标记为
  action required；401 reauthorization 才可按连接过期路径处理。

### 12.3 数据模型对齐

`EmailThread` 继续以 `(connection_id, provider_thread_id)` 唯一，`EmailMessage` 继续以
`(connection_id, provider_message_id)` 唯一。`CalendarEvent` 不得复用邮件对象的连接级二元
身份：Google 与 Microsoft 的事件 ID 都只能在单个日历内作为稳定供应商身份，Google Calendar
尤其明确自定义 event ID 只要求在目标 calendar 内唯一。因此事件唯一键固定为
`(connection_id, calendar_id, provider_event_id)`，同一连接下两个日历允许出现相同 event ID，
但同一三元组仍必须拒绝重复。

所有 `CalendarEvent` upsert、精确事件读取、单事件 tombstone、ETag/版本匹配和恢复准备查询都
必须显式携带 `user_id + connection_id + calendar_id + provider_event_id`；禁止只凭连接与事件 ID
跨日历覆盖或读取。目录 tombstone 或完整快照缺席按 `calendar_id` 清理整个来源缓存仍是合法的
日历级操作。所有硬编码 `provider == "google"` 的共享查询必须改为通过连接类型和供应商适配器
选择，不能复制第二套 Microsoft 领域模型。

`CalendarEvent` 的描述和地点密文必须绑定同一完整事件身份。历史 v1 AAD
`user_id:connection_id:provider_event_id:field` 只用于识别需要重同步的旧记录，不能用于正常解密。
当前 v2 必须由共享纯函数 `calendar_event_field_aad_v2`（或等价稳定命名）生成：固定 domain 后只按
`user_id`、`connection_id`、`calendar_id`、`provider_event_id`、`field` 顺序写入五个
`uint32_be(length) || raw_bytes` frame，禁止未分帧冒号拼接。同步 writer 与精确事件 reader 必须调用
同一个版本化 helper，不能复制编码、去掉 `calendar_id`、执行 Unicode normalization 或提供 legacy
fallback。所有新同步写入只允许使用第 17.1 节冻结的修正后 v2。

邮件消息额外保存 nullable `provider_updated_at`；历史 Google 行允许为 `NULL`。消息与线程的
描述字段更新必须遵守供应商版本排序，`latest_message_at` 只能单调增加。消息身份和线程归属
使用 connection/user/thread 组合约束，禁止跨用户或跨 connection 的 projection 覆盖。

## 13. 幂等、结果核对与补偿

### 13.1 幂等认领

每个 ToolExecution 的幂等键由以下事实组成：

```text
action + task_id + approval_id + approval_version + operation_id
```

数据库唯一约束先于供应商调用提交。重复 Worker 若看到：

- `succeeded`：复用标准化结果。
- `confirmed_failed`：复用明确失败。
- `retryable_failed`：按持久策略重新尝试同一执行。
- `claimed`、`executing` 或 `reconciling`：不得写入，只能核对。
- `needs_attention`：停止并等待用户处理。

### 13.2 安全重试

只有以下情况允许自动重试写请求：

- DNS、连接建立或本地序列化在请求可能发送前失败。
- 供应商以有文档保证的响应明确拒绝且未创建资源。
- 核对接口能够权威证明操作未应用。

请求发送后的超时、连接中断和语义不明确的 5xx 必须先核对。适配器返回内部
`ProviderWriteOutcome`，明确区分 `confirmed_applied`、`confirmed_not_applied` 和 `unknown`；
应用层不得根据普通异常字符串猜测。

OAuth automatic refresh grant 是单独的未知结果边界，不能套用“普通 retry”。每个既有 connection 的
automatic refresh 必须先由 `OAuthRefreshCoordinator` 取得 connection lease，并在网络前提交自己的
`oauth.refresh_started`；unknown、锁丢失或 CAS/commit 未知后，Taskiq/`TransientProviderError` 重入只读
unresolved fence，`provider_calls == 0`。用户显式发起的 progressive authorization-code exchange 使用同一
connection lease 和一次性 OAuthAttempt/state，但不创建第二个 refresh started，也不受 automatic fence 的
provider-call 阻断；同一 code 仍只能交换一次，失败后只能由用户创建新的 OAuthAttempt。

### 13.3 核对

核对使用只读能力和稳定关联信息：

- Gmail：Sent 中的供应商消息 ID、线程、Message-ID 或等价关联条件。
- Microsoft Mail：请求关联标识、Sent Items 和可验证邮件属性。
- Google Calendar：客户端事件 ID、目标日历和事件资源。
- Microsoft Calendar：操作关联标识、事件 ID 和版本信息。

核对采用有界持久重试，默认 1、5、30、120 秒共四次。仍不能确定则进入
`needs_attention`。

### 13.4 人工结果确认

`needs_attention` 页面允许：

- 重新运行只读核对。
- 打开供应商对应位置。
- 记录「已执行」或「未执行」。

人工结论必须记录 user、时间、ToolExecution、枚举结果原因和供应商检查入口。接口不接受
可能包含正文或地址的自由文本说明。确认「未执行」不会自动重发；用户必须克隆为新提案并
重新审批。

### 13.5 撤销与补偿

- 撤销连接或能力只取消尚未认领的操作。
- 邮件发送没有补偿操作。
- 日程修改成功后可生成恢复提案。
- 日程恢复若遇到版本变化、权限变化或目标不存在必须拒绝。
- 自动补偿禁止用于覆盖供应商中的后续合法修改。

## 14. 数据模型与迁移

### 14.1 新实体

| 实体 | 关键字段与约束 |
|---|---|
| `connection_capabilities` | `user_id`、`connection_id`、`capability` 唯一；状态、实际 scope、验证时间、错误码 |
| `provider_calendars` | `connection_id + provider_calendar_id` 唯一；名称、时区、primary、访问角色、可写状态和 provider URL |
| `mail_drafts` | `user_id`、`connection_id`、源线程、当前版本、状态、保留截止时间 |
| `mail_draft_versions` | `draft_id + version` 唯一；地址与主题元数据、正文 AEAD 三元组、Prompt/模型版本 |
| `calendar_change_proposals` | 用户、连接、日历、操作类型、目标事件、基础 ETag、版本、状态 |
| `calendar_change_snapshots` | 提案归属、内容 AEAD 三元组、规范哈希、保留截止时间 |

所有用户域实体直接包含非空 `user_id`，并用组合外键防止跨用户、跨连接归属错配。

现有 CalendarEvent 增加规范化 organizer、attendees、访问角色或 `can_edit` 投影。同步游标新增
非空 `scope_key`，唯一键扩展为 `connection_id + resource_kind + scope_key`：邮箱使用稳定 mailbox
或 folder key，日历使用 provider calendar ID。迁移期间现有 Google 游标回填到明确的 primary
calendar scope，不能丢失原同步位置。

CalendarEvent 的供应商身份从旧 `(connection_id, provider_event_id)` 放宽为
`(connection_id, calendar_id, provider_event_id)`。迁移不得删除、合并或重写任何事件业务行；
必须先建立并验证新的三元唯一索引，再把它挂载为稳定命名约束，最后才移除旧二元约束。

现有 `CalendarEvent` 还增加 `description_aad_version` 和 `location_aad_version`。
`description_ciphertext + description_nonce + description_key_version + description_aad_version` 必须
全为 `NULL` 或全为非空；`location_ciphertext + location_nonce + location_key_version +
location_aad_version` 使用相同的独立四列约束，两个字段不能共用版本标记。`aad_version` 是封闭
版本标记：仅识别历史 `v1` 和当前 `v2`，其他值不得被当作任一已知格式读取。`v2` 逐字表示
第 17.1 节的 canonical framing；由于 `0019` 尚未发布，本次直接修正 v2，不新增 v3、兼容分支或
旧 v2 fallback。

### 14.2 扩展实体

`ApprovalRequest` 增加：

- `schema_version`
- `risk_level`
- `payload_ciphertext`
- `payload_nonce`
- `payload_key_version`
- `proposal_kind`
- `proposal_id`
- `proposal_version`
- `approved_execution_deadline_at`

M1 假写记录允许在迁移期保留原 JSONB payload；所有 M2 真实命令必须使用加密列。读取路径按
`schema_version` 明确区分，不能静默回退到明文。

现有非空 JSONB `payload` 列在 M2 保留。M2 记录只写入不含敏感内容的存储标记和 Schema
版本，例如 `{"storage":"encrypted","schema_version":"mail_send.v1"}`；完整命令只存在于
AEAD 列。加密 AAD 至少绑定 `user_id`、ApprovalRequest ID、action 和 schema_version，防止
跨用户、跨记录替换密文。

`ToolExecution` 增加：

- `operation_id`
- `provider`
- `provider_resource_id`
- `provider_request_id`
- `correlation_id`
- `claimed_at`、`request_started_at`、`completed_at`
- `reconciliation_attempt_count`、`last_reconciled_at`
- `manual_resolution`、`manual_resolved_by_user_id`、`manual_resolved_at`

`User` 或 `UserSettings` 增加：

- 默认邮件连接。
- 默认日历连接与日历 ID。
- 类型化每周工作时间。
- 会议缓冲分钟数。

### 14.3 迁移策略

1. 邮件身份迁移采用两阶段 online expand/contract：0016 只添加 nullable
   `email_messages.connection_id` 与 `provider_updated_at`、回填并 fail-closed 检查，保留旧
   unique/约束；0017 在短 metadata 事务外使用 `CREATE UNIQUE INDEX CONCURRENTLY`，再通过
   `USING INDEX`、`NOT VALID`/`VALIDATE` 复合 ownership FK 和 validated `NOT NULL` check 完成
   contract，最后才移除旧 unique。CONCURRENTLY 失败留下的 valid/invalid index 必须可安全探测、
   清理和重跑，不得假设 Alembic 单事务包住该操作。上线顺序固定为：先应用仍兼容旧应用实例的
   0016 nullable expand；再部署同时兼容 0016 与 0017、始终双写 `connection_id` 和
   `provider_updated_at` 的 Repository；排空全部旧应用实例后才应用 0017。0017 必须先以与
   0016 相同的有界 autocommit 批处理追赶旧实例在部署窗口留下的 `connection_id IS NULL` 行，
   然后才能执行 preflight、并发索引和 contract，并在最后移除旧 unique。供应商网络 I/O 在
   整个部署窗口内仍位于数据库事务之外。
2. CalendarEvent 身份迁移使用一个受控 contract revision：先在旧二元约束仍存在时以
   `CREATE UNIQUE INDEX CONCURRENTLY` 建立并验证
   `(connection_id, calendar_id, provider_event_id)` 索引；只允许清理本迁移固定名称、且经 catalog
   证明形状完全一致的 invalid 索引，同名错误对象必须 fail closed。随后在短 metadata 事务中
   `UNIQUE USING INDEX` 挂载新约束并移除旧二元约束，全程保留业务行。旧代码和新代码都通过命名
   约束执行 `ON CONFLICT`，因此该切换不支持旧新 Calendar 写入实例无缝混跑。上线顺序固定为：
   先关闭 Calendar 周期调度，排空并停止全部可能执行 `sync_calendar` 的旧 Worker；确认无旧事件
   upsert 事务后应用迁移；再部署引用新三元约束的 API/Worker/Scheduler，最后恢复 Worker 与调度。
   迁移后禁止旧 Worker 回流。若 downgrade 前已产生跨日历同 ID，旧二元约束无法无损恢复，必须
   fail closed 并先由人工制定数据保留方案，迁移不得删除任一事件来强行回退。
3. `0019` 是紧随 CalendarEvent 身份迁移的前向 AAD 轮换 revision。Task 16A 首次创建该 revision
   时就必须同时冻结最终 typed rollout guard 协议与第 17.1 节的 v2 canonical framing helper，并让
   migration 在任何 DDL/DML 前调用；
   `backend/migrations/env.py` 还必须通过 Alembic `on_version_apply` 在版本表更新后、外层迁移事务
   最终提交前再次调用同一 guard。Task 16A 还必须在同一个 Alembic 外层在线迁移边界冻结独立的
   management/target/schema 三层 lifecycle boundary 与 schema-lifecycle session advisory 读写锁，固定键为
   `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`：所有 migration 入口在打开外层迁移事务前执行
   管理库 `postgres` target lifecycle lock，再取得目标 maintenance lock/catalog guard，最后执行
   `pg_advisory_lock(20260806, 143)` 取得 exclusive lock，并直到该事务明确 commit/rollback 后才释放；
   Task 27D 的普通备份则从读取 dump 前 Alembic revision 起执行
   `pg_advisory_lock_shared(20260806, 143)`，覆盖完整 `pg_dump`、post-revision CAS 与本地 manifest-last
   发布。Compose migration、`just db-upgrade`、integration fixture、E2E bootstrap 与专用 0019 migration
   one-off 都必须经过这一外层边界，不能各自实现可漂移的锁协议；新建数据库必须先通过第 22 节的
   受控 `bootstrap_candidate → pristine_idle` 过渡，之后才能进入普通 migration。该锁只绑定 dump 与 schema revision，
   不改变本 revision 已冻结的 typed guard 分支，也不替代 `(20260809, 19)` rollout lease 或恢复时的
   database-wide maintenance/call authority 与 CONNECT revocation。锁序固定为 management lifecycle →
   target → schema；`db-reset` 必须由同一 typed lifecycle wrapper持锁覆盖 catalog 检查、drop、create、
   bootstrap transition 与 migration，不能直接执行未受保护的 `dropdb`/`createdb`。普通
   `upgrade head` 仍是已经完成 catalog bootstrap 的 fresh database、CI/E2E、Compose migration
   service 和常规运维的唯一迁移入口；它不得把 PostgreSQL 默认 ACL 新库直接视为 `pristine_idle`。
   这里的 0019 guard 仍只有两个封闭分支：

   - 已经通过普通 catalog admission 的 fresh/new database，或在 0019 mutation 前以同一查询严格证明
     affected set 为空时，使用内建 affected-set zero-bootstrap 分支，不要求生产 rollout artifact，但必须
     在 mutation 前和最终提交前重复空集合证明；该分支与数据库 role/ACL bootstrap 是两件事，不能
     接受或修复 `bootstrap_candidate`；
   - 数据库当前精确位于 0018 且存在历史完整 AEAD 三元组时，必须注入 schema-valid typed rollout
     guard；缺失、错误类型、artifact/image/affected-set/deadline 不匹配都在任何 DDL/DML 前 fail closed。

   Task 27C 只能实现真实 artifact guard 并通过 Task 16A 冻结的 composition boundary 注入；不得再修改
   0019 revision、`migrations/env.py` 或同 revision 的分支语义。Task 16A 之前仓库没有发布过任何
   `20260809_0019` revision；若旧开发/测试环境曾手工或实验性应用同名、未知或不完整的 obsolete 0019，
   先为该精确非生产目标创建取证备份，再仅使用上述 management lifecycle lock 与 catalog guard 保护的
   `just db-reset` 完整重建，完成 bootstrap transition 后再执行普通 `upgrade head`。holder crash 后的新 reset 必须拒绝 active/
   `needs_attention` restore authority并保持 drop/create 为零。不得新增悬空 repair recipe、重写已应用
   revision、静默 stamp、伪 downgrade 或用普通
   upgrade 假装修复既有错误 revision。生产环境一旦发现未知、同名或 obsolete 0019，必须立即停止发布并
   保持服务与写入关闭，禁止 `db-reset`、revision/stamp 改写或任何破坏性回退，直到新的已批准事故方案
   明确数据处置。Schema 变更只增加
   `description_aad_version`、`location_aad_version` 及两个字段各自“四列全空或全非空”的检查约束；
   数据变更仅将既有全非空 `ciphertext + nonce + key_version` 三元组标记为历史 `v1`，既有全空
   三元组继续保持 `aad_version=NULL`。若发现任一旧三元组部分为空，升级必须 fail closed，不能
   猜测、补齐或删除该事件。迁移在任何 DDL/DML 前还必须从历史完整三元组推导每个受影响的精确
   `(connection_id, calendar_id)` pair 及其用户归属，并同时证明以下本地可恢复性事实：

   - 精确非 `directory` Calendar 事件游标已存在；匹配必须同时满足
     `sync_cursors.connection_id = affected_connection_id`、`resource_kind = 'calendar'`、
     `scope_key = affected_calendar_id` 和 `scope_key <> 'directory'`。
   - owning OAuth connection 存在、与事件/游标属于同一用户，且状态精确为 `connected`。
   - 同用户、同连接的 `calendar.read` 能力行存在且状态精确为 `enabled`。
   - 当前 Calendar Worker 凭据解析必需的本地 AEAD credential 行存在并属于同一用户/连接；每个
     affected connection 必须同时具有 `access_token` 与 `refresh_token`，access-only 不得迁移，亦不得
     从其他连接借用、复制或伪造凭据。迁移只能证明 credential 行与归属存在；refresh token 的 AEAD
     可解密性和供应商可用性必须由生产窗口的独立 preflight 在 0019 前主动刷新证明。
   - 同用户、同连接、同 `provider_calendar_id` 的精确 `ProviderCalendar` 目录行存在。

   任一 pair 缺失上述事实，或连接已断开、能力为 disabled/revoked/其他非 enabled 状态时，升级都
   必须保持 revision `20260809_0018` 和全部数据不变，也不得创建猜测 marker。迁移本身不执行供应商
   网络 I/O；生产变更窗口还必须在备份、审计和 migration 前通过第 22 节定义的主动 refresh 加精确
   pair 只读 probe preflight。迁移不得解密、重加密、删除、合并或改写任何 CalendarEvent 内容。迁移后的
   专用 resync writer 与此后所有普通同步 writer 只允许调用第 17.1 节的共享 helper 写入修正后 v2；
   reader 必须消费同一个 helper，不能存在旧 v2 冒号编码的迁移期读写窗口。本地可恢复性 preflight 通过后，
   只失效上述精确 pair 的游标：将 `cursor` 与
   `last_success_at` 置空，并把 `last_error_code` 设为不含内容的
   `calendar_event_resync_required`；其他 connection 即使复用相同 `calendar_id` 也不得变化，
   `directory` 游标及其 freshness、revision 和错误状态必须原样保留。0019 是 forward-only；
   downgrade 必须拒绝移除版本列、约束或恢复 legacy AAD 读取，不能通过破坏性回退重新引入
   跨日历替换风险。
4. 除本节明确批准且已先建立替代事实的约束 contract 外，迁移只增加新表、新列、新索引和新状态
   值；任何迁移都不得删除业务行，也不得依赖破坏性 downgrade。
5. 为现有 Google 连接根据已保存 scope 回填读取能力，写能力统一为 disabled。
6. 先让代码兼容旧审批数据，再切换 M2 写入路径；将共享 Repository 的 Google 常量过滤改为显式
   供应商参数。

## 15. API 与 SSE

### 15.1 邮件草稿 API

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/api/v1/mail/drafts` | 分页列出当前用户草稿 |
| `POST` | `/api/v1/mail/drafts` | 创建空白或源线程草稿，返回 201 |
| `GET` | `/api/v1/mail/drafts/{id}` | 返回解密后的当前草稿视图 |
| `PATCH` | `/api/v1/mail/drafts/{id}` | 携带版本更新并创建新版本 |
| `DELETE` | `/api/v1/mail/drafts/{id}` | 取消未发送本地草稿 |
| `POST` | `/api/v1/mail/drafts/{id}/generate` | 创建异步模型草拟任务，返回 202 |
| `POST` | `/api/v1/mail/drafts/{id}/submit` | 冻结当前版本并返回 202 与 task_id |

### 15.2 日程提案 API

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/api/v1/calendar/proposals` | 分页列出提案 |
| `POST` | `/api/v1/calendar/proposals` | 创建日程创建或修改提案 |
| `GET` | `/api/v1/calendar/proposals/{id}` | 返回当前提案及冲突结果 |
| `PATCH` | `/api/v1/calendar/proposals/{id}` | 携带版本更新并重新计算冲突 |
| `DELETE` | `/api/v1/calendar/proposals/{id}` | 取消未执行提案 |
| `POST` | `/api/v1/calendar/proposals/{id}/suggest-times` | 在短事务外同步计算并返回 200 与确定性候选 |
| `POST` | `/api/v1/calendar/proposals/{id}/submit` | 冻结当前版本并返回 202 |
| `POST` | `/api/v1/calendar/events/{id}/restore-proposal` | 从历史快照创建恢复提案 |

### 15.3 操作与连接 API

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/api/v1/actions` | 统一列出草稿、提案和执行状态 |
| `GET` | `/api/v1/actions/{task_id}` | 返回可信操作快照 |
| `POST` | `/api/v1/actions/{task_id}/reconcile` | 对 unknown 结果创建只读核对任务 |
| `POST` | `/api/v1/actions/{task_id}/manual-resolution` | 记录用户检查后的结果 |
| `GET` | `/api/v1/connections/{id}/capabilities` | 返回实际 scope 与能力状态 |
| `POST` | `/api/v1/connections/{id}/capabilities/{capability}/enable` | 返回渐进授权 URL |
| `POST` | `/api/v1/connections/{id}/capabilities/{capability}/disable` | 本地关闭能力并取消未认领任务 |

所有修改请求需要当前 Cookie 会话和 CSRF。草稿与提案保存为短事务；模型生成、提交、写入、
核对和隐私删除等长任务返回 HTTP 202。

`manual-resolution` 只接受 `confirmed_executed` 或 `confirmed_not_executed` 枚举、当前任务版本
和 CSRF，不接受自由文本证据；提交前 UI 必须再次说明该决定不会调用供应商。

### 15.4 SSE 事件

现有任务 SSE 增加以下持久事件：

- `action.submitted`
- `approval.invalidated`
- `tool.claimed`
- `tool.reconciling`
- `tool.needs_attention`
- `tool.manually_resolved`

事件 payload 只包含 ID、状态、版本、错误码和脱敏摘要，不包含地址、主题、正文、日程标题、
描述或完整参会人列表。旧前端遇到未知事件必须忽略并回退任务快照。

草稿、提案和连接能力在创建任务前没有 `task_id`，因此不为它们新增第二套用户级 SSE。它们的
创建、编辑和能力变更以 REST 响应为准，操作中心在重新聚焦或恢复连接时重新拉取列表。

所有返回草稿、审批完整预览或日程敏感内容的响应必须设置 `Cache-Control: no-store`。前端不得
把这些响应写入 localStorage、sessionStorage、URL、console 或持久离线缓存。

## 16. 前端体验

### 16.1 操作中心

新增 `/actions` 页面，包含：

- 邮件草稿。
- 日程提案。
- 待审批。
- 正在执行或核对。
- 需要人工确认。
- 已完成历史。

该页面是统一投影，不成为第二份状态来源；刷新后从 API 和任务快照恢复。

### 16.2 邮件编辑器

- 展示供应商、发送账户和固定回复线程。
- 支持 To、CC、BCC、主题和纯文本正文。
- 显示模型生成中、生成失败、保存冲突和草稿版本。
- 提交审批前显示收件人数和不可撤销提示。
- 不提供附件、HTML、富文本或供应商草稿同步入口。

### 16.3 日程编辑器

- 展示账户、目标日历、IANA 时区和完整字段。
- 展示当前冲突、工作时间外警告和最多三个候选时间。
- 展示参会人和通知策略。
- 修改任一时间字段后使旧冲突结果失效并重新计算。
- 修改或恢复时展示前后差异及 ETag 冲突。

### 16.4 审批卡片

按命令类型展示结构化预览，不使用通用 JSON `<pre>` 作为正式体验。批准和拒绝期间禁用重复
提交；过期、版本变化、哈希冲突和能力撤销统一展示可执行恢复动作。

### 16.5 `needs_attention`

页面提供：

- 当前核对尝试和最后错误。
- 供应商检查链接。
- 「重新核对」「确认已执行」「确认未执行」。
- 明确警告：确认未执行不会自动重发。

所有交互支持键盘、可见焦点、程序化 label、状态 live region 和窄屏布局。

## 17. 安全、隐私与保留

### 17.1 安全边界

- 所有外部写必须通过 API 权限、连接能力和精确审批三层校验。
- 审批不使用密码二次验证，因此会话 Cookie、CSRF、会话撤销、Secure、HttpOnly 和 SameSite
  属性是强制边界。
- Provider Token 继续使用带版本 AEAD；明文只存在于受控内存。
- `AeadCipher` port 与实现必须公开只读 `key_version` 属性，composition root 只能通过该冻结接口让
  AEAD 与 refresh-token identity service 使用同一版本；不得读取私有实现字段或在 Tasks 27A–27C 另造版本来源。
- M2 命令、邮件正文、日程描述、地点和补偿快照使用字段或记录级 AEAD。
- CalendarEvent 描述和地点分别验证自己的四列原子组。四列全空时字段读取为空字符串；四列非空
  且版本为 v2 时，加解密必须调用同一个共享、版本化、无 I/O 的纯函数 helper。v2 canonical bytes
  固定为：

  ```text
  b"AIEMPLOYEE/calendar-event-field-aad/v2\x00"
  || frame(user_id)
  || frame(connection_id)
  || frame(calendar_id)
  || frame(provider_event_id)
  || frame(field)

  frame(raw_bytes) = uint32_be(len(raw_bytes)) || raw_bytes
  ```

  domain 恰好 39 bytes。五个字段都必填且只编码一次：`user_id` 与 `connection_id` 必须先验证为
  小写 canonical UUID ASCII；`calendar_id` 与 `provider_event_id` 使用严格 UTF-8 的原始标量字节；
  `field` 只允许 ASCII `description` 或 `location`。frame 没有 NULL tag、delimiter、Unicode
  normalization 或 JSON。缺失值、UUID 非 canonical、严格编码失败、未知 field、既有标量边界失败或
  任一 raw byte length 无法用 uint32 表示时，必须在调用 AEAD 前 fail closed；稳定错误不得回显原始 ID。
  writer 与 reader 禁止各自复制 framing、静默正规化、删除 `calendar_id` 或尝试 legacy bytes。

  v1 只作为历史迁移标记与受限重同步触发器；新写和正常读取均不得生成或解密 v1。读取 v1 或未知
  版本统一返回 `calendar_event_resync_required`。由于 `0019` 尚未发布，本协议直接定义修正后的 v2，
  不新增 v3，也不保留任何旧的冒号拼接 v2 fallback。

  以下完全合成向量冻结独立复算结果。规范总长度使用
  `39 + Σ(4 + len(raw_field_bytes))` 计算；expected bytes/base64 必须作为测试常量保存，不能调用生产
  helper 反向生成：

  - delimiter vector A：user `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`，connection
    `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb`，calendar `a:b`，event `c`，field `description`。
    五个 raw 长度依次为 `36, 36, 3, 1, 11`，规范总长度为 `146`，完整 base64 为：

    ```text
    QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAADYTpiAAAAAWMAAAALZGVzY3JpcHRpb24=
    ```

    独立 SHA-256 交叉检查值为
    `67ba9e40f2157a49de2d987e1f4d8e61c2a78a7d71d4da7ce13421fde58a8e70`。
  - delimiter vector B 使用相同 user/connection/field，但 calendar `a`、event `b:c`。raw 长度为
    `36, 36, 1, 3, 11`，规范总长度同为 `146`，完整 base64 为：

    ```text
    QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAABYQAAAANiOmMAAAALZGVzY3JpcHRpb24=
    ```

    独立 SHA-256 为
    `52e707318449a55779a023931f5914909d662944f5364545e1795edbcf555dad`；因此 `a:b`/`c` 与
    `a`/`b:c` 即使未分帧拼接会得到相同可见文本，也必须产生不同 canonical bytes。
  - Unicode vector 使用相同 user/connection，calendar `日历/α`、event `事件:é`、field `location`。
    两个 opaque ID 的严格 UTF-8 分别为 hex `e697a5e58e862fceb1` 与 `e4ba8be4bbb63ac3a9`；五个 raw
    长度为 `36, 36, 9, 9, 8`，规范总长度为 `157`，完整 base64 为：

    ```text
    QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAAJ5pel5Y6GL86xAAAACeS6i+S7tjrDqQAAAAhsb2NhdGlvbg==
    ```

    独立 SHA-256 为
    `0bdf656c1b037423e71c950df9f6bb5c56a737bcef8fe7d6e58e26d7dee04370`。helper 必须保留这些原始
    scalar bytes；例如 composed `é` 与 `e` + U+0301 不得被 normalize 成相同 AAD。
- 单密钥 AEAD reader 必须在调用 AES-GCM 前精确比较持久 `key_version` 与当前密钥版本；不匹配时
  抛出稳定类型化 key-version 错误，M2 不引入多 key keyring。CalendarEvent reader 遇到 cipher
  不可用、该 key-version 错误、明确类型化的解密边界错误、无效 UTF-8 或 v2 `InvalidTag` 时，必须
  以同一稳定业务错误 fail closed；只捕获这些明确类型，不捕获 `AttributeError` 或 broad
  `Exception`。任何路径都不能尝试 v1 AAD、去掉 `calendar_id`、忽略认证或返回部分明文。密文、
  nonce、key version、AAD version 任一被篡改，都不得触发 legacy fallback。
- 地址、主题和参会人不得进入日志、指标 label、Trace attribute 或 URL query。
- 所有真实 API 启动入口必须关闭 Uvicorn 默认 access logger；应用结构化日志 schema 禁止保存 raw
  URL、query string 或 request target。OAuth callback query 即使包含 `code`、`state` 或
  `error_description`，也只能由应用边界消费并写入脱敏的稳定审计结果，不能落入 stdout、stderr 或
  JSON 日志。
- 正式与开发默认不允许真实写入；显式运行开关和连接能力缺一不可。

### 17.2 保留

复用现有用户设置：

| 数据 | 默认周期 | 到期处理 |
|---|---:|---|
| 邮件草稿正文与邮件审批正文 | 30 天 | 清除 AEAD 三元组，保留哈希和脱敏结果 |
| 邮件草稿元数据与操作摘要 | 180 天 | 删除或最小化元数据 |
| 日程内容与补偿快照 | 事件结束后 180 天 | 清除内容密文 |
| Task、状态与不含内容的审计 | 365 天 | 按现有工作区历史清理 |
| OAuth 凭据 | 连接期间 | 断开时删除本地密文并尽力撤销 |

未发送草稿按最后编辑时间应用邮件正文周期。正文到期后，草稿不可再次提交，但可以保留一个
不含内容的历史占位。

CalendarEvent 描述或地点到期清理必须按字段在同一事务中同时清空其 `ciphertext`、`nonce`、
`key_version` 和 `aad_version`，不得留下只有版本或部分 AEAD 列非空的记录。

365 天工作区历史清理不得把 OAuth refresh 事件当作普通审计直接删除。generic AuditEvent delete 必须
排除 `oauth.refresh_started`、`oauth.refresh_confirmed`、
`oauth.refresh_recovery_authorization_started`、
`oauth.refresh_recovery_unsatisfied` 与
`oauth.refresh_credential_replaced`；专用 cleanup 只解析 content-free metadata，并显式按 `user_id`
过滤。

matching confirmed 只关闭它精确引用的 automatic started；matching unsatisfied 只关闭本次 recovery
authorization started，不能关闭原 unresolved automatic fence；matching replacement 同时关闭它精确引用的
recovery authorization started 和原 automatic fence，且只有 replacement 能消费原 fence。cleanup 必须使用
17.3 的同一版本化 result union 匹配关闭事实；automatic success、unsatisfied recovery 和 successful recovery
分别按完整组原子清理，且组内每一行都必须早于 cutoff，等价于组内最大
`created_at < cutoff`。旧 started 加较新 confirmed/unsatisfied/consumption 不得提前删除，不能先删 result
再留下伪 unresolved 事实。

unsatisfied recovery 组还必须按其关闭 schema 精确匹配 OAuthAttempt/recovery event、F/S/T、requested
capabilities、`capability_transition` 与稳定 `error_code`/`result_code`；`action_required` 和
`stale_target_noop` 都只关闭
本次 recovery attempt。cleanup 不读取或要求当前 capability 仍保持当时状态，避免后续新授权使完整旧组
无法清理，但绝不能因此消费原 automatic fence。

未 confirmed、未被 replacement consumption 消费的 automatic started 必须跨 cutoff 保留，并继续让
automatic provider call 为零；多次显式 recovery 的 closed unsatisfied pair 不改变该事实。cleanup 与所有
credential CAS 使用统一锁序，锁后重查 matching event、F/S/T、old/new identity/version、
`refresh_identity_changed` 与严格 `created_at` 顺序，并通过同一 result-union parser 验证 flag/equality 关系，
但不读取或约束后续 current credential lineage。M2 root key/version 固定且不得轮换，
因此 retention 不引入 historical identity key 保存或删除逻辑；root key 缺失/变化一律 fail closed。

`database.restore.completed` 不获得 retention 或隐私删除豁免。database-wide
completed `ai_employee.restore_call_authority` + matching `ai_employee.restore_completion` zero-slot pair 是
completed admission 与 ACK reconcile 的持久 catalog authority；它不依赖 AuditEvent 存续，且 completed
call 不可缺失。generic AuditEvent cleanup 按普通 365 天 cutoff 处理 completion audit，全数据删除也必须像
其他该用户 audit 一样删除它，不能为了 restore proof 绕过用户隐私删除。后续 audit retention、全数据删除或
审计分区维护使该行缺失，不得让已提交 restore 失效，也不得阻止下一次合法 restore。AuditEvent 的普通
BigInteger ID、event 数量与 metadata matcher 都不参与 admission/ACK authority；exact completion audit 只在
最终提交事务当刻作为原子审计证据校验。

### 17.3 OAuth refresh fence 与显式恢复协议

M2 只定义一个固定的 `refresh_token_identity_v1`。它不是 AEAD 物理快照摘要，而是使用现有
`APP_MASTER_KEY_FILE` 内容计算的密钥化 fingerprint；API、Worker、CLI composition root 必须从同一
Secret bytes 和同一个 `AeadCipher.key_version=v` 同时实例化 AEAD cipher 与 identity service，不新增
identity Secret、multi-key lookup 或独立轮换配置。固定算法为 HKDF-SHA256：

```text
K_fp(v) = HKDF-Expand(
    HKDF-Extract(
        salt=b"AIEMPLOYEE/oauth/refresh-token-identity/hkdf-salt/v1\x00",
        IKM=application_master_key,
    ),
    info=b"AIEMPLOYEE/oauth/refresh-token-identity/fingerprint-key/v1\x00"
         || frame(ASCII(decimal(v))),
    L=32,
)
refresh_token_identity_v1 = HMAC-SHA256(
    K_fp(v),
    b"AIEMPLOYEE/oauth/refresh-token-identity/v1\x00"
    || frame(refresh_token_plaintext),
)
```

其中 `frame` 仍是 `NULL => 0x00`、非 NULL
`=> 0x01 || uint32_be(length) || raw_bytes`；refresh plaintext 必须为 non-empty bytes，输出为小写
十六进制。M2 期间 `APP_MASTER_KEY_FILE` 的 root key 与 key version 不得更换；存在 unresolved refresh
fence 时更严禁更换。未来 root-key rotation 必须先通过新的 ADR/里程碑设计 ciphertext 重加密、identity
重算和 fence 迁移，不能在 M2 内加入隐式 keyring。identity 只作恒定时间 durable guard；old/new plaintext
同时可得时仍必须在受控内存使用 `secrets.compare_digest`。identity、token plaintext、root key 和派生
HMAC key 不得进入日志、Trace、SSE、API 或 release evidence。

identity 的运行顺序固定为：先在受控内存按精确 credential AAD 解密 refresh token，再验证 non-empty、
合法 UTF-8 与现有长度/边界条件，随后才对规范 UTF-8 bytes 计算 `refresh_token_identity_v1`。完成
started claim、提交 durable fence 并在调用前重检所需 session lease 后，才允许把已验证 token 交给
provider；不得对未解密或未验证的 ciphertext 猜测 identity，也不得因 identity 已计算而跳过 claim/lease。

OAuth refresh 只有两条状态通道：

1. **Automatic refresh grant**：0019 preflight 与 Google/Microsoft mail/calendar Worker 使用旧 refresh
   token 调用 provider。它们共享 `OAuthRefreshCoordinator`，每次获准调用都创建自己的
   `oauth.refresh_started`；已存在 unresolved automatic fence 时 provider call 为零。
2. **Explicit progressive recovery**：用户通过带 `connection_id` 的现有渐进授权 start 创建一次性
   `OAuthAttempt`，随后用 authorization code 恢复旧 fence。该通道取得同一 connection lease，但不创建
   第二个 `oauth.refresh_started`。无 target 的 `/provider/start` 不属于恢复通道。

`OAuthRefreshCoordinator` 的 automatic source union 仅为 `calendar_aad_preflight` 与
`provider_refresh`。automatic claim 先按 `connection_id` 取得 domain-separated PostgreSQL session
advisory lease，再以固定 connection → access row → refresh row → matching started audit 锁序冻结
connection generation `G`、两行完整物理 snapshot、old `refresh_token_identity_v1` 与固定 key
version。claim 事务必须先确认不存在 unresolved `oauth.refresh_started`，然后追加新的 started 并提交；
网络阶段只保留 session lease，不持有业务事务。provider 前、响应后、CAS 前和结果提交前都由同一 session
证明 lease 仍在。

`oauth.refresh_started` 使用关闭 metadata
`fence_schema_version="oauth_refresh_fence.v1"`，精确包含：

- `source`、canonical lowercase `refresh_attempt_id`、`connection_digest`；
- 适用时的 `source_revision`、`target_revision`、`rollout_digest_v1`；普通 provider refresh 对这些
  字段使用规范 NULL；
- `fence_generation=G`、`pre_credential_snapshot_digest_v1`、
  `pre_refresh_credential_snapshot_digest_v1`；
- `old_refresh_token_identity_v1`、`refresh_token_identity_key_version` 与稳定 `result_code`。

AuditEvent 的 `created_at` 是 provider call 前已提交的时序事实。任何 token、raw scope、provider
response、正文、raw `calendar_id`、ciphertext 或 credential `updated_at` 都不得进入 metadata。

provider 返回的 token response 只有通过非空 access token、正数 expiry、canonical scope 覆盖等完整验证
后才是 known-valid。coordinator 随后在另一短事务重检 lease、connection/capability/generation、两行旧
snapshot 与 old identity；credential CAS 与 matching `oauth.refresh_confirmed` 必须原子提交。valid
response 无论省略 refresh token、返回相同 plaintext 或返回不同 plaintext，都关闭**本次** started：

- missing/same refresh 只更新 access row，逐字节保留 refresh row，并要求
  `old_refresh_token_identity_v1 == new_refresh_token_identity_v1`、
  `refresh_identity_changed=false`；
- different non-empty refresh 更新两行，要求 old/new identity 不等且
  `refresh_identity_changed=true`，但只确认自己的 started，不生成
  `oauth.refresh_credential_replaced`，也不能消费任何更早 unknown fence；
- 实际上若存在更早 unresolved fence，automatic claim 必须在 provider 前阻断，因此不得出现“靠下一次
  automatic rotation 自动修复旧 fence”的路径。

`oauth.refresh_confirmed` 使用关闭 metadata
`result_schema_version="oauth_refresh_confirmed.v1"`，精确包含
`source`、`started_source`、matching `refresh_attempt_id`、`connection_digest`、适用的
revision/rollout 字段、`fence_generation=source_generation=pre_generation=post_generation=G`、两个
pre-digest、两个 required post-digest、old/new `refresh_token_identity_v1` 与各自固定 key version、
实际持久化 `token_expires_at`、`refresh_token_disposition="missing"|"same"|"different"`、
`refresh_identity_changed: bool` 和稳定 `result_code`。confirmed parser 必须拒绝 disposition、flag 与
identity equality 不一致的 metadata：missing/same 只能是 old == new 且 changed=false，different 只能是
old != new 且 changed=true。0019 preflight 还必须包含
`rollout_deadline_candidate = token_expires_at - 900 seconds`；普通 provider refresh 对该字段使用规范
NULL。该 candidate 只证明历史 confirmed result schema 与当时 persisted expiry 自洽，绝不能作为
ACK-lost 恢复后或任一后续 guard 的当前 deadline 输入。confirmed 的 `created_at` 必须严格晚于
matching started。confirmed crash 后只从持久 credential、expiry 和 post-digest 恢复，不再次调用
provider。

network/response unknown、`invalid_grant`、malformed token/expiry/scope、scope shrink、响应后 lease loss 或
CAS miss 都不能产生 confirmed；数据库明确证明 result 事务已 rollback 时，started 保持 unresolved。
provider 一旦已经被调用，无论该 rollback 是否可证明，后续都不得再次调用 provider。

ACK-lost 只读 reconcile 的返回类型必须是版本化、按 event type 判别的
`OAuthRefreshResultV1 = ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1`：

- automatic `oauth.refresh_started` 只接受 matching `oauth.refresh_confirmed` 或
  `oauth.refresh_credential_replaced` 作为关闭 result；
- explicit `oauth.refresh_recovery_authorization_started` 只接受 matching
  `oauth.refresh_recovery_unsatisfied` 或同一个 `oauth.refresh_credential_replaced` consumption 作为关闭
  result；replacement 同时关闭 recovery attempt 与它绑定的 original automatic fence，而 unsatisfied 只关闭
  recovery attempt。

每个 union member 都必须验证自己的 event type、result/proof schema version、精确 user/connection/attempt、
source、必要的 original-attempt 关联、identity-change 关系和严格 `created_at` 顺序。confirmed 必须满足上述
disposition/flag/equality 矩阵；replacement 必须满足 old != new 且 `refresh_identity_changed=true`。任何不一致
metadata 都不是合法 union member，不能关闭 attempt。只要 append-only matching result 合法存在，
对应外部调用或 authorization attempt 就永久 closed；关闭证明不得要求 current credential、generation、
capability 或 scope 仍等于历史 result 产生时的值，也不得因后续合法变化而让旧 attempt 复活。

coordinator/result 短事务在发出 commit 后若 ACK 丢失，或异常使调用方无法证明事务已 rollback，必须分类为
commit-result-unknown，而不是断言“必然未写 result”。丢失原 connection/session/lease 不影响核对：实现使用
新的数据库 session，以 `user_id + connection_id + attempt_id` 查询上述 result union；automatic attempt 使用
`refresh_attempt_id`，explicit recovery 使用 `OAuthAttempt.id` 并核对其 original refresh attempt。若允许的
matching result 合法存在，事务按 actual commit 处理，attempt 永久 closed，reconcile 不增加 provider call；
confirmed/replacement 保持原总调用数 1，error callback unsatisfied 可保持 0，完成 exchange 后的 unsatisfied
可保持 1。若没有合法 matching result，才按 actual rollback/未决结果处理：禁止补写猜测 result，started
保持 unresolved并进入稳定 `needs_attention`，且 provider/code 都不得重放。Taskiq 重投、
`TransientProviderError`、进程恢复和资源 401 重新进入都只读该事实。只有 started 尚未提交且 provider call
尚未开始的纯本地失败可以重新 claim。

attempt 关闭与 current readiness 必须分两步核对。对已关闭的 confirmed/replacement：current credential
精确等于 result post-state 时，可直接从持久 result 恢复成功；若另一次合法 refresh/reauthorization 已先提交，
则按 current generation、credential、scope 与 capability 重新执行本地 preflight/readiness，不要求历史
post-state 等于 current state，也不重开旧 attempt。old-identity rollback guard 只适用于
`refresh_identity_changed=true` 的合法 confirmed/replacement：若 current identity 回到该 result 的 old
identity，则返回稳定 `oauth_credential_state_conflict`/`needs_attention` 并阻断 rollout。对
`refresh_identity_changed=false` 的 closed confirmed，old == new；ACK 丢失后发生合法 access-only refresh 或
同 plaintext refresh-token 重加密时，即使完整物理 snapshot 已变化且 current identity 仍等于 old == new，仍是
正常 current state，不得误报 rollback。无论 flag 为何，缺少必需 credential 行、AEAD/归属无效或不能满足
其他合法 current-state 不变量都必须返回相同稳定冲突。旧 attempt 始终 closed，`provider_calls == 0`。
unsatisfied 没有 credential post/expiry proof，关闭后同样只按 current capability facts 检查 readiness。防重放
不要求构造完整 credential lineage；append-only result 证明历史 attempt 已闭合，readiness 独立证明当前状态
是否安全。

每次 ACK-lost closure 与 current readiness 都通过后，以及 backup、audit、migration、resync、restore
eligibility 和 post-resync 的每个后续 guard，都必须重新读取每个 affected connection 当前持久化
`access_token` credential 行的 `token_expires_at`，计算
`current_deadline = min(current_token_expires_at) - 900 seconds`。非空 rollout 的
`effective_deadline` 只能收紧：
`min(original_artifact_deadline, current_deadline)`。后续合法 refresh/reauth 得到更短 expiry 时必须
立即收紧；得到更长 expiry 也不得延长原 artifact 窗口。历史
`oauth.refresh_confirmed.rollout_deadline_candidate` 仅参与 result schema 校验，不能替代上述 current
row 读取。任一 current access credential 缺失、expiry 无效或 `now >= effective_deadline` 都 fail closed；
零 affected set 仍只允许经过实时空集合证明的规范 no-deadline 分支。

显式 progressive recovery 直接映射现有 `OAuthAttempt + AuditEvent`，不新增 0018 schema：

1. start 事务锁定仍为 `connected` 的原 connection，读取 `source_generation=S`、current credential
   snapshot/identity 和恰好一个 unresolved automatic fence。只有 current identity 等于 fence old identity
   且 `S >= F` 才允许继续；现有 `set_capabilities_authorizing` 仍只递增一次到
   `target_generation=T=S+1`，`OAuthAttempt` 仍只保存 target connection 与 target `T`。
2. 同一事务创建 OAuthAttempt 后追加
   `oauth.refresh_recovery_authorization_started`，其关闭 metadata 精确包含
   `recovery_schema_version="oauth_refresh_recovery_authorization.v1"`、
   `source="progressive_recovery"`、`oauth_attempt_id`、`connection_digest`、原
   `refresh_attempt_id`、`started_source`、`fence_generation=F`、`source_generation=S`、
   `target_generation=T`、两个 frozen pre-digest、old identity/key version 和稳定 `result_code`。
   该事件的 `created_at` 必须严格晚于原 automatic started；它不是 refresh grant started。
3. callback 一次性消费 state/code，按 `OAuthAttempt.id` 查询上述关联，取得同一 connection lease并在
   exchange 前重检 target `T`、原 unresolved fence 与 frozen identity/snapshot。authorization-code
   exchange 保持在业务事务外；unknown fence 不阻断这次用户显式 exchange，但同一 code 永远不得重放。
4. 只有供应商明确返回 non-empty、且
   `secrets.compare_digest(old_refresh_plaintext, new_refresh_plaintext) == false`，并通过 normalized
   account identity、scope 与 expiry 验证时，callback 才可在一个短事务用 target-`T` generation、两行
   snapshot 和 old identity CAS 写入 access/refresh credential、实际 scopes/capabilities，并追加
   `oauth.refresh_credential_replaced`。callback 保持
   `pre_generation=post_generation=T`，不得再次递增 generation。
5. missing/empty/same refresh、用户拒绝、管理员同意缺失、可安全确认的供应商/网络失败或 identity
   mismatch 都不消费原 fence。start 的 `S→T` 已把本次 requested capabilities 置为 `authorizing`；当原
   connection 仍为 `connected` 且 `authorization_generation == T` 时，callback 复用并扩展现有
   `mark_progressive_authorization_failed` 契约，在同一短事务中只把这些 requested capabilities 收敛为
   `action_required`、写稳定 `error_code`，同时追加
   `oauth.refresh_recovery_unsatisfied`。该事务保留原 `actual_scopes`、`last_verified_at` 等
   last-verified facts，不保存新 credential、scope、provider account 或 supplier identity facts。若 `T`
   已过时或 connection 已由较新授权改变，capability 更新必须 no-op，只允许追加不会覆盖较新状态的安全
   audit；不得把旧失败重新写到新 generation。
6. unsatisfied 使用关闭 metadata
   `result_schema_version="oauth_refresh_recovery_unsatisfied.v1"`，精确绑定 matching
   recovery-started event/OAuthAttempt IDs、原 `refresh_attempt_id`/`started_source`/`connection_digest`、
   F/S/T、两个 frozen pre-digest、old identity/固定 key version、规范排序的 requested capabilities、
   `capability_transition="action_required"|"stale_target_noop"`、稳定 `error_code` 与 `result_code`；其
   `created_at` 必须严格晚于 matching recovery started。它只关闭本次 authorization attempt，不关闭原
   automatic fence。该 schema 不得包含 credential post snapshot/digest、new identity、persisted expiry 或
   deadline 字段；current-`T` 的 result 必须证明 `action_required` 与原 `actual_scopes`/last-verified facts 保留，
   stale-`T` 的 result 必须证明 capability no-op。lease loss、CAS miss、明确 rollback 或
   commit-result-unknown 按上文 result union 核对，不得重放 provider/code 或补写猜测 result；用户只能显式
   创建新的 progressive OAuthAttempt。

`oauth.refresh_credential_replaced` 是 result union 中同时关闭 matching recovery authorization started 与
unresolved original automatic fence 的 append-only consumption，也是唯一能够关闭 original fence 的
result。其关闭 metadata
`proof_schema_version="oauth_refresh_credential_replaced.v1"` 精确包含：

- `source="progressive_recovery"`、原 `started_source`、`connection_digest`、原
  `refresh_attempt_id`；
- `recovery_oauth_attempt_id` 与
  `recovery_authorization_started_event_id`；
- `fence_generation=F`、`source_generation=S`、`target_generation=T`、
  `pre_generation=post_generation=T`，并满足 `F <= S`、`T=S+1`；
- 原 started/recovery 绑定的两个 pre-digest、credential CAS 后的两个 required post-digest；
- old/new `refresh_token_identity_v1` 与各自固定 key version、
  `refresh_identity_changed=true`、实际持久化 `token_expires_at` 和稳定 `result_code`；old/new identity
  必须不等，否则该 proof 不是合法 result union member。

consumption 的 `created_at` 必须严格晚于原 automatic started 与 matching recovery authorization
started。repository 必须锁住并重查两者仍匹配、OAuthAttempt target `T` 未过时且原 fence 尚未消费，
再把 credential/scopes/capabilities 与 consumption 同事务提交。多个显式 recovery attempt 可以先后
产生各自 started/unsatisfied pair，但最多一个不同-token callback 能消费原 fence。

无 target 的 `/provider/start` 继续执行普通一次性 exchange、账户创建或无 fence 的 identity merge。
若规范化 provider identity 命中已有 connected connection 且该 connection 有 unresolved automatic
fence，callback 在保存任何 credential、scope 或 capability 前返回稳定
`oauth_refresh_recovery_requires_connection_start`；可追加 content-free blocked audit，但该事实不关闭
fence。禁止 candidate snapshot、targetless consumption、第二 connection 或无条件 upsert。

0019 refresh fence 使用两个职责明确的物理快照摘要：`credential_snapshot_digest_v1` 绑定 access 与
refresh 两行的完整 CAS 身份；`refresh_credential_snapshot_digest_v1` 只绑定 refresh row，供 fence
配对与 refresh-row CAS 使用。两者都只证明数据库物理快照，不证明 refresh token plaintext 已经替换；
ciphertext、nonce、`updated_at` 或其他物理字段变化可能只是同一 plaintext 重加密。

`credential_snapshot_digest_v1` 使用 SHA-256 并输出小写十六进制。规范字节以固定 domain
`b"AIEMPLOYEE/calendar-aad/credential-snapshot/v1\x00"` 开头，随后每个字段使用同一 framing：
`NULL` 编码为单字节 `0x00`；非 NULL 编码为
`0x01 || uint32_be(length) || raw_bytes`。空 bytes 是长度为零的非 NULL 值，与 NULL 不同。顶层字段
顺序固定为 access-token credential row、refresh-token credential row；`authorization_generation` 不进入
该摘要，而是事件的独立字段。

每行字段顺序固定为 `credential_kind`、row `id`、`user_id`、`connection_id`、`ciphertext`、
`nonce`、`key_version`、`token_expires_at`、`updated_at`。UUID 使用小写 canonical ASCII；整数
使用无 `+`、无多余前导零的十进制 ASCII；时间使用 UTC RFC3339、固定六位 microseconds 和 `Z`；
ciphertext/nonce 使用原始 bytes；两行的 `credential_kind` 必须分别是 ASCII `access_token` 与
`refresh_token`。协议不包含 token plaintext、raw scope 或 raw `calendar_id`。字段、顺序、domain 或
编码发生任何变化都必须新增 digest version，禁止静默修改 v1。

`refresh_credential_snapshot_digest_v1` 使用相同 framing、SHA-256 和小写十六进制，固定 domain 为
`b"AIEMPLOYEE/calendar-aad/refresh-credential-snapshot/v1\x00"`，随后只编码 refresh credential row，
字段顺序与上述 row 完全一致。它同样是物理快照摘要；单独改变该摘要不能证明 plaintext token 不同。

`rollout_digest_v1` 使用相同 framing、SHA-256 和小写十六进制，固定 domain 为
`b"AIEMPLOYEE/calendar-aad/rollout/v1\x00"`，字段顺序固定为
`schema_version="calendar_aad_0019_preflight.v1"`、`source_revision="20260809_0018"`、
`target_revision="20260809_0019"`、已验证的 `backup_artifact_basename`、精确小写
`sha256:...` immutable image content ID、`safety_margin_seconds=900`。它绑定发布身份，不包含运行后
才产生的 token、expiry、deadline 或 provider response。

confirmed 只关闭自己的 automatic started；recovery unsatisfied 只关闭 matching recovery authorization
started；credential replacement consumption 同时关闭 matching recovery authorization started，并消费它精确
引用的原 unresolved automatic started。retention matcher 必须复用同一 versioned result-union parser，包含
confirmed disposition/flag/equality 与 replacement changed=true/identity-inequality 校验；不得另写宽松 matcher，
也不得比较 current credential lineage。365 天
清理的 generic AuditEvent delete 必须排除上述五种事件，专用 user-scoped cleanup 按以下组处理：

- automatic success 组为 started + confirmed；
- unsuccessful recovery 组为 recovery authorization started + unsatisfied；
- successful recovery 组为原 unresolved started + matching recovery authorization started + replacement
  consumption；同一原 fence 的更早 unsatisfied pair 仍按各自关闭组处理。

只有组内每一行 `created_at < cutoff`，等价于组内最大 `created_at < cutoff`，才可在同一事务删除该组。
未消费的 original started 必须跨 cutoff 保留；unsatisfied 不能释放它。cleanup 固定按 connection →
access row → refresh row → original started → recovery started 锁序重查关联与时序，不读取后续 credential
lineage，也不能先删 result 留下伪 unresolved 事实。

replacement consumption 首次有效后永久关闭原 fence，并且必为 `refresh_identity_changed=true`。后续
generation、scope/capability、access 或 refresh credential 正常变化不使它复活；current guard 使用同一固定
root key/version 重算 current identity。只有曾由 changed=true proof 证明 A→B 后，current identity 又回到
consumed old identity A（A→B→A）时，才以 `oauth_credential_state_conflict` fail closed。单纯同 plaintext
refresh-token 重加密保持 identity 不变，必须由 changed=false confirmed 表达，既不能伪造 rotation，也不是
rollback。disconnect 会删除 old credential，因此不能用于补造 consumption；新 connection 映射、direct SQL、
marker waiver 或 physical digest/generation 变化都不能解除 fence。

以下四个合成向量是协议常量。测试必须把 expected canonical bytes/base64、digest/HMAC 写成固定常量，
不得调用生产 helper 生成 expected 值。

Full credential vector 输入：user `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`，connection
`bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb`；access row 为 id
`11111111-1111-4111-8111-111111111111`、ciphertext base64 `AAECA/Dx8vM=`、nonce base64
`EBESExQVFhcYGRob`、key version `3`、expires `2030-01-02T03:04:05.123456Z`、updated
`2030-01-02T02:00:00.000001Z`；refresh row 为 id
`22222222-2222-4222-8222-222222222222`、ciphertext base64 `3q2+7wB/gP8=`、nonce base64
`ICEiIyQlJicoKSor`、key version `4`、expires NULL、updated `2030-01-01T00:00:00.999999Z`。规范字节长度
为 `497`，完整 base64 为：

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvY3JlZGVudGlhbC1zbmFwc2hvdC92MQABAAAADGFjY2Vzc190b2tlbgEAAAAkMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTExMTExMTExAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAIAAECA/Dx8vMBAAAADBAREhMUFRYXGBkaGwEAAAABMwEAAAAbMjAzMC0wMS0wMlQwMzowNDowNS4xMjM0NTZaAQAAABsyMDMwLTAxLTAyVDAyOjAwOjAwLjAwMDAwMVoBAAAADXJlZnJlc2hfdG9rZW4BAAAAJDIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIyMgEAAAAkYWFhYWFhYWEtYWFhYS00YWFhLThhYWEtYWFhYWFhYWFhYWFhAQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIBAAAACN6tvu8Af4D/AQAAAAwgISIjJCUmJygpKisBAAAAATQAAQAAABsyMDMwLTAxLTAxVDAwOjAwOjAwLjk5OTk5OVo=
```

expected lowercase SHA-256 为
`607c4c12cb3e4ff7736cb56f06b9dd2f63d2681c8d7a36c7801160eaafcaa84a`。

Refresh credential vector 使用上述 refresh row。规范字节长度为 `265`，完整 base64 为：

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcmVmcmVzaC1jcmVkZW50aWFsLXNuYXBzaG90L3YxAAEAAAANcmVmcmVzaF90b2tlbgEAAAAkMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAI3q2+7wB/gP8BAAAADCAhIiMkJSYnKCkqKwEAAAABNAABAAAAGzIwMzAtMDEtMDFUMDA6MDA6MDAuOTk5OTk5Wg==
```

expected lowercase SHA-256 为
`3a4b1dbd0e77c915067c3af352772580949957815573c953baa41131827582f0`。

Rollout vector 输入依次为 `calendar_aad_0019_preflight.v1`、`20260809_0018`、`20260809_0019`、
`calendar-aad-0019-synthetic`、
`sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef`、`900`。规范字节长度为
`222`，完整 base64 为：

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcm9sbG91dC92MQABAAAAHmNhbGVuZGFyX2FhZF8wMDE5X3ByZWZsaWdodC52MQEAAAANMjAyNjA4MDlfMDAxOAEAAAANMjAyNjA4MDlfMDAxOQEAAAAbY2FsZW5kYXItYWFkLTAwMTktc3ludGhldGljAQAAAEdzaGEyNTY6MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWYwMTIzNDU2Nzg5YWJjZGVmMDEyMzQ1Njc4OWFiY2RlZgEAAAADOTAw
```

expected lowercase SHA-256 为
`0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5`。

Refresh identity HMAC vector 使用仅供测试的 32-byte application master key hex
`000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`、identity key version `7`，以及
ASCII refresh plaintext `synthetic-refresh-token-A`。按本节 HKDF-SHA256 规范得到的 fingerprint key hex
固定为 `47119abdcf6b97afcdd6416bf5a8b7771b314a630c53c1553aa1789cc02f95c1`；HMAC message 长度为
`73`，完整 base64 为：

```text
QUlFTVBMT1lFRS9vYXV0aC9yZWZyZXNoLXRva2VuLWlkZW50aXR5L3YxAAEAAAAZc3ludGhldGljLXJlZnJlc2gtdG9rZW4tQQ==
```

expected lowercase `refresh_token_identity_v1` 为
`9de29ff352c78092c4b7dae5e0e3093fee49815a3473d75c3876e3b316222564`。测试必须独立实现标准 HKDF/HMAC
或使用受审计的标准库计算 expected 对照，不能调用生产 identity helper 生成 expected；任何日志、审计输出
或失败消息都不得打印该 synthetic key 之外的真实 master key、派生 key 或 refresh plaintext。

### 17.4 删除能力

- 清除邮箱缓存：取消并删除依赖对应线程且尚未认领的草稿和审批；已认领操作保留最小执行与
  核对事实，不能因删除来源缓存而伪装成未执行。
- 清除日历缓存：取消对应且尚未认领的提案和恢复操作；已认领操作按相同规则保留最小事实。
- 从零创建且未引用源数据的本地草稿不随来源缓存删除。
- 删除全部数据：先建立用户级写入屏障，取消未认领操作，并对已认领操作执行一次有界核对；
  随后删除连接、草稿、提案、任务内容、密文和 provider IDs。即使结果仍未知也必须完成本地
  删除，但应在删除前向用户明确提示外部副作用可能已经发生，并只保留不含个人内容的删除审计。

## 18. 错误契约

新增稳定错误码至少包括：

| 错误码 | HTTP/任务语义 |
|---|---|
| `connection_capability_disabled` | 409，用户需启用能力 |
| `connection_scope_missing` | 409，用户需重新授权 |
| `oauth_authorization_failed` | 沿用既有通用授权失败 Problem；用户拒绝或未知但输入合法的 callback error |
| `microsoft_admin_consent_required` | 409，组织管理员操作 |
| `microsoft_reauthorization_required` | 403，用户需重新完成 Microsoft 授权交互 |
| `oauth_refresh_claim_locked` | 409/任务非重试失败，同 connection 已有 coordinator lease |
| `oauth_refresh_result_unknown` | 任务进入 `needs_attention`，禁止 Taskiq/TransientProviderError 重放 |
| `oauth_refresh_claim_lost` | 任务进入 `needs_attention`，provider 后 session lease 已丢失 |
| `oauth_credential_state_conflict` | 409/任务失败，changed=true 的 closed result 后 current credential 回到 old identity，或必需行缺失、AEAD/归属无效、状态不一致 |
| `draft_version_conflict` | 409，重新加载草稿 |
| `proposal_version_conflict` | 409，重新加载提案 |
| `approval_invalidated_by_edit` | 409，提交新版本 |
| `approval_execution_deadline_expired` | 409/任务失败，重新审批 |
| `mail_recipient_limit_exceeded` | 422，减少地址 |
| `mail_thread_binding_conflict` | 409，源线程已变化或不可访问 |
| `calendar_event_version_conflict` | 409，基于最新事件重新提案 |
| `calendar_event_resync_required` | 409/任务安全失败，等待精确日历 scope 受限重同步后重试 |
| `calendar_recurring_event_unsupported` | 422，不支持的 M2 操作 |
| `calendar_notification_mapping_unsupported` | 422，供应商不能无损映射 |
| `provider_write_outcome_unknown` | 任务进入 `needs_attention` |
| `provider_reconciliation_failed` | 保留真实未知状态 |
| `manual_resolution_conflict` | 409，结果已被其他事实收敛 |

Problem Details 不返回供应商原始响应、完整地址或正文。未知异常仍映射为内部错误并保留 trace_id。

## 19. 可观测性与运维

新增指标：

- `provider_write_requests_total{provider,action,outcome}`
- `approval_decisions_total{action,decision}`
- `approval_expired_total{action}`
- `tool_reconciliation_total{provider,action,outcome}`
- `tool_reconciliation_age_seconds{provider,action}`
- `needs_attention_tasks{provider,action}`
- `calendar_version_conflicts_total{provider}`
- `connection_capability_state{provider,capability,state}`
- `write_kill_switch_state{provider}`

指标 label 不允许 account、地址、主题、日程标题或 task_id 等高基数字段。

必须告警：

- `needs_attention` 超过 15 分钟。
- 同一幂等键出现第二次供应商调用企图。
- 写入开关配置与运行时适配器状态不一致。
- OAuth 撤销失败持续积压。
- 核对扫描器或 Outbox relay 停止心跳。

运行手册新增：

- Google/Microsoft 权限升级与重新授权。
- Microsoft 管理员同意故障处理。
- 全局和供应商写入停机开关。
- 未知结果人工核对。
- 日程恢复和冲突处理。
- 疑似误发、重复发送或未审批写入的事故响应。

API 的四个实际 Uvicorn 启动入口（生产 Compose、开发 Compose、根目录开发 recipe 与 E2E backend
脚本）必须显式使用 `--no-access-log` 或等价 `access_log=False`。静态测试扫描所有入口，防止后来
新增未受控启动方式；真实子进程 canary 必须向 OAuth callback 发送带 synthetic `code`、`state` 和
`error_description` 的 query，扫描 stdout、stderr 与 JSON logs，证明三个值均为零泄露，同时查询
持久审计证明正常 callback-error 状态消费与脱敏审计仍然成立。仅有 router/filter 单元测试不构成该边界
的证据。

## 20. 测试策略

### 20.1 单元测试

- 命令 Schema、规范序列化与跨进程稳定哈希。
- 地址解析、去重、回复和全部回复规则。
- 草稿、提案、Task、Approval 和 ToolExecution 状态迁移。
- 能力启用、关闭、断开和执行时复核。
- 工作时间、DST、全天事件、缓冲和候选时间算法。
- ETag、before snapshot 和恢复规则。
- CalendarEvent v2 AAD 的 39-byte domain、五个无 NULL tag 的 `uint32_be` frame、精确字段顺序、
  UUID ASCII/opaque-ID strict UTF-8/field whitelist、描述/地点独立版本选择，以及全空字段读取为空。
  测试必须把第 17.1 节三个向量的完整 expected bytes/base64/长度作为独立常量，覆盖
  `a:b`/`c` 与 `a`/`b:c` delimiter split collision、合法非 ASCII opaque ID、composed/decomposed
  Unicode 不正规化、未知 field/编码或长度失败发生在 AEAD 调用前。writer 与 reader 必须注入或导入
  同一个 helper identity；测试应使任何复制 framing、冒号拼接、去掉 calendar ID 或 legacy fallback 失败。
- `credential_snapshot_digest_v1`、`refresh_credential_snapshot_digest_v1` 与 `rollout_digest_v1` 的字段
  framing、NULL/空 bytes 区分、UUID/整数/UTC 时间规范化、固定字段顺序和上述完整合成向量；expected
  bytes/base64/hash 必须是独立固定常量，不得由生产 helper 反向生成。
- `refresh_token_identity_v1` 的标准 HKDF-SHA256/HMAC-SHA256、domain/framing、固定 synthetic vector，以及
  API/Worker/CLI 从同一 `APP_MASTER_KEY_FILE` 和 key version 构造 AEAD/identity service。M2 不实现 keyring
  或 rotation；root key/version 变化必须 fail closed。同 plaintext 重加密必须得到同一 identity、只能表达为
  changed=false，不能伪造 rotation；只有 changed=true 的 A→B proof 后 current identity 回到 A，才是
  A→B→A rollback conflict。两者都不能只比较 AEAD physical digest。测试还必须证明顺序为“受控内存解密 →
  non-empty/UTF-8/边界验证 →
  identity 计算 → committed started/lease 重检 → provider access”，不能先对 ciphertext 猜测 identity。
- 两通道状态机：automatic refresh 的 started → confirmed/unknown，以及 explicit progressive recovery 的
  authorization-started → unsatisfied/credential-replaced。普通 automatic valid response 的 missing/same/
  different refresh 都只写 matching confirmed；missing/same 必须 old == new 且
  `refresh_identity_changed=false`，different 必须 old != new 且 changed=true。只有 progressive callback 的
  non-empty different plaintext 可写 old != new、changed=true 的 replacement 并消费旧 fence。parser 必须
  拒绝 disposition、flag、identity equality 任一不一致的 confirmed/replacement metadata。多次 recovery、
  F/S/T、target-`T` anti-replay、strict `created_at` 顺序和完整 metadata 都必须独立测试。unsatisfied 单元测试
  覆盖 requested capabilities 从 `authorizing` 收敛为
  `action_required`、稳定 error code、保留 actual scopes/last-verified facts、stale-`T` no-op 和创建新
  OAuthAttempt 重试。另测试版本化 `OAuthRefreshResultV1` 的判别：automatic started 只接受
  confirmed/replacement，recovery authorization started 只接受 unsatisfied/replacement；合法 append-only
  result 永久关闭对应 attempt，current readiness 变化不得重开它。
- `OAuthRefreshCoordinator` 的 automatic 状态机：session advisory lease → committed started → network →
  atomic confirmed；started 未提交前 provider call 为零，started 后的 lock loss/network unknown/CAS miss/
  已确认 rollback 都留下 unresolved fence。commit ACK 丢失必须用新 session 做只读 result reconcile，而非
  假定 rollback；result closure 与 current readiness 分开测试，包括 ACK 丢失后另一合法 refresh/reauth 先
  提交仍不复活旧 attempt。changed=false confirmed 必须覆盖后续合法 access-only refresh，以及同 plaintext
  refresh-token 重加密导致完整物理 snapshot 改变但 current identity 仍为 old == new 的正常路径；两者都不得
  报冲突。changed=true confirmed/replacement 必须覆盖 A→B→A 回到 old identity 的稳定
  `oauth_credential_state_conflict`，并与缺行、AEAD/归属无效场景一起证明 provider call 为零。
  Taskiq/`TransientProviderError` delivery 重新进入时只读 fence/result，显式 progressive authorization-code
  exchange 取得 lease但不追加第二个 started。
- ProviderWriteOutcome 与重试安全判断。
- 模型输入裁剪、签名清洗和输出 Schema。

### 20.2 数据库与队列集成测试

- 所有迁移、约束、用户隔离和 AEAD AAD 归属。
- 同一草稿并发更新和旧版本提交。
- 编辑后旧审批失效。
- 重复审批决定、过期和执行截止时间。
- 两个 Worker 竞争同一工具执行。
- 崩溃点：认领前、认领后请求前、请求后结果提交前、结果提交后队列确认前。
- Redis 清空、Outbox 重投、Checkpoint 恢复和 SSE 重放。
- 能力在等待审批、排队和执行期间被关闭。
- `needs_attention` 核对和人工结论竞争。
- 日程修改与恢复的 ETag 竞争。
- `0019` 只增加 AAD 版本列与约束，正确标记 v1/NULL，保留全部事件与 `directory` 游标，并仅把
  相关精确 `(connection_id, calendar_id)` 非 directory 事件 scope 的 cursor/freshness 置空且留下
  content-free `calendar_event_resync_required`。迁移测试必须包含两个 connection 复用同一
  `calendar_id`、但只有一个 connection 存在 v1 字段的场景，证明另一 connection 的游标完全不变；
  还必须分别证明缺失任一精确 scope 游标、owning connection 已断开、`calendar.read` 为
  disabled/revoked、缺失 access credential、缺失 refresh credential、仅 access-token 可用或缺失精确
  `ProviderCalendar` 时，都会在任何 DDL/DML 前 fail closed，revision 仍为 0018 且全部数据原样保留。
- 0019 migration 测试必须覆盖 Task 16A 冻结的同一 typed guard 在 mutation 前与
  `on_version_apply` 最终提交前各执行一次：已经先完成 `bootstrap_candidate → pristine_idle` 的 fresh
  database 和严格空 affected set 的普通 `upgrade head` 走 affected-set zero-bootstrap 并正常服务
  CI/E2E/Compose；仍处于 default/legacy candidate 的数据库直接 migration 必须零 DDL/DML/AuditEvent。
  revision 0018 且 affected set 非空时，
  缺失/错误 guard 必须在任何 DDL/DML 前失败。Fake guard 要证明两次调用使用同一 protocol；Task 27C
  的真实 artifact composition 只能注入该接口，不能通过改写 0019 或 `migrations/env.py` 改变结果。
  同一矩阵还必须冻结 `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`：独立 session 持 shared lock 时，
  Compose、`just db-upgrade`、integration fixture、E2E 与 0019 one-off 的在线 migration 都必须在
  external transaction commit 前等待；释放 shared lock 后只能提交一次。任何入口绕过 exclusive lock、
  migration 事务尚未结束就释放，或连接丢失后继续提交都必须失败。该回归不得修改 0019 typed guard
  的 phase、zero-bootstrap 或 artifact-backed 语义。
- `calendar-aad-preflight-0019` 只在 revision 0018 扫描历史完整 AEAD 三元组推导 affected pairs，并按
  connection UUID 去重、确定性串行处理。preflight 在检查 rollout artifact 是否存在或调用任何供应商
  之前，必须通过独立、非业务事务 PostgreSQL 连接执行 session-level
  `pg_try_advisory_lock(20260809, 19)`；该固定 revision 域同时互斥相同和不同 backup basename，不能把
  basename、connection 或 pair 混入锁键。获取失败返回稳定错误 `calendar_aad_rollout_locked`，不得
  refresh、写凭据或发布 artifact。该连接从获取锁起保持到 preflight artifact 原子发布或失败退出，
  供应商网络 I/O 期间不得持有业务事务；每次供应商调用、credential commit 和 artifact publish 前都要
  通过同一 session 核验连接与锁仍存在，连接断开或锁丢失立即 fail closed。进程退出或连接关闭依赖
  PostgreSQL 自动释放 session lock，不能用进程内 mutex 代替跨进程互斥。
- 每个 affected connection 在任何事件 scope probe 前必须冻结 access/refresh 两行完整 snapshot、
  connection generation `G` 与 fixed-key `refresh_token_identity_v1`。本地 credential 校验成功后，
  preflight 取得 connection-scoped coordinator lease；若已有 unresolved `oauth.refresh_started`，相同或
  不同 basename 都在 provider 前以 `provider_calls == 0` 失败。否则短事务追加
  `source="calendar_aad_preflight"` 的 `oauth.refresh_started`，并证明 started.created_at 早于 provider
  call。事件使用 `oauth_refresh_fence.v1` 的完整关闭字段，不新增 0018 schema。
- preflight 的 known-valid response 无论 missing/same/different refresh，都必须通过 shared CAS repository
  原子写 credential 与 matching `oauth.refresh_confirmed`。confirmed 包含 started/source、G/G/G、两个
  pre/post digest、old/new identity/version、persisted expiry、refresh disposition、rollout digest 和
  deadline candidate、`refresh_identity_changed`，且 `confirmed.created_at > started.created_at`。missing/same
  逐字节保留 refresh row并满足 old == new、changed=false；different 更新两行并满足 old != new、changed=true，
  但不追加 replacement consumption。schema 关系不一致的 event 必须被 union parser 拒绝。network unknown、
  `invalid_grant`、malformed、
  scope shrink、任一 lease loss、CAS miss 或明确 rollback 不产生 confirmed，并让后续 Taskiq/
  `TransientProviderError` delivery provider call 为零。result commit ACK 丢失必须另测两种数据库事实：
  事务实际 commit 时，用新 session 按 `user_id + connection_id + attempt_id` 读到合法 versioned confirmed
  union member 即永久关闭 started，`provider_calls == 1`；事务实际 rollback 时，reconcile 不补写 result，
  started 保持 unresolved，进入 `needs_attention`，后续 `provider_calls == 0`。原 coordinator connection 或
  lease 丢失不得阻止该 PostgreSQL 只读核对。再覆盖 ACK 丢失后另一合法 refresh/reauthorization 先提交：
  historical post 与 current 不同仍保持旧 attempt closed，并按 current facts 继续 readiness。对 changed=false
  confirmed，后续 access-only refresh 或同 plaintext 重加密即使改变完整物理 snapshot、current identity 仍为
  old == new，也不得报冲突；对 changed=true confirmed/replacement，current identity 回到 old identity 的
  A→B→A、缺行、AEAD/归属无效或 current facts 无法自洽时返回
  `oauth_credential_state_conflict`、阻断 artifact，但 provider call 仍为零。confirmed 后 artifact 前崩溃只从
  持久 result 和 current readiness 恢复。
- Google/Microsoft progressive recovery 测试必须直接使用现有 target-bound OAuthAttempt。start 事务在
  `S >= F` 且 current identity 等于 unresolved fence old identity 时，原子执行唯一
  `S→T=S+1`、创建 OAuthAttempt，并追加
  `oauth.refresh_recovery_authorization_started`；它绑定 recovery schema/source、OAuthAttempt.id、connection
  digest、原 refresh attempt/started source、F/S/T、两个 pre-digest、old identity/version、稳定 result
  code 与严格晚于 original started 的 `created_at`，且不创建第二个 refresh started。callback 按
  OAuthAttempt.id 查询关联、取得 lease、一次性交换 code，并保持 target-`T` anti-replay。
- progressive callback 只有得到 non-empty different refresh plaintext 才能把 credential、scopes、
  capabilities 和完整 `oauth.refresh_credential_replaced` proof 同事务提交。proof 必须包含
  started_source、original/recovery attempt IDs、F/S/T、pre/post digests、old/new identity/key versions、
  `refresh_identity_changed=true`、persisted expiry 与稳定 result，满足 old != new、`F <= S`、`T=S+1`、
  `pre_generation=post_generation=T`，且 created_at 严格晚于 original started 与 recovery started。
  missing/empty/same refresh、用户拒绝、管理员同意缺失、已知 provider/network failure 或 identity
  mismatch 都不写 credential/scope/account facts；在 target `T` 仍匹配时，同一短事务复用
  `mark_progressive_authorization_failed` 把本次 requested capabilities 置为 `action_required`、写稳定
  `error_code`、保留原 actual scopes/last-verified facts，并写完整
  `oauth_refresh_recovery_unsatisfied.v1` result。该 result 不含 credential post/expiry 字段；`T` 已过时则
  capability mutation 为 no-op，
  safe audit 标记 `stale_target_noop` 且不得覆盖较新状态。测试分别覆盖 denial、missing、same、stale `T`
  和创建新 attempt 重试；第一次 unsatisfied 后原 fence 仍阻断 automatic refresh，第二次 different token
  才消费原 fence，同一 code 永不重放。unsatisfied 与 replacement 两种 result 的 commit ACK 丢失都必须
  覆盖 actual commit/actual rollback：actual commit 通过对应 union member 永久关闭 recovery attempt，且只有
  replacement 消费 original fence；actual rollback 不补写 result、不重放 code/provider。测试还覆盖 ACK
  丢失后另一合法 reauthorization 已改变 generation/capability 的竞态，历史 attempt 仍 closed并独立核对
  current readiness。
- Google/Microsoft callback API 集成测试必须证明 `code+state` 与任意合法 non-empty `error+state` 互斥且
  state 一次性消费；所有合法 error 先走 callback-error/target-`T` unsatisfied 收敛，并共享 raw
  error/description 不持久化、不记录日志和 replay 拒绝不变量。公开 Problem 与持久稳定 error code 按分类矩阵
  断言：用户拒绝为 `oauth_authorization_failed`，Microsoft consent evidence 为
  `microsoft_admin_consent_required` 并保留管理员指引，普通 `interaction_required` 为
  `microsoft_reauthorization_required`，仅未知名称回退为 `oauth_authorization_failed`。raw provider value
  不能成为持久 error code。缺 state、同时 `code+error`、两者都缺失或 malformed error 必须在消费前拒绝；
  首次未知 error 已消费后，以同一 state 重放相同或不同未知 error 必须拒绝。两家 targetless fenced identity
  仍在任何保存前阻断。
- 日志集成测试必须静态检查全部四个 Uvicorn API 启动入口均关闭 access log，并启动真实 Uvicorn
  子进程执行 OAuth callback canary。synthetic `code`、`state`、`error_description` 在 stdout、
  stderr、应用 JSON log 中都必须零命中；应用日志 schema 还必须拒绝 raw URL/query/request target，
  而 PostgreSQL 中仍能查询到对应的脱敏 callback-error 审计与一次性 state 消费事实。
- 无 target 的 Google/Microsoft `/provider/start` 继续覆盖新连接与无 fence identity merge。若一次性
  exchange 后规范化 identity 命中已有 connected connection 且存在 unresolved fence，callback 必须在任何
  credential/scope/capability 写入前返回
  `oauth_refresh_recovery_requires_connection_start`；不得消费 fence、创建第二 connection、要求
  candidate snapshot/新 API 参数或调用无条件 upsert。
- 共享 coordinator/rotation repository 的 Google/Microsoft mail/calendar 测试必须覆盖：两个 Worker 对同一
  connection 只有一个 automatic provider call；每次 successful automatic call 只关闭自己的 started；
  Google access-only/missing-refresh response 写完整 confirmed、更新 access、逐字节保留 refresh；different
  refresh 写 confirmed 但不消费旧 fence；存在 earlier unresolved fence 时 provider call 为零。另覆盖
  response-after-lock-loss、network unknown、CAS miss、result-union commit ACK-loss reconcile、confirmed rollback、Taskiq 和
  `TransientProviderError` zero-call re-entry、固定 lock order、stale generation/row CAS，以及所有
  confirmed/unsatisfied/consumption 的 required metadata 与严格 created_at ordering；另覆盖 closing result 后
  current credential/generation 合法变化不复活旧 attempt、confirmed/replacement identity-change schema mismatch
  被拒绝、changed=false 后续 access-only/same-plaintext re-encryption 不冲突，以及 changed=true 的 A→B→A
  old-identity rollback conflict 零调用阻断。
- retention 集成测试必须使用同一 result union 覆盖 confirmed 关闭自己的 started、unsatisfied 只关闭
  recovery attempt、replacement 同时关闭 recovery attempt 并消费 original fence、多次 explicit recovery、
  targetless blocked audit 不关闭 fence，以及 automatic-success、unsatisfied-recovery、successful-recovery 各组只有
  `max(created_at) < cutoff` 才原子删除；未消费 original started 跨 cutoff 保留且不影响其他用户/普通
  audit cleanup。retention 必须复用相同 parser 拒绝 disposition/flag/equality 不一致的 result，且不得比较
  current credential lineage。
- preflight 使用刷新后 access token，通过 Fake reader 或 HTTP mock 下的真实 Google/Microsoft 只读
  Calendar adapter 完成每个精确 `initial_pages(scope_key)` 探测并取得最终 cursor。它不等待资源 401，
  主动刷新后的 401 必须直接失败且 OAuth refresh 调用次数不增加。测试还必须证明它不调用
  `directory_pages()`、连接级 owner、其他 pair 或任何 Calendar 写适配器，不写 CalendarEvent、cursor
  或 marker；403、权限缺失、malformed page、无最终 cursor 或任何 pair 失败都返回非零并阻止 0019。
- 对每个已轮换 access token 使用同一注入 UTC clock 持久化 `token_expires_at`，首次 artifact 固定计算
  `rollout_deadline = min(token_expires_at) - 900 seconds`；900 秒不是配置项或运维估计。ACK-lost closure
  与 current readiness 后，以及每个后续 guard，都要从当前持久化 access credential 行重读 expiry，
  计算 `current_deadline`，并使用
  `effective_deadline = min(original_artifact_deadline, current_deadline)`。测试必须覆盖历史
  `rollout_deadline_candidate` 不能替代 current-row 读取、ACK-lost 后较短 expiry 立即收紧、较长 expiry
  不延长原窗口、expiry 不足 900 秒、preflight probe/rollout artifact 提交时越过当次重算的
  `effective_deadline`、多个
  connection 取最早 expiry，以及零 affected pair 的显式 no-op。preflight artifact 只允许 schema/revision、
  `rollout_digest_v1`、安全 backup basename、宿主机从 Compose 选定 backend image 解析的 immutable image content ID、earliest expiry/
  deadline、固定 margin、affected/connection count、hashed connection IDs、pair digests 和稳定结果码，
  权限为 `0600`；不得包含 token、scope 原文、正文、描述/地点、供应商响应或 raw `calendar_id`。镜像
  content ID 不接受运维人员单独输入，所有后续 one-off 都必须重新解析实际镜像并与 artifact 比较。
- `0019` 恢复入口只扫描 `calendar_event_resync_required` marker，并以稳定任务种类
  `calendar.aad_0019.resync` 管理每个精确 pair 的正整数恢复 ordinal。首次无历史尝试时才原子创建
  ordinal 1 的 `TaskRun + AuditEvent + Outbox`；任何 `created/queued/running/retry_scheduled` 活动尝试都
  必须原样返回并复用，不能创建下一个 ordinal。集成测试还必须证明两个并发 planner 对同一新 ordinal
  只产生一个任务和一条初始 Outbox；仅当上一尝试已 `failed` 后修复条件并再次显式运行才创建
  ordinal+1 并成功；`cancelled` 可在后续调用创建新 ordinal；`succeeded` 与 marker 并存、多个活动尝试或该恢复种类出现
  `waiting_approval/reconciling/needs_attention` 都 fail closed。旧终态 TaskRun 必须保持不变，单次 CLI
  调用对每个 pair 最多创建一个新 ordinal，TaskRun 内部 `attempt_count` 仍只表示现有 runner 的有界
  自动重试。两个 connection 复用同一 `calendar_id` 时只读取被标记者，目录 reader 调用次数为零，
  普通 `sync_calendar` 队列事实保持未处理；成功只清除同一 marker 并恢复 cursor/freshness，失败保留
  marker，且整个流程不创建 ApprovalRequest、ToolExecution 或任何供应商写调用。
- deadline 集成测试必须让 backup、`pre-migration`/`post-migration` audit、0019 migration、recovery
  planner/Worker final CAS 与 `post-resync` artifact 分别在启动或关键提交前跨过各自从当前 access rows
  重算的 `effective_deadline`，并证明
  全部 fail closed、通用服务不启动、未创建额外 ordinal；还必须证明零 affected-pair state 只能在每个
  边界重新得到空集合时走规范 no-deadline 分支，任何伪造零值、集合漂移或镜像 content ID 不匹配都
  fail closed。测试还必须覆盖相同 basename 和不同 basename 的并发 preflight，证明 revision-global
  lease 只允许一个调用进入供应商；并覆盖 provider 前 started 提交、调用后锁丢失、网络未知、
  `invalid_grant`、malformed response、scope shrink、credential CAS、result 事务实际 commit 但 ACK 丢失、
  result 事务实际 rollback、confirmed 后 artifact 发布前崩溃和下一次 invocation。actual commit 必须由
  新 session reconcile confirmed 且 `provider_calls == 1`；actual rollback 不补写 result，started unresolved
  且后续 `provider_calls == 0`。started 未 confirmed/needs-attention 时，同一或不同 basename 的
  provider 调用数必须为零；只有 explicit progressive recovery 精确匹配原 attempt/started source/F、
  两个原 pre-digest、old identity/version、recovery OAuthAttempt/event、F/S/T、两个 post-digest、
  different-token new identity/version、`refresh_identity_changed=true`、persisted expiry 与严格时序的
  `oauth.refresh_credential_replaced` consumption 才能永久关闭旧 fence，current 状态随后独立校验；
  confirmed crash recovery 必须复用持久 token/expiry，Fake refresh 调用数保持不变。
- retention 跨越 365 天 cutoff 时，未匹配 confirmed 或有效 progressive replacement consumption 的
  automatic started fence 必须仍存在，且下一次同/异 basename invocation 的 `provider_calls == 0`。测试
  必须同时证明普通审计与另一用户的审计仍按各自 cutoff 处理；automatic started + confirmed、recovery
  started + unsatisfied、original automatic started + successful recovery started + replacement consumption
  三类完整组只有在组内每一事件都早于 cutoff 时才允许删除，不要求 current credential/generation 保持
  consumption 后的 lineage。旧 started 加新 result 不得提前清理；unsatisfied、automatic different-token、
  physical digest/generation/access-only 变化不允许清理 original fence 或解锁。并发测试按
  connection → access row → refresh row → started audit 的统一锁序制造 retention 与 confirmed/recovery-result/consumption
  CAS 竞争，
  锁后重查 matching result/consumption，证明不会误删或死锁，且不读取后续 current credential lineage；
  schema mismatch 测试证明 retention 与 reconcile 复用同一 disposition/flag/equality parser。
- 普通备份、remote publication、retention 与 orphan cleanup 必须由两个固定互斥层串行化：同一数据库
  所有主机共用 session-level `BACKUP_LIFECYCLE_LOCK=(20260806, 274)`，同一 `${BACKUP_DIR}` 再持有
  `${BACKUP_DIR}/.ai-employee-backup.lock` 的本地 `flock`。两把锁从创建本 run 唯一
  `.partial.<uuid>` staging、最终名冲突检查、dump/checksum/manifest 发布、remote 上传、日/周保留到
  orphan cleanup 全程保持。测试必须覆盖同 basename、不同 basename、同主机和不同主机并发，证明
  同一数据库只有一个 run 能进入 publication/cleanup 临界区；失败只清理本 run 的 staging 和可证明由
  本 run 发布的不完整成员，绝不删除另一 run。orphan grace 固定为 3600 秒；锁内删除前重新读取 mtime
  与 final manifest，只处理超时 `.partial.<uuid>` 或缺 final manifest 的超时组。remote 使用唯一 staging
  prefix、final manifest 最后发布，并采用同一 3600 秒 grace 与删除前重查。必须覆盖 cleanup race、
  manifest 前崩溃和 remote conflict。
- 普通备份还必须取得 `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` 的 shared session lock：在锁内读取
  pre-revision，保持锁覆盖完整 `pg_dump`，再读取 post-revision 并要求逐字节相等，且只在同一 session
  仍证明持锁时发布本地 final manifest。所有 migration 入口持对应 exclusive lock，backup 期间不得
  commit。测试必须覆盖 backup 与 migration 竞态、绕锁 mutation、revision CAS 变化和 lock loss；任何
  一项都不得发布 dump/checksum/manifest 三件套。这里不要求 dump 与 revision 查询共用同一 MVCC
  snapshot，但 shared/exclusive lock 加 pre/post CAS 都是强制条件。
- 通用备份/灾备恢复测试必须冻结 versioned manifest 三件套：安全 basename、UTC `created_at`、
  PostgreSQL/Alembic revision、dump SHA-256/size、checksum filename、immutable backend image 与
  content-free metadata、`restore_fingerprint_v1` 与镜像内 executable digest map 完整且权限为 `0600`，
  DSN/Token/内容字段零出现；checksum 同时绑定 dump 与最终 manifest，manifest 反向绑定 checksum 和 dump
  digest/size。测试必须实际从 repository-root context 构建 `backend/Dockerfile`，证明全部运行入口已 COPY
  进镜像且没有 executable bind mount，并覆盖 manifest-last/no-clobber 原子发布、任一成员失败/冲突不发布、
  rclone 三件套复制以及上述整组保留/孤儿协议。
- 通用 `just restore file` 不要求 0019 preflight 或 `pre-migration` artifact，可恢复任意受支持 Alembic
  revision（包括普通 0019 备份），但必须在任一 owner Secret/connection/`pg_restore` 前验证完整
  manifest-bound 组。生产测试必须分别让任一写开关开启、Caddy/API/Worker/Scheduler/migration/
  role-bootstrap/general/owner consumer 运行或二次确认缺失，并断言 `pg_restore_calls == 0`。
- database-maintenance integration 必须验证 signed-int64→uint64 conversion、低位与 SQL `-1` 高位 target
  digest，以及两种 lock-key synthetic vector，并用两个
  manifest、generic/sealed、并发 session 与 `db-reset` race 证明 management lifecycle → target → schema
  锁序。bootstrap matrix 必须复用共享 canonical ACL/runtime-role readers，逐项覆盖 fresh/default candidate
  的“两角色同时缺失”与“两角色同时 safe”、pre-protocol legacy candidate 的“两角色同时 safe”、已是
  `baseline posture` 的 `pristine_idle` 重复幂等 role-bootstrap、`completed_idle` 保留 completed pair 的幂等
  最小 grants，以及 `active`/`needs_attention` 阻断。ACL 负例必须逐项覆盖 phase 外的 `PUBLIC CREATE`/
  `PUBLIC TEMPORARY`、任一 `PUBLIC` grant option、app/retention 的 `CREATE`/`TEMPORARY`/任一 grant option、
  额外 CONNECT grantee、错误 grantor、额外/duplicate/未知 tuple；role 负例必须逐个翻转
  `rolcanlogin`、`rolinherit`、`rolsuper`、`rolcreaterole`、`rolcreatedb`、`rolreplication`、
  `rolbypassrls` 与 `rolconnlimit` 的冻结值，并覆盖 non-null `rolvaliduntil`、non-null `rolconfig`、
  `pg_auth_members` 任一方向 membership，以及各 phase 不允许的 missing/mixed role shape。每个负例连同
  phase-specific object-grant 偏差、三项 catalog fact 任一 present/duplicate/role-specific/malformed、活动
  非 owner session，都必须断言 `CREATE/ALTER ROLE`、password rotation、ACL/object grant、DDL、DML 与
  `AuditEvent` 写入全部为零；role/ACL transaction SQL failure 或 crash 则必须全量 rollback。测试还必须
  显式断言没有第四个 steady state，candidate 不能被 migration 或 restore admission 接受。restore holder
  crash 后，新 reset 取得 lifecycle lock仍必须从 durable
  gate/call/completion facts 拒绝，drop/create/bootstrap/migration 为零；migration、role-bootstrap/
  init-db-roles 与 owner one-off 在 held lock、active gate、malformed/role override/duplicate authority 下也
  保持 DDL/DML/audit/grant/CONNECT/call-authority/`pg_restore` writes 为零。测试必须覆盖从管理库持锁、
  同一 live typed lease 包住 reset check→drop→create→bootstrap transition→ordinary migration，证明顺序不能
  交换或拆 session；Compose/首次安装也必须 role-bootstrap 成功后才启动 migration。direct migration on
  candidate 必须为零 DDL/DML，另覆盖 exact GUC grammar、database-wide-only row、新 session 可见性与
  每次 transition CAS race。
- restore 测试必须证明 no-gate exact-post `restore_already_applied` 是数据库零写、`psql_calls=0`、
  `pg_restore_calls=0`，只以 kind-aware no-clobber 方式发布 deterministic mode-`0600` local evidence；matching-gate direct edge 则
  CAS 到 `restore_succeeded` 并继续统一 completion。真实 call 先以新 ordinal CAS
  `restore_backend_starting`，启动唯一 credential-bearing target-owner psql child，使用 exact
  `PGAPPNAME`/`ON_ERROR_STOP`/`--single-transaction`，再从 `pg_stat_activity` 持久化 PID/backend_start 为
  `restore_backend_ready`。只有 ready/state/gate 一致并把 `restore_started` CAS+fsync 后才允许任何 SQL byte。
  credential-free `pg_restore --file=-` 仅生成 SQL，由 Python controller 逐块转发；禁止 shell pipeline，
  必须捕获两个 child 的 exit/status。`restore_backend_starting` 的跨主机 invocation 在没有原 controller
  本地 child/pipe identity 时必须 fail closed 为 `needs_attention`，不得判定 not-applied 或分配新 ordinal；
  测试覆盖 spawn-before-visible crash、backend-ready-before-feed、
  mid-stream crash、transaction completion trailer 缺失、generator/consumer failure、commit ACK unknown、
  两 host overlap、跨主机 projection 缺失、partial/inconsistent 与 operator-authorized exact-pre new ordinal。
- grants 与 generic/sealed-specific verifier 必须复用 holder owner session，在 gate active、database ACL
  精确为 `active` 且两 runtime roles 保持 safe posture 时完成 `SET ROLE ai_employee_app`、`BEGIN READ ONLY`、session/current-user 与 SQLSTATE
  `25006`。phase/CAS 测试逐边覆盖冻结 graph；grant rollback 保持 `restore_succeeded`，verifier failure 保持
  `grants_succeeded`，无非法 self-transition。最终事务同时把 call authority CAS 为 completed、插入普通
  BigInteger-ID 的 `database.restore.completed`/
  `ai_employee.database_restore_completed.v1` exact metadata audit、设置
  `ai_employee.restore_completion=restore_completion:v1:<authority_digest>`、RESET gate，并以 owner grantor、
  non-grantable 方式只恢复 app/retention CONNECT，使 database ACL 精确回到 `baseline`；`PUBLIC` 仍无任何
  database privilege tuple。每个新 active attempt 的 gate transaction 必须先 RESET completion GUC；
  gate transaction 不写会被 `pg_restore` 覆盖的 AuditEvent，而把 gate/call timeline 保存在 catalog call 与
  state-v4；最终 completion audit 才在恢复后数据库追加。commit ACK-lost 必须同时读取不可缺失的 completed
  call、expected completion GUC、gate、exact ACL tuples 与 runtime role posture，区分 completed、not-applied 或 mixed，再重建
  projection，或 CAS
  `reopen_committing →
  reopen_not_applied`；后者只有显式新 `reopen_ordinal` 才可重试。commit 后 host crash 不得误报为 reopen
  failure。测试还必须覆盖完整 `pristine → active → completed → next active` 矩阵，以及第二次恢复较旧
  backup 时先验证/归档旧 completed pair、再原子替换为 active call并确保 active gate 内不存在历史
  completion GUC；最终
  restore commit 成功后 controller/host crash 仍按 completed call + completion GUC + gate + exact ACL tuples +
  runtime role posture 收敛而不重放 reopen。
- legacy pre-manifest 备份在 production 必须拒绝。`just restore-legacy-to-isolated file output_basename`
  只允许 `APP_ENV=development|test`，由专用 `scripts/convert-legacy-backup.sh` 与 Compose isolated
  PostgreSQL/conversion services 创建唯一 project、internal network、ephemeral volume 和 `0600` 临时
  Secret；所有资源先进入 fsynced registry并带统一 attempt/kind/created-at labels。测试必须 SIGKILL
  converter、模拟 Docker daemon crash，随后由固定 3600 秒 grace、per-attempt lock 与 active-container
  recheck 的 `legacy-backup-scavenge` 只删除超时孤儿，保留另一 active run，并清理 volume/network/temp
  Secret/registry且零敏感输出。legacy conversion 只操作 disposable target 与自己的 registry/scavenger，不写
  workspace/production target 的 maintenance gate/call/completion facts，也不混用 generic/sealed artifact 或
  validator。
- `scripts/test-m2-release.sh` 必须在启动任何子命令前固定并导出合成 Task 13 测试数据库：若外部已提供
  `TEST_DATABASE_URL`，只有精确等于
  `postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test`
  才能继续；缺失时使用同一常量，其他或 production-like DSN 立即 fail closed 且不得启动 `just`、pytest、
  audit shell 或 integration child process。之后所有 pytest、`just ci`、audit shell、部署/tooling shell
  和 integration 子进程只继承这一环境，不得仅给单条命令加前缀或在后续覆盖；测试必须同时覆盖缺失环境的
  确定性 Task13 选择、外部精确值保留和其他 DSN 拒绝。
- sealed 0018 恢复测试必须先独立验证本窗口 manifest/preflight/`pre-migration`/revision/exact image，
  再以 `kind=sealed_0018` 调用与 generic 完全相同的 target lock、database gate、durable attempt/reconcile、
  same-session role verifier 与 atomic reopen primitive；只有 artifact/image/revision verifier 不同。generic、
  sealed、不同 manifest 或第二个 sealed attempt 并发同 target 都必须拒绝。镜像测试必须从 built immutable
  image 内读取 restore/audit/guard script digest并匹配 content ID，禁止 host bind-mounted executable。
  artifact mismatch、image-internal script mismatch、maintenance gate mismatch 或任一 ACK-lost 分支均不得
  绕过 gate、提前 CONNECT 或启动服务。任何路径都不得调用 Alembic downgrade、直接 SQL、跳过 marker
  或启动 OAuth-only 临时服务。
- release tooling 测试必须证明 `scripts/test-m2-release.sh` 在任何 child process 前把缺失或精确匹配的
  外部值统一导出为固定 Task13 `TEST_DATABASE_URL`，拒绝其他/production-like DSN，并显式执行
  `bash scripts/test-calendar-aad-0019-audit.sh`；audit、pytest 与其他 shell child 都必须继承同一值，不能
  只给单条命令加前缀，也不能以 `just ci` 可能间接包含该脚本作为发布证据。
- 两个不同日历使用相同 `provider_event_id`，以及同一日历中的两个不同事件之间，互换描述或地点的
  完整 `ciphertext + nonce + key_version + aad_version` 四列组都必须认证失败；v1 行必须强制进入重同步，
  未知版本与 v2 四列中任一值篡改必须 fail closed，且任何路径都不得回退尝试 v1 AAD、未分帧 v2、
  去掉 `calendar_id` 或其他事件 identity。
- Google 增量目录与 Microsoft 完整目录快照的显式语义、空快照、缺席删除、重新出现重建和
  directory revision CAS；陈旧 Microsoft 快照不得覆盖先提交的目录事实。
- Microsoft Calendar Worker 的 access/refresh token 轮换、无新 refresh token 保留、连续独立
  401、refresh 拒绝、403 立即停止、能力隔离、provider 指标标签和完全离线测试模式。

### 20.3 供应商契约测试

Google 与 Microsoft 分别覆盖：

- OAuth state、PKCE、渐进 scope、刷新、撤销和缺失 scope。
- 初始同步、Delta/History、分页、游标失效和删除墓碑。
- 邮件发送、新回复、全部回复和 Sent 核对。
- 日程创建、修改、通知策略、ETag 和稳定关联标识。
- 401 只能触发一次 coordinator-claimed refresh；started 后的 5xx/超时/unknown、lock loss 或
  `TransientProviderError` 不得再次调用 refresh。另覆盖 403 权限和资源读取的 429 Retry-After。
- Microsoft 个人账户与工作/学校账户响应差异。
- Microsoft `/me/calendars` 的真实完整 collection 形状、nextLink 分页、无 deltaLink 最终页、
  directory 404/410 永久错误，以及每个 calendar 独立 CalendarView Delta 的 cursor expiry。
- 日历字段长度/控制字符/邮箱/URL、七位以上小数与 offset、DST、全天边界、时间顺序和 Graph
  recurrence type 矛盾；畸形 item 不能触碰持久事实或推进 cursor。

CI 使用 HTTP mock 和脱敏 fixture，不访问真实供应商。

### 20.4 前端与 E2E

- 草稿编辑、版本冲突、模型失败和本地恢复。
- 日程冲突、候选时间和通知预览。
- 结构化审批、拒绝、过期、修改失效和 409。
- 断线、刷新、乱序/重复 SSE 和快照兜底。
- `reconciling`、`needs_attention` 和人工结论。
- 连接能力渐进授权和管理员同意错误。
- 键盘、焦点、live region 和移动布局。

自动 E2E 使用 Fake provider。一次发布前人工 E2E 只使用专用 Google 与 Microsoft 测试账户和
合成数据。

### 20.5 模型评测

邮件草稿评测集使用合成线程，至少验证：

- 回答了明确问题且没有虚构承诺。
- 未改变收件人、主题或线程事实。
- 不复制签名、跟踪信息或不相关历史。
- 在上下文不足时表达不确定，不伪造日期、金额或决定。
- Prompt 版本、模型名、输入哈希和结构化结果可追踪。

## 21. 验收标准

### 21.1 功能

- Google 与 Microsoft 都能只读同步并完成新邮件、回复、全部回复。
- Google 与 Microsoft 都能创建和修改非重复日程。
- 本地草稿编辑、版本、提交、拒绝、过期和发送结果可恢复。
- 冲突建议遵循用户全部连接、工作时间、时区和缓冲。
- 日程恢复始终产生新审批。
- 操作中心从 PostgreSQL 快照恢复全部非临时状态。

### 21.2 可信执行

- 无有效能力、审批版本和匹配哈希时，供应商写调用次数为零。
- 重复队列投递和 Graph 恢复本身不能产生第二次写调用；只有适配器能够证明先前调用未应用时，
  持久重试策略才允许再次调用。
- 写请求结果未知时不能自动重发。
- 每个写入都有唯一 ToolExecution、供应商关联事实和追加审计。
- 编辑草稿或提案后，旧审批必定失败。
- 撤销能力或连接后，尚未认领操作不会执行。

### 21.3 可靠性

- Worker 在所有指定崩溃点都能恢复到成功、明确失败或 `needs_attention`，不伪造结果。
- Redis 丢失不丢失草稿、提案、任务、审批或工具结果。
- ETag 冲突不会覆盖供应商中的并发修改。
- 同步游标失效能够受限回退，不扩大数据读取窗口。
- Microsoft 完整日历目录不伪造 provider cursor；并发快照通过独立 revision CAS 拒绝陈旧提交，
  只有已声明完整的快照才执行缺席删除。
- Google/Microsoft mail/calendar Worker 与 0019 preflight 的 automatic refresh 共用
  `OAuthRefreshCoordinator`：provider 前取得 connection lease 并提交自己的 `oauth.refresh_started`，valid
  response 以 G/G/G CAS 和完整 `oauth.refresh_confirmed` 关闭本次 started；missing/same refresh 也必须
  confirmed 并逐字节保留 refresh row。confirmed 必须携带 `refresh_identity_changed`，且 missing/same 只能
  old == new、changed=false，different 只能 old != new、changed=true；不一致 metadata 不能关闭 attempt。
  unknown、lease loss、CAS miss、已确认 rollback 或既有 unresolved
  fence 时，Taskiq/`TransientProviderError` 重入 provider call 为零；result commit ACK 丢失时不得假定
  rollback，而要用新数据库 session 按 user/connection/attempt 查询版本化 result union。合法 matching
  confirmed/replacement 永久关闭 automatic started；合法 unsatisfied/replacement 永久关闭 recovery started，
  其中 unsatisfied 不消费 original fence。关闭后再独立检查 current readiness：历史 post 精确匹配可直接
  恢复，后续合法 credential/generation 变化按 current facts 继续；changed=false 后续 access-only refresh 或
  同 plaintext 重加密且 current identity 为 old == new 是正常状态，只有 changed=true 后 current identity 回到
  old 的 A→B→A、缺行、AEAD/归属无效或其他不一致才返回 `oauth_credential_state_conflict` 并阻断，但都不能
  复活旧 attempt或增加 provider call。没有合法 result 才
  保持 started unresolved、进入 `needs_attention` 且不补写 result。带
  connection ID 的显式 progressive authorization-code recovery 取得相同 lease但不创建第二个 started；
  只有 target-`T` callback 的 different refresh 可写 replacement consumption。无 target callback 命中带
  unresolved fence 的既有 connection 时，保存前 fail closed，不能承担恢复。
- `0019` 不改变或删除 CalendarEvent 业务行和 directory revision；受影响事件 scope 会失去旧
  cursor/freshness。恢复只能由 v2-only one-off 入口执行：它从 marker 推导精确
  `(connection_id, calendar_id)`，不运行目录发现或连接级同步，并在成功提交同一 scope 的 v2 事件、
  cursor 与 freshness 后清除 `calendar_event_resync_required`。该入口和 `post-resync` 审计完成前，
  通用 Scheduler、普通 Taskiq Worker、API 与 Caddy 必须保持停止。
- 0019 revision 在 Task 16A 创建时已经包含最终 typed guard：已经先 admission 为 `pristine_idle` 的
  fresh/严格空集合数据库可通过普通 `upgrade head` 的 affected-set zero-bootstrap；revision 0018 且
  affected set 非空时，缺少注入 guard 必须在
  mutation 前失败，并在 `on_version_apply` 最终提交前再次核验。Task 27C 只提供真实 artifact guard
  composition，不改写 revision 或 Alembic 环境语义。
- Task 16A 的 Alembic 外层在线迁移边界还统一持有固定
  `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` exclusive session lock，并直到外层 migration transaction
  commit/rollback 后才释放；Compose、`just db-upgrade`、integration/E2E 与 0019 one-off 均不得绕过。
  Task 27D backup 从 pre-revision 开始持对应 shared lock，覆盖 `pg_dump`、post-revision CAS 和本地
  manifest-last 发布；只有 pre/post revision 相等且同一 session 仍持锁才可发布。无需强制同一 MVCC
  snapshot，但 backup 期间 migration 不得提交，typed 0019 guard 语义保持不变。
- restore catalog 的稳定 admission 永远只有 `pristine_idle | active | completed_idle`。共享 reader 必须用
  `aclexplode(COALESCE(datacl, acldefault('d', datdba)))` 比较完整排序 multiset，并把
  `fresh_default | pre_protocol_legacy | baseline | active` 四个 exact database ACL tuple 集与 safe runtime-role
  posture、无任一方向 membership 一起冻结；steady parser 只接受 `baseline posture` 或 `active posture`。
  default fresh candidate 的 app/retention 只能同时缺失或同时 safe，pre-protocol legacy 必须两者同时 safe；
  `bootstrap_candidate` 只能由 standalone role-bootstrap 或 protected `db-reset` 的 create 后步骤，在同一
  management/target/schema locks 与单一 owner transaction 下收敛为 `baseline posture`，再由普通 parser
  读回 `pristine_idle`。candidate 不能启动 migration/restore、写三项 authority 或 AuditEvent；任一额外、
  duplicate、错误 grantor/grant option/privilege tuple，unsafe/missing/mixed role、role config/valid-until、
  membership、object-grant 偏差、任一 catalog fact、活动非 owner session或 transaction failure 都必须零写/
  整体 rollback。已有 pristine/completed 的 role-bootstrap 只轮换 safe 既有角色密码并幂等重申 owner-granted、
  non-grantable 最小 grants，保留 completed pair；active/`needs_attention` 拒绝。`db-reset` 与首次安装顺序
  固定为 check→drop→create→bootstrap→ordinary migration，且整个序列不能拆开 live management lease 或
  target lifecycle。
- 恢复 planner 遇到 `created/queued/running/retry_scheduled` 时必须始终复用该活动 ordinal，绝不分配
  新 ordinal；多个活动尝试是 fail-closed 不变量错误。只有上一尝试已经 `failed` 或 `cancelled`、marker
  仍存在且运维人员再次显式运行无参数 one-off 时，才能在精确 cursor 锁下分配下一个 ordinal。旧终态
  TaskRun 不会被复活、改写或重新认领；每个新 TaskRun 仍受持久 `started_at` 总超时、单步超时、
  `max_transient_retries` 与 `attempt_count` 上限约束，因此既不会无限自动重试，也不会用新 ordinal
  绕过单次调用边界。
- 0019 rollout 的所有 provider probes 和本地提交都受同一个 content-free rollout artifact 约束：非空
  affected set 的原始 artifact deadline 固定为 `min(token_expires_at) - 900 seconds`；ACK-lost closure
  与 current readiness 后以及每个后续 guard 都从当前持久化 access credential 重读 expiry，并只使用
  `effective_deadline = min(original_artifact_deadline, current_deadline)`。更短 expiry 必须收紧，更长
  expiry 不得延长窗口；历史 confirmed deadline candidate 不能替代 current-row 读取。零 affected pair 只允许
  使用规范 null deadline 且在每个边界重新证明集合仍为空。backup、migration、resync 与
  `post-resync` audit 必须通过对应 guard；任何启动或关键提交 guard 失败都保持通用服务停止。若 0019
  已提交且无法在当前重算的 `effective_deadline` 前清完 marker，只允许在尚未重开服务、没有业务写入的同一 sealed window
  内，从指定的 refresh 后/迁移前加密整库备份恢复到 0018，再核验 dump/checksum/manifest、原/恢复后 audit、revision
  和原 0018 image 健康；禁止 downgrade、直接 SQL、marker waiver 或 OAuth-only 临时服务。
- 每次 0019 preflight 都必须先持有 revision-global PostgreSQL session advisory lease；同一数据库内不论
  basename 都只能有一个 preflight 进入 artifact/provider 边界。每个 distinct connection 还取得 shared
  coordinator lease，并在 provider 前提交完整 `oauth.refresh_started`。known-valid missing/same/different
  response 都以 credential CAS + `oauth.refresh_confirmed` 原子提交，confirmed 必须有 full/refresh
  post-digests、old/new identity/version、persisted expiry、deadline candidate、G/G/G、
  `refresh_identity_changed` 与严格 created_at 顺序，并满足 disposition/flag/equality 矩阵。
  provider/response unknown 或数据库明确 rollback 不产生 confirmed并阻断所有 automatic retry；
  commit outcome unknown 必须通过新 session 只读 reconcile，不能预先断言 confirmed 不存在。
- unresolved automatic fence 只能由用户显式 progressive recovery 消费。start 在同一事务保留现有
  S→T、OAuthAttempt target T，并追加关联原 fence 的 recovery authorization event；requested capabilities
  同时进入 `authorizing`，callback code 只交换一次。denial、missing/same 或已知 provider/network failure
  的 unsatisfied result 在 target `T` 仍匹配时，把本次 requested capabilities 与 audit 同事务收敛为
  `action_required`，写稳定 error code，保留 actual scopes/last-verified facts，且不含 credential post/expiry
  字段；stale `T` 只允许 capability no-op 和安全审计。原 fence 始终保留，用户可创建新 attempt。different
  token 才把 credential/scopes/capabilities 与 old != new、changed=true 的完整 replacement proof 原子提交。
  Google 与 Microsoft callback 只接受互斥的 `code+state` 或 bounded non-empty `error+state`；任意通过输入边界
  的 error 都先一次性消费 state，并在 target `T` 上提交 unsatisfied/no-op。Problem/error code 按安全分类矩阵
  返回：用户拒绝为 `oauth_authorization_failed`，Microsoft consent evidence 为
  `microsoft_admin_consent_required`，普通 `interaction_required` 为
  `microsoft_reauthorization_required`，仅未知名称回退为 `oauth_authorization_failed`。缺 state、code/error
  歧义、两者都缺失或 malformed error 在消费前拒绝；raw error 不持久化、不写日志或 Trace，也不能作为
  持久错误码，已消费 state 的相同或不同 error 重放都 fail closed。targetless identity merge 遇到 fence 保存
  前失败。versioned result union 按 automatic started→confirmed/replacement、recovery started→unsatisfied/
  replacement 精确关闭；replacement 同时消费 original fence。retention 复用同一 union，且只有完整组全部
  早于 cutoff 时删除。
- closing result 后 current state 独立校验，正常 generation/scope/credential 变化不使旧 attempt 复活；固定
  APP root key/version 重算后，只有 `refresh_identity_changed=true` 的 closed result 出现 current identity
  回到 old identity 才是 rollback conflict。changed=false 的 closed confirmed 在后续 access-only refresh 或
  同 plaintext 重加密后仍为 old == new 是正常状态；必需 credential 行缺失、AEAD/归属无效或其他状态不一致
  始终返回 `oauth_credential_state_conflict` 并 fail closed，但 provider call 为零。M2 不轮换 root key，也不
  引入 identity keyring。closing result 已提交但 artifact 尚未发布时，精确 post-state 从持久
  token/expiry 恢复；若另一合法 refresh/reauth 已先提交，则通过独立 current readiness 使用 current
  token/expiry，均不能再次调用旧 attempt。disconnect/reconnect、unconditional upsert 或 physical digest
  变化不能成为恢复路径。
- 每个新普通备份都以权限 `0600` 的 dump/checksum/versioned-manifest 三件套发布。固定数据库级
  `BACKUP_LIFECYCLE_LOCK=(20260806, 274)` 与 `${BACKUP_DIR}/.ai-employee-backup.lock` `flock` 从唯一
  `.partial.<uuid>` staging 创建起覆盖 dump、no-clobber manifest-last publication、remote staging/
  manifest-last、retention 与 orphan cleanup。任一失败只清理本 run；本地/remote orphan grace 固定为
  3600 秒，锁内删除前必须重查 mtime 与 final manifest，不能删除另一 run、完整组或尚在 grace 内的组。
  同一数据库不同主机由 PostgreSQL lock 互斥；同一目录由 `flock` 互斥。
- 每次普通备份还持 schema-lifecycle shared lock，执行 pre-revision → 完整 `pg_dump` → post-revision CAS；
  pre/post 不同、session/lock 丢失或 migration 绕过 exclusive lock都不得发布三件套。manifest 记录的
  revision 因而与 dump 生命周期绑定，而不是事后猜测。
- 通用 `just restore file` 保持灾备入口语义：它只接受 manifest-bound 组，可恢复备份自身支持的
  revision，包括普通 0019 备份；它不依赖 0019 preflight/`pre-migration` artifact。production 在任何
  owner action 前必须证明三层写开关关闭、Caddy/API/Worker/Scheduler/migration/role-bootstrap、所有
  general consumer 和 owner maintenance/general consumer 停止，并完成二次确认。随后必须持
  管理库 lifecycle lock，再持 target-derived advisory lock 与 exclusive schema lifecycle lock；先验证并
  fsync prior completed pair archive（若有），再在同一 owner transaction 重检该 pair、RESET completion GUC、设置 database-wide
  maintenance gate/active call authority，并从已验证 `baseline` 只撤 app/retention CONNECT以形成精确
  `active` tuple multiset；`PUBLIC` 已无 database privilege tuple。该阶段不写 AuditEvent，只在
  commit 后 fsync state-v4。进程 crash 只释放 lock，不清除 catalog
  authority。所有 migration、reset/drop/create、role bootstrap、restore/verifier 与 owner one-off 都在任何写
  前检查同一 primitive，非 matching attempt 零写入。
- generic/sealed restore 先支持 no-gate database-zero-write local-evidence no-op 与 matching-gate direct edge；
  真实 call 由受控 Python executor 管理一个 target-owner psql consumer 和一个无 credential 的 pg_restore SQL
  generator。call authority 持久化 starting/ready/started phase、ordinal、exact backend PID/start 与 pre/expected
  facts；ready 之前不启动 generator，started CAS+projection fsync 之前不写 SQL byte。controller 独占 pipe
  write end，transaction-local completion guard 使 controller crash/EOF/任一 child failure 都回滚 psql 的
  single transaction。reconcile 在 ready/started 后先证明 exact backend 退出，再读 fingerprint。grants 与
  role-switched verifier 复用 lock-holder connection；每个新 active attempt 在建立 gate 的同一事务先 RESET
  completion GUC；最终单一事务把 authority CAS 为 completed、写普通 BigInteger-ID exact metadata audit、
  设置 completion GUC、RESET gate，并只恢复 owner-granted、non-grantable app/retention CONNECT以形成
  `baseline`。ACK unknown 从新 session 同时校验 exact completed call、expected completion GUC、gate、
  ACL tuples 与 safe runtime role posture；audit 后续缺失不影响 completed pair。not-applied reopen 进入
  `reopen_not_applied` 并要求新 `reopen_ordinal`，服务始终由 operator 之后显式启动。
- pre-manifest legacy backup 只能通过
  `just restore-legacy-to-isolated file output_basename` 在 development/test 转换：专用脚本与隔离 Compose
  services 使用唯一 project/internal network/ephemeral volume/临时 `0600` Secret，资源先登记 registry/
  labels但不使用 workspace/production restore catalog admission；实读 revision/健康后调用普通锁定备份路径生成新三件套。
  trap 与 `legacy-backup-scavenge` 分别覆盖正常退出和 SIGKILL/daemon-crash 孤儿。它不能直接用于
  production、伪造 manifest、连接 workspace 数据库或执行 revision repair。
- sealed-window 的 0018 整库恢复只能由独立
  `just calendar-aad-restore-0018 file` 与 operations-profile owner-role one-off 执行。该服务不继承会
  自动 migration 的 backend common 配置，只依赖 healthy PostgreSQL。宿主必须在任何 owner
  Secret/connection/`pg_restore` 前独立验证 basename、dump/checksum/versioned-manifest、
  preflight/`pre-migration` artifacts 和 image binding；服务只能使用内部注入的精确 `sha256:...` image，无 tag/`build:`/pull，并以
  `--pull never` 启动。容器入口在读取 bootstrap Secret 前重复 artifact/image guard，随后才取得
  controller bootstrap owner credential；controller 只把该 credential 与固定 target connection facts交给
  psql consumer，pg_restore generator 的环境必须移除全部 `PG*`/DSN/Secret，且只输出 SQL。controller 不得
  使用 shell pipeline，必须管理 pipe、transaction completion guard 与两个 child status。移动 tag、缺失/错误 image 或
  artifact mismatch 必须保持 owner connection 与 `pg_restore` 调用为零。通过前置 guard 后，sealed 只以
  `kind=sealed_0018` 复用 generic 的 management/target/exclusive-schema locks、durable gate/call/completion facts、state-v4
  projection/backend-starting/ready/started stream reconcile、持锁 owner-session verifier 与 completion-GUC atomic reopen；generic/sealed/legacy 的
  validator/artifact/recipe 仍分离。
  `backend/Dockerfile` 必须把全部所需脚本/CLI COPY 进 immutable image，sealed 不得 bind mount host
  executable，并验证镜像内 digest。

### 21.4 安全与隐私

- OAuth scope 与能力矩阵一致；Microsoft 仅额外使用 delegated `User.Read` 获取 Graph `/me`
  的稳定身份，不请求目录/应用权限、Contacts、Gmail Draft 或 `Mail.ReadWrite`。
- Token、真实命令、正文和日程敏感字段按规格加密。
- 所有新同步的 CalendarEvent 描述和地点都标记为 v2，并由同一共享 helper 使用固定 domain 与五个
  `uint32_be(length) || raw_bytes` frame 绑定完整日历/事件/字段身份；跨日历或跨事件完整四列密文互换、
  delimiter split、Unicode normalization、v2 篡改、v1/未知版本读取均 fail closed，且没有冒号拼接、
  去掉 calendar ID 或其他 legacy fallback。
- OAuth refresh fence 只使用现有 OAuthAttempt 与 append-only AuditEvent，不新增 0018 Schema。
  automatic started/confirmed、progressive recovery started/unsatisfied 和 replacement consumption 都使用
  17.3 的关闭 metadata，按各自 schema 精确绑定适用的 matching attempt/source/started_source、F/S/T 或
  G/G/G、pre-digests、old identity/key version 与严格 `created_at` 时序；confirmed/replacement 另包含各自
  required post-digests、new identity/version、`refresh_identity_changed` 与 persisted expiry，并分别满足
  confirmed disposition/flag/equality 矩阵和 replacement old != new、changed=true。只有 explicit progressive recovery 的
  different refresh consumption 能关闭 old unknown fence；automatic confirmed 只关闭自己的 started，
  unsatisfied 只关闭本次 OAuthAttempt。targetless callback 不能消费 fence。
- `refresh_token_identity_v1` 只从现有 `APP_MASTER_KEY_FILE` 与固定 `AeadCipher.key_version` 按固定
  HKDF/HMAC 派生。API、Worker、CLI 必须从同一 Secret 构造 AEAD 与 identity service；M2 禁止更换 root
  key/version，不存在 multi-key lookup。不得输出 token、root key、派生 key、identity fingerprint、
  raw scope、provider response、正文或 raw `calendar_id`。未决 started 受 retention 例外保护；只有
  matching event 组全部超过 cutoff 才能专用清理。
- 日志、Trace、指标、SSE 和 fixture 不包含敏感内容。
- 四个 Uvicorn 启动入口关闭默认 access log，应用日志 schema 不含 raw URL/query/request target；真实
  callback 子进程 canary 证明 synthetic OAuth query 零泄露且脱敏审计仍被持久化。
- 所有修改 API 通过会话、CSRF、用户隔离和版本验证。
- 保留和删除任务覆盖 M2 新实体、既有密文三元组及 CalendarEvent 四列密文原子组。

### 21.5 发布门禁

M2 采用以下已批准门禁，不要求 7 天或 14 天持续试用：

1. `just ci` 全部通过并保存本轮完整输出摘要。
2. 安全 scope 审核、四入口 access-log 静态扫描与真实子进程 canary、CalendarEvent v2 固定 framing
   向量/碰撞/Unicode/共享 helper 验证、四个 exact database ACL tuple multisets/shared catalog reader、
   safe runtime-role posture/双向无 membership、transient bootstrap candidate/三稳态 admission/全部负例零写、
   transaction rollback 与 create→bootstrap→migration 顺序、0019 affected-set zero-bootstrap/typed-guard 迁移
   验证、current-expiry deadline 收紧测试、通用与 sealed restore 隔离测试，以及显式
   `scripts/test-calendar-aad-0019-audit.sh` release gate 全部通过。
3. 专用 Google 测试账户完成新邮件、回复、全部回复、日程创建、修改和恢复。
4. 一个专用 Microsoft 测试账户完成同等流程；另一账户类型至少通过完整 OAuth、同步和写入
   契约 fixture，且个人与工作/学校账户都必须包含在自动化契约矩阵中。
5. 审计证明每项真实测试写入都绑定有效审批和唯一 ToolExecution。
6. 完成一次部署、manifest-bound 通用加密备份恢复、backup/migration/reset/cleanup 竞态、管理库
   lifecycle lock、target lock 与 crash-surviving database gate/call/completion facts 的 owner-entry/drop-create 零写
   门禁、legacy isolated conversion/scavenger、sealed
   0018 共享恢复 primitive 和 Worker 调用后崩溃演练；证据必须包含三件套
   digest/size/revision/fingerprint/image/script metadata、低/高位 target digest vectors 与 lock ordering、独立
   `RESTORE_STATE_DIR` v4 projection、read-only backup/artifact mounts、catalog CAS phase graph、exact
   psql backend PID/start、spawn-before-visible/backend-ready-before-feed SQL-byte-zero、mid-stream rollback、
   credential-free pg_restore generator、database-zero-write already-applied no-op、pre/post/inconsistent
   unknown-outcome 核对、owner session
   `SET ROLE ai_employee_app` read-only verifier，以及带 `reopen_not_applied`/新 `reopen_ordinal` 的
   active-attempt completion RESET + completed-call/completion-GUC pair authority + 固定外层 exact metadata audit
   insertion + gate reset + exact `active → baseline` tuple transition；该 transition 只能恢复 owner-granted、
   non-grantable app/retention CONNECT，`PUBLIC` 仍无 tuple且 runtime roles 保持 safe posture。ACK-lost 必须由
   新 session 按 completed call/completion GUC/gate/exact ACL tuples/runtime role posture 核对，且不能混用
   generic、sealed、legacy 的 validator 或 artifact。

不执行持续试用会降低发现供应商偶发行为和长期 Token 问题的概率，这是明确接受的 M2 发布
风险；自动化重复投递和崩溃测试仍不可省略。

## 22. 部署与运行开关

外部写入必须同时满足三层开关：

- 全局外部写入开关。
- Google 或 Microsoft 供应商开关。
- 连接级能力状态。

任何一层关闭都阻止新写任务，并使尚未认领的相关审批失效。已认领操作只能完成核对。
`APP_TEST_MODE` 只能注入 Fake 适配器。专用测试账户真实 API 验证使用独立环境、显式写入开关
和账户允许列表；测试环境不得因误配连接其他账户。

Task 27D 必须把每个新普通 PostgreSQL 备份发布为不可拆分的三件套：加密 custom dump、相邻
`${dump}.sha256` 和相邻 `${dump}.manifest.json`。manifest 固定使用
`ai_employee.postgres_backup_manifest.v1`，至少包含安全 dump basename、严格 UTC RFC 3339
`created_at`、PostgreSQL server version、Alembic revision、加密 dump 的小写 SHA-256 与字节数、精确
checksum filename，以及实际解析得到的 immutable backend image `sha256:` content ID 与允许列表内的
content-free build/release metadata、备份时计算的 `restore_fingerprint_v1`，以及镜像内 generic/sealed/
legacy/audit/maintenance-guard 可执行脚本与 CLI 的 allowlisted digest map；不得包含 DSN、主机凭据、
Token、Cookie、正文、日程内容或其他个人数据。`restore_fingerprint_v1` 是规范化 Schema、Alembic
revision、约束与 content-free 健康/抽样事实的确定性摘要，不包含业务明文。三件套权限均为 `0600`。
checksum 只使用安全相对 basename，并同时校验加密 dump 与最终 manifest；manifest 反向绑定精确
checksum filename并重复 dump digest/size。验证器必须重算并交叉比较全部这些事实，拒绝额外条目、
绝对路径、符号链接、大小/digest/revision/image/script metadata 不一致或任一缺件。

备份、remote publication、retention 与 orphan cleanup 使用两层固定互斥。每个 run 先取得同一目标数据库
共享的 session-level `BACKUP_LIFECYCLE_LOCK=(20260806, 274)`，即执行
`pg_advisory_lock(20260806, 274)`，再取得
`${BACKUP_DIR}/.ai-employee-backup.lock` 的本地 `flock`；从创建唯一 `.partial.<uuid>` staging、锁内最终
名冲突检查、dump/checksum/manifest 发布、remote 上传、日/周保留到 orphan cleanup 完成前都不得释放。
PostgreSQL lock 负责同一数据库不同主机，`flock` 负责同一目录；二者均不能省略或用进程内 mutex 代替。

在上述临界区内，备份还通过同一数据库 session 取得固定
`SCHEMA_LIFECYCLE_LOCK=(20260806, 143)` 的 shared advisory lock，在读取 pre-Alembic revision 后保持
该锁覆盖完整 `pg_dump`，随后读取 post-revision 并执行精确 CAS。只有 pre/post revision 相同、session
仍存活且仍持锁，才可把该 revision 写入 manifest 并发布本地 final manifest。所有 migration 入口通过
Task 16A 的 Alembic 外层边界持对应 exclusive lock直到 migration transaction commit/rollback；backup
期间 migration 不得 commit。这里不要求 revision 查询与 `pg_dump` 共享同一 MVCC snapshot，但 shared/
exclusive lock 与 pre/post CAS 缺一不可；绕锁 schema mutation、revision 变化或锁丢失均不得发布三件套。

脚本只在本 run 唯一 staging 中生成 dump、最终 manifest 和 checksum，验证三者绑定并设置权限；最终
名称在锁内执行 no-clobber 发布，任一成员已存在都按整组冲突 fail closed，不能覆盖或拼接旧成员。只
允许清理本 run staging及可证明由本 run 已发布但尚无 final manifest 的成员；不得删除冲突成员或另一
run。只有三件套全部成功后才以 manifest 原子 rename 作为最后发布标记；没有 final manifest 的 dump/
checksum 不是可恢复备份。rclone 使用本 run 唯一 remote staging prefix，复制 dump/checksum 后最后发布
remote manifest，远端整组完成前命令不得成功。local/remote orphan grace 固定为 3600 秒；cleanup 在两把
锁内删除前重新读取 mtime 与 final manifest，只处理超时 `.partial.<uuid>` 或缺 final manifest 的超时组，
并在 remote conflict、manifest 前崩溃或 cleanup race 中绝不删除另一 run。日/周保留与冲突处理同样只能
按完整 basename 组执行。

Task 27D 必须修改 `backend/Dockerfile`，使用 repository root build context 将
`backup-postgres.sh`、`restore-postgres.sh`、`restore-calendar-aad-0018.sh`、`init-db-roles.sh`、
`convert-legacy-backup.sh`、`scavenge-legacy-backups.sh`、`audit-calendar-aad-0019.sh`，以及 generic/sealed
verifier、maintenance-gate Python CLI、`ai_employee.cli.postgres_restore` 与
`ai_employee.infrastructure.db.postgres_restore_stream` 实际 COPY/安装到 immutable backend image 的固定只读路径。Compose/recipe 只能执行镜像内文件；backup
volume、artifact 与 Secret 可以只读 mount，但禁止用 host bind mount 覆盖脚本/CLI。sealed host guard 与
容器内 guard 都要比较实际 image content ID 和镜像内 allowlisted script digest；moved image、missing
script、digest mismatch 或 host-mounted executable 必须在 owner Secret/connection/`pg_restore` 前失败。

所有 repository-owned owner 写入口与 database create/drop/reset 入口统一使用数据库维护 primitive；目标
state 文件、manifest basename、Compose project name 或进程 mutex 都不能充当互斥。先从固定管理库
`postgres` 取得 cluster/target session-level `database_lifecycle_lock_key_v1`，再从目标数据库取得
`maintenance_target_lock_key_v1`，需要 schema mutation 时最后取得 `SCHEMA_LIFECYCLE_LOCK=(20260806, 143)`；
锁序逐字冻结为 `management lifecycle lock → target lock → schema lifecycle lock`。generic/sealed restore
依次取得三者并持 exclusive schema lock覆盖完整 attempt、psql backend registration/stream reconcile、
grants、verifier 与 completion-GUC reopen；`db-reset` 的 management lock 覆盖精确
环境/目标复核、catalog gate/call/completion facts 检查、drop、create、bootstrap transition 与普通 migration。holder crash 后，新 reset
即使取得 management lock，仍须从 catalog 拒绝 active/`needs_attention` attempt并保持 drop/create 为零。
所有 drop/create 只能经该 wrapper；reset 内 migration 接受同一进程内绑定 live management session 的 typed
lease，不能靠环境变量、文件或另一个 session 伪造。普通 migration/backup/role-bootstrap 等 lifecycle
wrapper 也必须先取得 management lock，避免目标在其连接/检查期间被另一主机删除或替换。
`db-reset` 不得在 create 后释放并重新开始 lifecycle：同一 live management lease 与 target lifecycle 必须
覆盖 check→drop→create→bootstrap transition→ordinary migration；create 后立即逐字节复核目标身份并在
同一 wrapper 内取得新目标 session/target lock，只有 bootstrap 重新 admission 为 `pristine_idle` 后才把
该 typed lease 交给 ordinary migration。

`target_identity_digest_v1` 的输入字节精确冻结为
`SHA-256(b"ai_employee.restore_target.v1" || 0x00 || system_identifier_ascii || 0x00 || database_name_utf8)`，
末尾没有 NUL。`pg_control_system().system_identifier` 的 SQL `bigint` 输入只接受
`-9223372036854775808..9223372036854775807`：非负值直接作为 uint64，负值必须先用无溢出的 numeric/host
integer 加 `18446744073709551616` 恢复原始 uint64，再序列化为无符号十进制 ASCII。
`system_identifier_ascii` 数值范围固定 `1..18446744073709551615`，禁止符号、空白与前导零；
`database_name_utf8` 来自 `pg_database.datname`，必须为合法 UTF-8、1–63 bytes、
不含 NUL，不做 normalization、case-fold 或截断，并在目标存在时逐字节等于 `current_database()` 的 UTF-8。
目标不存在时，只有持 management lifecycle lock 的 create/reset wrapper 可使用已通过完整名称确认的同一
bytes，并必须在 create 后从 `pg_database.datname` 逐字节复核；其他入口不得凭 DSN/环境猜测名称。
固定 synthetic vector 为 system identifier `72623859790382856`、database `ai_employee_restore_test`，结果
必须是 `71f328c1e8eb5d9cd2f8e1f815afac4b723f94a274900853c895fd9d72e60e49`。
高位固定 vector 使用 SQL value `-1`，必须恢复为 uint64 `18446744073709551615`；同一 database 的 digest
必须是 `ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b`。expected 由独立标准库测试实现
生成，不能调用生产 helper。
`database_lifecycle_lock_key_v1` 与 `maintenance_target_lock_key_v1` 分别取
`SHA-256(b"ai_employee.database_lifecycle_lock.v1\0" || raw_target_digest)` 与
`SHA-256(b"ai_employee.database_maintenance_lock.v1\0" || raw_target_digest)` 的前 8 bytes，解释为 signed
big-endian bigint；上述 vector 分别为 `-9116408252019116299` 与 `5971001870339114140`。

target lock 必须由同一目标 owner session nonblocking 取得；普通入口持有到该入口 commit/rollback，
manifest-bound restore 按下文跨事务持有到终态。Task 16A 的
migration entry、`db-reset`/drop/create wrapper、`role-bootstrap`/`scripts/init-db-roles.sh`，以及 Task 27D 的
generic/sealed restore/verifier 与 owner one-off 都调用同一 typed helper。legacy conversion 只在独立 disposable
target 上使用自己的 durable registry/scavenger 与普通 backup lifecycle lock，不写 workspace/production target
的 restore catalog facts。取得 target lock 后必须直接
读取 `pg_db_role_setting` 中三个 database-wide catalog fact：`ai_employee.maintenance_gate`、
`ai_employee.restore_call_authority` 与 `ai_employee.restore_completion`。三者都只接受当前 database、`setrole=0` 的唯一 row/key；role-specific
override、重复 key/row、malformed array、session-local override 或只信任 `current_setting`/缓存均 fail
closed。新 owner session 的 `current_setting(..., true)` 只证明 catalog 值可见，不能替代权威 catalog 读取。

稳定 catalog admission 之前定义一个受控的瞬态分类 `bootstrap_candidate`。它不是数据库状态、不会写入
catalog，也不是第四个 steady/admitted restore state；该分类只在当前持锁调用内存在，不能被 migration、
generic/sealed restore 或其他 owner lifecycle 当作 `pristine_idle`。只有 standalone `role-bootstrap`，以及
protected `db-reset` 在本次 `CREATE DATABASE` 成功后的紧接 bootstrap 步骤可以请求该 transition。

调用方必须先按既有顺序持有同一个 live management lifecycle lease 与 target lock，再在任何 mutation 前
直接读取上述三项 database-wide restore facts。三项都必须完全 absent，且不存在 duplicate、role-specific、
malformed 或未知表示；同时必须证明目标库没有非 owner session，API/Worker/Scheduler/migration/restore、
其他 role-bootstrap 与所有能写业务数据的进程均未运行。

数据库 ACL 只允许通过一个共享 canonical catalog reader 读取。reader 必须从目标 `pg_database` 行取得
`datdba` 作为精确 owner OID，并逐字等价执行：

```sql
SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid
```

`datacl IS NULL` 必须经 `acldefault('d', datdba)` 展开，不能被当作“没有 ACL”。每个 tuple 固定为
`(grantee, grantor, privilege_type, is_grantable)`；`PUBLIC` 使用 OID `0`，owner 使用同一行的 `datdba`，
app/retention 使用按精确 role name 从 `pg_roles` 解析的 OID。reader 必须按完整排序 multiset 比较；只允许
PostgreSQL catalog 名称 `CREATE | CONNECT | TEMPORARY`。未知 privilege、重复或额外 tuple、错误 grantor、
错误 grant option、无法解析的 grantee 都 fail closed。禁止只比较 effective CONNECT、调用
`has_database_privilege` 或把 ACL 文本做宽松包含判断。

令 `O=datdba`、`P=PUBLIC/OID 0`、`A=ai_employee_app OID`、`R=ai_employee_retention OID`。四种唯一合法
database ACL tuple multiset 冻结为：

```text
fresh_default =
  (O, O, CREATE,    true)
  (O, O, CONNECT,   true)
  (O, O, TEMPORARY, true)
  (P, O, CONNECT,   false)
  (P, O, TEMPORARY, false)

pre_protocol_legacy = fresh_default +
  (A, O, CONNECT, false)
  (R, O, CONNECT, false)

baseline =
  (O, O, CREATE,    true)
  (O, O, CONNECT,   true)
  (O, O, TEMPORARY, true)
  (A, O, CONNECT,   false)
  (R, O, CONNECT,   false)

active =
  (O, O, CREATE,    true)
  (O, O, CONNECT,   true)
  (O, O, TEMPORARY, true)
```

每个列表都是完整 multiset，不是最低要求：fresh/default 之外 `PUBLIC` 不得有任何 database privilege；
baseline/active 中 `PUBLIC` 均无 tuple；app/retention 在 baseline 只能各有一个 owner 授予、不可转授的
CONNECT，在 active 不得有任何 database privilege tuple；任何其他 grantee 都非法。

同一个 admission helper 还必须从 `pg_roles` 与 `pg_auth_members` 冻结 runtime role posture。已存在的
`ai_employee_app` 与 `ai_employee_retention` 必须各自精确满足：`rolcanlogin=true`、`rolsuper=false`、
`rolinherit=true`、`rolcreaterole=false`、`rolcreatedb=false`、`rolreplication=false`、
`rolbypassrls=false`、`rolconnlimit=-1`、`rolvaliduntil IS NULL`、`rolconfig IS NULL`。密码值不得读取或
比较，只能在受控 transaction 中从 Secret 轮换。`pg_auth_members` 不得存在任何 `roleid IN (A,R)` 或
`member IN (A,R)` 的 row，既不允许 app/retention 继承其他角色，也不允许任何角色成为它们的成员。
任一 elevated attribute、role config、valid-until、任一方向 membership 都不是可自动修复的候选。

fresh/default candidate 只允许两种 role 形状：app 与 retention 同时缺失，或两者都存在且满足上述安全
posture；只存在一个角色属于 partial/mixed state，必须零写拒绝。pre-protocol legacy candidate 必须两者
都存在且安全。`bootstrap_candidate` 只有在 restore facts 全 absent、session/process 前提成立，并且
`fresh_default + 合法 fresh role 形状` 或 `pre_protocol_legacy + 两个安全角色` 二者之一精确成立时才存在。

database ACL tuple 与 schema/table/sequence object grants 是两个独立 catalog contract。后者继续由既有
按 schema revision/restore phase 的最小权限 verifier 精确比较；不得把 object grant 混入上述
`aclexplode` tuple。candidate 预检、bootstrap 提交前检查、baseline ordinary admission 与 restore
grants/verifier 阶段都必须在各自合法 phase 验证对应的最小 object-grant inventory；额外 object grant
同样 fail closed，transition 不得靠撤销未知权限来“修复”异常角色。

任一 restore fact present、ACL tuple 偏差、unsafe/missing/mixed role、membership、object-grant 偏差、
活动非 owner session或仍可能写入的业务进程，都必须在 role/ACL/DDL/DML/AuditEvent 等任何写入前 fail
closed；transition 不得自动降权、清空 `rolconfig`、删除 membership、补齐 partial ACL 或掩盖错误 grantor。

候选通过后，调用方在相同 management/target locks 下取得既有 schema-lifecycle exclusive lock，并只用一个
显式 owner transaction 完成全部 bootstrap。只有 fresh candidate 的两个角色同时缺失时才创建它们；
`CREATE ROLE` 必须显式使用 `LOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB NOREPLICATION
NOBYPASSRLS CONNECTION LIMIT -1`，不设置 `VALID UNTIL`、role config 或 membership。安全既有角色只从
Secret 轮换密码，不修改其他属性。transaction 必须执行
`REVOKE ALL PRIVILEGES ON DATABASE <target> FROM PUBLIC`，随后只向 app/retention 授予 owner-granted、
non-grantable CONNECT，并应用既有最小 schema/table/sequence grants。

PostgreSQL role/ACL DDL 必须依赖其事务语义整体提交；任一 SQL 错误、进程崩溃或最终校验失败都回滚
整个 transaction，不能留下 partial role、password、ACL、membership 或 object grants。提交前必须在同一
owner transaction 直接重读三项 restore facts 仍全部 absent、database ACL 精确等于 `baseline`、两角色
posture 与无 membership 仍精确成立，并由独立 verifier 确认 object grants 不多不少；只有这些检查全部
通过才可 commit。

commit 后仍保持 management/target/schema locks，并调用普通三态 parser 重新 admission；只有结果精确为
`pristine_idle` 才算 transition 完成，随后才可启动 migration。`bootstrap_candidate` 自身不能启动 restore、
写 maintenance gate/call/completion、写 `AuditEvent`、进入 0019 affected-set zero-bootstrap 或绕过 0019 typed
guard。Compose 与首次安装固定执行 role-bootstrap 成功后再运行普通 migration；直接对 candidate 运行
`upgrade head` 必须保持 DDL/DML/AuditEvent/catalog mutation 为零。

crash-surviving gate 固定为 database 自定义 setting `ai_employee.maintenance_gate`。空闲状态只能是
`ALTER DATABASE <target> RESET ai_employee.maintenance_gate`；活动值必须逐字节匹配
`restore:v1:<attempt_uuid>:<generic|sealed_0018>:<target_identity_digest_v1>:<source_binding_digest_v1>`。它只允许
ASCII，generic 值恰好 185 bytes、sealed 值恰好 189 bytes；UUID 必须是规范小写文本，两个 digest 都是
64 位小写十六进制，source digest 必须等于 manifest SHA-256。legacy conversion 不设置该 gate。

第二个 database-wide authority `ai_employee.restore_call_authority` 的规范值升级并逐字节冻结为
`restore-call:v4|<attempt_uuid>|<generic|sealed_0018>|<target_digest>|<source_digest>|<manifest_digest>|<previous_completion_digest_or_dash>|<call_ordinal>|<reopen_ordinal>|<phase>|<gate_established_at>|<call_started_at_or_dash>|<backend_pid_or_dash>|<backend_start_or_dash>|<pre_revision_or_dash>|<pre_fingerprint_or_dash>|<expected_revision>|<expected_fingerprint>|<observed_revision_or_dash>|<observed_fingerprint_or_dash>|<completed_at_or_dash>|<completion_authority_digest_or_dash>`。
parser 只接受 ASCII、恰好 21 个 `|`、总长不超过 1133 bytes 和上述字段顺序；禁止 NUL、CR/LF、空字段、
未知版本、额外字段或 delimiter escaping。UUID 为规范小写，所有 digest/fingerprint 为 64 位小写十六进制；
revision 为 1–128 个 ASCII alphanumeric/underscore。ordinal 是无前导零的非负十进制且固定
`0..999999`，保证 exact `PGAPPNAME` 最长 63 bytes、不被 PostgreSQL 截断；backend PID 是正 int32
十进制。所有时间都由数据库生成，使用 UTC RFC3339、恰好六位微秒和 `Z`。source/manifest digest 必须
逐字节等于同一 manifest SHA-256。

`previous_completion_digest_or_dash` 在 pristine 新 attempt 为 `-`；从 `completed_idle` 开始下一 attempt 时
必须等于被替换 completion pair 的 digest，并在该 attempt 全阶段保持不变。`gate_established_at` 在建立
active pair 的事务内冻结；每次分配新的 real-call ordinal 并进入 `restore_backend_starting` 时，都用本次
数据库时间写入新的 `call_started_at`，同一 ordinal 的后继 phase 必须保留该值；direct exact-post edge
保持 `-`。`restore_backend_starting` 的 backend 两字段为 `-`；
`restore_backend_ready`、`restore_started` 与其结果分支保留 exact PID/start。pre revision/fingerprint 在
`gate_established` 可为 `-`；每个 real-call ordinal 建立时都必须从 holder 本次刚验证的 exact current
revision/fingerprint 重新冻结，且在同一 ordinal 内不可变。expected revision/fingerprint 从 admission 起不可变；
observed revision/fingerprint 在 exact-post 成功判定时填充，并从 `restore_succeeded` 到 `completed` 不可变。
非 `completed` phase 的 `completed_at` 与 completion digest 都必须为 `-`；`completed` 必须含 observed facts、
数据库冻结的 `completed_at` 和合法 digest。phase、字段 presence/absence 与下述状态图不匹配即 malformed。
parser 的 phase-shape 闭集进一步冻结为：`gate_established` 的 call-start/backend/pre/observed/completed 字段
全为 `-`；`restore_backend_starting` 必须有 call-start 与 pre pair、ordinal 至少为 1，但 backend/observed 为
`-`；ready/started/outcome-unknown/not-applied 必须保留 call-start、pre 与 backend pair，observed 为 `-`；
`restore_succeeded` 及其全部后继（含 `reopen_not_applied`）可采用 direct exact-post shape（ordinal 0 且 call-start/backend/pre 全
为 `-`）或 real-call shape（ordinal 至少 1 且这些字段按前述已填充），两者都必须有 observed pair；
`completed` 还要求 reopen ordinal 至少为 1、completed_at 与 digest。所有 revision/fingerprint、PID/start、
call-start 和 observed 字段都必须成对为 `-` 或成对有值。字段单调性以同一 `call_ordinal` 为边界：starting
建立的 call-start/pre 必须全程保留；`starting → ready` 只允许首次填入 backend pair，ready/started/outcome
及其后继必须保留该 pair；exact-post 判定只允许首次填入 observed pair，后继必须保留。任何同一 ordinal
内已经填充的事实都不得清空或改写。唯一受控的 ordinal-boundary 例外是合法
`gate_established|restore_not_applied → restore_backend_starting` CAS：新 ordinal 必须递增且从不复用，写入
本次新的 `call_started_at`，把 backend PID/start 重置为 `-`，从本次 exact current 重新冻结 pre pair，并令
observed/completed 字段符合 starting shape；不得沿用上一 ordinal 的 backend facts。direct exact-post 仍是
ordinal 0、call-start/backend/pre 全为 `-` 的独立 shape，不进入该 real-call ordinal 重置规则。
`needs_attention` 只接受从 graph 合法入边继承而来的上述 predecessor shape，不得凭该终态新增、清除或
改写事实。任何其他组合即使 delimiter/字符合法也按 malformed fail closed。

第三个 database-wide fact `ai_employee.restore_completion` 只接受恰好 86 个 ASCII bytes 的
`restore_completion:v1:<completion_authority_digest_v1>`。writer 只能在持有 lifecycle/target locks 的 owner
transaction 内使用 `ALTER DATABASE SET/RESET`，随后直接重读 `pg_db_role_setting`；每个 present key 必须在
当前 database、`setrole=0` 的 `setconfig` 中恰好出现一次。重复 key、role-specific row、malformed array、
非 canonical encoding、未知 schema 或依赖 last-write-wins 的解析都 fail closed，不能由 writer/parser
“修复”成合法状态。

普通三态 parser 必须复用上述 canonical ACL 与 runtime-role readers。`baseline posture` 表示 database ACL
精确等于 `baseline` tuple multiset、app/retention 两角色都存在且满足安全 posture、无任一方向 membership；
`active posture` 对应精确 `active` tuple multiset与同一安全角色条件。database object grants 仍由独立、
phase-aware 的最小权限 verifier 校验，不得从 database ACL 推断。catalog admission 是三态闭集：

| 状态 | maintenance gate | call authority | completion GUC | database ACL / runtime roles | 允许入口 |
|---|---|---|---|---|---|
| `pristine_idle` | absent | absent | absent | baseline posture | ordinary migration/role-bootstrap/owner lifecycle，或新 generic/sealed admission |
| `active` | present | 同 attempt、合法且 phase 非 `completed` | absent | active posture | 仅 matching generic/sealed executor/reconcile |
| `completed_idle` | absent | 合法 `completed` | present，且与 completed call 双向 digest 绑定 | baseline posture | ordinary lifecycle，或验证并归档旧 pair 后的新 admission |

除此之外的任何组合都 fail closed，包括 gate-only、call-only、completion-only、active gate + completion、
completed call 缺失、completed call/completion digest 不匹配、cross-attempt pair、gate absent + active posture、
active gate + baseline posture、unsafe/missing role、membership 或任一 ACL tuple 偏差。不得再用
“completion absent + baseline posture”单独判非法，因为它正是 `pristine_idle`；但 PostgreSQL
fresh/default 与 pre-protocol legacy tuple 不是 baseline，必须先经过上面的 transient transition。稳定
admission 始终只有 `pristine_idle | active | completed_idle` 三态。`needs_attention` 是 active call 的
合法终态 phase，但只允许事故处置，不允许被 ordinary lifecycle 或新 attempt 覆盖。

无 active gate 的新 generic/sealed 请求在 management lifecycle + target lock 下、建立 gate 并把
`baseline posture` 精确转换为 `active posture`
之前，必须先通过只读 verifier 证明 current revision 和完整 `restore_fingerprint_v1` 逐字节等于 manifest
expected post。相等时返回 evidenced `restore_already_applied`，`psql_calls=0`、`pg_restore_calls=0`，数据库
写入、audit、gate/call/completion GUC mutation 全为零，既有 completed authority 保持不变。controller 只在
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>.<kind>.<source_binding_digest_v1>.already-applied.json` 发布 schema
`ai_employee.postgres_restore_already_applied.v1` 的 canonical content-free evidence；exact fields 为
schema/kind/target/source/manifest/expected revision+fingerprint/observed revision+fingerprint/result，observed
必须等于 expected，且 canonical content 中的 target/kind/source 必须与 filename 逐字节一致，使 generic 与
sealed 绝不互相覆盖。文件不含时间戳或随机 ID，以 same-directory temp、mode `0600`、file/directory fsync 与
no-clobber publication 发布；目标不存在时原子创建，已存在且 canonical bytes 完全相同才视为幂等成功，
已存在但任一 byte 不同必须 fail closed，禁止 overwrite。没有逐字段 evidence 不得跳过正常 restore。

新 generic/sealed attempt 只能从 `pristine_idle` 或 `completed_idle` 进入虚拟 `new`，并冻结新 attempt UUID、
`call_ordinal=0`、`reopen_ordinal=0`、database `gate_established_at` 与 exact kind/target/source/manifest。
`pristine_idle` 的 `previous_completion_digest` 为 `-`。从 `completed_idle` 开始时，holder 必须先重算并验证
旧 completed call 的 zero-slot canonical digest、matching completion GUC、gate absent 与 baseline posture，再把
exact old call + completion pair 重建/写入旧 attempt 的 mode-`0600` final state-v4 文件；该文件必须
file/directory fsync、no-clobber，已存在时只接受 canonical bytes 完全相同。这个 archive 只是不可变诊断
projection；新 call 的 `previous_completion_digest` 才把已验证旧 pair 的 digest 带入下一条 catalog chain。

随后一个持锁 owner transaction 必须再次比较 exact old three-fact tuple 与 archive digest：从 pristine
要求三者 absent；从 completed 要求旧 call/completion 仍互绑。事务按固定顺序 RESET completion（pristine
为 canonical no-op）、用新 `gate_established` active call 原子替换 absent/旧 completed call、设置 maintenance
gate，并只对 app/retention 执行 owner-granted CONNECT revoke；`PUBLIC` 在 baseline 已无任何 database
privilege tuple，不能再次用宽泛修复掩盖偏差。事务提交前必须直接重读 database ACL 已精确成为
`active`、两 runtime roles 仍为 safe posture 且无 membership。gate 建立阶段不向 `audit_events` 写任何行；
只持久化 catalog gate/call，并在 commit 后 fsync 外部 state-v4 projection。若在 projection 前 crash，另一主机必须仅凭
active gate + active call 重建。commit 后终止既有非 owner session并验证新 app/retention connection 被拒绝。
archive 完成但 catalog CAS 前 crash 只留下无害 archive，数据库仍是原 `completed_idle`；catalog commit 则
必须把旧 completion RESET、新 active call 替换与 gate/exact ACL tuple 变更作为一个原子结果，绝不能暴露 gate-only、
call missing 或 completion 已清除但 active call 尚未建立的中间状态。
`needs_attention` 不能被新 attempt 覆盖。restore 进程崩溃会释放 advisory lock，但 gate/call database
setting 继续阻断 owner entry/reset；非 matching attempt 在重新取得 lock 后仍必须零写入。不同 manifest
与 generic/sealed kind 由同一 lifecycle/target lock 与三态 catalog 串行；legacy conversion 只使用其独立
disposable target/registry，不占用或改写这组三个 catalog facts。

matching restore 只有持锁 session 本身可以继续；不能靠 `AI_EMPLOYEE_RESTORE_CONNECT_FENCE`、attempt
环境变量或第二条 owner connection 冒充 lock owner。正常 role bootstrap 在 gate 非空时绝不 grant
CONNECT 或其他权限；restore 所需 object/schema grants 与 verifier functions 必须由持锁 owner
maintenance executor 在同一 connection 上调用。对已经 admission 为 `pristine_idle` 或 `completed_idle` 的
数据库，standalone role-bootstrap 必须先证明 baseline posture 与独立 object-grant verifier 都精确通过，
随后只允许更新 Secret 驱动的 role password并幂等重申 owner-granted、non-grantable CONNECT 与既有最小
schema/table/sequence grants；它不得修改 role attribute/config/membership、清除 completed pair、扩权或
重跑 candidate transition。任一 unsafe role posture 在 password/grant 前零写拒绝；`active` 及其
`needs_attention` phase 一律拒绝。migration wrapper、role bootstrap 或任意 owner one-off
若在 active gate 建立后启动，无论原 restore 仍活跃还是已经 crash，都必须在第一笔写前拒绝，测试要求
migration/drop/create/grant/audit、CONNECT grants、`pg_restore` 与 catalog authority mutation 全为零。
restore executor 的 management session 与 target owner session 只用于 manifest-bound generic/sealed，并从
建立 gate 前分别持有 management lock 与 target/exclusive-schema locks，覆盖受监督的 psql consumer、
pg_restore generator、grants、verifier 与最终 reopen；任一 control session/lock 丢失后不得继续写阶段。
必须尽力终止仍受控 child并把结果视为 unknown；新的 matching invocation 只有重新取得 lifecycle/target/
schema locks 后，才能按 catalog authority 与 exact backend facts reconcile。

database catalog 是跨主机唯一 restore-call authority；本地 state 只是可丢弃 projection。generic/sealed
restore service 必须把 backup 与 sealed artifact mount 始终设为只读，并把独立绝对目录
`RESTORE_STATE_DIR` 作为唯一可写 state volume；该目录不得等于或位于任何 backup/artifact tree 下，
host/container 必须以无 symlink 的 resolved path 复核不同 mount source。
Task 27D 的 `.env.example` 提供 development/test host 默认 `RESTORE_STATE_DIR=./var/restore-state`；Compose
将其 read-write mount 到 container 固定 `/var/lib/ai-employee/restore-state` 并在容器内使用该绝对值。
production 必须显式设置专用绝对 host directory/volume，拒绝相对默认、symlink、group/world writable、
owner/mode 不符或与 backup/artifact resolved path 重叠；目录 `0700`，且不得存 password/Secret 或提交仓库。
attempt projection 路径固定为
`${RESTORE_STATE_DIR}/<target_identity_digest_v1>/<attempt_uuid>.json`，schema
`ai_employee.postgres_restore_state.v4`，父目录 `0700`、文件 `0600`，不上传、不参与 backup retention/orphan
cleanup。projection 记录 gate/call/completion catalog facts 的完整 content-free 镜像和稳定 result code，通过同目录 current-run
temp、mode check、file/directory `fsync` 与 atomic rename 发布，但不能授权 transition、spawn、retry 或
reopen。completed projection 包含 exact completed call 与 matching completion GUC，完成后转为 immutable
no-clobber archive；下一 attempt 只能在校验/归档该 pair 后替换 catalog call。另一主机没有 local state，或
state 丢失/损坏/滞后时，必须直接从 gate/call authority 与 completion GUC 重建；`completed_idle` 必须同时
读取不可缺失的 completed call、matching completion GUC、gate absent 与 baseline posture，任何一项缺失都不是
completed。completion audit 可以存在或已按 retention/privacy 删除，但不能补足 catalog pair。authority
信息不足、malformed 或互相矛盾时返回 `needs_attention`
disposition并停止。只有当前
phase 在下述 graph 中存在到 `needs_attention` 的边才持久化该 phase，否则保持 authority 不变等待事故
处置；不得把“缺 local state”解释成“从未 spawn”。legacy conversion 使用独立 registry，不能填充 v4 projection。

完整 phase graph 逐字冻结为：

```text
new → gate_established
gate_established → restore_backend_starting | restore_succeeded  # 后者只允许 exact post evidence，零 psql/pg_restore
restore_backend_starting → restore_backend_ready | needs_attention
restore_backend_ready → restore_started | restore_not_applied | needs_attention
restore_started → restore_succeeded | restore_outcome_unknown | restore_not_applied | needs_attention
restore_outcome_unknown → restore_succeeded | restore_not_applied | needs_attention
restore_not_applied → restore_backend_starting # 显式新 call_ordinal，且先证明 exact pre-state
restore_succeeded → grants_succeeded
grants_succeeded → verified
verified → reopen_committing
reopen_committing → completed | reopen_not_applied | needs_attention
reopen_not_applied → reopen_committing         # 新 reopen_ordinal
```

`completed`/`needs_attention` 是终态；禁止自动跳跃、倒退或非法 self-transition。每次 authority update 都在
owner transaction 中直接重读 `pg_db_role_setting` 并以 exact old value CAS，至少比较 attempt UUID、expected
phase 与 `call_ordinal`；reopen 分支还必须比较 `reopen_ordinal`。CAS miss 之后外部调用与后续写入均为零。
grant transaction 明确 rollback 时 phase 保持 `restore_succeeded`，verifier 失败时保持
`grants_succeeded`，只允许稳定上限的有界重试，不能通过 self-transition 隐藏失败。

matching `gate_established` attempt 恢复时若 exact read-only verifier 已证明 current 为 expected post，则直接
CAS `gate_established → restore_succeeded`；不得启动 psql/pg_restore，也不写单独 already-applied audit。
该 attempt 随后仍经过 grants、verifier 与统一 completion transaction。其他情况只能走真实 call。

每个真实 call 在分配新 ordinal 前，holder 必须证明 current 完整等于冻结 pre-state。随后在同一 owner
transaction 中以 exact old authority CAS
`gate_established|restore_not_applied → restore_backend_starting`：`call_ordinal` 必须恰好递增 1 且从不复用，
`reopen_ordinal` 保持不变，写入本次新的数据库 `call_started_at`，把 backend PID/start 重置为 `-`，从本次
刚验证的 exact current 重新冻结 pre revision/fingerprint，并把 observed/completed 字段置为 starting shape
要求的 `-`。从 `restore_not_applied` 重试时不得复制上一 ordinal 的 backend facts；这是后继字段可被清空的
唯一受控 ordinal-boundary 例外，同一 ordinal 的 ready/started/outcome 必须继续保留本次 call-start、pre 与
backend facts。direct exact-post 的 ordinal-0 shape 不受该规则影响。受控 Python executor 读取长期
bootstrap owner Secret，只用它启动一个固定 target database、固定
`ai_employee_owner`、固定 `PGAPPNAME=ai_employee_restore:<attempt_uuid>:<call_ordinal>` 的
`psql --set=ON_ERROR_STOP=1 --single-transaction` child。psql stdin 是 controller 独占 write end 的匿名
pipe；child 建立连接后等待 stdin，尚未收到 SQL。controller 从持锁 control session 查询
`pg_stat_activity`，要求 exact database/role/application name 只有一个 row，验证 PID 与 `backend_start`
晚于本次 spawn fence，再 CAS `restore_backend_starting → restore_backend_ready` 并持久化 PID/start。

只有 gate、authority、backend identity 与 controller-owned pipe 仍一致时，controller 才 CAS
`restore_backend_ready → restore_started` 并 fsync v4 projection；此前 SQL bytes 与 pg_restore calls 都为
零。随后 controller 先发送 transaction-local deferred completion guard prelude，再启动已清除全部
`PG*`/DSN/Secret 的
`pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>`。pg_restore 只生成
SQL；controller 逐块读取 stdout 并写入已登记 psql stdin，禁止 shell/uncontrolled pipeline、SQL 落盘或
日志输出。只有 generator EOF 且 exit=0 才发送 completion-guard trailer并正常关闭 pipe；缺 trailer 的
EOF、controller crash、任一 child 非零或 pipe failure 必须让 psql 的 deferred guard/single transaction
rollback。controller 必须终止另一 child、关闭全部 FD并收集两个 exit/status。

controller 在 backend visible 前 crash 时，唯一 pipe write end 关闭；psql 即使稍后连接也只见 EOF，且 phase
未到 `restore_started`，不得执行 SQL。ready 后 feed 前 crash 同样零 SQL；mid-stream crash 因 completion
trailer 缺失而 rollback。`restore_backend_ready`/`restore_started`/unknown reconcile 必须先按 authority 中
PID+backend_start+database+role+application name 证明 exact backend 已退出，再读 fingerprint。处于
`restore_backend_starting` 时，只有原 controller 仍在本机存活、仍持有可验证的唯一 child/pipe identity，且能
证明尚未向 psql 写入任何 SQL byte，才可继续发现并登记 backend、CAS 到 `restore_backend_ready`；任何另一
controller/主机或缺失、损坏、不可验证的 local projection 都不得用 pipe-owner-dead/SQL-byte-zero 猜测
not-applied，也不得分配新 ordinal，而必须 CAS `needs_attention`（CAS 不安全时只持久化同 disposition）并保持
`pg_restore_calls=0`。ready/started/unknown 在 backend 退出后，exact post 转 `restore_succeeded`，exact
pre-state 转 `restore_not_applied`，partial/inconsistent/不可区分转 `needs_attention`。starting 的人工处置必须
先由 operator 核对并终止可能存在的 child/backend，再通过显式 forward-fix 处置；不得声称自动恢复。只有
operator 显式授权并再次证明 exact pre-state，才可从合法 `restore_not_applied` 为同一 attempt 分配新
ordinal；禁止盲目 replay。测试还必须覆盖合法 `restore_not_applied → restore_backend_starting` 新 ordinal
写入新的 call-start、重新冻结 exact-current pre pair、清空旧 backend pair，并将 observed/completed 设为
starting shape；拒绝 ordinal 复用、旧 PID/start 继承以及同一 ordinal 内清空 call-start/pre/backend facts。测试覆盖
spawn-before-visible crash、backend-ready-before-feed、mid-stream crash、
generator/consumer failure、commit ACK unknown、两 host overlap、stale PID/backend_start、跨主机无 projection
且无 local child/pipe identity 时 `pg_restore_calls=0`/`needs_attention`，以及原 controller 仍持有 identity 的
唯一 starting→ready 成功路径。

restore 后的 object/schema grants、generic 或 sealed 专用 verifier 都在 gate active、database ACL 精确为
`active`、runtime roles 保持 safe posture且同一 target lock/session 持有时完成；所有可能失败的 Schema/revision/artifact/image/
抽样事实与 `SET ROLE ai_employee_app`、`BEGIN READ ONLY`、session/current-user、SQLSTATE `25006` 检查都必须
先完成。随后以 expected `verified`、attempt、call/reopen ordinal CAS 为 `reopen_committing` 并把独立
`reopen_ordinal` 增加 1。completion event type 固定为 `database.restore.completed`，metadata schema 固定为
`ai_employee.database_restore_completed.v1`；exact metadata keys 且禁止额外 key：`schema`、`attempt_id`、
`kind`、`target_identity_digest_v1`、`source_binding_digest_v1`、`manifest_sha256`、
`previous_completion_authority_digest_v1`（pristine 来源为 JSON `null`）、`final_call_ordinal`、
`final_reopen_ordinal`、`gate_established_at`、`final_call_started_at`（direct exact-post 为 JSON `null`）、
`final_backend_start`（未启动 psql 为 JSON `null`）、`expected_post_revision`、
`expected_post_restore_fingerprint_v1`、`observed_post_revision`、
`observed_post_restore_fingerprint_v1`、`maintenance_gate_version`（整数 `1`）、`maintenance_gate_value`、
`completed_at`、`completion_authority_digest_v1`。这些 timeline/fingerprint 字段都来自 active call 与最终
同 session verifier，只含安全 metadata；`completed_at` 由最终 database transaction 冻结为 UTC RFC3339、
恰好六位微秒和 `Z`。gate 建立、call 开始、failure 或 `needs_attention` 不预写 AuditEvent；恢复完成前的
持久事实只有 catalog call/gate 与 state-v4。事故稳定后 operator 可另行追加 content-free incident audit，
但它不反写 completion metadata 或 catalog authority。

completion digest 必须覆盖完整 completed-call schema，不能只哈希部分字段。最终事务先构造 exact
`restore-call:v4` completed value，其中 phase=`completed`、observed facts 与最终 verifier 相同、
`completed_at` 已冻结，最后一个 digest 槽位暂时使用恰好 64 个 ASCII `0`。该完整 1133-byte-bound
zero-slot authority value 是 `canonical_completed_call_bytes`；authority digest 固定为
`SHA-256(b"ai_employee.restore_completion_authority.v1\0" || canonical_completed_call_bytes)`。随后只把末尾
zero slot 替换为所得 64 位小写 digest，其他 byte 不得变化。验证者必须把 stored completed call 的末尾
digest 重新归零后重算，并同时要求 call 内 digest、completion GUC digest 与重算值三者相等；这种固定
zero-slot 规则消除自引用歧义并绑定 completed call 的每个事实字段。

最终事务必须先锁定并读取
`users`，要求全表恰好一行；该唯一管理员可以是 active，也可以是 M1 全数据删除后保留的 inactive anonymized
row，零行或多行都 fail closed。最终只允许一个 owner transaction 同时把 call authority CAS 为带该 digest
的 `completed`、插入一条普通 BigInteger-ID audit、
`ALTER DATABASE ... SET ai_employee.restore_completion='restore_completion:v1:<digest>'`、RESET maintenance
gate，并只以 owner grantor、non-grantable 方式为 app/retention 恢复 CONNECT，使 database ACL 精确成为
`baseline`；`PUBLIC` 不产生任何 tuple。事务提交前还必须重读 safe runtime role posture、无 membership 与
phase-appropriate object grants。AuditEvent ID 不进入 canonical bytes、
call authority、GUC 或 ACK predicate。completion audit 的外层字段固定为：`user_id` 等于上述唯一管理员
row 的 ID，`task_id=NULL`，`event_type=database.restore.completed`，`actor_type=system`，
`actor_id=database_restore`，`created_at` 使用数据库时间；metadata 使用上述 exact versioned key set，禁止
额外 key。最终事务当刻发现同 attempt/digest 已有 audit、INSERT 数量不是一、schema/key set/value/digest
不匹配都 rollback；这只是提交原子性与审计证据校验，不使 audit 成为 admission/ACK authority。该 audit
后续被普通 retention 或全数据删除移除不影响 completed call + completion GUC pair。

commit ACK unknown 必须关闭旧 session，用新 owner session重新取得 lifecycle/target lock并先读取
`ai_employee.restore_call_authority` 与 `ai_employee.restore_completion`。只有 gate absent、baseline posture、
合法 completed call、expected completion GUC，且 zero-slot 重算证明二者互绑，才是 `completed_idle` 并禁止
replay；audit 和 local projection 都可缺失，缺失时从 catalog pair 重建 projection。completed call 本身
不可缺失，也不能降级成“diagnostic projection”。由于每个 active attempt 建 gate 时已 RESET completion，
只有 exact active authority `reopen_committing` + completion GUC absent + matching gate + active posture 才证明
final transaction 未应用，并以 exact old call value CAS 到 `reopen_not_applied`。只有 operator 显式授权才
以新且从不复用的 `reopen_ordinal` 再 CAS 到 `reopen_committing`。任何 malformed/conflicting GUC、completed
call 缺失、digest mismatch、completion + active gate、absent completion + completed call、或其他不属于
三态矩阵的组合都进入 `needs_attention`。`pristine_idle` 的 completion absent + baseline posture 明确合法，
不得误判 mixed。本地 projection 不是数据库事务或 authority。commit 后 host crash 不是 reopen failure，
服务仍由 operator 之后显式启动。

通用 `just restore file` 只接受已发布 manifest-bound 组；sealed `just calendar-aad-restore-0018 file` 在
读取 owner Secret 前额外验证 preflight/`pre-migration`/exact-image/revision；legacy 只对 disposable
isolated target 使用经验证的旧 source binding。三者的 recipe、validator、artifact 与 output 不能互调或
混用；legacy 只在 disposable target 上复用普通 backup lifecycle，不能进入 workspace/production 的
三态 restore admission。manifest-bound generic/sealed 才共用 lifecycle/target/schema locks、catalog gate/call/
completion authority、state-v4 projection、Python psql/pg_restore stream reconcile、
holder-session verifier 与 completion-GUC atomic reopen executor；legacy 必须使用独立
durable registry、disposable target 和 cleanup/scavenger lifecycle，不得调用该 restore executor。
development/test 同 workspace writer 仍运行时仍必须拒绝；若不能停止，只能使用明确隔离目标。

Task 27D 以前产生且没有 manifest 的 legacy backup 禁止直接恢复 production，也禁止为既有加密字节
伪造 manifest。唯一转换入口固定为
`just restore-legacy-to-isolated file output_basename`，且只允许
`APP_ENV=development|test`。专用 `scripts/convert-legacy-backup.sh` 必须先验证 input 是受控路径下的普通
文件、旧 checksum 与安全无冲突 output basename。创建任何 Compose/temp 资源前，先在
`${BACKUP_DIR}/.legacy-conversion-registry/` 通过 temp+fsync+atomic rename 发布 mode `0600` registry
record，记录 attempt UUID、严格 UTC `created_at`、kind、project/resource names、统一 labels、受控临时
Secret path 与 source binding；目录 mode `0700` 且不上传/不参与 backup cleanup。随后才创建
`aiemployee-legacy-<32-lowercase-hex-uuid>` Compose project，只启动 profile `legacy-conversion` 下的
`legacy-conversion-postgres`/`legacy-backup-converter`，使用 project-private `internal: true` network、
ephemeral PostgreSQL volume 与登记路径中的 mode `0600` 临时 Secret；project、container、network、
volume 都必须带 `com.ai-employee.maintenance.attempt`、`.kind`、`.created_at` labels。Secret 不得输出。
isolated services 不连接或修改 workspace/production network、volume 或数据库。

脚本只把 legacy dump 恢复到该临时 PostgreSQL，以 app-compatible 只读路径实读 Alembic revision 和健康
事实；未知或不受支持 revision 立即失败。通过后，针对隔离数据库调用同一普通 backup 锁/manifest 路径，
向受控 `${BACKUP_DIR}` 以 `output_basename` no-clobber 发布新的 dump/checksum/manifest 三件套。成功只
输出 content-free 新组路径/结果码；失败输出稳定错误与清理结果，不得输出 Secret、DSN 或内容。每次
conversion 开始前先运行 `just legacy-backup-scavenge`。正常 trap 仍在成功和可捕获失败/signal 上执行
profile-scoped `down --volumes --remove-orphans`、删除 temp Secret/registry并确认 project resources 已清理；
另由 `scripts/scavenge-legacy-backups.sh` 扫描 registry 与精确 labels，固定 grace 3600 秒，只在 record 超时、
per-attempt nonblocking lock 可取得且无 active labeled container 时删除对应 container/network/volume、
临时 Secret 与 registry。Docker daemon 不可用、label/record 不匹配、仍有 lock/container 或尚在 grace
内都不删除。测试必须覆盖 SIGKILL、daemon crash 后遗留回收，以及另一 active run 不受影响。该流程不是 revision
repair、stamp、downgrade 或 sealed 0018 restore；generic/sealed/legacy recipe、service、script、fixture 与
tests 互不调用，也不可混用 artifact 或前置条件；共享的只有 admission primitive。

0019 使用一个固定、不可由运维覆盖的安全余量 `CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS = 900`，
与当前 durable task 总超时默认值对齐，但不随运行时配置漂移。preflight 为每个 distinct affected
connection 先提交 durable refresh fence，再主动 refresh，并只通过 callback、mail/calendar Worker 与
preflight 共用的 snapshot-CAS credential rotation boundary，把 credential 与 confirmed 事件原子提交；
随后以数据库中这些已 confirmed 新 token 的最早
`token_expires_at` 计算原始
`rollout_deadline = min(token_expires_at) - 900 seconds`。ACK-lost closure/current readiness 后和每个
后续 guard 都重新读取 affected connections 当前持久化 access credential expiry，计算
`current_deadline`，并以 `min(original_artifact_deadline, current_deadline)` 作为 effective deadline；
窗口只能收紧，不能因后续更长 expiry 延长。所有时间使用 UTC；禁止“token 足够新鲜”“窗口预计够用”
等主观判断，也禁止把历史 confirmed deadline candidate 当作 current deadline。零 affected pair 必须形成显式、可核验的 no-op
artifact；所有后续 guard 都必须重新证明 affected set 仍为空才可接受其 null deadline。其他情况若
preflight 完成时 `now >= effective_deadline`，必须在备份前失败。宿主机 recipe 还必须从 Compose 实际
选中的 backend image 解析内容 ID 并内部传入，artifact 绑定该 ID；运维人员不得手填 image ID，后续
backup/audit/migration/resync/restore verifier 必须拒绝当前镜像内容与 artifact 不一致。

部署顺序：

1. 保持外部写入默认关闭，关闭 Calendar 周期调度并停止新入口流量。
2. 排空并停止所有 pre-0019 CalendarEvent reader/writer，包括 Caddy、API、Worker 与 Scheduler；确认
   没有旧 `sync_calendar` 任务、事件 upsert 事务或旧 reader 仍在运行，PostgreSQL 与 Redis 保持运行。
3. 运行 Task 27C 提供的无参数 `just calendar-aad-preflight-0019`。该 0018-compatible one-off 首先通过
   独立 PostgreSQL session 执行固定 `pg_try_advisory_lock(20260809, 19)`，并在取得 revision-global
   lease 前禁止检查 artifact、调用供应商或写凭据；获取失败以 `calendar_aad_rollout_locked` 返回，零
   refresh、零持久写入。同一 lease 覆盖整个网络阶段和 artifact 发布，每次 provider call、credential
   commit、artifact publish 前都核验同一连接仍持锁，且不持有业务事务跨网络。随后只从历史完整 AEAD
   三元组推导 affected pairs，逐 pair 复核精确 cursor、connected owning connection、enabled
   `calendar.read`、同时存在的 access/refresh AEAD credential 和精确 `ProviderCalendar`。

   preflight 按 connection UUID 去重并确定性串行处理。每个 connection 在任何 refresh provider call 前还
   必须通过共享 `OAuthRefreshCoordinator` 取得该 `connection_id` 的 session advisory lease；revision-global
   lease 与 connection-scoped lease 必须同时保持有效，任一获取失败或响应后丢失都不得进入下一次 provider
   call、credential CAS 或 artifact publish。每个 connection 先冻结本轮 current generation、完整
   access/refresh snapshot；随后在受控内存按精确 AAD 解密 refresh token，验证 non-empty、UTF-8 和现有
   长度边界，才按 M2 固定 APP root key/version 计算 current `refresh_token_identity_v1`。这些事实作为可能
   新 automatic attempt 的 `fence_generation=F`、两个 pre-digest、old identity 与 key version，再检查既有
   refresh fence：同一 rollout 已有合法 matching `oauth.refresh_confirmed` 时，该 automatic attempt 已永久
   closed；current credential 精确匹配 post-snapshot 时从持久 token/expiry 恢复后续 probe，另一次合法
   refresh/reauthorization 已先改变 current facts 时则按 current readiness 继续，均不再次调用旧 attempt。
   存在没有合法 result-union member 的 unresolved `oauth.refresh_started` 时，相同或不同 basename 都先保持
   零 provider call。只有用户显式 progressive recovery 产生的 `oauth.refresh_credential_replaced` 精确绑定原
   automatic attempt、`started_source`、`fence_generation=F`、两个原 pre-digest、old identity/key version、
   recovery OAuthAttempt/event、F/S/T、两个 required post-digest、different-token new identity/version、
   `refresh_identity_changed=true`、persisted expiry 与严格晚于两个 matching started 的 `created_at`，且
   old/new identity 不等，才永久关闭旧 fence与 matching recovery
   attempt。automatic
   `provider_refresh`、targetless callback、generation/physical digest/access-only 变化都不能消费它。
   preflight 随后独立验证 current connection 仍 connected、`calendar.read` enabled、scopes 完整、credential
   存在/归属/AEAD 可解密，并以固定 key version 重算 current plaintext identity；合法的后续
   generation/credential 变化无需等于历史 post snapshot，也不要求构造完整 credential lineage。只有 closed
   result 为 `refresh_identity_changed=true` 且 current identity 回到 old identity 时，才触发 A→B→A rollback
   conflict；changed=false confirmed 后发生 access-only refresh 或同 plaintext 重加密、current identity 仍为
   old == new 时继续按 current readiness。必需行缺失、AEAD/归属无效或 current facts 无法满足合法状态不变量
   始终返回 `oauth_credential_state_conflict` 并 fail closed；该冲突只阻断 rollout，不重开旧 attempt且
   provider call 为零。没有 matching replacement consumption 时不得把 unsatisfied 当作 original-fence
   consumption。

   上述 current 前置条件通过后，preflight 必须重检本轮冻结的 current generation/snapshot，并在紧邻
   provider 调用的短事务中向既有 append-only `audit_events` 提交 content-free
   `oauth.refresh_started`。事件使用新生成的 canonical lowercase attempt UUID，并绑定
   `fence_schema_version="oauth_refresh_fence.v1"`、`source="calendar_aad_preflight"`、source/target revision、
   `rollout_digest_v1`、`fence_generation=F`、`pre_credential_snapshot_digest_v1`、
   `pre_refresh_credential_snapshot_digest_v1`、old `refresh_token_identity_v1`、固定 identity key version 与
   stable result code，再使用已解密 refresh token 主动调用现有 Google/Microsoft OAuth refresh。access-only
   或 AEAD 解密失败在 started/provider 前 fail closed；`invalid_grant`、返回 token/expiry/scope malformed 或
   实际 scope 未覆盖连接保存的 canonical scopes/`calendar.read` provider scope 则留下 durable unresolved
   fence 并 fail closed。

   provider 返回后只允许共享 credential-rotation repository 的短事务按固定锁序重检连接、能力与
   generation，以完整旧 snapshot CAS credential，并把新 access/可选 refresh AEAD、精确
   `token_expires_at` 与 content-free `oauth.refresh_confirmed` 原子提交。known-valid response 无论 missing、
   same 或 different refresh token 都必须 confirmed：missing/same 只更新 access row 并逐字节保留旧 refresh
   row；different non-empty refresh 更新两行，但只关闭本次 automatic started，不追加
   `oauth.refresh_credential_replaced`，也不能消费任何旧 fence。confirmed 复用同一 attempt UUID、source、
   `rollout_digest_v1`、G/G/G、两个 pre-digest、old/new identity/version，并强制增加
   `post_credential_snapshot_digest_v1`、`post_refresh_credential_snapshot_digest_v1`、实际持久化
   `token_expires_at`、`refresh_token_disposition`、`refresh_identity_changed` 和
   `rollout_deadline_candidate = token_expires_at - 900 seconds`（仅作历史 result schema proof，不作后续
   current deadline 输入）；其 `created_at` 必须严格晚于 matching
   started。missing/same 必须 old == new、changed=false，different 必须 old != new、changed=true；不一致
   metadata 不能作为合法 result。禁止现有无条件 upsert。网络未知、调用后任一 lease 丢失、scope shrink、CAS miss 或数据库
   明确 rollback 均不产生 confirmed、执行 probe 或发布 artifact，也不得对同 snapshot 重放 provider。
   result commit ACK 丢失或异常导致 rollback 不可证明时，丢弃旧 session/lease，用新数据库 session 按
   user/connection/attempt 查询 versioned result union。matching confirmed/replacement 的 event schema、
   attempt/source 与时序合法即永久关闭本次 automatic attempt，provider 调用数仍为 1；没有合法 result 才
   保持 started unresolved、进入 `needs_attention`，后续 provider call 为零，禁止补写猜测 result。关闭后
   独立执行 current readiness：精确 post-state 可恢复 persisted expiry；ACK 丢失期间另一合法 refresh/reauth
   已提交则使用 current credential/expiry；changed=false 后续 access-only refresh 或同 plaintext 重加密不
   冲突，changed=true 后 current identity 回到 old identity 的 A→B→A、缺行、AEAD/归属无效或其他不一致返回
   `oauth_credential_state_conflict` 并阻断 artifact，但不重开 attempt。后续 Taskiq delivery 或
   `TransientProviderError` 路径只读取该关闭事实与 current readiness。confirmed 已提交但 artifact 发布前崩溃
   时，同一 rollout按上述规则继续。全部
   connection confirmed 后，以及 ACK-lost closure/current readiness 的每次恢复路径，都重新读取每个
   affected connection 当前持久化 access credential 的 expiry，计算只能收紧的 effective deadline，再通过
   供应商只读 Calendar adapter 调用与恢复路径一致的
   `initial_pages(scope_key)` 并要求最终 cursor；全部 probe 成功且最终 rollout guard 通过后才原子提交
   content-free rollout artifact。它不等待资源 401；主动刷新后的 401 直接失败且不得第二次 refresh。
   它不得调用 `directory_pages()`、连接级/full-account sync、其他 pair 或 Calendar 写适配器，也不得写
   CalendarEvent、cursor 或 marker；除 refresh fence 与成功的 credential CAS/confirmed 事实外不更新
   连接、能力或其他业务事实。preflight 使用与恢复相同的
   `SHA-256(20260809_0019 + NUL + connection_id + NUL + calendar_id)` locally hashed pair digest 输出
   content-free 结果。artifact 只包含 schema/revision、`rollout_digest_v1`、安全 basename、实际
   immutable image content ID、
   earliest expiry/deadline、固定 margin、affected/connection count、hashed connection IDs、pair digests
   和稳定结果码。任一失败都阻止迁移；禁止 retry waiver、直接 SQL、伪造 credential、access-only 迁移
   或迁移后清 marker。

   若 unresolved fence 通过带 connection ID 的 explicit progressive OAuthAttempt 恢复，start 已把 requested
   capabilities 置为 `authorizing` 并固定 target `T`。Google/Microsoft 任何输入合法的 provider error、
   missing/same refresh 或已知 provider/network failure 在 `T` 仍匹配时，必须复用
   `mark_progressive_authorization_failed`，把这些能力与 unsatisfied audit 同事务收敛为
   `action_required`，保留 actual scopes/last-verified facts且不写新 credential/scope/account facts；stale
   `T` 只做 capability no-op 与安全审计。matching `oauth.refresh_recovery_unsatisfied` 只关闭本次
   OAuthAttempt，不能消费 original fence；只有 different non-empty refresh 的完整 replacement proof 能消费。
   Google/Microsoft callback 缺 state、同时 code/error、两者都缺失或 malformed error 在消费前拒绝；任何
   合法 error（包括未知名称）先消费 state/收敛 attempt，并按安全分类矩阵返回稳定脱敏 Problem：用户拒绝
   与未知名称为 `oauth_authorization_failed`，Microsoft consent evidence 为
   `microsoft_admin_consent_required`，普通 `interaction_required` 为
   `microsoft_reauthorization_required`。raw error 不记录且不能成为持久错误码，随后 replay fail closed。
   targetless fenced identity 仍在任何保存前阻断。
4. preflight 全部通过后，在 Caddy/API/Worker/Scheduler/migration/general consumer 已停且三层写开关仍为
   `false` 的 sealed 维护窗口内，生成 manifest-bound 加密备份三件套并执行迁移前只读审计；backup/audit
   在启动和 artifact 原子提交前校验同一 rollout guard：非空集合从当前 access rows 重算并要求
   `now < effective_deadline`，零分支要求重新证明 affected set 仍为空。备份的 manifest、checksum 和
   dump 必须作为不可拆组进入发布证据，不能以单个 dump 代替。
   审计必须证明数据库仍位于 0018、没有部分 AEAD 三元组，并重复记录每个
   affected pair 的本地可恢复性事实、必需 access/refresh credential 和完全一致的 locally hashed set。
5. 通过受限 `calendar-aad-migrate-0019` one-off 应用前向 `0019`。0019 revision 与
   `migrations/env.py` 的 mutation 前/`on_version_apply` 最终 guard 已由 Task 16A 冻结；该 one-off
   只能注入 Task 27C 的真实 artifact guard，在任何 DDL/DML 前和事务最终提交前校验 preflight artifact、
   实际镜像内容 ID、精确 affected set、本地可恢复性和 effective deadline。已经 admission 为
   `pristine_idle` 的 fresh/严格空集合数据库仍由普通 `upgrade head` 走内建 affected-set zero-bootstrap；
   default/legacy bootstrap candidate 不能进入该分支，非空 0018 数据库缺 guard 则 mutation 前失败；再执行
   迁移后只读审计。验证
   只新增版本列/约束、v1 标记和精确 pair 的事件 scope 游标失效，其他 connection 的同名 calendar、
   directory cursor/revision 与全部事件 AEAD 字节均未变化。
6. 使只写 v2、正常读取拒绝 v1 的不可变镜像可供 one-off 恢复命令使用，但继续保持 Caddy、API、
   普通 Worker 与 Scheduler 停止；0019 不支持旧新 Calendar reader/writer 混跑，任何旧组件都不得回流。
7. 运行 Task 27C 提供的 `just calendar-aad-resync-0019`。该无参数入口在启动和每个任务最终本地提交前
   校验同一 rollout guard，只扫描
   `calendar_event_resync_required` marker，并对每个精确 cursor 行使用 `FOR UPDATE` 或等价 CAS
   串行化 planner。逻辑 pair digest 是 revision、connection ID 与 calendar ID 的 SHA-256；首次 ordinal
   为 1，稳定任务键固定为 `calendar-aad-0019:<pair_digest>:attempt:<ordinal>`，输入精确包含
   `connection_id`、`scope_key`、`recovery_revision`、`pair_digest` 与
   `recovery_attempt_ordinal`。`created/queued/running/retry_scheduled` 活动尝试始终返回并复用原 ordinal，
   绝不分配新 ordinal；仅当上一尝试已 `failed/cancelled` 且 marker 仍存在时，下一次显式 CLI 才能
   分配 `max_ordinal + 1`。多个活动尝试、
   `succeeded` 与 marker 并存，或 `waiting_approval/reconciling/needs_attention` 都 fail closed。旧终态
   TaskRun 不得复活或改写，单次 CLI 调用对每个 pair 最多创建一个新 ordinal，且 recovery ordinal 与
   单个 TaskRun 的 `attempt_count` 完全分离。每个 ordinal 的 TaskRun、审计与初始 Outbox 原子提交。
8. CLI 只精确分派并以内联 `DurableTaskRunner` 执行本次 planner 返回的任务 ID，不启动 Taskiq Worker、
   不扫描普通队列，也不发布或执行无关 `sync_calendar` 任务。专用恢复任务步骤在供应商访问前重新
   计算 pair digest，并核对 canonical task input、ordinal、幂等键、TaskRun 用户归属、现存非
   `directory` 精确 cursor、marker、连接读取能力、必需 access/refresh AEAD credential 与目录中的同一
   calendar；任一条件不满足都以无供应商调用的幂等 no-op 或稳定失败结束。有效任务只调用单日历事件 scope 路径，不调用
   `directory_pages()`、连接级 owner 或其他 provider/calendar；完成提交必须以 marker 为 CAS 前提，
   失败保留 marker。该流程只读供应商并写本地同步事实，不创建审批、ToolExecution 或真实写操作。
9. 执行 `post-resync` 只读审计，在读取前和 artifact 原子提交前检查同一 rollout guard，验证新密文均为
   v2、每个精确 pair 的 cursor/freshness 恢复且 marker 清除。任一 pair 失败、非空集合当前重算的
   `effective_deadline` 已到或 zero state 漂移时保持所有通用服务和真实写开关关闭。该
   `effective_deadline` 尚未到且精确失败条件可修复时，只能由后续显式 CLI 按既定 ordinal 规则重试；
   `effective_deadline` 已到或已不能证明会及时提交 artifact，且仍在
   sealed maintenance window、没有任何业务写入时，才通过独立
   `just calendar-aad-restore-0018 file` 使用本窗口 refresh 后/0019 前指定的加密整库备份恢复到
   revision 0018。该入口与通用 `just restore file` 完全分离；后者仍用于不要求 0019 artifacts 的普通
   灾备恢复。sealed 宿主必须先在任何 owner Secret、owner connection 或 `pg_restore` 前独立验证
   basename、dump/checksum/versioned-manifest、preflight/原 `pre-migration` artifacts、immutable image
   content ID 与镜像内 allowlisted executable digests；移动 tag、缺失/错误 image/script、executable bind
   mount 或 artifact mismatch 必须保持 owner/`pg_restore` 调用为零。通过后才向 operations profile 中不
   继承 `backend-common` 的专用 restore service 注入 artifact 匹配的精确
   `CALENDAR_AAD_RESTORE_IMAGE_ID=sha256:...`；service 的 `image:` 直接引用该 ID，无 tag、`build:` 或 pull，
   并固定 `docker compose --profile operations run --rm --pull never ... calendar-aad-restore-0018`。该服务只
   依赖 healthy PostgreSQL，且只能执行 immutable image 内的 sealed validator/executor。

   容器内 guard 在读取任何 owner Secret 前重复验证 manifest/artifact/revision/image/script binding；通过
   后以 `kind=sealed_0018` 调用与 generic 完全相同的 management lifecycle/target lock、database-wide
   maintenance gate/call/completion facts、独立 `RESTORE_STATE_DIR` v4 projection、Python-managed psql
   backend registration 与 SQL stream reconcile；backup/preflight/artifact mount 全部保持只读。controller
   只把 bootstrap owner credential 交给固定目标库的
   `psql --set=ON_ERROR_STOP=1 --single-transaction` consumer；credential-free
   `pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --file=- <dump>` 只生成 SQL。
   ready/state/gate 一致并 CAS+fsync `restore_started` 前 SQL bytes 为零；controller 管理 completion guard、
   pipe、两个 child status，禁止 shell pipeline。grants 和
   sealed-specific revision/artifact verifier 都在 gate active、exact active posture且同一 lock-holder owner
   session 上完成，verifier 必须 `SET ROLE ai_employee_app`、`BEGIN READ ONLY` 并证明 DML 被 SQLSTATE
   `25006` 拒绝。所有会失败的 digest/revision/data checks 必须先于最终单一事务；该事务原子写 completion
   ordinary BigInteger-ID exact metadata audit、把 call authority CAS 为 completed、设置 completion GUC、
   RESET gate并只恢复 owner-granted、non-grantable app/retention CONNECT，使 ACL 精确回到 baseline且
   `PUBLIC` 无 tuple。新 sealed attempt 建 gate 的同一
   owner transaction 必须先 RESET completion GUC。ACK unknown 必须关闭旧 session并由新 owner session同时
   校验 exact completed call、expected completion GUC、gate、ACL tuples 与 runtime role posture；completion audit 后续缺失不影响
   completed pair。明确
   not-applied 先进入 `reopen_not_applied`，只有新 `reopen_ordinal` 的显式 operator retry 才能再提交。只有该
   核对证明 completed 后，
   才检查并启动原 0018-compatible image。zero state 漂移或镜像/basename 不匹配必须先人工调查，只有仍能独立
   证明没有业务写入时才可能使用同一恢复例外。禁止 Alembic downgrade、直接 SQL、给 app 服务临时
   DDL、临时全量同步、connection-level directory sync、跳过 marker 或 OAuth-only 临时服务。
10. 只有 `post-resync` 审计通过后，才启动 v2-only API、普通 Worker、Scheduler 与 Caddy，运行
   `just health`，并验证 M1 登录、普通同步、简报、审批假工具和任务恢复。
11. 只为允许列表中的专用测试账户启用供应商写入，完成人工 E2E 与审计检查；最后再打开正式环境
    供应商开关，每个连接仍需用户单独渐进授权。

回滚应用镜像时，新表和列保留。一旦 0019 的 `post-resync` 审计通过、任一通用服务启动或发生任一
业务写入，应用回滚下限就是理解 AAD 版本列、拒绝 v1 读取并且只写 v2 的 0019-compatible 镜像；
无论 M2 任务是否均为终态，都不得回滚到 pre-0019/M1、v1 reader 或旧 Calendar writer。仅在上述
尚未重开服务且无业务写入的 sealed window，才允许通过指定迁移前整库备份恢复整个 PostgreSQL 到
0018 后启动原 0018 image；不得用 downgrade 模拟该恢复。切换到较旧但仍兼容 0019 的 M2 镜像前，
仍必须确认所有 M2 任务均为终态；若存在 `reconciling` 或 `needs_attention`，应部署保留 M2 Schema、
v2-only Calendar 能力和核对能力的前滚修复镜像，而不是直接回滚。

## 23. 主要风险与控制

| 风险 | 控制 |
|---|---|
| 邮件发送结果未知导致重复发送 | 先持久认领、禁止盲重试、Sent 核对、`needs_attention` |
| 会话被短暂接管后批准写入 | Secure/HttpOnly/SameSite、CSRF、10 分钟决定窗口、5 分钟认领窗口、会话撤销 |
| 多账户选择错误 | 回复绑定原连接、新建使用可切换默认值、审批展示精确账户 |
| 日程并发修改被覆盖 | 基础 ETag、执行前重读、条件更新、冲突后重新提案 |
| CalendarEvent 旧 AAD 未绑定日历，冒号 split collision 或混跑 writer 导致跨事件密文替换 | v2 使用固定 domain 与五个长度 frame 的共享纯 helper，UUID/strict UTF-8/field fail-closed 校验，字段独立版本与四列约束、固定 delimiter/Unicode 向量、0019 强制受限重同步、排空旧 Worker，且无 v1/旧 v2/去 calendar ID fallback |
| 0019 依赖 access-only/不可用 refresh，或恢复跨过 token 有效窗口 | affected connection 必须有可解密可用 refresh；preflight 按 connection 主动 refresh、验证实际 scope 并轮换 AEAD；artifact deadline 固定，后续从 current access rows 重算并只允许收紧；超时仅允许 sealed-window 整库恢复到 0018 |
| 历史 OAuth callback query 被 Uvicorn access logger 输出 | 四个真实启动入口全部关闭 access log；应用日志 schema 禁止 raw URL/query/request target；真实子进程 canary 同时证明 query 零泄露与脱敏审计正常 |
| PostgreSQL default/legacy ACL 或 unsafe runtime role 被误当作 `pristine_idle`，或 bootstrap crash 留下 partial role/ACL | 共享 reader 通过 `aclexplode(COALESCE(datacl, acldefault('d', datdba)))` 精确比较 `fresh_default`、`pre_protocol_legacy`、`baseline`、`active` 完整 tuple multiset，并同时验证 safe role attributes 与双向无 membership；fresh 只允许两角色同时缺失或同时 safe，legacy 必须同时 safe。仅 standalone role-bootstrap 与 protected reset post-create 可识别 candidate，并在同一 management/target/schema locks 下用单一 owner transaction 创建明确最小角色或只轮换 safe 既有角色密码、撤销 `PUBLIC` 全部 database privilege、授予 owner-granted/non-grantable 最小权限，再重读三 facts、exact ACL/runtime-role posture 与独立 object grants；任一偏差零写，错误整体 rollback，稳定 admission 仍只有三态。 |
| 0019 guard 只在专用 rollout CLI 存在，破坏 fresh DB 或普通 `upgrade head` | Task 16A 在 revision 内冻结 mutation 前与 `on_version_apply` 最终 guard；已先 admission 的 fresh/严格空集合走 affected-set zero-bootstrap，candidate 不能借此迁移，0018 非空无 typed guard 在 mutation 前失败；Task 27C 只注入，不重写 revision |
| 双 Worker、Taskiq 重投或 `TransientProviderError` 导致同一 connection 重复 refresh/exchange | 所有 writer 共用 `OAuthRefreshCoordinator` 与 connection-scoped session lease；provider 前提交 attempt-bound started，网络外无业务事务，响应后同事务 CAS/result；网络未知、响应后 lock loss、CAS miss 或已确认 rollback 后续 delivery 只读 fence且 provider call 为零；commit ACK 丢失用新 session 按 user/connection/attempt 查询版本化 confirmed/unsatisfied/replacement result union，合法 matching event 永久关闭对应 attempt，再独立核对 current readiness；后续合法状态变化不复活，只有 changed=true 的 old-identity rollback 或缺行/AEAD/归属/状态冲突返回 `oauth_credential_state_conflict` 且零调用，无 result 才 unresolved且绝不补写 |
| explicit recovery 拒绝或 missing/same token 后 capability 长期停在 `authorizing` | start 的 S→T 与 requested capability `authorizing` 原子提交；可安全确认的 unsatisfied 在同一短事务复用 `mark_progressive_authorization_failed` 收敛为 `action_required`、写稳定 error code并保留 actual scopes/last-verified facts；stale T 只做 no-op 与安全审计，用户可创建新 attempt，原 automatic fence 不被消费 |
| Google callback 的歧义、错误分类被抹平或 error 重放绕过一次性授权状态 | Google 与 Microsoft 都要求 state 且仅接受互斥 code/error；缺 state、code+error、两者都缺失或 malformed error 在消费前拒绝；任何合法 error（含未知名称）都先一次性消费 state并在 target T 写 unsatisfied/no-op，raw error 不入库/日志且 replay fail closed；公开 Problem 与持久 error code 使用安全分类矩阵，保留 `microsoft_admin_consent_required`/`microsoft_reauthorization_required`，用户拒绝与未知名称回退 `oauth_authorization_failed`；targetless fenced identity 仍在保存前阻断 |
| targetless callback 命中带 unresolved fence 的既有 identity，却覆盖 credential 或创建重复 connection | callback 在保存任何 credential、scope 或 capability 前返回 `oauth_refresh_recovery_requires_connection_start`；可追加 content-free blocked audit，但不得创建 candidate snapshot、消费 fence、调用 `ensure_connection`/无条件 upsert或创建第二个 connection |
| refresh token A→B→A 绕过 rollback guard，或同 plaintext 重加密被误判为 rollback | M2 从固定 `APP_MASTER_KEY_FILE` root key 与固定 `AeadCipher.key_version` 经 HKDF-SHA256/HMAC-SHA256 生成 `refresh_token_identity_v1`；confirmed/replacement 以 `refresh_identity_changed` 区分 plaintext 是否变化，parser 强制 disposition/flag/equality 一致。changed=false 的同 plaintext 重加密保持 old == new、不是 rollback；只有 changed=true 的 A→B proof 后 current identity 回到 A 才返回 `oauth_credential_state_conflict`，且不重开旧 attempt；M2 不更换 root key/version，也不引入 multi-key lookup |
| 并发或未知结果 preflight 重复 refresh，或陈旧 writer 覆盖新 token | revision-global lease 互斥全部 basename，且每个 connection 同时持有共享 coordinator lease；generic started/confirmed 精确绑定 attempt UUID、source、`rollout_digest_v1`、G/G/G、full/refresh pre/post 摘要、old/new identity/version、`refresh_identity_changed`、persisted expiry与 deadline candidate；mail/calendar Worker 与 preflight 共享完整 snapshot/generation CAS 和固定锁序。未确认 fence 跨 basename 阻断；automatic missing/same/different refresh 都只 confirmed 自己的 started并满足 flag/equality 矩阵，只有带 connection ID 的 explicit progressive recovery 返回 different non-empty refresh token并原子提交 old != new、changed=true 的完整 F/S/T replacement proof 才消费旧 fence |
| 历史清理删除未决 refresh fence 后触发迟到重放 | AuditEvent 使用 user-scoped fence-aware retention并复用同一 result union；confirmed 关闭 automatic started，unsatisfied 只关闭 recovery started，replacement 同时关闭 recovery started并消费 original fence。未匹配 union result 的 automatic started 跨 cutoff 保留；各完整事件组只有全部早于 cutoff 才删，且关闭不依赖后续 current credential lineage，跨 365 天回归要求后续 provider call 为零 |
| 并发 backup/migration/reset/cleanup 使目标被替换、manifest revision 错绑、覆盖或误删另一 run | 固定管理库 target lifecycle lock 先串行 target create/drop/reset 与所有 lifecycle wrapper；backup 另用 `BACKUP_LIFECYCLE_LOCK=(20260806, 274)` 跨主机串行同一数据库，`${BACKUP_DIR}` `flock` 串行本地目录。每 run 唯一 `.partial.<uuid>`、no-clobber/manifest-last、本地与 remote 3600 秒 grace 加锁内 mtime/manifest 重查；schema-lifecycle shared/exclusive lock与 pre/post revision CAS 绑定 dump，backup 期间 migration 不得提交，active restore authority 下 reset 必须零 drop/create。 |
| 通用灾备 restore 与 0019 sealed restore 混淆，或 crash/ACK 丢失后盲目重放/提前开闸 | generic、sealed 与 legacy validator/artifact/recipe 分离；legacy 只用 registry/labels/disposable target/scavenger，不写 workspace restore catalog。manifest-bound generic/sealed 持 management/target/exclusive-schema locks，并以 `restore-call:v4` 和逐 transition CAS 为唯一跨主机调用事实。本地 `RESTORE_STATE_DIR` v4 文件仅是 projection/archive，backup/artifact mount 只读。Python controller 先登记 credential-bearing psql backend，持久化 ready 后再 CAS+fsync started；credential-free pg_restore 只生成 SQL并由 controller 逐块转发，completion guard 保证 controller crash/EOF/mid-stream failure 回滚。跨主机无法证明本地 child/pipe identity 的 `restore_backend_starting` 必须进入 `needs_attention`，不得自动判 not-applied。no-gate exact post 使用 kind-aware、no-clobber 的 DB-zero-write local evidence，matching gate direct edge仍走统一 completion。每个 active attempt 原子验证/归档 prior completed pair、RESET completion 并建立 gate/active call，把 exact `baseline` tuples 变为 `active` tuples且保持 safe runtime-role posture，并且不写 pre-restore gate audit；最终事务写恢复后普通 BigInteger-ID exact metadata audit、设置 `restore_completion:v1:<digest>`、保留 completed call、RESET gate，并只恢复 owner-granted/non-grantable app/retention CONNECT，使 tuple multiset 精确回到 `baseline`，`PUBLIC` 仍无 tuple。ACK unknown 必须验证 completed call + completion GUC 的 zero-slot digest 互绑、gate absent、exact `baseline` tuples 与 safe runtime-role posture；audit retention/privacy 删除不影响该 pair authority。sealed 额外执行 exact-image/artifact/script guard。 |
| 日程通知行为不一致 | 通知策略进入冻结载荷、适配器无损映射，不支持即审批前拒绝 |
| Microsoft 个人与企业授权差异 | `common` 类委托授权、delegated `User.Read` 的 Graph `/me` 身份读取、tenant/account 规范身份、管理员同意状态 |
| 通用抽象扩大到 M5 | 命令联合只允许四个 M2 动作，不提供动态工具注册或 Planner |
| 敏感审批载荷明文落库 | 完整命令 AEAD、哈希绑定、受控内存解密、内容保留清理 |
| 无持续试用遗漏偶发问题 | 强化故障注入、两家专用账户人工 E2E、明确接受残余风险 |

## 24. 开源与官方文档参考

M2 供应商行为以实施时的官方文档和契约测试为准，当前设计参考：

- [Gmail API sending guide](https://developers.google.com/workspace/gmail/api/guides/sending)
- [Gmail threads guide](https://developers.google.com/workspace/gmail/api/guides/threads)
- [Google Calendar create events](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [Google Calendar events.insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/graph/permissions-reference)
- [Microsoft Graph get user](https://learn.microsoft.com/graph/api/user-get?view=graph-rest-1.0)
- [Microsoft Graph sendMail](https://learn.microsoft.com/graph/api/user-sendmail)
- [Microsoft Graph message delta](https://learn.microsoft.com/graph/delta-query-messages)
- [Microsoft Graph calendarView delta](https://learn.microsoft.com/graph/delta-query-events)

供应商文档变化不得静默改变 scope、数据流向或写入语义。若官方能力无法满足本文冻结命令和
结果核对要求，必须先修订规格，而不是在适配器中弱化安全保证。

## 25. 设计完成条件

本规格已经明确 M2 的产品边界、用户控制、架构、命令 Schema、任务和工具状态、审批、邮件、
日历、OAuth、Microsoft 同步、幂等、核对、补偿、数据模型、API/SSE、前端、安全、保留、
错误、可观测性、测试、验收、部署和主要风险。

书面规格经用户复核批准后，下一步应创建独立的任务级 M2 实施计划。实施计划必须按测试驱动
方式拆分迁移、可信操作内核、Google、Microsoft、前端和发布证据，不得直接沿用 M1 计划或在
同一任务中同时修改多个高风险供应商写路径。
