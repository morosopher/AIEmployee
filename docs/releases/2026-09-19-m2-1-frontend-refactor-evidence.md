# M2.1 前端组件库重构验收记录

验收日期：2026-10-05（Asia/Shanghai）。执行人：Codex 实施代理；任务级独立审查由控制器调度的审查代理完成。
范围为[M2.1 规格](../superpowers/specs/2026-09-19-frontend-component-refactor-m2-1-design.md)、
[实施计划](../superpowers/plans/2026-09-19-frontend-component-refactor-m2-1.md)及已批准的
[完整任务历史补充](2026-10-01-task-history-evidence.md)。文件名前缀保留计划日期 2026-09-19。
M2.1 展示迁移及规定门禁完成；最终文档提交后再运行完整 CI，完成状态以该提交的独立记录为准。
M2.2「上线收尾」尚无获批规格，不得提前实施。本文不重开 M2 真实账户发布验收，也不代表生产部署。

## 验证基准与完整门禁

产品构建基准 HEAD：`a481bded033e9207a2f1574c232f10216f6a366f`。文档前独立 check 在 `fbcc9c856a50a802f3046fac4723a9ccb2a74571` 加前端测试异步等待／场景拆分修复候选和主计划文档清单的工作树上运行；这份测试修复随后独立提交为上方 CI 基准，九文档清单事实修订仍留在主计划工作树。完整 CI 时产品、测试、依赖与门禁源码均为该 HEAD，仅主计划的文档清单未提交。实际 HEAD 与 dirty 路径分别记录于 `unit-fix-complete-check.json`、`pre-ci-final-tests-started.json` 和 `pre-ci-final-tests.json`，不把脏文档树说成纯净 HEAD。
迁移前体积及 live region 基线仍为 `4ce84ae`，不能与产品构建基准混用。

首轮失败原件 `pre-ci.log/.json` 保留：在 `a1554d1` 上于 2026-10-04T20:37:15.791945～21:09:35.487300 UTC 执行完整 CI，check／regular 2516／lifecycle 485／构建预算均通过，最终 E2E 147/148、退出 1。唯一失败是邮件窄屏人工确认的裸 `Animation.finished` 等待抛出 AbortError，不能把这轮写成完整通过。
有界诊断为单进程 5 次与六进程 12 次，均未重现自然取消；原 CI 的具体取消来源仍未知。随后在原用例附加无内容节点和真实有限动画，实例公开 finished getter 返回原生 Promise 后微任务取消；旧等待稳定 RED，并核对取消触发与原生 AbortError。两处邮件等待改用已有 `settlePresentation` 后六项邮件 E2E GREEN，保留原 axe／焦点／inert／请求／状态断言，产品不变。该独立 `test: tolerate cancelled presentation animations` 修复经限定复审，再运行下表完整 CI。原始独立 `pre-check.log/.json`（2730／698、退出 0）及失败轮 21 PNG／23 axe JSON 同样保留，不与新成功轮混同。

第二轮 `pre-ci-after-animation.log/.json` 在 `4d3a51c` 上于 2026-10-04T21:34:32.718866～21:53:24.808079 UTC 退出 1：regular 为 2515 passed／1 failed／2 deselected，失败是 SIGKILL 演练的持久事实比较；此轮没有执行 lifecycle、构建或 E2E。原日志未保留具体差异字段，不能倒推它必然是 checkpoint 变化。

有界自然诊断未复现后，只读追踪确认默认异步 checkpoint 保存可与下一图节点并发。一次受控诊断在原 Guarded 保存进入锁／事务前等待，首次原事实采样后才释放原保存并等真实提交，然后继续原 SIGKILL，确实复现仅 checkpoint 4→5 导致旧比较失败；旧身份保留、其他业务事实相同，四秒时限未改。用户于 2026-10-05 明确批准仅修改 `backend/tests/integration/faults/m2_process_drill.py`、新增 `backend/tests/unit/test_m2_process_drill.py` 并同步范围文档。新比较由数据库计算身份与整行摘要，非租约业务事实严格相等、旧 checkpoint 身份/内容全保留，允许 kill 前真实追加，kill 后到重启前完整快照仍严格不变。18 项正负控、原两场景及同一受控恢复在真实跟踪树上通过，实际 SIGKILL=-9、write1／reconcile1、原消息二次交付与 pending 清零均保持。修复已经限定复审；它修正已证实的比较语义，不声称识别了原 CI 的未知差异字段。

