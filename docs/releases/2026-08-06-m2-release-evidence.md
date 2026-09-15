# M2 发布证据

本记录对应已批准的 M2「可执行邮件与日历助手」。验收日期为 **2026-09-15（UTC）**，文档收尾为上海时间 2026-09-16，下文运行时间均为 UTC；
真实专用账户的所有者和逐命令审批人为本会话用户，自动化、只读复核和记录整理人为 Codex 控制器。
本次 **fresh/zero-bootstrap 发布门禁通过**，12 项专用账户操作已完成。生产部署和非空生产 0019
维护窗口未执行，真实写入已关闭。

| 候选标识 | 实际值 |
| --- | --- |
| 分支 | `feat/m2-executable-mail-calendar` |
| 验证基准 HEAD | `7e57ea8a57d104c6852a53394fd9a9b587373b62` |
| 含未提交实现的完整候选源码 SHA-256 | `ae85effb71d7c55f7eb87f309dff2410dbbe31ae96614fdd9b232d64210039d0` |
| 完整源码清单 | 722 个文件；逐文件 path/SHA 的规范 JSON 摘要 |
| 完整门禁 | `release-gate-f334665afd86404daa954aba3c46968b`，原生 exit 0 |
| 提交边界 | 已审 Task30 实现、限定修复、验收文档；提交信息 `test: freeze M2 release evidence` |

HEAD 与源码清单共同标识被测候选，不能把未提交实现当作基准 HEAD 的内容。本记录的交付提交
由 `git log -1 --format=%H -- docs/releases/2026-08-06-m2-release-evidence.md` 精确定位，避免在提交中
嵌入自身 SHA。最后的文档更新以逐文件差异证明运行代码、依赖锁、配置和镜像输入未变。