第三轮 `pre-ci-approved-fixes.log/.json` 在 `fbcc9c8` 上于 2026-10-05T04:01:20.905500～04:06:09.322083 UTC 退出 1：后端 2748 通过，前端 696 通过／2 失败，未运行后续阶段。有效时序诊断确认恢复测试父版本已可见，但字段查询结束早于真实异步 Form 完成；原三表单场景串行累计 5263ms 超过默认 5000ms。最初临时 CJS 配置启动错误为零测试，不作为业务 RED。

前端修复过程保留所有失败：首次等待／三场景拆分后聚焦 72 项通过，但 `unit-fix-check.log/.json` 的默认完整 check 仍为 699 通过／1 个设置场景超时；旧 `settings-timing-diagnostic.log` 的临时 700 通过不作为正式门禁。真实 Select 反例表明 editable 输入后仅 blur 不关闭浮层，八字段输入完成时仍有 419 个可感知 option，关闭断言 RED。第一次 Escape 事件仅传 key、漏传库按其分派的 code，聚焦 71／1 失败；补 code 后 72 项通过，但退出过渡仍留下 419 个 option，因此不能称 DOM 已清理。自审晚于启动检查造成一次额外 check，`unit-fix-resume-check.log/.json` 在 2026-10-05T07:47:53.401174～07:52:42.155663 UTC 为 699／1，新增失败来自 CalendarPresentation 同形助手在真实 Form 加载中耗尽字段等待；不是设置产品逻辑失败。

最终独立修复 `a481bde` 仅五文件：两份日程测试助手均等待父版本 DOM→公开 `vi.dynamicImportSettled`→原字段查询；邮件／设置／日程字段公告拆为三场景并仅同步三条 inventory 映射；设置通过真实 Escape key+code，等待 aria-expanded=false 及公开 listbox（包含隐藏节点）实际离开 DOM，断言非法值保留后继续其余字段。相同提交前探针的 options 从 419 变为 0，八条字段公告仍在一次真实提交中同时验证。所有原断言与角色/count保留，默认 1s／5s、workers、生产及专属懒加载 E2E 均未改变。库存由 23 组变 25 组，94 条追溯／92 条观察不变，前端总数由 698 变 700。最终聚焦 103 项与 `unit-fix-complete-check` 的 2748／700 全部通过；限定独立审查 Spec PASS／quality Approved 后，才运行下表候选完整 CI。原超时现场未做 CPU 归因，不声称已知所有耗时来源或任意负载下无超时。

工作目录为仓库根目录；标准复现前提是 Python 3.12／uv、Node 24／pnpm、Docker、受管测试
PostgreSQL／Redis、合成 Secret 和 Playwright 浏览器已按运行手册配置。正式命令依序为：

```bash
just check
just ci
# 本文提交后，在干净的最终文档 HEAD 上执行：
just ci
```

本地宿主 8000 端口限制通过既有 `run-check-netns.sh` 包装适配，仍执行原 `just` recipe，
没有拆分成功片段冒充完整 CI。数据库相关工作严格串行；原始 `20260809_0018` 锚点保持只读，
测试编排器管理临时库与角色。环境为 PostgreSQL 17.5、Redis 7.4、Playwright 1.62.0，
Node 24.18.0／zlib 1.3.1-e00f703。使用 Fake Google／Microsoft 和合成数据，不连接真实账号。

| 执行 | UTC 开始 | UTC 结束 | 退出码 |
|---|---|---|---|
| 文档前 `just check` | 2026-10-05T08:01:20.487685+00:00 | 2026-10-05T08:08:27.846229+00:00 | 0 |
| 文档前完整 `just ci` | 2026-10-05T08:19:09.270820+00:00 | 2026-10-05T08:52:43.731525+00:00 | 0 |

| CI 阶段 | 本轮结果 |
|---|---|
| Ruff／ESLint、mypy／vue-tsc | 通过；mypy 检查 226 个源文件 |
| 后端单元 | 2748 passed（含新增 18 项 checkpoint 保持性正负控），保留 1 条 Starlette/httpx 弃用警告 |
| 前端单元 | 55 文件、700 passed，含 25 组真实 live region 场景及 22 项 helper 正负控 |
| regular 集成／契约／evals | 2516 passed；阶段内 2 deselected 转交 lifecycle，不是遗漏 |
| lifecycle | 485 passed；独立 pytest 会话验证迁移、权限、维护与恢复边界 |
| tooling／E2E backend／CI workflow | 原入口全部通过；tooling 4 项命令合约、后端脚本 8 项 |
| 生产构建、许可证、严格样式、默认体积预算 | 全部通过，数值见下文 |
| Playwright | 148 passed；0 failed、0 retry、0 skipped、0 flaky |

148 项由既有 103 项只在 chromium 运行一次，加 21 个七视图×三宽度 axe、21 个截图场景和
3 个布局交互场景组成；project 总数为 chromium 118、tablet 15、mobile 15。没有把旧套件运行三遍。

文档前完整日志及元数据保存于本地未提交目录
`.superpowers/sdd/2026-09-19-frontend-component-refactor-m2-1/task-17-validation/`：
`unit-fix-complete-check.log/.json`、`pre-ci-final-tests.log/.json`。本文提交后，`final-ci.log`、`final-ci.json` 保存
最终文档 SHA、干净工作树状态、UTC 起止及原始退出码；`task-17-report.md` 与最终整分支审查记录
另行归档。正文保留上述产品构建基准，不为写入自身 SHA 循环修改文档。

## 交付范围与只读边界

迁移前的 9 页面／25 组件是旧足迹。本次实际清点为 `src/pages/` 9 个 Vue 页面、
`src/components/` 38 个 Vue 展示组件，加 `App.vue` 共 48 个 Vue 文件，全部无 `<style>` 块。
新文件用于已批准的骨架、基础展示、任务历史、异步表单和墙上时间适配，不引入新业务入口。

| 任务 | 主要交付提交及收敛 |
|---|---|
| 1 工具链、token、许可 | `25f3fdd`，许可精确匹配修复 `c47407b`、干净类型构建修复 `a039521` |
| 2～6 骨架、基础、登录、简报、聊天 | `68f7dfe`、`a398a22`／`00f3523`、`110103d`、`98b4cf6`、`4a11592` |
| 7 完整任务历史 | 补充计划交付 `addc635`，限定导航／SSE／测试收敛及验收至 `714d126`，详见独立证据 |
| 8 连接 | 独立异步确认端口 `70aedc0`，页面 `51d025b` |
| 9 设置 | 用户批准的最小错误码投影 `393b625`，表单 `7a138f3` |
| 10 操作中心 | `2314cda`，切组焦点 `7159fee`、稳定布局等待 `d8c1ef5` |
| 11 邮件 | `674ae1d`，收件人键盘可见焦点 `75f9269` |
| 12 日程 | `ee0ad29`，目标墙上时间及已保存 offset `38197df` |
| 13～15 重新准备、审批、人工确认 | `decdd58`、`d447eb9`、`15c5154` |
| 16 强制门禁与运行时库存 | 设置载体 `2401c4b`、Password ARIA `a8e7126`、次级按钮对比度 `3d6e54a`；主门禁 `7e39c77`，断言可靠性 `a1554d1` |
| 17 文档与完整验收 | 动画取消等待 `4d3a51c`、用户批准的 checkpoint 采样比较 `fbcc9c8`，五文件异步等待／字段场景测试修复 `a481bde`；九份授权文档，提交信息 `docs: freeze M2.1 frontend refactor evidence`；文档提交 CI 独立留档 |