规格与验收依据为 [M2 设计](../superpowers/specs/2026-08-06-executable-mail-calendar-assistant-m2-design.md)、
[实施计划 Task30](../superpowers/plans/2026-08-06-executable-mail-calendar-assistant-m2.md#task-30-prove-crash-safety-complete-e2e-and-freeze-m2-release-evidence)
及[验收清单](../acceptance-checklist.md)。本文的 `.superpowers/` 路径标识本地受控、Git 忽略的原件；
公开记录只包含结果、合成向量和内容外摘要，不包含 Secret、真实身份键、原始 scope、供应商正文或响应。
原始失败与成功记录分开保留，历史执行不改写为当前候选结果。

## 固定合成协议向量

以下字节全部来自合成 fixture；它们不是实际凭据、身份指纹或生产时间。CalendarEvent writer/reader 共用 framed v2 helper；严格 UTF-8、不做 Unicode 正规化，A/B 不等，v1 与冒号拼接 fallback 均拒绝。完整门禁显式执行向量测试。

### CalendarEvent framed AAD v2

`A`：146 bytes；SHA-256 `67ba9e40f2157a49de2d987e1f4d8e61c2a78a7d71d4da7ce13421fde58a8e70`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAADYTpiAAAAAWMAAAALZGVzY3JpcHRpb24=
```

`B`：146 bytes；SHA-256 `52e707318449a55779a023931f5914909d662944f5364545e1795edbcf555dad`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAABYQAAAANiOmMAAAALZGVzY3JpcHRpb24=
```

`Unicode`：157 bytes；SHA-256 `0bdf656c1b037423e71c950df9f6bb5c56a737bcef8fe7d6e58e26d7dee04370`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEtNGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIAAAAJ5pel5Y6GL86xAAAACeS6i+S7tjrDqQAAAAhsb2NhdGlvbg==
```

### 物理快照与 rollout v1

`FULL_BYTES_B64`：497 bytes；SHA-256 `607c4c12cb3e4ff7736cb56f06b9dd2f63d2681c8d7a36c7801160eaafcaa84a`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvY3JlZGVudGlhbC1zbmFwc2hvdC92MQABAAAADGFjY2Vzc190b2tlbgEAAAAkMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTExMTExMTExAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAIAAECA/Dx8vMBAAAADBAREhMUFRYXGBkaGwEAAAABMwEAAAAbMjAzMC0wMS0wMlQwMzowNDowNS4xMjM0NTZaAQAAABsyMDMwLTAxLTAyVDAyOjAwOjAwLjAwMDAwMVoBAAAADXJlZnJlc2hfdG9rZW4BAAAAJDIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIyMgEAAAAkYWFhYWFhYWEtYWFhYS00YWFhLThhYWEtYWFhYWFhYWFhYWFhAQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIBAAAACN6tvu8Af4D/AQAAAAwgISIjJCUmJygpKisBAAAAATQAAQAAABsyMDMwLTAxLTAxVDAwOjAwOjAwLjk5OTk5OVo=
```

`REFRESH_BYTES_B64`：265 bytes；SHA-256 `3a4b1dbd0e77c915067c3af352772580949957815573c953baa41131827582f0`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcmVmcmVzaC1jcmVkZW50aWFsLXNuYXBzaG90L3YxAAEAAAANcmVmcmVzaF90b2tlbgEAAAAkMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAI3q2+7wB/gP8BAAAADCAhIiMkJSYnKCkqKwEAAAABNAABAAAAGzIwMzAtMDEtMDFUMDA6MDA6MDAuOTk5OTk5Wg==
```

`ROLLOUT_BYTES_B64`：222 bytes；SHA-256 `0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5`。

```text
QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcm9sbG91dC92MQABAAAAHmNhbGVuZGFyX2FhZF8wMDE5X3ByZWZsaWdodC52MQEAAAANMjAyNjA4MDlfMDAxOAEAAAANMjAyNjA4MDlfMDAxOQEAAAAbY2FsZW5kYXItYWFkLTAwMTktc3ludGhldGljAQAAAEdzaGEyNTY6MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWYwMTIzNDU2Nzg5YWJjZGVmMDEyMzQ1Njc4OWFiY2RlZgEAAAADOTAw
```

### 固定 root 的 refresh identity

独立 HKDF-SHA256/HMAC-SHA256 合成向量使用 `bytes(range(32))`、key version `7`、消息长度 `73`；derived key hex `47119abdcf6b97afcdd6416bf5a8b7771b314a630c53c1553aa1789cc02f95c1`；identity hex `9de29ff352c78092c4b7dae5e0e3093fee49815a3473d75c3876e3b316222564`。标准库独立计算与产品 helper 相等。实际身份 fingerprint、明文 token 和其 hash 均不进入本记录。

API deps、mail Worker、calendar Worker、0019 preflight CLI 均调用 `integrations/registry.py::build_oauth_security_services`，一次读取/解码固定 `APP_MASTER_KEY_FILE`，同一 root 构造 `AeadCipher` 与 `OAuthRefreshIdentity`，后者使用 cipher 的 key version。M2 没有可变 root、多 key lookup 或第二份 identity Secret。

| 组合点/固定向量来源 | 文件 SHA-256 |
| --- | --- |
| `backend/src/ai_employee/api/deps.py` | `75e4d5403fb880261ae2cdd90249ffb7e839a6be65337bc138e70cb3c4a717a0` |
| `backend/src/ai_employee/cli/calendar_aad_preflight_0019.py` | `a9cf95852171ba52278757435cbddd1b3dd9acb33cfbda65d1396bba1474aa50` |
| `backend/src/ai_employee/integrations/registry.py` | `2d41ea903c552973ffb403e1b7ee5069a6a4c402dbf46d58c8644a59052aa3cd` |
| `backend/src/ai_employee/workers/sync_calendar.py` | `2b8e174127aa18c7d20723d8d289fe6f4a9941fc01aad660768ba0f3de6c02aa` |
| `backend/src/ai_employee/workers/sync_mail.py` | `701a41e308b3b0ce33b89e1b6a4d44891301d89b39e270cf70efe8cdd86324cb` |
| `backend/tests/unit/application/test_calendar_aad_digests.py` | `04b1f0016773434941ea1462dfa62b5a19c4a4c4a3be30aeb99792d4c831b241` |
| `backend/tests/unit/application/test_calendar_event_aad.py` | `1b3cbc2c104eb33e46a9dda4f20ac70bb8bfc99aed5e289bcf92eb8b8988f9bb` |
| `backend/tests/unit/application/test_oauth_refresh_identity.py` | `c7beb97f57046c65f9de16d5d169f816a491dfcc6fa92cef07a9ed54b6ab9d40` |
| `backend/tests/unit/infrastructure/db/test_database_access.py` | `e5e6c03169c0be35cbeab4f568438eca4d3e49b63ef3089f768248aea9086164` |
| `backend/tests/unit/infrastructure/db/test_database_grants.py` | `416346c90916c11371291cc97f85c824f4e4dff20eabd18909e8fb4936a5833a` |

该合成 HKDF 向量的完整 framing 输入如下，转义均按 Python bytes literal 解释：

```python
root = bytes(range(32))
salt = b"AIEMPLOYEE/oauth/refresh-token-identity/hkdf-salt/v1\x00"
info = b"AIEMPLOYEE/oauth/refresh-token-identity/fingerprint-key/v1\x00\x01\x00\x00\x00\x017"
```

HMAC message 的完整 base64（73 bytes）为：

```text
QUlFTVBMT1lFRS9vYXV0aC9yZWZyZXNoLXRva2VuLWlkZW50aXR5L3YxAAEAAAAZc3ludGhldGljLXJlZnJlc2gtdG9rZW4tQQ==
```

HKDF-Extract 为 HMAC-SHA256(salt, root)，Expand 的唯一块为 HMAC-SHA256(extracted, info + `b"\x01"`)；最终 identity 为 HMAC-SHA256(derived, message)。以上内容仅为公开固定 fixture，运行中 APP root 与实际 keyed identity 不输出。

## 完整自动化发布门禁

在仓库根目录运行 `bash scripts/test-m2-release.sh`，使用唯一固定 Task13 合成 PostgreSQL 锚点、
独立 Redis DB15、Fake Google/Microsoft/模型、合成 Secret 与浏览器；开始前验证并一次 export 精确
`TEST_DATABASE_URL`。普通 suite 使用新 UUID 数据库；lifecycle phase 独立维护 roles-absent 初态。
该门禁未使用专用真实账户。版本为 Python 3.12.13、uv 0.12.14、Node 24.21.0、pnpm 11.21.0、
just 1.58.0、Docker 29.8.0；沿用既有 pnpm launcher，未安装或改写依赖锁。

门禁开始于 `2026-09-15T14:41:22.857572+00:00`，原生耗时 `2725` 秒；
原生与监督器 exit 均为 0。完整 582 行、60676 bytes 日志已读取，SHA-256
`6885dd9f3bf0584aa7b032ecf2bb5bb3e5a1b84408e4e2fcdab5bb1d165b5d1a`。独立验证回执 SHA-256 `0ad7f03350e0edc6dc88591bfff43d989926cc7e0d225c921069863e5ac6d35f`，
确认源码不变、原四个专用验收服务恢复且镜像/容器 ID 未变、写开关关闭。

以下各选择有重叠，数量不相加；`regular` 包含 integration、contract 与 eval。父 pytest 的
`no tests ran` 是已收集节点委派到子进程的正常外层输出，实际子进程结果如下。

| 阶段 | 实际结果 |
| --- | --- |
| 后端单元测试 | 2579 passed |
| 前端单元测试 | 265 passed |
| 完整 CI regular | 2476 passed，2 deselected；被移出的生命周期节点由独立 phase 执行 |
| 完整 CI lifecycle | 480 passed |
| E2E backend 工具合约 | 8 passed |
| 完整 Playwright | 56 passed，0 retry、0 flaky、0 skipped |
| 显式 AAD audit | 1 regular + 21 lifecycle passed |
| 显式故障矩阵 | 167 passed |
| AAD/ACL/迁移/回调 | 307 regular + 90 lifecycle passed |
| digest/identity/coordinator/capability | 154 passed |
| OAuth/sync/preflight | 258 regular + 21 lifecycle passed |
| maintenance/backup/restore/deadline | 264 passed |
| retention/privacy/recovery | 311 regular + 60 lifecycle passed |
| legacy shell selection | 1 regular + 14 lifecycle passed |
| 部署、工具链、CI workflow、生产构建 | 全部 exit 0 |
| 原始敏感输出 | 17 files，0 findings |
| git diff --check | exit 0 |

发布脚本按已批准顺序执行 13 条顶层命令：完整 `just ci`、直接 AAD audit、故障选择、AAD/ACL/迁移/
callback、digest/identity/coordinator/capability、OAuth/sync/preflight、maintenance/backup/restore/deadline、
retention/privacy/recovery、legacy conversion、deployment、tooling、敏感扫描和 diff check。没有跳过参数。
缺省/精确锚点值被接受，其他地址在任何子命令前拒绝的 shell 合约也已通过。

完整浏览器报告从 HTML 内 ZIP 逐个读取全部详细结果，每项只有一次执行，retry=0。两种微软账户类型
通过 `backend/tests/contract/microsoft/account_cases.py` 与 `backend/tests/integration/microsoft/test_account_mode_contract.py`
覆盖 OAuth、分页/增量同步、邮件和日历读写合约；个人/工作或学校账户的 consent、重新授权、delta URL
形式均有固定 fixture。测试中的 API、编辑、submit、审批使用正式边界，Fake 只替换外部适配器。

已审修复还覆盖 inactive 用户的迟到 OAuth/模型/sync/编辑/审批结果、连接能力锁顺序、读取错误规范化、
Microsoft 纯文本正文语义和 enabled 能力的显式重新授权入口；无 M2 之外的新增动作或供应商。

## 独立 Worker SIGKILL 与队列恢复

两轮均杀死真实 Taskiq Worker 进程组，退出码 −9；replacement 在真实期限后使用 XAUTOCLAIM 接管原
pending entry。kill 后独立快照至 replacement 启动前，原数据库期限/状态、消息 ID/owner/交付次数/载荷
保持不变；请求时间和 ToolExecution ID 不变，没有在崩溃后改写时钟事实。每轮两例、10 个原始文件在
阶段结束时逐字节保全；测试临时目录清理后，最终核验使用这些完整归档副本。

| 选择 | 崩溃点 | ToolExecution | 写次数 | 核对次数 | 同消息 delivery/pending |
| --- | --- | --- | --- | --- | --- |
| full_ci_regular | after_provider_return | `a4bf3b65-59b5-4894-9f75-72c4a1cd3ac1` | 1 | 1 | 2/0 |
| full_ci_regular | before_queue_ack | `41327ec4-0525-4db9-b36e-d9f565d26ec8` | 1 | 0 | 2/0 |
| explicit_fault_gate | after_provider_return | `212e72a4-523a-40d7-921b-7c48b8c402ad` | 1 | 1 | 2/0 |
| explicit_fault_gate | before_queue_ack | `f020012c-c319-4ee9-b11a-6dec5c488220` | 1 | 0 | 2/0 |

完整 CI 轮 manifest SHA-256 `37c618ee52910d28bdcb4427be42d01d72fcdeb178ae9024012e22a8cec98902`；显式故障轮
`ae70d2d8a67c75b113d37f820990f00756675add133c4c60ad57c77bfe017e73`。response 后的未知结果通过一次只读核对收敛；结果已提交、
ACK 前崩溃不再核对或重发。事务注入矩阵与这四次真正的进程终止证据分别保存。

## 原始输出与隐私扫描

扫描器 `scripts/verify-m2-sensitive-output.py` SHA-256
`45b014dacdf7caa00757644569e3b862bbd745d2299db655a071d5d2fde9b293`。21 份合成 fixture 的 schema/值静态检查通过；
运行时 address、subject、body、title、description、location、attendee、token、cookie、prompt 十类随机
canary 均实际到达，扫描原始字节及常见编码结果为 0 findings。包含一个真实浏览器流程、337 条 Trace、
10 条 browser JSONL；本文不记录 canary 的值。

Playwright PID 136036 exit 0；web PID 135969 受控 SIGTERM exit −15；backend PID 135874 exit 0，三个
group_exited 均为 true。所有生产者退出后才生成摘要，原始输出随后逐字节复制并由纯扫描函数再次核验，
没有用预先脱敏的日志替代扫描，也未为核验重跑浏览器。

报告原件 `/tmp/ai-employee-m2-sensitive-hll4ybje/report.json`，完整副本位于本轮 gate 的
`sensitive-evidence/`；report SHA-256 `073cc616c14135ab870a6da64267457a38c06d35a1810533ec80be769e2fa481`，合成 input SHA-256
`cec5e05544f6a4ff5a5afd481301d9fd4442702ea03f67f080c1ed411d642504`。完整原始输出清单为：

| 原始输出 | bytes | SHA-256 |
| --- | --- | --- |
| raw/browser-results/.last-run.json | 45 | `91d1c43004802cd49950d78eb11c8fa7d05da8ffffe219a8b13b2f561bc00903` |
| raw/browser.jsonl | 1509 | `5c0aa94e9742e5c012140834d68f4622a07337268a710db693a29c1e20409424` |
| raw/junit.xml | 442 | `e76b5b5e8e0aa61308a568c686f6580ff53c820cdbb9c974414ce2a488c77bab` |
| raw/metrics.txt | 3429 | `28e83b341168a3b873563649b8d64a2d64383265d25868ed94cbafb309a56456` |
| raw/playwright.json | 3444 | `5cabfe50338b5fb35fd3117673ced4325d612e5ffeaa6c9296b49b1d2db7503c` |
| raw/playwright.stderr.log | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| raw/playwright.stdout.log | 204 | `6e84ec77bd25c8e0caea170cc2b5f4759cce1b10e322ec3664249f65ea907a37` |
| raw/producers-stopped.json | 252 | `86e8eeb311c76b155c97d36dc50bf8912d7d3a2eba58312777c158eff32d129d` |
| raw/reached.json | 148 | `9b3d02942cc143cd42d24b8a1f9fe5ea55dfaae1a0d7eb498b22270291c02690` |
| raw/services.stderr.log | 5294 | `83deace8e28adfe81bfbb17a2352be9d19552a6f8be7a59a6177dbc32d77d08b` |
| raw/services.stdout.log | 97 | `6ac20ea53f130f00f43e59024035f5e09c67012ef3ffe9b47ebce934f664d3af` |
| raw/supervisor.stderr.log | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| raw/supervisor.stdout.log | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| raw/traces.jsonl | 375511 | `df93b80fff06d8acbf9f5f0b85e6de960b97f1d60a700c3ca6253e9eced42175` |
| raw/transport-reached.json | 31 | `710288b13f2b061ca321ad33b2a81fd68f59e5152c97259fb2119670389d2cd4` |
| raw/web.stderr.log | 37 | `7274243f0580c6d26eafc42607727a7edd50740f701ca18a16831eeb98386ce6` |
| raw/web.stdout.log | 188 | `ed430c69a721910461756c8d5d17aa0c3ae36653b2820d91fa8acd55cbb9b841` |

## 专用账户真实人工矩阵

验收操作者为本会话用户（专用账户所有者、逐项应用内审批人）；Codex 仅准备、观察和整理证据。12 项均已取得一次有效用户审批，冻结版本/hash 匹配，每项一个 ToolExecution、一次写尝试、最终 `succeeded`；agent 审批/人工结果代确认次数为 0。Microsoft 三封邮件各有一次只读 reconciliation，其余为 0；没有再次发送。

版本必须分开记录：`C0=4da90dfa43ad275087674f12da2b7e2a5bcad25c4ab61eedd9b9738ad0f632bd` 执行首四项两家的 new/create；`C1=ae85effb71d7c55f7eb87f309dff2410dbbe31ae96614fdd9b232d64210039d0` 执行后八项 reply/reply_all/update/restore。最终 12 项聚合与只读复核绑定 C1。C1 全套自动化覆盖全部动作；已成功的 C0 操作保持原版本事实，没有为追求同版本矩阵重放。

| case | provider | 操作 | 源码 | 冻结版本 | payload SHA-256 | 只读核对次数 |
| --- | --- | --- | --- | --- | --- | --- |
| M01 | google | new | C0 | 4 | `7eaf397ff15216422dbe8e595507bf9a74f2be2b583d84ef145c5bfeba4810af` | 0 |
| M02 | microsoft | new | C0 | 4 | `99ef920243d9a8db3e487ce38856d972c079ff5bab1a668b9a9047ffe2369450` | 1 |
| M03 | google | create | C0 | 3 | `e6e94533c40afec65969dab2b0616dce0dab4998455099ad4051f2f15e12f618` | 0 |
| M04 | microsoft | create | C0 | 3 | `ec0e3d4c16d8af5afeefb012e80f553e28a73a72cc879a5895c8beabbcfce653` | 0 |
| M05 | google | reply | C1 | 1 | `47e1b5696146769b7653bde79657a60e63c1e4829a9437a541348399e09888de` | 0 |
| M06 | google | reply_all | C1 | 1 | `43d93ffacc77230a0ca4f5865055379119e03a2c20233dce6c9ebf493d497a97` | 0 |
| M07 | microsoft | reply | C1 | 1 | `42fc6fafce363adaba66ee0f969e1b263767172c5c2f3398d5e4e75cf746f865` | 1 |
| M08 | microsoft | reply_all | C1 | 1 | `e86e2768bc1abc781efe0d04fcb2425122e90a0cdeaed8807a0620519a4af200` | 1 |
| M09 | google | update | C1 | 1 | `9ab462566c225211e1b3f53cf8242513662776274cd38ead2a27dfab3187c416` | 0 |
| M10 | microsoft | update | C1 | 1 | `3489fe2f9fd1bb56381549a232d5cea5126ae80772dcd3c3129f648d8adf8ca8` | 0 |
| M11 | google | restore | C1 | 3 | `f2a42272169e2ec0f29582b23976aa9d3aaf1c1e13b7ff428cd5d678aef7aa23` | 0 |
| M12 | microsoft | restore | C1 | 3 | `a8c078106353e7bca1d56794060825cc3c973bb9bf6c89233615ec4670e63306` | 0 |

| case | TaskRun | ApprovalRequest | ToolExecution |
| --- | --- | --- | --- |
| M01 | `9c6d062f-0997-464e-ae2e-61943eebfddc` | `91997ce4-dd3a-440a-81e7-8fb4a05d4133` | `f1e96c45-0d33-41b1-aa05-b513c7d76668` |
| M02 | `c33d57fa-7ff4-4d62-80a7-10bd16736f0c` | `4e95229f-e957-4711-9fc0-14a79470c27d` | `b44679f9-ff3b-4251-bd43-f20491752d04` |
| M03 | `0f5091d6-94ef-42fe-a3de-345ac5d3abcd` | `d1abdf60-c4f1-4123-9bdf-0d7d8a98ec77` | `43668727-3223-4e33-9a89-5f72471e6ef8` |
| M04 | `d2ca3cc7-5b3f-4f54-b86e-cf461f9533af` | `afe51282-e46a-48ec-baef-aaacd2c2feec` | `ce441f52-8256-4064-9d31-2e2aed9f12a3` |
| M05 | `a9da84ed-f337-4ad8-a9a8-0ec27ec50a1a` | `cedd57f4-9372-4313-a890-aa91d360ff04` | `566aa845-e721-4991-aa7f-d78e6f458665` |
| M06 | `f805c7ca-1751-49f1-aedc-b5f9a70b5ad0` | `a71cc6dd-3a77-4933-b02b-f48d24ad8aa8` | `004ee7a0-5e0e-475e-ba2f-d9416074d9bb` |
| M07 | `eca921c8-1fd9-4e1f-9f9c-cacecd3d69bc` | `7ff3d236-e7fc-4f6a-adaf-f927d08cbb80` | `bfecaf11-2f2a-4800-a460-2a5bc51a7397` |
| M08 | `9ae59b47-423c-458e-8dc8-6b20795ed0e9` | `1e894298-d23a-47b1-8845-deedbc34092d` | `7237e2a9-9452-49ff-96ee-c7ee970419f3` |
| M09 | `46b03d03-cbd1-4f86-89a7-a454b2bda975` | `6edfc978-ce83-43e4-a138-92ab8b2d8343` | `f6253992-db3e-4865-8f18-5d026a053530` |
| M10 | `b4b37775-969d-42df-affc-6f2f09fc148b` | `6be290f3-80dd-4b35-8260-f3656aaadf70` | `d52febb6-9b97-4791-9b9b-0a499699cd13` |
| M11 | `2a3f1ad1-23d9-4000-874d-5d02f92196d7` | `56caa776-00cf-44fd-886b-2f0954184850` | `a36baad7-8230-4bad-8882-165245dd165b` |
| M12 | `138f0dae-fac0-41d9-a41f-f6e74751c68d` | `3381a8dd-a716-4331-b281-65ee5963e393` | `b12eb253-fa31-4302-bc05-2b805f6a2b23` |

完整追加审计时间线（按回执顺序，ID 为普通审计 ID）：

- M01: `approval.requested:2932` → `task.running:2936` → `approval.resolved:2984` → `task.running:2987` → `tool.claimed:2988` → `task.succeeded:2992` → `tool.succeeded:2994`
- M02: `approval.requested:2933` → `task.running:2937` → `approval.resolved:2970` → `task.running:2973` → `tool.claimed:2976` → `tool.reconciling:2983` → `task.succeeded:2995` → `tool.succeeded:2997`
- M03: `approval.requested:2934` → `task.running:2938` → `approval.resolved:2969` → `task.running:2972` → `tool.claimed:2975` → `task.succeeded:2977` → `tool.succeeded:2979`
- M04: `approval.requested:2935` → `task.running:2939` → `approval.resolved:2968` → `task.running:2971` → `tool.claimed:2974` → `task.succeeded:2980` → `tool.succeeded:2982`
- M05: `approval.requested:3546` → `task.running:3552` → `approval.resolved:3588` → `task.running:3603` → `tool.claimed:3607` → `task.succeeded:3610` → `tool.succeeded:3612`
- M06: `approval.requested:3547` → `task.running:3553` → `approval.resolved:3586` → `task.running:3602` → `tool.claimed:3606` → `task.succeeded:3613` → `tool.succeeded:3615`
- M07: `approval.requested:3548` → `task.running:3554` → `approval.resolved:3584` → `task.running:3600` → `tool.claimed:3605` → `tool.reconciling:3619` → `task.succeeded:3629` → `tool.succeeded:3631`
- M08: `approval.requested:3549` → `task.running:3555` → `approval.resolved:3580` → `task.running:3599` → `tool.claimed:3604` → `tool.reconciling:3620` → `task.succeeded:3626` → `tool.succeeded:3628`
- M09: `approval.requested:3550` → `task.running:3556` → `approval.resolved:3561` → `task.running:3597` → `tool.claimed:3601` → `task.succeeded:3616` → `tool.succeeded:3618`
- M10: `approval.requested:3551` → `task.running:3557` → `approval.resolved:3558` → `task.running:3559` → `tool.claimed:3560` → `task.succeeded:3575` → `tool.succeeded:3577`
- M11: `approval.requested:3684` → `task.running:3686` → `approval.resolved:3717` → `task.running:3719` → `tool.claimed:3721` → `task.succeeded:3722` → `tool.succeeded:3724`
- M12: `approval.requested:3685` → `task.running:3687` → `approval.resolved:3716` → `task.running:3718` → `tool.claimed:3720` → `task.succeeded:3725` → `tool.succeeded:3727`

最终聚合回执 `.superpowers/local-m2-runtime/manual-write-closure-afd8107d69be4bcba4c01981d6dc429c/manual-matrix.json`，SHA-256 `f60a49363579a8621fda572770e3e879ac772003b8464d0a9797ff024a6f270a`；验证时间 `2026-09-15T12:27:05.424609+00:00`。

### 账户、能力、授权与收尾

两个身份均为已授权的专用测试账户；Microsoft 使用 personal 账户类型。用户已完成环境/账户授权、
逐项应用内审批及需要时的显式 OAuth 恢复。原始 canonical identity 只在私有允许列表中；下表是本地
SHA-256，不能拿它替代实际写入时的精确身份匹配。

| 供应商/专用账户类型 | canonical identity 本地 SHA-256 | 实际 scope 数 | 核验 |
| --- | --- | --- | --- |
| Google | `7e24d5530931aa45738d2f1ad753b30cf4f02a9c99216c8caf99d6a3f9f0443a` | 6 | 批准 registry 内；四能力 enabled、verified |
| Microsoft personal | `62ea273b9db6701f71f172d31f9c92792f34d6f7fd411ead3b38690b74dced76` | 8 | 批准 registry 内；四能力 enabled、verified |

允许列表精确含 2 项。执行人工矩阵时全局/Google/Microsoft 三写开关均显式启用，且每条命令重新验证
连接能力、精确账户、冻结版本/规范载荷 hash、AEAD 和一次有效用户批准；未启用供应商草稿同步或额外
scope。账户模式回执 SHA-256 `d9c959b9e76d0ccd0bfa1c47539daf394913afbe007a8888d202557fde12b53d`；最终 scope/执行/
runtime 只读回执 SHA-256 `b12e64e466ef0641e87c977a7505f85fc806fd0253bec9c0495cc7ad5fe295d6`。

原 automatic refresh 的 Microsoft started 审计 3158 由合法 replacement 3475 闭合，Google 3159 由
3473 闭合。严格 parser 证明未闭合为 0、generation 按规则更新且未增加旧 automatic start；没有重放
旧 refresh。这里只公开事件 ID 和闭合结论，credential 时间、fingerprint、原始 scope 和连接身份不公开。
Microsoft 旧 HTML 缓存在正式只读同步后恢复为批准的纯文本；旧不可变快照保留，后续 update 使用新
提案和新冻结 hash，成功的首四项未重放。

12 项成功聚合后 pending approval=0、unfinished write=0、ToolExecution=12、write attempt=12。
按原配置对象身份及 SHA 做三开关 true→false，重建并核对 API/Worker/Scheduler；最终只读 probe 的
provider_calls=0。关闭后的 `.env` SHA-256
`3663b2ac64c451e7cca9ce140ac5dcd396f8c0e9dc67af48d6d2fd16153f77b4`，原四个验收服务在完整门禁和原生
演练结束后仍是相同容器/镜像且健康。专用账户内的测试邮件和日程保留，未执行 M2 范围外的删除/取消；
应用端测试产物保持私有。Microsoft 工作/学校账户完成自动化契约验证，未进行第二个真实账户验收。

## 当前候选镜像与原生部署

| 镜像 | 本轮标签 | 不可变 content ID |
| --- | --- | --- |
| 后端 | localhost/ai-employee-backend:task30-final-ae85-20260915-v1 | `sha256:69a08734c1720221e84e6bff7481825d1851a67bdb83994b8a56f301a217f04e` |
| 前端 | localhost/ai-employee-frontend:task30-final-ae85-20260915-v1 | `sha256:1fa788de8538819d36238c263944c07d8479b505a4b0af73b75042b78430dbe1` |

两个镜像均从仓库根上下文和实际 Dockerfile 新建，绑定 C1。后端逐文件核对 221 个 Python runtime、
21 个受管运维入口（去重后的 runtime map 共 230 项）、22 个 migration Python、262 个 test Python 和
4 份 metadata；默认 UID/GID 65532 在禁网、无挂载、无 Secret 容器中执行 uv import 成功，legacy main
未执行。后端 inventory SHA-256 `2d57dd8b43da7ad324730d5d150d7b0d845c4025995174b59f68244bf12b21a6`。

前端 Caddy 2.10.2，全部 3 个静态文件与本轮 CI dist 逐字节一致，生产 Caddyfile 精确一致；inventory
SHA-256 `db5bdf8e81291b853f6020e1f04c049f0e2496bdb536b45507c25eb47d709cb9`。部署使用同一前端镜像，测试 Caddyfile 只加 `tls internal`，
生产路由、静态目录、API/SSE 代理不变。构建日志的 gzip 估计大小差异不影响已核对的实际静态字节。

| 静态文件 | bytes | SHA-256 |
| --- | --- | --- |
| assets/index-CBZPt86H.js | 378937 | `57e05e330fd1bc0f79ba70f5f310a9c6e93c28db3f5ce9b54d92970d3f30226a` |
| assets/index-lDGIe0Zl.css | 10428 | `08833658122d765b8fd0eb144cbec629cd638489335ea8fb315beab59c027dec` |
| index.html | 398 | `c8f237beee18753d3494c65a3e08ab1a923de12337d4e015540949ec4e249322` |

合成项目 `aiemployee-task30-final-host-generic-fee00b9e` 使用新数据库 `ai_employee_task30_final_host_generic`、独立卷和 internal 网络，空模型/供应商
入口、Fake 模式、三写开关 false，完全不读真实 `.env`。先执行真实 role-bootstrap、普通 migration 至
`20260809_0019`，再植入一个 inactive 合成管理员；按 Redis→API→Worker→Scheduler→Caddy 串行启动。
本地 rootful Docker 的 fixture 使用宿主 UID/GID `1000:967` 读取
所有者独占的 0600 Secret，userns=host，实际 UID/GID map 已核对；没有注入 UV/XDG 环境或覆盖镜像内 venv。

首次 deploy 原生 exit 1：Caddy 443 正常监听、容器 IP 的严格 HTTPS readiness=200，但 Docker 29 的
internal-only bridge 下 `HostConfig.PortBindings` 仅是请求配置，实际 `NetworkSettings.Ports[443/tcp]=null`，
宿主回环连接 errno 111。失败日志和全部前 30 个成功 transcript 原样保留。接续仅新建绑定
`127.0.0.1:48629`、固定容器 ID/IP/镜像的临时 TCP 转发；不终止 TLS、不记录载荷、不改网络或
产品源码。派生入口只改变自校验绑定与证据目录，原 TLS/静态/API/浏览器断言完全保留。

接续与恢复后两次都以 Caddy 本次公开根证书执行 TLS 1.3 证书链和 hostname 严格校验；全部静态摘要、
SPA `/actions`、API readiness 正常，`/metrics`=404。浏览器在只连回环、禁止 foreign request 的条件下
验证 `/actions`→`/login` 与登录控件，page errors=0。浏览器仅为渲染绕过私有 CA，证书安全结论来自
独立严格 TLS 探针，未安装宿主根证书。原 deploy 仍记录失败，成功结论来自单独的 continuation。

原生目录为 `.superpowers/local-m2-runtime/final-native-ae85-20260915-v1/`。下表所有日志均为真实运行
输出，已检查完整字节与原生退出码；时间均为 UTC。

| 阶段 | 开始 | 结束 | exit | 完整日志 SHA-256 |
| --- | --- | --- | --- | --- |
| build-backend | 2026-09-15T15:28:16.250780+00:00 | 2026-09-15T15:28:25.253090+00:00 | 0 | `a1e3e082c8faf8d731ce19bce387c91514a424b1e38d337a20e807be79e27562` |
| verify-backend | 2026-09-15T15:29:02.867173+00:00 | 2026-09-15T15:29:06.868362+00:00 | 0 | `2abafbe83b06550695fecdd3741802504fe0f5b56132086738f5ecd05cff13b6` |
| build-frontend | 2026-09-15T15:29:34.419208+00:00 | 2026-09-15T15:29:51.422664+00:00 | 0 | `c93b410e6571e7ba3c820a6d9c7ca80c4ef345d2d56184c951bdcf37ef7daae0` |
| verify-frontend | 2026-09-15T15:30:26.432086+00:00 | 2026-09-15T15:30:27.432729+00:00 | 0 | `483758a5304da70ba0ec4b08e916ab1ff8a77de4a65bf8847f4449622a56b1d7` |
| prepare | 2026-09-15T15:30:42.048896+00:00 | 2026-09-15T15:30:58.052524+00:00 | 0 | `2773c67553258eabe3a56d2cf96c01e64e6c13b1c0835b73a4cc87a49e38dbeb` |
| deploy | 2026-09-15T15:31:59.342842+00:00 | 2026-09-15T15:32:34.349238+00:00 | 1 | `e89f35dd64557b4d3aa8895dabcaa2caa940ab35f87f1724d1173debcfb21e7d` |
| stop | 2026-09-15T15:40:10.496517+00:00 | 2026-09-15T15:40:31.502779+00:00 | 0 | `1f42be6a220ac88513abb8b8c06e5708e64a48f79213761e6762674676217693` |
| backup | 2026-09-15T15:40:40.655103+00:00 | 2026-09-15T15:40:45.656921+00:00 | 0 | `05f482e667fc57b9e8f98ff62d86a5d1bf4e5ae8b38a9ada95cb0c6d54952d4b` |
| change | 2026-09-15T15:40:45.694441+00:00 | 2026-09-15T15:40:48.695390+00:00 | 0 | `8355849a5f3ec505ee900cdafed23cc8ca726cddf0bcd05cbbd9aac34bcaa61f` |
| restore | 2026-09-15T15:40:48.728083+00:00 | 2026-09-15T15:40:58.730003+00:00 | 0 | `da4b462758c6f2ae686daf2e4b8b0f8998dc962285a7e2544a81419b27fe28e3` |
| after-restore | 2026-09-15T15:40:58.769226+00:00 | 2026-09-15T15:41:01.770413+00:00 | 0 | `9b422e5da30d67edc4433e9d414c409a845b9c7bbea28fc82846d7f5fa5ede7a` |
| already | 2026-09-15T15:41:01.807711+00:00 | 2026-09-15T15:41:09.809271+00:00 | 0 | `31ac223821e170b807e36f974a9d24bb1522e91940e449a95c2121615f22e75c` |
| after-already | 2026-09-15T15:41:09.846260+00:00 | 2026-09-15T15:41:12.847234+00:00 | 0 | `4dc1b94f8c62c52aec5fcd57d25b7e256de59af1405314714adaef67264bbff5` |
| restart | 2026-09-15T15:41:12.883866+00:00 | 2026-09-15T15:41:37.888852+00:00 | 0 | `8fac1c18676a94ce2f137a6281dca4d6395a050dac70d9e9e7e2907c088cfcca` |

接续 `continue-deploy-v1.py`：`2026-09-15T15:39:47.739973+00:00` 至 `2026-09-15T15:39:50.503797+00:00`，exit 0；
result SHA-256 `89a922825e277b26fc9dc1aae9d86f4e3a97bdc945e457c8ddd456c9420086b3`，diagnosis SHA-256
`f949ff98057d4460eef989cd4c07c7de6025a9f213b5b2b3791c143bab00a0e6`。部署入口证明 SHA-256
`055c7dd77d895f5c0db05549dd1411dff6daefcca05ad5ffe4b24f561a0b5e6d`；恢复后入口证明 SHA-256
`39dce6b3dbbf689004009f81686c00c77516c9de9e23c6616268b61998a10d55`。四个最初冻结的原生助手未修改。

## 当前镜像的加密备份、恢复与零重放

在合成 API/Worker/Scheduler/Caddy 停止后实际执行 `just backup`，再经持有 typed idle guard 的 fixture
修改唯一合成行使当前 fingerprint 不同，随后两次执行正式 `just restore`。两次均按真实提示逐次回答
recipe 确认与精确文件路径确认，未使用跳过确认选项。第一次输出 `restore_completed`，第二次输出
`restore_already_applied`。普通恢复不要求 0019 rollout artifact，没有借用 sealed 路径。

三件套 schema=`ai_employee.postgres_backup_manifest.v1`，created_at=`2026-09-15T15:40:44.412729Z`，PostgreSQL
`17.5`，revision=`20260809_0019`，build metadata
`release_version=0.1.0`；image 为上表当前后端 content ID，fingerprint
`d24e14693dbabc3afcfaf55288db30025ae0da4dc66c22234b05069dd68ac13d`。以下三件套均为 0600，checksum 同时绑定 dump 与 manifest，
manifest 反向绑定 checksum filename；完整受管 executable map 与镜像及源码相同。

| 三件套文件 | bytes | SHA-256 |
| --- | --- | --- |
| ai_employee-20260915T154044.137783Z.dump.enc | 124752 | `1edce5a58d714ca33f6aaee2cd1ec946aaf99fb616ab0dc2777fbd68e05120af` |
| ai_employee-20260915T154044.137783Z.dump.enc.manifest.json | 3205 | `9a67b300b1fc0e833dfa24196ac173fb0d7755bb21696a2022eb30825e85d7cb` |
| ai_employee-20260915T154044.137783Z.dump.enc.sha256 | 236 | `662e1453163d9037089464cc19de6d22bd19ef3e71d3f3ad3a7382553e14c37f` |

| 镜像内受管入口 | SHA-256 |
| --- | --- |
| /app/backend/src/ai_employee/cli/calendar_aad_audit_0019.py | `78a36aa49f1482ac4c73a8df570ed2f8330fc5755f34eb02634600b83c3af15f` |
| /app/backend/src/ai_employee/cli/calendar_aad_backup_0019.py | `fa115a3e333daf71ab588f3c67267c62788b6090b00b28e195888bcd1c4d14ad` |
| /app/backend/src/ai_employee/cli/calendar_aad_verify_restored_0018.py | `7adaea701b165e32320f3e93fadfb5ab43dd0f8ffbc967960fa04b737e28e12d` |
| /app/backend/src/ai_employee/cli/database_maintenance.py | `14cefc2982a2734e041d90455b5e932394a9acdc5c120fa973e3977598f2ae7d` |
| /app/backend/src/ai_employee/cli/postgres_backup.py | `db56a21cdeca13d80110b69177f9b80ec6bdf079e6d36f711f85b3d2392e9962` |
| /app/backend/src/ai_employee/cli/postgres_restore.py | `fdeb3590e0763ca1a2fdefec69ae621ac0928753befb91bf0033c884fa819878` |
| /app/backend/src/ai_employee/cli/verify_restored_backup.py | `d79ed02bb760998c3f330b9531b8cb4a5b13966a68d8c31f06479ab00d86b007` |
| /app/backend/src/ai_employee/infrastructure/db/database_access.py | `d51e4453242ebffb51adc8e70bf37f81fbed8d0cb4324f0d250ecfac1c5591d9` |
| /app/backend/src/ai_employee/infrastructure/db/database_grants.py | `fce0248b6a24701a4b92e9aad0c0f42c66e469e48ab728e2652a6f4ea6bd6ba4` |
| /app/backend/src/ai_employee/infrastructure/db/database_maintenance.py | `e0d9d364f07c28655b3ee275d51c2451c12c5702ea4f58634e68b133f48061a2` |
| /app/backend/src/ai_employee/infrastructure/db/postgres_backup_manifest.py | `d36b2f6ca12f3e2fb779af5664c0ba1812a575ec3458fadd45e87afda1979edb` |
| /app/backend/src/ai_employee/infrastructure/db/postgres_restore_stream.py | `2d0a908d49d3b6cb121723bf3854d7cb333114d37eae657cdcf82fe1c8973807` |
| /app/scripts/audit-calendar-aad-0019.sh | `04cd99cc9ed07a0dcdeabb6396be2c2c574b43a1c33dcd0be3bfdfb9cb34e612` |
| /app/scripts/backup-postgres.sh | `ac18777f0c053d39eae9a1de542f32ebed8d623400cf1e5f1a95c8b311da0690` |
| /app/scripts/convert-legacy-backup.sh | `9487f82dc3b46f777a5734acebdf6f7cdaabda6450ff1fc39301a2976d702453` |
| /app/scripts/init-db-roles.sh | `314a98614975927cc5eb1c05cb4bc7f21a31e09ebb150d41863a5e985859fe9e` |
| /app/scripts/legacy-backup-operations.py | `9c0b10b859ee46c6b7a61ccabd344792f3025cca950e17ade66fff56f3c7e06e` |
| /app/scripts/restore-calendar-aad-0018.sh | `a2d3cf0da5875fb1abf31f6d5330bcbbc30be39b5842adf86c9bbae6662bf1a3` |
| /app/scripts/restore-postgres.sh | `78399a7f0b6f314658d92a5d0c06e9f285873adc64aa9bd8dbc1e17f3e0ecf00` |
| /app/scripts/run-postgres-operations.py | `f237910d394d3a51337fd70473435d3bc956689744267066e1b92c23fc17fa87` |
| /app/scripts/scavenge-legacy-backups.sh | `f02c471403f73a8da572eddaa21b8f52a55978400fea70b46b4410244315b3ed` |

恢复后、新会话两次只读核对得到逐字节相同的 2586-byte facts，SHA-256
`6c88ad0df9439836a1c8f6f654be244cdbc43ef7516ade4e1a74b52d674a36fd`。数据库 OID=16384，call_ordinal=1、reopen_ordinal=1，
精确 psql PID=543、backend-start=`2026-09-15T15:40:53.440575+00:00` 已退出；按相同 PID/start 查询
和 restore application name 查询均为 0。两轮均验证完整 fingerprint、baseline object grants、safe roles、
双向零 membership 和 completed-idle；审计仅 1 条，普通 BigInteger ID=1，row SHA-256
`ed4a7b8837aab31f3f1fd8f0e94432b64cc0edd0f064cdbab716d2d981c80e2a`。此审计是可清理记录，数据库 completed pair 才是 authority。

实际 maintenance gate 为 NULL；restore call 是 689 个 ASCII bytes、
21 个 `|`，完整内容外 catalog 事实为：

```text
restore-call:v4|d2e8d807-22f7-4149-a1d2-8cdc7ab32a26|generic|9a585e9c14505d1ce9a5650feee6ae5c4b7781f66fa095770196c475e556e7d5|9a67b300b1fc0e833dfa24196ac173fb0d7755bb21696a2022eb30825e85d7cb|9a67b300b1fc0e833dfa24196ac173fb0d7755bb21696a2022eb30825e85d7cb|-|1|1|completed|2026-09-15T15:40:53.166400Z|2026-09-15T15:40:53.371555Z|543|2026-09-15T15:40:53.440575Z|20260809_0019|bec3c4e58342b38a672f4f1b8b117c0a6a46854f8c614a2856dd586ae8082be7|20260809_0019|d24e14693dbabc3afcfaf55288db30025ae0da4dc66c22234b05069dd68ac13d|20260809_0019|d24e14693dbabc3afcfaf55288db30025ae0da4dc66c22234b05069dd68ac13d|2026-09-15T15:40:54.741118Z|e8f77a4c974861133bade158c9632b9ae9489e021cd442ee36b28f7610a1968a
restore_completion:v1:e8f77a4c974861133bade158c9632b9ae9489e021cd442ee36b28f7610a1968a
```

owner-session 的 `SET ROLE ai_employee_app` + `BEGIN READ ONLY` 验证已发生，PostgreSQL 原始日志明确
以 SQLSTATE 25006 拒绝 `UPDATE ... WHERE false`。guarded stream、grants、completed CAS、completion GUC、
gate reset 与最小 CONNECT reopen 通过同一受管实现；生产脚本挂载 backup/artifact 为只读，state 独立
可写。原生 state 投影与以上 catalog 逐字段相等，第二次 already-applied 没有修改 catalog、fingerprint、
ACL、object grants、执行序号或审计行。

| 独立 state 证据 | schema | bytes | SHA-256 |
| --- | --- | --- | --- |
| restore_already_applied | ai_employee.postgres_restore_already_applied.v1 | 666 | `dd1f268b8df6eb3d4df7319fac6919cb3e649fed7cb5fcb7f19bf5812fec5837` |
| restore_completed | ai_employee.postgres_restore_state.v4 | 1194 | `285bed96602cb7f319b49a892444cc0c56b40e82f02829e21bc1cfd7aec3acac` |

### 合成目标 bytes 与锁向量

身份输入为 `b"ai_employee.restore_target.v1\0" + system_identifier_ascii + b"\0" + database_name_utf8`，末尾没有 NUL；数据库名严格为 1–63 UTF-8 bytes。signed int64 的 −1 先规范化为 uint64 最大值，拒绝 bool/越界。schema lifecycle 固定为 `(20260806, 143)`；所有 restore/migration/reset 路径遵循 management→target→schema 的锁序。

| case | system identifier ASCII | database UTF-8 | 完整输入 hex | SHA-256 | lifecycle key | target key |
| --- | --- | --- | --- | --- | --- | --- |
| low | 72623859790382856 | ai_employee_restore_test | 61695f656d706c6f7965652e726573746f72655f7461726765742e76310037323632333835393739303338323835360061695f656d706c6f7965655f726573746f72655f74657374 | 71f328c1e8eb5d9cd2f8e1f815afac4b723f94a274900853c895fd9d72e60e49 | -9116408252019116299 | 5971001870339114140 |
| high | 18446744073709551615 | ai_employee_restore_test | 61695f656d706c6f7965652e726573746f72655f7461726765742e76310031383434363734343037333730393535313631350061695f656d706c6f7965655f726573746f72655f74657374 | ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b | -7619787752010613680 | -7493882204033559214 |

低位 digest 与两类 key、高位 digest 是独立测试常量；高位 key 另由同一已测函数与精确输入导出并记录。

## 旧格式输入的原生隔离转换

把上述合成加密 dump 逐字节复制为仅带旧 checksum、无 manifest 的专用输入，原三件套保持不变；
随后执行正式 `just restore-legacy-to-isolated <synthetic-file> converted-current`。这是当前 0019 合成
数据的旧格式入口验证，不伪称读取了生产历史备份。真实命令开始 `2026-09-15T15:41:59.628979+00:00`，结束
`2026-09-15T15:42:10.980577+00:00`，exit 0，完整 command.log SHA-256 `f88d77eba993c995d9eb88076607776b55101673869e11141700d7d701612e48`。

conversion attempt=`0116a71f-082c-402f-b538-f58da4ee7cb0`；创建资源前 fsync 的 durable registry 已保全，镜像是当前
后端 content ID。观察到独立 UUID project/internal network/volume、精确跨资源 labels、0600 临时 Secret、
PostgreSQL UID 70 和零 published port。真实日志确认读取 revision=0019 并拒绝 app read-only DML；
原生日志 SHA-256 `d9ba850a86a17700c422b2a7b7f01f2107709ddeb8de3f490e0fdba492313fe5`。转换后通过普通 locked backup 发布正常三件套，
manifest 的 21 项 executable map 与当前镜像完全相等，没有编造 manifest。

新三件套 fingerprint=`d24e14693dbabc3afcfaf55288db30025ae0da4dc66c22234b05069dd68ac13d`，image/revision 与输入匹配。converter
精确清理自己的容器、网络、卷、registry 和临时 Secret；既有资源集合均保留，未访问工作区/生产数据库。
完成回执 SHA-256 `b0337d1b60d66554986f36a5735d9d7cc5068d30febd07732900963a3ed812e4`。异常中断、3600 秒 grace 与 live/uncertain
资源保护的 scavenger 证据来自当前自动化矩阵；本次成功原生转换没有伪造一次 SIGKILL 清理。

| 转换后三件套 | bytes | SHA-256 |
| --- | --- | --- |
| converted-current.dump.enc | 124704 | `4336f4d1365292b02267fd765973c2903ba5f8f9b88d30e62c6fa87e7588af4f` |
| converted-current.dump.enc.sha256 | 200 | `f12b1ff8e1c001f5e7a9fc3b15d016faf77cbf095765ad6bf2408242dd728fb9` |
| converted-current.dump.enc.manifest.json | 3169 | `e036e0c47ba692f85cd83cb0645b975984359c5a9d31bd063e38149b3af26f18` |

所有原生验收结束后，仅停止本次六个合成服务和临时 TCP relay，保留容器、卷、备份与原始记录供审计。
原验收环境的四个服务仍正常、三写开关仍关闭。最终独立回执 SHA-256
`3ec65e4952527bcf3cbcf8b8b0c9f48dda47adbc86831014931e866a9eb23ef1`，验证时间 `2026-09-15T15:45:11.546432+00:00`；
它还核对 HEAD/index、722 文件 C1 清单和关闭后的 `.env` 都未变。

## 数据库 ACL 与逐版本对象权限

Canonical catalog SQL：

```sql
SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid
```

下列是排序后的完整 multiset，tuple 顺序为 `(grantee, grantor, privilege_type, is_grantable)`。`O=owner`、`P=0/PUBLIC`、`A=app`、`R=retention`；包括 PostgreSQL 17 owner 在内，database ACL 的 grantable 均为 false。

`active`：

```text
(O, O, CONNECT, false)
(O, O, CREATE, false)
(O, O, TEMPORARY, false)
```

`baseline`：

```text
(O, O, CONNECT, false)
(O, O, CREATE, false)
(O, O, TEMPORARY, false)
(A, O, CONNECT, false)
(R, O, CONNECT, false)
```

`fresh_default`：

```text
(P, O, CONNECT, false)
(P, O, TEMPORARY, false)
(O, O, CONNECT, false)
(O, O, CREATE, false)
(O, O, TEMPORARY, false)
```

`pre_protocol_legacy`：

```text
(P, O, CONNECT, false)
(P, O, TEMPORARY, false)
(O, O, CONNECT, false)
(O, O, CREATE, false)
(O, O, TEMPORARY, false)
(A, O, CONNECT, false)
(R, O, CONNECT, false)
```

两 runtime roles 均 `can_login=true`、`inherits=true`、`superuser/create_db/create_role/replicate/bypass_rls=false`、`connection_limit=-1`、`valid_until/config=null`，membership 两个方向均为 0。fresh candidate 只接受两角色同时缺失或同时 safe；legacy candidate 要求两角色同时 safe。只有 role-bootstrap 可在同一 management→target→schema 锁和 owner 事务内收敛候选；直接 migration 对候选零写拒绝。Compose 顺序为 bootstrap→migration→服务，没有 post-repair。

对象权限 inventory 由独立 fixture 与产品 closed registry 逐项比较，未把数据库 CONNECT ACL 当作表权限。下表列出全部 22 个 revision 的 destination baseline/active 摘要，以及每次迁移实际允许的 runtime grant/revoke 数；空改动明确为 0。SHA-256 按私有 protocol JSON 中完整排序 tuple 数组的规范序列化计算。

| destination revision | baseline | active | +runtime | −runtime | baseline SHA-256 | active SHA-256 |
| --- | --- | --- | --- | --- | --- | --- |
| `base` | 5 | 5 | 0 | 0 | `737eacfa9c15da43be228249261aca80a2ef51ee0f21097fbfa45baa9b8c8f44` | `737eacfa9c15da43be228249261aca80a2ef51ee0f21097fbfa45baa9b8c8f44` |
| `20260730_0001` | 44 | 45 | 15 | 0 | `4c313de0281de565a0432d847a40e608269a3ac714073b6b037db2c39d9ee8e8` | `cbd05df5364536a69f554af0f58302c39f85dcd21fc8839f4e8a59fb3760619d` |
| `20260730_0002` | 132 | 133 | 37 | 0 | `ff065a1e6564ebe5dd0a4e85d9042c988062318bbfbce337f7b141fdd7e99924` | `273e094180c1d7b8f60f42c54eba9f04a0f92166b1a4885e721064c9f130a363` |
| `20260803_0003` | 132 | 133 | 0 | 0 | `ff065a1e6564ebe5dd0a4e85d9042c988062318bbfbce337f7b141fdd7e99924` | `273e094180c1d7b8f60f42c54eba9f04a0f92166b1a4885e721064c9f130a363` |
| `20260803_0004` | 185 | 186 | 18 | 0 | `1724e21f12493ab3fb706325480de9b6f2b8ea76f97f6d96e390a3a05e221b08` | `059957c5be9f8abade2dadd5f42c227ca305d21289d4e81811204d4128b012cc` |
| `20260803_0005` | 185 | 186 | 0 | 0 | `1724e21f12493ab3fb706325480de9b6f2b8ea76f97f6d96e390a3a05e221b08` | `059957c5be9f8abade2dadd5f42c227ca305d21289d4e81811204d4128b012cc` |
| `20260803_0006` | 185 | 186 | 0 | 0 | `1724e21f12493ab3fb706325480de9b6f2b8ea76f97f6d96e390a3a05e221b08` | `059957c5be9f8abade2dadd5f42c227ca305d21289d4e81811204d4128b012cc` |
| `20260804_0007_google_sources` | 298 | 299 | 49 | 0 | `814e8a15667cae6ff44a01e7ef7191b637ad78b2b5b0633d53f8cd4f949a1c10` | `a67ed28c6c7376fad1be327cec740d1edca1ec57f1632f2e01b531ef5864d2b9` |
| `20260804_0007` | 298 | 299 | 0 | 0 | `814e8a15667cae6ff44a01e7ef7191b637ad78b2b5b0633d53f8cd4f949a1c10` | `a67ed28c6c7376fad1be327cec740d1edca1ec57f1632f2e01b531ef5864d2b9` |
| `20260804_0008` | 298 | 299 | 0 | 0 | `814e8a15667cae6ff44a01e7ef7191b637ad78b2b5b0633d53f8cd4f949a1c10` | `a67ed28c6c7376fad1be327cec740d1edca1ec57f1632f2e01b531ef5864d2b9` |
| `20260730_0004` | 371 | 372 | 33 | 0 | `596e2267aa73ff02ff145a223c45a7aa6fce75d8822919d6b3aeeb5a8e4cedfa` | `66a47950870f4bcc44f20f19a55a9e3845824b8dc90a6022759fa3e522fe2054` |
| `20260804_0009` | 371 | 372 | 0 | 0 | `596e2267aa73ff02ff145a223c45a7aa6fce75d8822919d6b3aeeb5a8e4cedfa` | `66a47950870f4bcc44f20f19a55a9e3845824b8dc90a6022759fa3e522fe2054` |
| `20260804_0010` | 374 | 375 | 3 | 0 | `b0a7c6873571f937d80e6440bd03fd304b4b30a2c31c06e323dd59d67d70b138` | `f177e617bc57b6ba168b39406c7724ae0bf2239a6f2dca8ce781d38d558ea3b0` |
| `20260806_0011` | 398 | 399 | 8 | 0 | `f3d2872f1c16af3e4c66c94e362bbeef52151895fcd2e6eebb2500ca28db38a9` | `4104c328d775e29627da1ce9adb25a0c445b717e9c6c741cfb7b4e9881524b49` |
| `20260806_0012` | 446 | 447 | 16 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260806_0013` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260807_0014` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260808_0015` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260808_0016` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260808_0017` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260809_0018` | 446 | 447 | 0 | 0 | `eefc42aba16253276cdc9a0b9a367902b9001aba77bee1d605ce04d207c81c92` | `c176e7fbc14fc6cd58301b3d4f3709966530909b20beaf131b0409761ef62663` |
| `20260809_0019` | 504 | 505 | 59 | 1 | `97f554dde682a9f37c2cb17236dc65a07c29311f4dc08da40cda5c10bea701ce` | `6f71a505312e31a981abca2233441f38ffa933f80466409ebb071b413db17ada` |

下列完整增量表示可重建每份 destination inventory，避免把重复的全量数组抄写 22 次。以空 multiset 开始，依次应用 `B−`、`B+` 得到 baseline；除 `base` 外，active 在 baseline 上恰好增加 `(table, public, alembic_version, null, ai_employee_app, synthetic_owner, SELECT, false)` 一项。`R+`/`R−` 单独列出迁移 runtime delta；DDL 自动形成的 owner tuple 只进入 `B` inventory，不伪称额外 GRANT。每行按相同 object/grantee/grantor/grantable 分组，逗号分隔的 privilege 展开为独立 tuple；`—` 表示 null column。

### Inventory delta base

`B−`：0 tuples。

`B+`：5 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| schema | public | public | — | PUBLIC | pg_database_owner | USAGE | false |
| schema | public | public | — | ai_employee_app | pg_database_owner | USAGE | false |
| schema | public | public | — | ai_employee_retention | pg_database_owner | USAGE | false |
| schema | public | public | — | pg_database_owner | pg_database_owner | CREATE,USAGE | false |

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260730_0001

`B−`：0 tuples。

`B+`：39 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | users | display_name | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | email | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | is_active | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | password_hash | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | alembic_version | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | user_sessions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | user_sessions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | user_sessions | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | users | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | users | — | ai_employee_retention | synthetic_owner | SELECT | false |
| table | public | users | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：15 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | users | display_name | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | email | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | is_active | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | password_hash | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | user_sessions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | user_sessions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | users | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | users | — | ai_employee_retention | synthetic_owner | SELECT | false |

### Inventory delta 20260730_0002

`B−`：0 tuples。

`B+`：88 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | audit_events_id_seq | — | ai_employee_app | synthetic_owner | USAGE | false |
| sequence | public | audit_events_id_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |
| sequence | public | audit_events_id_seq | — | synthetic_owner | synthetic_owner | SELECT,UPDATE,USAGE | false |
| table | public | approval_requests | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | approval_requests | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | approval_requests | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | audit_events | — | ai_employee_app | synthetic_owner | INSERT,SELECT | false |
| table | public | audit_events | — | ai_employee_retention | synthetic_owner | DELETE,INSERT,SELECT | false |
| table | public | audit_events | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | outbox_events | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | outbox_events | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | outbox_events | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | task_runs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | task_runs | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | task_runs | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | task_steps | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | task_steps | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | task_steps | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | tool_executions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | tool_executions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | tool_executions | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：37 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | audit_events_id_seq | — | ai_employee_app | synthetic_owner | USAGE | false |
| sequence | public | audit_events_id_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |
| table | public | approval_requests | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | approval_requests | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | audit_events | — | ai_employee_app | synthetic_owner | INSERT,SELECT | false |
| table | public | audit_events | — | ai_employee_retention | synthetic_owner | DELETE,INSERT,SELECT | false |
| table | public | outbox_events | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | outbox_events | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | task_runs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | task_runs | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | task_steps | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | task_steps | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | tool_executions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | tool_executions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |

### Inventory delta 20260803_0003

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260803_0004

`B−`：0 tuples。

`B+`：53 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_app | synthetic_owner | USAGE | false |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |
| sequence | public | checkpoint_migrations_v_seq | — | synthetic_owner | synthetic_owner | SELECT,UPDATE,USAGE | false |
| table | public | checkpoint_blobs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoint_blobs | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | checkpoint_migrations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoint_migrations | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | checkpoint_writes | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoint_writes | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | checkpoints | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoints | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：18 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_app | synthetic_owner | USAGE | false |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |
| table | public | checkpoint_blobs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoint_migrations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoint_writes | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | checkpoints | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |

### Inventory delta 20260803_0005

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260803_0006

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260804_0007_google_sources

`B−`：0 tuples。

`B+`：113 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | sync_cursors | cursor | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_attempt_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_success_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | calendar_events | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | calendar_events | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | calendar_events | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | email_analyses | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_analyses | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_analyses | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | email_messages | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_messages | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_messages | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | email_threads | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_threads | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_threads | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | encrypted_credentials | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | encrypted_credentials | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | encrypted_credentials | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | oauth_attempts | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | oauth_attempts | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | oauth_connections | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | oauth_connections | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | oauth_connections | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | sync_cursors | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | sync_cursors | — | ai_employee_retention | synthetic_owner | SELECT | false |
| table | public | sync_cursors | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：49 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | sync_cursors | cursor | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_attempt_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | sync_cursors | last_success_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | calendar_events | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | calendar_events | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_analyses | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_analyses | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_messages | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_messages | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | email_threads | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | email_threads | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | encrypted_credentials | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | encrypted_credentials | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | oauth_attempts | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | oauth_connections | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | oauth_connections | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | sync_cursors | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | sync_cursors | — | ai_employee_retention | synthetic_owner | SELECT | false |

### Inventory delta 20260804_0007

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260804_0008

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260730_0004

`B−`：0 tuples。

`B+`：73 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | users | email_body_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | source_metadata_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | workspace_history_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | conversations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | conversations | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | conversations | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | daily_brief_items | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | daily_brief_items | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | daily_brief_items | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | daily_briefs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | daily_briefs | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | daily_briefs | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | llm_invocations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | llm_invocations | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | llm_invocations | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | messages | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | messages | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | messages | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：33 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | users | email_body_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | source_metadata_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | workspace_history_retention_days | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | conversations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | conversations | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | daily_brief_items | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | daily_brief_items | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | daily_briefs | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | daily_briefs | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | llm_invocations | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | llm_invocations | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | messages | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | messages | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |

### Inventory delta 20260804_0009

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260804_0010

`B−`：0 tuples。

`B+`：3 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | email_messages | body_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | body_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | body_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |

`R−`：0 tuples。

`R+`：3 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | email_messages | body_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | body_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | body_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |

### Inventory delta 20260806_0011

`B−`：0 tuples。

`B+`：24 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| table | public | connection_capabilities | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | connection_capabilities | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | provider_calendars | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | provider_calendars | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：8 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| table | public | connection_capabilities | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | provider_calendars | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |

### Inventory delta 20260806_0012

`B−`：0 tuples。

`B+`：48 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| table | public | calendar_change_proposals | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | calendar_change_proposals | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | calendar_change_snapshots | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | calendar_change_snapshots | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | mail_draft_versions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | mail_draft_versions | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |
| table | public | mail_drafts | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | mail_drafts | — | synthetic_owner | synthetic_owner | DELETE,INSERT,MAINTAIN,REFERENCES,SELECT,TRIGGER,TRUNCATE,UPDATE | false |

`R−`：0 tuples。

`R+`：16 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| table | public | calendar_change_proposals | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | calendar_change_snapshots | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | mail_draft_versions | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |
| table | public | mail_drafts | — | ai_employee_app | synthetic_owner | DELETE,INSERT,SELECT,UPDATE | false |

### Inventory delta 20260806_0013

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260807_0014

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260808_0015

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260808_0016

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260808_0017

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260809_0018

`B−`：0 tuples。

`B+`：0 tuples。

`R−`：0 tuples。

`R+`：0 tuples。

### Inventory delta 20260809_0019

`B−`：1 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |

`B+`：59 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | approval_requests | payload_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | payload_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | payload_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_proposals | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_proposals | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_aad_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_aad_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_drafts | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_drafts | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | approval_checkpoint_recovery_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | finished_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | lease_expires_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | lease_owner | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | retry_recovery_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | scheduled_for | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | tool_executions | error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | tool_executions | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | brief_time | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_calendar_connection_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_calendar_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_mail_connection_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | locale | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | meeting_buffer_minutes | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | timezone | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | working_hours | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | calendar_change_proposals | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | calendar_change_snapshots | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | connection_capabilities | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | mail_draft_versions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | mail_drafts | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | oauth_attempts | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | provider_calendars | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | sync_cursors | — | ai_employee_retention | synthetic_owner | DELETE | false |

`R−`：1 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sequence | public | checkpoint_migrations_v_seq | — | ai_employee_retention | synthetic_owner | USAGE | false |

`R+`：59 tuples。

| kind | schema | object | column | grantee | grantor | privileges | grantable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| column | public | approval_requests | payload_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | payload_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | payload_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | approval_requests | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_proposals | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_proposals | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_change_snapshots | content_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_aad_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | description_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_aad_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | location_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | calendar_events | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | email_messages | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_ciphertext | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_key_version | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_draft_versions | body_nonce | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_drafts | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | mail_drafts | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | approval_checkpoint_recovery_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | finished_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | lease_expires_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | lease_owner | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | retry_recovery_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | scheduled_for | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | task_runs | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | tool_executions | error_code | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | tool_executions | status | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | brief_time | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_calendar_connection_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_calendar_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | default_mail_connection_id | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | locale | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | meeting_buffer_minutes | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | timezone | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | updated_at | ai_employee_retention | synthetic_owner | UPDATE | false |
| column | public | users | working_hours | ai_employee_retention | synthetic_owner | UPDATE | false |
| table | public | calendar_change_proposals | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | calendar_change_snapshots | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | connection_capabilities | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | mail_draft_versions | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | mail_drafts | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | oauth_attempts | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | provider_calendars | — | ai_employee_retention | synthetic_owner | DELETE,SELECT | false |
| table | public | sync_cursors | — | ai_employee_retention | synthetic_owner | DELETE | false |

迁移前先验证 source grant posture，再执行固定 new-object/policy delta，最后验证 destination inventory。未知对象或 drift 在首笔 DDL 前拒绝；destination verification 或 0019 最终 guard 失败时，事务内 DDL/version/grants 整体回滚。0016–0018 的 autocommit/concurrent-index 用例另外保留实际 partial state，只接受冻结的 exact resume candidate，不把它们写成全事务回滚。对应当前门禁显式选择 `test_database_access.py`、`test_database_grants.py` 与 `test_migrations.py`。

## OAuth、0019 和回调边界的自动化证据

本节属于当前候选的合成自动化矩阵。真实 PostgreSQL 事务、会话锁、SQLAlchemy commit/rollback 和 HTTP 边界与受控 Fake 供应商共同执行；它不声称在非空生产数据库实施了 0019。结果由本记录的完整门禁及显式选择绑定，测试路径均相对仓库根目录。

### 严格结果联合、CAS 与未知提交结果

`OAuthRefreshResultV1 = ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1` 共用严格 parser，retention 也使用同一解释。当前矩阵覆盖如下实际事务结果：

| result member | 真实 commit 且 ACK 丢失 | 真实 rollback 且 ACK 丢失 | 原 automatic fence |
| --- | --- | --- | --- |
| confirmed | 新 session 按 user/connection/attempt 读到 matching result；保留已发生的 1 次 refresh，无新增调用 | 无 matching result，started 保持 unresolved；后续调用为 0 | 只关闭匹配的本次 automatic attempt，不消费更早 fence |
| recovery unsatisfied | denial callback 的 code exchange 为 0；missing/same 或安全已知失败的 exchange 可为 1；新 session 只读闭合 recovery | 无 matching result，recovery started 仍未闭合；state 已消费，code/error replay 均拒绝 | 始终不消费 original fence |
| credential replaced | different non-empty refresh 与完整 F/S/T proof 同事务提交；新 session 闭合 recovery 和 original automatic，exchange 总数仍为 1 | credential/scope/capability/result 同时回滚，started 未闭合，后续 code/provider 调用为 0 | 只有合法 replacement 可消费 |

`backend/tests/integration/m2/test_oauth_refresh_coordinator.py` 中
`test_confirmed_commit_ack_loss_uses_fresh_session_without_provider_replay` 使用真正的 before_commit/after_commit hook，交叉测试物理 lease 丢失；
`test_recovery_result_commit_ack_loss_uses_fresh_union_and_keeps_state_consumed` 覆盖 denial/missing/replacement 的 commit 与 rollback；
`test_recovery_ack_loss_later_authorization_commits_before_fresh_reconcile` 覆盖 ACK 丢失期间另一合法授权先提交。它们读取新 session 的持久事实并验证原连接归还连接池，不用模拟返回值代替数据库提交。

`test_rotation_commits_fence_before_network_and_preserves_refresh_bytes`、`test_rotation_cas_miss_never_overwrites_concurrent_facts_or_replays`、`test_rotation_and_confirmed_rollback_together_on_post_mutation_failure` 位于 `backend/tests/integration/m2/test_credential_rotation_repository.py`。它们验证解密→非空 UTF-8/长度验证→固定 identity→lease 与已提交 started→provider 的顺序；网络期间不持业务行锁；响应后同一事务对完整 access/refresh row snapshot、generation 和身份做 CAS，并写 credential/expiry 与 confirmed。

自动 confirmed 的 `source/started_source`、canonical attempt UUID、rollout binding、G/G/G、两类 pre/post digest 字段、old/new identity 字段及 key version、实际持久 expiry 与 historical deadline candidate 均由独立断言核对存在、相等关系及严格时间顺序。missing/same 为 old==new、changed=false，refresh row 字节不变；different 为 old!=new、changed=true。parser 拒绝 disposition/flag/identity 的矛盾组合。真实凭据摘要、identity fingerprint 和逐尝试 credential 时间不进入本文。

恢复 start 绑定原 attempt/started source/connection、本次 OAuthAttempt/event、F/S/T、冻结的两类 pre-digest 和 old identity/key version。当前 T 的 denial、missing、same 与已知安全失败将且仅将 requested capabilities 从 authorizing 收敛为 action_required，保留 actual scopes/last_verified；`oauth_refresh_recovery_unsatisfied.v1` 不得带 post credential/digest、新 identity、persisted expiry 或 deadline 字段。stale T 使用 `stale_target_noop`，不覆盖较新授权。之后只有新的连接绑定 OAuthAttempt 返回不同非空 refresh，才能同事务提交 `source=progressive_recovery`、`pre_generation=post_generation=T`、完整 post proof、old!=new/changed=true 的 replacement；它必须严格晚于两个 started。

对应节点还包括 `test_recovery_start_binds_f_s_t_and_repeated_attempts_increment_once`、`test_recovery_unsatisfied_preserves_facts_and_only_closes_recovery`、`test_recovery_different_token_consumes_original_after_an_unsatisfied_attempt`、`test_recovery_replacement_mutation_failure_rolls_back_all_callback_facts`。`compare_digest` 决策、两 Worker 同一 lease 仅一调用、callback 单消费者、stale snapshot/CAS miss、响应后 lock loss、network unknown、Taskiq/TransientProviderError 重入均有零额外调用断言。

historical closure 与 current readiness 分开核对：exact post-state 可以继续；后来合法 refresh/reauth、generation/scope/capability 变化不重开历史 attempt。changed=false 的 access-only 或同明文重加密可以继续，即使物理 snapshot 改变；changed=true 后 current identity 回到 old 是 A→B→A，返回 `oauth_credential_state_conflict`。缺行、无效 AEAD/ownership 和其他不一致也阻断，原 attempt 仍闭合且不会重新调用。unsatisfied 没有 credential post/expiry proof，只按当前能力事实处理。对应 `test_historical_closure_and_current_readiness_are_independent`、`test_recovery_historical_replacement_closure_survives_later_current_changes` 和显式 0019 preflight 选择。

### 回调输入、显式恢复入口和四入口 canary

Google/Microsoft 只接受互斥的 code+state 或有界非空 error+state。缺 state、code/error 同时存在、两者都无或 malformed error 在消费前拒绝。任何合法 error（包括未知名称）消费 state 一次并在当前 T 做 unsatisfied/no-op；重放同一或不同 error 都拒绝。安全分类为：denial/未知名称→`oauth_authorization_failed`；Microsoft consent evidence→`microsoft_admin_consent_required`；普通 interaction_required→`microsoft_reauthorization_required`。raw error/description/codes 不进入持久错误码、日志或 Trace。

targetless callback 命中有 fence 的既有 identity 时，在 credential/scope/capability/重复 connection 的任何保存前拒绝，以上写入计数均为 0；disconnect/reconnect、新 connection 映射、missing/same token、access-only、重加密或旧明文不可用都不能证明 fence 已消费。连接页 enabled 能力的“重新授权”入口绑定原 connection；不会在页面加载时自动交换 code，busy/disconnected 仍禁用。

实际 Uvicorn 子进程 canary 节点为 `backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py::test_real_uvicorn_oauth_query_has_zero_log_matches_and_keeps_error_audit`。四个 case ID 是 `compose`、`compose_dev`、`just_dev`、`e2e`，分别核对生产 Compose、开发 Compose、just API 开发入口和 E2E backend。每例 code/state/error_description 在 stdout、stderr、application JSON logs 零命中，同时 PostgreSQL 中保留脱敏 callback-error audit 与一次消费事实。本文只记录 case ID 和通过结论，不记录 canary query 值。

### 0019 guard、截止线与恢复 ordinal

`backend/tests/integration/db/test_migrations.py` 与 `backend/tests/integration/operations/test_calendar_aad_0019_preflight.py` 验证 revision-global lease 在 artifact/provider 前获取，随后每 connection 共享 coordinator lease；每个受影响 connection 只主动 refresh 一次。scope 缺失、invalid_grant、malformed response、network unknown、lease 丢失、CAS 失败、result rollback 均阻断，不发布成功 artifact。相同/不同 basename、旧 D0 和 Taskiq 重入都不会绕开 durable fence。

`backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py` 验证每个 backup/audit/migration/resync/restore-eligibility/post-resync guard 都重新读取 current access expiry：`current_deadline=min(current_expiry)-900s`，`effective_deadline=min(original_artifact_deadline,current_deadline)`。历史 confirmed candidate 只验证 schema，不给当前步骤授权；短 expiry 立即收紧，长 expiry 不延长窗口。`test_calendar_aad_ack_lost_then_legal_refresh_uses_shorter_current_expiry` 在真正 after_commit ACK/lease 丢失后，让另一 coordinator 提交 10/30 分钟的 current expiry，分别证明阻断或按更短窗口继续。零集合每次重验证，漂移/超时 fail closed。

`backend/tests/integration/operations/test_calendar_aad_0019_recovery.py` 及显式审计验证精确 pair 的 cursor/freshness/marker/v2 AEAD；活动 TaskRun 复用原 ordinal，只有上一 failed/cancelled 且 marker 仍存在时，下一显式 invocation 才分配新 ordinal。TaskRun、audit、Outbox 原子写入，旧终态不复活；不扫描普通队列、不调用其他 calendar/directory、不写供应商。post-resync artifact 必须先于 general startup 且在 effective deadline 内提交。

本次实际安装采用 fresh/zero-bootstrap：没有待轮换的旧 CalendarEvent pair，没有原 0018 生产镜像或 sealed 维护窗口，affected connection/pair 均为 0，deadline 为 no-deadline。因此没有生产 preflight/provider refresh、pre/post-migration rollout artifact、resync invocation 或 production restore confirmation；这些分支的安全性来自上面的当前自动化矩阵，不能补造实际执行记录。原生安装和普通 0019 灾备证据另列。

## 保留、隐私屏障和 PostgreSQL 恢复矩阵

30/180/365 天边界、未来日程保留、邮件正文/审批、日程 snapshot/审批同事务清空，及 CalendarEvent 描述/地点各四列清空由 `backend/tests/integration/retention/test_m2_action_retention.py`、`backend/tests/integration/privacy/test_source_cache_cleanup.py` 和显式权限测试覆盖。source-thread/source-message 任一非空及 target-event 精确绑定删除来源；独立草稿/create 保留，跨用户不可读/删，所有供应商写次数为 0。普通历史和全数据恢复在 TaskRun→user 锁序下清理三张原生 checkpoint 表，保留 checkpoint_migrations；失败保留父 TaskRun，迟到 saver 不可写回。

五种 OAuth refresh 审计不交给普通逐行 cutoff。未决 automatic started 即使超过 365 天仍保留，后续 provider 调用为 0，其他用户和普通审计仍能清理。automatic-success、unsatisfied-recovery、successful-recovery 只有整个严格 result-union 组每项均早于 cutoff 才原子删除；旧 started+新 result、畸形/重复/矛盾组保留，不拿后来 credential lineage 作为删除条件。真实 writer 与 EXCLUSIVE NOWAIT 清理竞争、锁后重读与失败回滚有独立断言。

全数据删除以 true→false user CAS 与精确 `privacy_deletion_started.v1` authority 同事务建立屏障。两个 pre-barrier RUNNING 请求中只有赢家能写唯一 started；零/畸形/多条/冲突 authority fail closed 且不进入普通 retention。inactive 普通 TaskRun acquire/renew 均零写；只有 exact task/user/request 绑定且 lease 过期的赢家可取得 `inactive_all_data_recovery`，续租仍要求 exact owner 和未过期时间。重要节点为：

- `test_task27e_inactive_acquisition_admits_only_exact_expired_winner`、`test_task27e_inactive_renewal_requires_live_exact_winner`、`test_task27e_generic_writes_preserve_inactive_winner_running`。
- `test_task27e_each_deletion_phase_crash_keeps_same_winner_recoverable`、`test_task27e_barrier_loser_performs_no_cleanup_and_cannot_create_second_authority`。
- `backend/tests/integration/workers/test_retry_recovery.py` 的 `test_task27e_winner_recovery_preserves_running_deduplicates_and_survives_redis_loss`、`test_task27e_recovery_limit_excludes_unchanged_prefix_before_winner`、`test_task27e_inactive_winner_real_redis_loss_replays_identifier_only_outbox`。

各删除 phase、总/步超时、安全 domain failure、unknown exception、acquisition ACK loss 均保留原 RUNNING/started_at/lease history；不写终态或 retry 状态，不解密 trusted command、不创建 ToolExecution、不写邮件/日历、不重新激活用户。same-bucket 并发 stale scan 与 Outbox 重投去重；已发布消息随 Redis 丢失后，后续 bucket 可以再次入队；超过 limit 的低排序 inactive 普通/loser/当前 bucket 项目不能饿死赢家。loser 不接管并最终被删除。

phase 5 保留当前赢家 TaskRun 与 authority；最终 authority/task-delete/anonymize/audit 任一点故障都整事务回滚。成功后只留 inactive 匿名管理员和冻结默认值、一条无内容完成审计；赢家 authority/TaskRun/Outbox 一起消失，队列 ACK 丢失不增加审计。迟到的 OAuth、模型、sync、设置、人工结果确认和审批/写执行均受 user 屏障约束。隐私只读 reconciliation 的 started 在 GET 前提交，缺定位/能力/access 为零网络，UNKNOWN 不伪造成功；不 refresh/code exchange/命令解密。撤销最多解密一个 token，先提交本地凭据删除，远端失败/不支持仍可完成，恢复不重新取已删除凭据。

旧 `database.restore.completed`（`ai_employee.database_restore_completed.v1`）按普通 365 天 cutoff 删除；全数据删除也无特殊豁免。完成的 restore-call:v4 与 `ai_employee.restore_completion=restore_completion:v1:<digest>` 的精确 zero-slot pair 不变，fresh completed admission/ACK 仍从 pair、gate absence、baseline ACL、safe roles、双向零 membership、完整 object grants 判定。普通 BigInteger audit ID、存在/计数和 metadata/digest 都不是 authority；缺失/malformed/mismatch catalog pair 必须拒绝。后来恢复可把新的固定 outer-field 审计绑定唯一 inactive 匿名用户，不能要求复活旧用户。

## 通用/ sealed 恢复协议的当前自动化证据

`backend/tests/integration/operations/test_database_maintenance_gate.py`、`test_postgres_backup_restore.py` 和 `test_calendar_aad_0019_deadline_restore.py` 由当前门禁显式选择；真实 PostgreSQL catalog/锁/事务断言、受控进程与 shell 合约断言共同覆盖本节。真正的当前镜像 host 命令另列，避免把注入边界或历史镜像当作本次原生演练。

备份同时持固定数据库 backup lock、BACKUP_DIR flock 和 `(20260806,143)` shared schema lifecycle lock；pre-revision→pg_dump→post-revision CAS 期间与 migration exclusive lock 互斥。revision 改变或 lock loss 零发布；本地/远端唯一 staging、manifest-last no-clobber、3600 秒复核后孤儿清理、按完整三件套保留以及冲突补偿均有断言。普通 0019 backup/restore 不需要 0019 rollout artifact。

restore 从 postgres 管理库持 management lock 到 terminal reconcile/reopen；db-reset 从 check→drop→create→migration 同样持锁。holder 崩溃后，active/needs_attention catalog authority 阻断 drop/create/migration。target holder 非阻塞、先核对精确 baseline/角色/membership/object grants，随后同事务建 gate/call authority、RESET completion、撤销 app/retention CONNECT；终止既有非 owner session，拒绝新连接。仍有 runtime/owner writer 时零 restore。`pg_restore --clean` 可替换审计，因此 pre-restore audit 不参与授权。

backup/artifact 为只读挂载，独立 `RESTORE_STATE_DIR` 保存 0600 state-v4 projection；另一个无本地文件的 host 可以按数据库 authority 重建投影。三项 database-wide facts 的 parser 核对 ASCII 长度、分隔符、重复/畸形输入，以及 attempt/kind/target/source/manifest/previous-completion、pre/expected/observed、call_ordinal/reopen_ordinal、phase/timeline、completion digest 和 psql PID/backend-start。合法 frozen phase edge 以 CAS 推进，跳步/self-transition/stale CAS 拒绝，projection 不授予权限。

`restore_backend_starting` 先于 spawn；psql 的精确 PID/backend-start/database/role/PGAPPNAME 注册先于 ready；started CAS 和 projection fsync 先于第一 SQL byte/pg_restore call。只有固定 psql consumer 接收 bootstrap owner credential，pg_restore generator 不接收 PG*、DSN 或 Secret，只输出 SQL。controller 管理 pipe 和 deferred completion guard，controller crash、缺 trailer、generator/consumer 失败或 EOF 导致回滚。在 authority-bound backend 退出前，不判断 fingerprint/not-applied/重放；单凭 pg_stat_activity 或旧 PID 不够。

另一 host 无法证明本地 child/pipe identity 时，starting→needs_attention，pg_restore 和新 ordinal 均为 0；只有原存活 controller 可 starting→ready。spawn-before-visible、ready-before-feed 保持 SQL-byte-zero。相同 ordinal 保留 call-start/pre/backend/observed；仅 exact-pre 且合法 restore_not_applied 才能 ordinal+1，重冻 exact-current pre、写新 call-start、清旧 backend 身份。direct exact-post 保留 ordinal 0。grant rollback 保持 restore_succeeded，verifier failure 保持 grants_succeeded，不提早 reopen。

对较旧备份的第二次 restore，先验证并 fsync prior completed pair 的不可变 archive，再在同一 owner 事务替换旧 completed call、RESET completion、建新 gate 并撤销最小 CONNECT。恢复后拒绝未知对象/pre-grant drift，在既有 holder session 使用共享 manifest-revision helper 应用 active inventory；同 session SET ROLE ai_employee_app、BEGIN READ ONLY、current/session-user 和真实 DML SQLSTATE 25006 验证先完成。最终一个事务完成 exact-one-active-or-inactive-admin 检查、completed authority CAS、固定 user_id/NULL task/system actor/database time 的普通审计、completion GUC、gate reset、active→baseline grant delta、最小 app/retention CONNECT；PUBLIC 仍撤销，完整 access/grant posture 再核对。

reopen ACK unknown 由新的 owner session 只读区分 completed pair 与 definite absent-GUC active rollback：completed 可在审计已被 retention/privacy 删除后继续；缺 completed call 或混合 tuple 不可通过。`reopen_committing→reopen_not_applied` 重试需要新 reopen_ordinal；commit 后 host crash 不重放。generic 和 sealed 使用同一 primitive/ordinal/backend-exit/completion-GUC 路径；没有 reopen 后再做影响成功判定的独立 app verifier。

生产 pre-manifest input 必须在 owner/pg_restore 前拒绝。非生产 `just restore-legacy-to-isolated` 使用 checksum/name/conflict 验证、资源创建前 fsync registry、唯一 Compose project/internal network/volume、临时 0600 Secret、精确跨资源 labels 和 per-attempt nonblocking lock。SIGKILL/daemon/host-crash 遗留由固定 3600 秒 grace 后的 registry scavenger 重新确认 inactive 再清理；存活或 uncertain 的其他 attempt 保留。实际转换读取 revision/health，再通过普通 locked backup 发布新 manifest，不能编造 manifest 或接触 workspace/production 数据库。

sealed 的独立 artifact/image 合约在读取 owner Secret 前验证 basename/dump/checksum/manifest/preflight/pre-migration/内部 executable map；moved-tag、missing/wrong-image、artifact mismatch 或 executable bind mount 的 owner/pg_restore 计数为 0。service 只依赖 PostgreSQL，精确注入 sha256 image、--pull never、无 tag/build/pull，entrypoint 重复 pre-Secret 检查。当前自动化覆盖这些分支；本次正常 fresh 发布没有实施 sealed 0018 回退，也没有将历史 native sealed 的成功重新归到当前镜像。

## 独立审查、失败处置与发布决定

四轮既定审查已关闭，限定问题均有实际修复和回归；本次不重开审查或启动新代理。整分支
I1–I5/M1/M2 均 ADDRESSED，后三轮 spec/quality 通过且无未决 Critical/Important/Minor。
历史报告中的“Task30 未完成”反映报告当时的验收阶段，不改写报告原文。

| 关闭记录 | SHA-256 |
| --- | --- |
| .superpowers/sdd/2026-08-06-executable-mail-calendar-assistant-m2/task-30-whole-branch-fix-rereview-closure-v1-controller.json | `8792c9190b927e14598dd0ae4b96ef79b197f742dad79796b12d9640a9400316` |
| .superpowers/local-m2-runtime/acceptance-fix-review-8abb445f44434c398758710304d6c61c/review-closure.json | `9619ad552b99d6180e0191790c92c9487817d04589f60b3174ae528fb119ec24` |
| .superpowers/sdd/2026-08-06-executable-mail-calendar-assistant-m2/task-30-calendar-text-fix-v1/review-closure.json | `93bcc5db618a58e9605c80c6be2c609a00461a802da7e975bb588629c852c24d` |
| .superpowers/sdd/2026-08-06-executable-mail-calendar-assistant-m2/task-30-enabled-reauthorization-fix-v1/review-closure.json | `b5c04faa32d0a68887bff1365aea67bfc369ace6420a7ce9b4425d0ec8736149` |

历史失败及处置全部保留：

| 原运行 | 当时结果 | 处置与本次结论 |
| --- | --- | --- |
| C0 `4da90dfa…` / gate `01e28ec2…` | 完整门禁 exit 0，log SHA `9a39d83e713662bd730c68440229ae6b530cbd1024927c95af221603112b25df` | 只支持首四项真实执行的历史源码；不作为 C1 门禁 |
| C1 gate `6123231d8d5a42e18602a40d25de1621` | exit 1；Worker 指标端口 9101 bind errno 98；log SHA `c01502478f929f82934e7d1ce7a88ae28b757fde61919673db66573c41b6defd` | 仅合成环境改用可绑定的 19101，原失败测试聚焦通过；产品源码未改；由 f334 完整门禁替代 |
| C1 gate `a89e564…` | 会话中断，原 PID 消失、无 result，集成停在 43%；原生退出码未知 | 可用日志 SHA `6073bed584d388627e23569bf875043bcf64375508b27a71fcb9cfc66ac310d2`；恢复原四服务，不冒充成功 |
| C1 gate `4ee1b8…` | exit 1；旧合成 PG/Redis 停止；PG WAL 恢复后遗留临时库和固定角色 | 原日志 SHA `050c3d41272b27407b915b70ca55aefc11d230c69d70c7b8dc48324afb38099c`；保留旧资源，仅停精确旧 PG；建立 UUID fixture `163aa01d…` 后执行 f334 |
| 当前 native deploy | exit 1；internal-only 网络没有实际回环映射 | 日志 SHA `e89f35dd64557b4d3aa8895dabcaa2caa940ab35f87f1724d1173debcfb21e7d`；独立诊断/临时 TCP relay 接续 exit 0，原记录及四冻结助手不变 |
| 文档收尾首次 `just check` / `f628eb7500ab46ffaf8c6b456906cc0c` | exit 1；lint/typecheck 通过，后端 2578 passed、1 failed；日志 SHA `cf1c09ad6ffb293b7cfaad5ac665c2573116174863b261826e097a690c655a9b` | 验收清单改写丢失两条固定约束表述；恢复“任务级验证不等于发布验收”和“四个 Uvicorn 入口”，保持测试及产品源码不变 |
| 较早 native sealed/legacy | 各自历史镜像上的结果 | 旧镜像只匹配当前部分入口，保留历史时态；当前 generic/legacy 已重新绑定本轮镜像，sealed 安全性由当前完整自动化覆盖 |

f334 新合成环境 SHA-256 `562c98c4db3dae38820a2a3f36f65510ba4f9a3cb0916119ba90f19e220a7acd`；
仅继承已验证的 Redis 回环与指标 19101 调整。所有失败均没有更改真实写入许可或重放成功操作。

本次发布决定为 **M2 验收通过，按 fresh/zero-bootstrap 分支冻结交付**。实际 affected connection/pair=0、
no-deadline；没有非空生产 rollout、原 0018 生产镜像、生产 preflight/pre/post/post-resync artifact 或
production restore confirmation。当前后端 0019 镜像是普通兼容恢复基线；没有执行 sealed 回退，也没有
把一次 aborted 0019 window 写成成功。非空迁移、deadline 收紧、sealed、ACK 丢失、隐私/retention 和
scavenger 的复杂分支明确属于本轮合成自动化证据。

发布范围仍仅四种受控写命令；未部署生产、未 push/merge、未启用额外写权限或产品多 Agent。
Microsoft 另一账户类型只有契约验证；未执行长期持续试用，供应商偶发行为和长期 Token 问题的覆盖
受此限制。用户的 12 次应用内批准是真实操作授权，控制器没有代替用户审批或人工确认结果。

## 提交前核验

文档合约修正后，聚焦 `test_acceptance_requires_lifecycle_and_inherited_operator_evidence` 为 1 passed，
stdout SHA-256 `4bcefcc9f6c645923b9993ccf73ad92e6b400ca25458ea14e095857fc2131342`。随后从仓库根目录
在同一已登记合成环境执行完整 `just check`：开始 `2026-09-15T15:56:46.291883+00:00`，结束 `2026-09-15T15:57:26.603666+00:00`，
原生 exit 0，耗时 40.31 秒；后端 2579 passed、前端 265 passed（25 个文件），ruff、ESLint、
mypy 的 221 个源文件和 vue-tsc 均通过。既有 Starlette 弃用及测试路由警告保留，没有新增失败。

完整 185 行、14024 bytes 日志已读取：
`.superpowers/sdd/2026-08-06-executable-mail-calendar-assistant-m2/task-30-calendar-text-fix-v1/controller-check-37e8104f96ed44a5a6a2ac043e95563d/check.log`，
SHA-256 `e33e41a4ca8540a968c0e4ea9dec15a498215144d427e969efaf9583c47a35f2`。检查前后 723 文件源码摘要均为
`161c8a8b26a2e9880d4c3d3001e5e60428310447b4e4291f305b1e7c40737bc3`，source_unchanged=true。

完整发布门禁之后仅新增本记录、更新 README/验收清单/Task30 状态；所有运行源码、测试、锁文件和
配置逐字节保持 C1。最后只补记本节和完成勾选，不改变已验证的运行行为。提交保留已审 Task30 的
全部既有改动；按既有批准从版本控制移除私有 Task19 报告，其本地原件仍保留。本地 Secret、日志、dump、浏览器报告和其他忽略产物
不属于交付文件。