表中为实现追溯，不把各任务重叠的聚焦用例相加为总数。Tasks 1～16 均经过任务级审查及所需复审。
完整任务历史补充收尾曾因平台 `agent thread limit reached`，对三行精确标签修正及该补充文档采用
控制器复核；其余已完成独立审查不受影响，也不把这次 fallback 写成独立审查通过。

`git diff 4ce84ae..a481bde` 的行为层差异只对应批准的任务历史补充（含索引／迁移守卫与
只读 API／列表状态、有限 SSE 取消清理）、连接异步确认端口、真实错误码元数据和显式格式校验文件。
Task 17 另有用户于 2026-10-05 批准的两个后端测试文件修复及四份范围文档同步，生产后端未因此修改。原 `src/stores/`、`src/composables/`、`src/router/` 无 diff；Markdown 仅改模板排版类，
清洗与可信本地链接逻辑不变。最终验收文档提交只包含九份文档；三项前置测试修复以独立提交交付。
Task 12 的 `calendar-restore.spec.ts` 旧选择器已为公开 role/name，旧基线及最终 36 项相关 E2E
均执行过；清单改为 Validate，不制造无意义代码 diff。

## 许可证、样式与构建预算

锁文件中的 `primevue`、`@primevue/forms`、`@primevue/auto-import-resolver`、`@primevue/core`、
`@primevue/icons`、`@primevue/metadata` 均为 4.5.5；`@primeuix/themes` 为 2.0.3，Tailwind 为 4.3.3。
实际检查输出为 `prod 58 package versions checked`、`dev 375 package versions checked`、
`frontend licenses: OK`。生产／开发默认白名单及规格 §3.2 的包名＋精确版本＋许可原文例外均被执行；
不存在 PrimeVue 5、`@primeui/*` 或 themes 3.x，未增加许可证例外。

严格样式输出为 `0 hex colour literal(s) (0 distinct) and 0 @media quer(ies)`、`frontend styles: OK`。
`just check` 不构建；`just ci` 在真实构建后执行 `report-frontend-bundle.sh --budget`，预算未抬高。
`tokens.spec.ts` 的 11 项包括语义色板和实际次级按钮状态对比度；Password 和设置时间控件的前置修复
保留真实键盘与原业务提交行为。

| 度量口径 | JS gzip | 相对基线增量 | CSS gzip |
|---|---:|---:|---:|
| 固定迁移前 `4ce84ae` 基线 | 140373 B | — | 2369 B |
| 本轮 CI，Node 24.18.0／zlib 1.3.1-e00f703 | 332450 B | 192077 B | 8229 B |
| 默认门禁 | 总量 ≤ 340373 B | ≤ 200000 B | ≤ 60000 B |

KB 按 1000 字节计算。Task 16 曾对同一 dist 用宿主 Node 24.21.0／zlib 1.3.2 得到
330108 B／8244 B；那是另一压缩实现口径，不替代本轮值。Task 16 的真实生产资源探针记录了
登录与默认任务页只加载入口 JS／CSS，显式进入操作中心后才加载异步资源；该探针是既有 Task 16
证据，本轮没有冒称重跑。图标字体由本地构建提供，原预算只统计 JS／CSS。

依赖漏洞审计只引用 Task 1 已记录的 23 项基线（9 high／11 moderate／3 low），本轮未执行新鲜
`pnpm audit`，没有声称当前公告计数不变或 audit clean。Starlette/httpx、Zod PURE 注释与大 chunk
提示原样保留；没有改依赖或门禁来消除它们。

## 三种布局、axe 与截图

浏览器支持基线为 Chrome 111、Safari 16.4、Firefox 128 及以上。本轮自动化引擎是 Chromium，
375×812、900×1000、1400×900 是视口基线，不是三种浏览器；全页截图高度可大于视口。
布局在字体就绪、有限过渡完成、已关闭 listbox 真正卸载后检查严格 scrollWidth；不保证用户恰在
Select 离场动画中改变视口的每一帧无溢出，也未使用像素宽限或固定 sleep。

七个主视图×三宽度共 21 次全页 axe，以及手机导航／时间线两个打开 Drawer 的扫描，均为
serious=0、critical=0。没有关闭颜色对比度规则或排除问题区域。原始摘要 JSON 保留以下六个
非阻断规则实例，不能称“axe 全规则零问题”：

| 规则 | 等级 | 场景 | 节点数／原生 target |
|---|---|---|---|
| aria-allowed-role | minor | mail：375、900、1400 | 每场景 1；`#pv_id_0_6_multiple_option_0` |
| region | moderate | brief、mail、calendar：900 | 每场景 1；`.p-panel-title` |

上述 target 为 axe 原始输出，仅用于报告，不是测试选择器。完整原始 JSON 摘要包括所有
rule／impact／nodes／targets，不保存正文 DOM。21 PNG、23 axe JSON 和 HTML 报告已复制到
`task-17-validation/pre-ci-final-tests-artifacts/`；每份文件哈希与来源见 `pre-ci-final-tests-manifest.json`，
矩阵见 `pre-ci-final-tests-matrix.json`。PNG 未含 tEXt／zTXt／iTXt／eXIf 元数据，仅使用合成 fixture。
本轮 19/21 张 PNG 与 Task 16 归档字节相同；具体差异及人工查看结论见本轮报告。

| 视图 | 375px SHA-256 | 900px SHA-256 | 1400px SHA-256 |
|---|---|---|---|
| login | `3ac12d46e80c4a5a36f57fac345f6b237a1f104a623a706a1abeadfbd2a77fce` | `fa4dc5b286310f55d2b0c15c1579a7a02cefc54e506e105c546f52b4ccb9fab7` | `db1bff8253c393df63fc2ad5c1a99009d453c990ee1ff8dec7250e8f5e24f92b` |
| brief | `5bdd89239e794389fdb443b03aa587e2f1ceeee24fa151b2e8d974481d2b97d1` | `5bab77c62ccdec91335741f41e458d82ebacc2b8e5b237caa5935e03de6019d6` | `6e74bb08ec1597326cc05ab91e1255b74391285d137f5ea8e318aaf1ee2f0792` |
| actions | `eddbafef15a3793191a7338d453bf1f09739b4beb93ce066bdebc837948abc24` | `d7225487dffa212646307ab46a7585edfc304d85237c0b1f303c0a6ef5cbfcf7` | `eccba052ad63658449118b45daf5ada4e713f45a72f3d7bad0331f0bb9d24a68` |
| mail | `607dc89140f8d7c800283d7523d5b72e9cd5ab7019f7a4d9867458660e229a6d` | `56f7aa310596bec01458f3a5f061922321a4553fb1ae9ba2b43e5128a6e2181d` | `c0c9f7a3cfa956a7d3dd80d7a847b63edc547b8402773975e6fcc8e5b42d042b` |
| calendar | `9c3da0c243da6094d4aaaa54a0ef780420fd7b329d975a28990c42406dd5e594` | `55f9227b73d4ad7242d1c57c09d716508e4418daae4d91bac9215983c5ee0fd8` | `80f990ce1d4920f29c7373ed985db6b43393f548dddb684b3e4f3c23145db752` |
| approval | `06db2bf95576f7956affddab816502b9bfea0ed9f0a7f7e1eb0b5111338065c2` | `8fe75aade1e0a12d9a30a9c452a386591f3b5a6ed80b0d0bca20a9f3673a77eb` | `2d6684aa96522308ec0555668dc3bfe1842fedf7f6a7c38e558810a1659059ad` |
| needs_attention | `5175859708d0124cafdbedf164e8cd5407d9ac8270950f92e90971133a2547bd` | `b5b3b10f9c720542be6234f37d632e61fd7c1a157367e20b71eff6f0cfbeab0e` | `52dfe3e3f176fd0795ed1deaaecc127c7fe80ff1ae0be552a12cb9a610944390` |

## Live region 逐项追溯与运行时结果

`4ce84ae` 的历史全源码词法计数为 status 43／alert 32／dialog 1。两个 alert 命中分别是
`WorkSettingsForm.vue:240`、`ConnectionsPage.vue:166` 的 CSS 选择器，并非 DOM 公告；
排除后模板声明为 43／30／1，共 74 处。原 `CalendarConflictNotice` 无 role 的 polite 区域也单列追溯。
没有为两处 CSS 添加空 alert，也不把源码计数当作运行时验收。

逐项原位置、触发／文案片段、当前组件、迁移提交和变化原因均在跟踪文件
[`inventory.ts`](../../frontend/src/test-support/live-regions/inventory.ts)。
`coreScenarios.ts`、`editorScenarios.ts`、`workspaceScenarios.ts` 通过真实组件、Form、Store、
路由与受控 HTTP 触发场景；`assertions.ts` 校验实际文字、角色、优先级、准确实例数及双向嵌套。
本轮 `pre-ci-final-tests-live-runtime.json` 含 92 项真实观察＋2 项 CSS 排除；25 组场景全部通过。
相对权威 `task-16-fix1-live-runtime-final.json`，角色、实例数和观察次数一致；Task 17 拆分三表单测试后仅三项 scenario 名称改变，文字仅审批倒计时变化。
旧 `task-16-live-runtime-final.json` 的聊天第二项是修复前假阳性，不用来证明恢复成功。

| 数量／语义变化类别 | 逐项说明与本轮证据 |
|---|---|
| 旧公告抽取 | SystemAlertBanner、EditorRecovery、表单与页面保留原触发和文案；74 项逐一观察，非机械凑总数 |
| 原到期双实例 | ActionDetail／ApprovalCard 原各一处，组合保持 2，helper 证明第三个跨 role 副本被拒绝 |
| 外壳与任务历史 | 断线 status、一次恢复 Toast、错误 Toast、两个按需 Drawer、唯一 ConfirmDialog；独立列表加载／新任务／错误不覆盖详情或 SSE |
| 原冲突提示 | CalendarConflictNotice 迁到单一 region＋polite；缺失来源说明静态 off，不嵌套重复公告 |
| 表单与库内置公告 | 设置／邮件／日程字段格式错误分别 8／5／5 个测试字段；Select 搜索／选择、三个收件人搜索及当前弹层选择反馈各司其职 |
| 审批倒计时 | 单独 polite；不替代服务端有效性或必须阅读的审批结果 |
| 浏览器专属场景 | 4 个异步模块加载／失败、设置删除与离开、连接断开、重新准备、人工确认共 9 个实际浏览器用例在本轮 148 项中通过；邮件／日程离开确认的 2 个组件用例在 700 项中通过 |

额外 18 项是追溯分类，并非某页同时出现的节点总数。22 项 helper 正负控拒绝跨 status／alert／log、
显式 polite／assertive、副本与内嵌根；同节点 role/live 去重，隐藏／inert／off 排除；不同完整文字
保留独立播报。聊天真实 error→onopen 链两次文字依次为“任务：执行中 · 正在恢复实时连接”和
“任务：执行中”，第二项等待完整文本及恢复后缀消失后记录。没有修改生产 SSE 来满足测试。

## 本轮安全回归与故障演练

以下均由本轮完整 CI 的原套件执行，不以历史 M2 结果替代，也不把测试文件存在当作通过。
regular 阶段包含 integration／contract／evals，生命周期用例由原编排器转入独立 session。

| 风险 | 具体可复验用例／断言 |
|---|---|
| SSE 断线、重放与连接清理 | `integration/api/test_task_sse.py` 的 Last-Event-ID、通知丢失 PostgreSQL 补齐、快照缺口与临时 heartbeat；`test_task_connection_cleanup.py` 的 query/pre_ping×三读取×取消矩阵和真实 rollback 超时；`e2e/reconnect.spec.ts` 两条原游标／刷新去重流程 |
| 实际 Worker 崩溃及唯一写入 | `integration/faults/test_m2_write_recovery.py::test_m2_worker_process_kill_preserves_request_and_reuses_pending_message` 杀死真实 Taskiq 进程组并从原 pending 恢复；`test_m2_four_actions_converge_after_every_crash_boundary` 覆盖四动作事务故障边界；供应商均为 Fake |
| 幂等认领、精确审批与安全重试 | `integration/m2/test_tool_execution_claim.py` 的双 Worker 唯一认领、payload hash／AAD 篡改、request-start 后崩溃只核对、精确 not-applied 证明；`e2e/m2-actions.spec.ts` 的 Google／Microsoft 新邮件／回复／全部回复与 create／update／单独批准 restore、拒绝／过期／失效零调用 |
| 未知结果与人工确认 | `e2e/m2-actions.spec.ts` 的两种 unknown result 和 delayed reconciliation；`e2e/mail-editor.spec.ts` 的窄屏键盘人工确认、409 后权威刷新、仅发送 enum 且不会自动重发 |
| 浏览器注入与零存储 | `e2e/action-workspace.spec.ts` 的 chat trusted local link／恶意路径／javascript 链接拒绝及 noopener；`e2e/actions.spec.ts` 的危险供应商 URL 拒绝；`m2-actions`／`mail-editor` 保留 localStorage 与 sessionStorage 长度均为 0，正文仅纯文本 |
| 时间与恢复版本 | `CalendarPresentation.spec.ts`、`CalendarWallTimeFields.spec.ts`、`SettingsTimeInput.spec.ts` 及相应 E2E 覆盖目标 IANA、宿主 DST 缺失小时、未变快照 offset／精度、无效字符串与零隐式回写；恢复仍需新版本和新审批 |

真实模态 Dialog／Drawer 的焦点循环、Esc、背景 inert 与触发器返回由组件及浏览器共同验证。
DatePicker 锚定时间／日期弹层明确 aria-modal=false，仍保留原键盘循环与 Esc 返回输入；不宣称背景 inert。
CI 还执行已批准的迁移、ACL、备份恢复生命周期回归，但本次未重新执行 M2 专用真实账户操作、独立
`test-m2-release.sh` 发布扫描矩阵、生产备份或部署，不能由本轮通过推导这些未运行活动完成。

## 原件摘要与限制

| 原件 | SHA-256 |
|---|---|
| `unit-fix-complete-check.log` | `ffa6cc273feaf0d38bfcf8030b5c9135c428d4d0d8cdcda8057f7b11960a0260` |
| `pre-ci-final-tests.log` | `f9a9ab8b953a0ad0b82d8d9b52da648f47a44652657369fe17d0dd1b32fc70a3` |
| `pre-ci-final-tests-live-runtime.json` | `f8e7a9448bd39ca98b82c65809fca177e2eb32d43e74c203dfeb392bcc06b48a` |
| `pre-ci-final-tests-manifest.json` | `47f89192c74d74990e3e29f2b9bdbff9e57de492d58db2ab23a14567b0e0a537` |
| `pre-ci-final-tests-matrix.json` | `71bac485eb526c8bbabf250dafbed6cab31cb1d775c8150199a70191d1fba3e9` |

原件只保留在本地 SDD 审计目录，不提交运行日志、截图、缓存、生成物或 Secret。跟踪文件中的
测试、清单、脚本与上述命令可复验；文档提交后另存最终产物，防止下一次 Playwright 覆盖前一轮证据。
首轮失败及确定性红绿记录均保留；没有用重复绿色诊断冒充修复，也没有提高门禁阈值。既有 minor／moderate axe、漏洞基线、弃用／构建提示、跨浏览器未实测、
Select 动画中途 resize 边界保留。此前 Task 12 两次首次访问异常的原因仍未证实；本轮通过不把
“Vite 优化时序”假说升级为已确认修复。

`final-branch-review.md` 的实现部分审查范围为 `a2eb9bab..a481bde`，结论为 Spec 符合、Quality
Approved with Minor，无 Critical／Important；该实现结论不替代九文档及最终 CI 的后续补审。
四项实际 Minor 保留：当前路由导航项不关闭手机 Drawer（仍可 Esc／关闭按钮退出）；
LinkedActionPanel 同名嵌套静态 region；三档邮件 li/group 的 aria-allowed-role；三个平板
时间线标题在 landmark 外的 region。后两类共六个 axe 规则实例如上披露，不能称全面 AT 验证
或 axe 全规则零问题。历史版本零 POST 的显式断言、字段数组相同／增删／顺序变化是两项可选
覆盖建议，不作为额外产品缺陷。未执行真实读屏器、Safari／Firefox 的完整兼容验收。

## 执行裁决与审计附录

用户批准事实为 M2.1 规格、精确许可例外、完整任务历史及其有限 SSE 取消清理补充、2026-10-03
两文件最小真实 error_code 元数据例外，以及 2026-10-05 两个后端测试文件的限定修复及范围同步。下面是控制器在这些范围内作出的实施细化，不能写成用户新增批准。
原文及后续追加裁决保留于主 `progress.md` 和任务历史补充 `progress.md`，并有本轮 `pre-ci-final-tests-rulings-index.json`
按路径／行号索引；不删除历史失败或唯一原件。

| 裁决分组 | 理由与判断错误的代价 |
|---|---|
| 模型路由／审查 fallback | 平台不支持显式型号或达到线程上限；记录实际调度与控制器复核。代价是审查独立性／延迟限制，不降低产品要求 |
| ProblemMessage 两次最小扩展 | 安全本地描述及无 ProblemError 场景不得伪造服务端码。代价是调整小型展示接口，禁止原始 title/detail 透传 |
| 简报缺失事实 | 未提供结构化同步时间／缺失源时诚实显示未提供，保留 cutoff 与警告。代价是调整缺失呈现，不能捏造数据 |
| 操作中心／邮件／日程异步展示边界 | 满足既有表单和体积要求，保留单份原 hook。代价是调整局部组件边界及加载测试，不改路由／状态契约 |
| 稳定布局等待 | 已捕获离场 Select portal 在旧宽度停留；等待公开 listbox 卸载后严格检查。代价是仍需单独评估动画中途 resize，不保证每帧 |
| 日程 IANA 双显示与日期 footer/PT | 保留原 ISO／快照精度，避免宿主 Date 改写合法目标时间；纠正早期把 Tab 退出误认作非模态必然行为的判断。代价是局部兼容组合返工，不改共享时间业务层 |
| 重新准备三入口、深层确认隔离 | 同一确认端口覆盖全部入口，类型化 UI lease 隔离真实模态背景。代价是小型展示连接返工，不引入 Store 或新写操作 |
| 设置时间／Password／次级按钮 | 真实浏览器证明 DST 载体、无 popup ARIA、稳定对比度问题；分别独立修复。代价是撤回局部展示覆盖，不改认证、API、依赖 |
| 库存模块与断言正负控 | 74 处旧模板＋附加场景需要分组；审查复现跨 role 重复和聊天短前缀假阳性后补强。代价是测试组织／匹配返工，不靠增加 count 掩盖生产问题 |
| 任务历史迁移／唯一详情布局 | 保留 AAD 最终 callback 和原全局锁；历史页自有详情避免重复时间线。代价是迁移协调或布局测试返工，不放宽准入与执行语义 |
| Task 17 动画取消等待 | 原完整 CI 的 AbortError 与真实动画取消反例证明裸等待不可靠；复用既有等待并保留原断言。代价是调整测试同步，不推断未复现的具体取消来源，不放宽 axe 或产品行为 |
| Task 17 异步等待与字段场景 | 两份日程助手等待真实Form就绪，三种Form独立用例，设置真实Escape及退出完成后继续输入；419→0探针和八条同时公告保持。代价是局部测试同步返工，自审不足造成额外check已披露；不放宽时限或改变产品 |
| Task 17 文件动作与状态同步 | 实际缺少 CLAUDE，新建简短入口；把主 spec／plan 和后端指南首段纳入九文档事实同步。代价是修正文档，不开放 M2.2 |
