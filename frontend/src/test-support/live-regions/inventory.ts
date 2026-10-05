/**
 * 4ce84ae 的可追溯词法清单：43 status / 32 alert / 1 dialog；两项 CSS 明确排除，
 * 实际模板为 43 / 30 / 1。original 保留原触发／文案片段；运行时必须调用 assertLive
 * 逐项核对真实组件，静态清单或总数本身不能证明播报正确。
 */
export interface InventoryEntry {
  id: string
  source: string
  role: 'status' | 'alert' | 'dialog' | 'alertdialog' | 'region'
  scenario: string
  original: string
  currentComponent: string
  migration: string
  change: string
  excluded: boolean
}
export const inventory: readonly InventoryEntry[] = [
  {
    id: 'ActionConfirmationDialog:47',
    scenario: 'attention',
    source: 'frontend/src/components/ActionConfirmationDialog.vue:47',
    role: 'dialog',
    original:
      '<section ref="region" role="dialog" aria-modal="true" :aria-label="title" class="confirmation-dialog" @keydown="containFocus" @keydown.esc.prevent="!busy && emit(\'cancel\')" > <h3>{{ title }}</h3>',
    currentComponent: 'ActionConfirmationDialog',
    migration: 'd447eb9',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionDetail:72',
    scenario: 'actionDetail',
    source: 'frontend/src/components/ActionDetail.vue:72',
    role: 'status',
    original:
      '<p v-if="snapshot.status === \'reconciling\'" role="status" > 正在只读核对执行结果，请等待服务端更新。 </p> <p v-if="snapshot.error_code" class="error" >',
    currentComponent: 'ActionDetail',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionDetail:85',
    scenario: 'actionDetail',
    source: 'frontend/src/components/ActionDetail.vue:85',
    role: 'status',
    original:
      'v-if="snapshot.approval?.content_status === \'redacted\'" class="content-expired" role="status" > 内容已到期，仅保留执行历史。 </p> <p v-if="missingSources" class="attention" role="status"',
    currentComponent: 'ActionDetail',
    migration: '2314cda',
    change:
      '保留ActionDetail与ApprovalCard各自原有到期status；组合场景仍是基线的2处，不增加第三处。',
    excluded: false,
  },
  {
    id: 'ActionDetail:92',
    scenario: 'actionDetail',
    source: 'frontend/src/components/ActionDetail.vue:92',
    role: 'status',
    original:
      'v-if="missingSources" class="attention" role="status" > 部分日历来源尚未同步（{{ missingSources }} 个连接），冲突检查可能不完整。 </p> <dl> <template v-if="snapshot.approval"> <dt>审批状态</dt> <dd>{{ approvalLabels[snapshot.approval.status] }}</dd>',
    currentComponent: 'ActionDetail',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionDetail:154',
    scenario: 'actionDetail',
    source: 'frontend/src/components/ActionDetail.vue:154',
    role: 'alert',
    original:
      '<p v-if="error" role="alert" > {{ error.message }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span> </p> <NeedsAttentionPanel v-if="snapshot.status === \'needs_attention\'" :snapshot="snapshot"',
    currentComponent: 'ActionDetail',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'AppShell:34',
    scenario: 'shellAlerts',
    source: 'frontend/src/components/AppShell.vue:34',
    role: 'status',
    original:
      'v-if="alertsLoading" class="system-alert-state" role="status" > 正在检查系统告警 </p> <p v-if="alertsError" class="system-alert-state" role="alert"',
    currentComponent: 'SystemAlertBanner',
    migration: '68f7dfe',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'AppShell:41',
    scenario: 'shellAlerts',
    source: 'frontend/src/components/AppShell.vue:41',
    role: 'alert',
    original:
      'v-if="alertsError" class="system-alert-state" role="alert" > 系统告警暂时无法刷新。 </p> <div v-if="alerts.length" class="overdue-alert" role="alert"',
    currentComponent: 'SystemAlertBanner',
    migration: '68f7dfe',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'AppShell:48',
    scenario: 'shellAlerts',
    source: 'frontend/src/components/AppShell.vue:48',
    role: 'alert',
    original:
      'v-if="alerts.length" class="overdue-alert" role="alert" > <strong>每日简报已逾期</strong> <RouterLink v-if="alerts[0]?.diagnostic_task_id" :to="{ path: \'/tasks\', query: { task_id: alerts[0].diagnostic_task_id } }" > 查看诊断任务',
    currentComponent: 'SystemAlertBanner',
    migration: '68f7dfe',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ApprovalCard:171',
    scenario: 'approval',
    source: 'frontend/src/components/ApprovalCard.vue:171',
    role: 'status',
    original:
      '<p v-else-if="structured" role="status" > 内容已到期，仅保留执行历史。 </p> <dl v-else-if="legacy"> <template v-for="(value, key) in legacy.payload" :key="key"',
    currentComponent: 'ApprovalCard',
    migration: 'd447eb9',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ApprovalCard:221',
    scenario: 'approval',
    source: 'frontend/src/components/ApprovalCard.vue:221',
    role: 'status',
    original:
      '<p v-if="busy || decided" role="status" > {{ busy ? \'正在记录决定…\' : \'决定已记录，执行结果以服务端后续状态为准。\' }} </p> <div v-if="error"',
    currentComponent: 'ApprovalCard',
    migration: 'd447eb9',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ApprovalCard:229',
    scenario: 'approval',
    source: 'frontend/src/components/ApprovalCard.vue:229',
    role: 'alert',
    original:
      '<div v-if="error" role="alert" data-testid="approval-error" class="error" > <p> {{ error.message }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span> </p>',
    currentComponent: 'ApprovalCard',
    migration: 'd447eb9',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'BriefView:33',
    scenario: 'sources',
    source: 'frontend/src/components/BriefView.vue:33',
    role: 'alert',
    original:
      'v-if="brief.warnings.length" class="warning" role="alert" > <strong>部分数据未完成</strong> <ul> <li v-for="warning in brief.warnings" :key="warning" >',
    currentComponent: 'BriefView',
    migration: '98b4cf6',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarRepreparePanel:40',
    scenario: 'calendarSources',
    source: 'frontend/src/components/CalendarRepreparePanel.vue:40',
    role: 'status',
    original:
      '<p v-if="source.requires_sync" role="status" > 本地日程版本尚未更新，请先同步来源账户，任务完成后重新读取提案。 </p> <p v-if="syncTaskId" role="status" >',
    currentComponent: 'CalendarRepreparePanel',
    migration: 'decdd58',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarRepreparePanel:46',
    scenario: 'calendarSources',
    source: 'frontend/src/components/CalendarRepreparePanel.vue:46',
    role: 'status',
    original:
      "<p v-if=\"syncTaskId\" role=\"status\" > {{ syncRunning ? '同步进行中' : syncStatus === 'succeeded' ? '同步已完成，请重新读取提案。' : '同步未完成，请检查任务结果和连接。'",
    currentComponent: 'CalendarRepreparePanel',
    migration: 'decdd58',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarRepreparePanel:89',
    scenario: 'calendarSources',
    source: 'frontend/src/components/CalendarRepreparePanel.vue:89',
    role: 'status',
    original:
      '<p v-else role="status" > 原来源无法核实，请<RouterLink to="/brief"> 重新选择来源 </RouterLink>。 </p> <RouterLink to="/connections"> 检查连接、同步或重新授权',
    currentComponent: 'CalendarRepreparePanel',
    migration: 'decdd58',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CapabilityRows:50',
    scenario: 'sources',
    source: 'frontend/src/components/CapabilityRows.vue:50',
    role: 'status',
    original:
      '<div> <strong>{{ row.capability }}</strong> · <span role="status">{{ labels[row.status] }}</span> </div> <p v-if="row.capability === \'mail.send\'"> 启用 mail.send 同时需要 mail.read。 </p> <p v-if="row.capability === \'calendar.write\'"> 启用 calendar.write 同时需要 calendar.read。 </p>',
    currentComponent: 'CapabilityRows',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'EditorConnectionSelect:56',
    scenario: 'sources',
    source: 'frontend/src/components/EditorConnectionSelect.vue:56',
    role: 'alert',
    original:
      '<div v-if="unavailable.length" role="alert" > <p>部分账户能力读取失败，已保留当前选择。请到连接页面刷新后返回。</p> <p v-for="entry in unavailable" :key="entry.connection.id" > {{ providerLabel(entry.connection.provider)',
    currentComponent: 'EditorConnectionSelect',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'EditorConnectionSelect:73',
    scenario: 'sources',
    source: 'frontend/src/components/EditorConnectionSelect.vue:73',
    role: 'status',
    original:
      '<p v-if="current && !hasCapability(current, capability)" role="status" > 当前账户能力不可用，请检查连接授权。 </p> </template>',
    currentComponent: 'EditorConnectionSelect',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'EditorRecovery:16',
    scenario: 'sources',
    source: 'frontend/src/components/EditorRecovery.vue:16',
    role: 'alert',
    original:
      '<div v-if="error" role="alert" class="editor-error" > <p> {{ error.message }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span> </p> <RouterLink',
    currentComponent: 'EditorRecovery',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'LinkedActionPanel:55',
    scenario: 'linkedAction',
    source: 'frontend/src/components/LinkedActionPanel.vue:55',
    role: 'status',
    original:
      '<p v-if="actions.snapshotLoading[taskId]" role="status" > 正在读取关联任务… </p> <p v-if="actions.snapshotErrors[taskId]" role="alert" >',
    currentComponent: 'LinkedActionPanel',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'LinkedActionPanel:61',
    scenario: 'linkedAction',
    source: 'frontend/src/components/LinkedActionPanel.vue:61',
    role: 'alert',
    original:
      '<p v-if="actions.snapshotErrors[taskId]" role="alert" > 关联任务读取失败，请到操作中心重新加载。<span v-if="actions.snapshotErrors[taskId]?.trace_id" > 追踪编号：{{ actions.snapshotErrors[taskId]?.trace_id }}</span> </p> <p',
    currentComponent: 'LinkedActionPanel',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'LinkedActionPanel:70',
    scenario: 'linkedAction',
    source: 'frontend/src/components/LinkedActionPanel.vue:70',
    role: 'alert',
    original:
      '<p v-if="snapshot && !matching" role="alert" > 此任务与当前编辑对象不匹配，请在操作中心核对。 </p> <template v-if="matching"> <p v-if="connection !== \'connected\'" role="status"',
    currentComponent: 'LinkedActionPanel',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'LinkedActionPanel:77',
    scenario: 'linkedAction',
    source: 'frontend/src/components/LinkedActionPanel.vue:77',
    role: 'status',
    original:
      '<p v-if="connection !== \'connected\'" role="status" > 任务连接正在恢复，状态以服务端为准。 </p> <ActionDetail :snapshot="matching" :timezone="auth.user?.timezone ?? \'UTC\'" @changed="$emit(\'changed\')"',
    currentComponent: 'LinkedActionPanel',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'MailDraftFields:72',
    scenario: 'mailForm',
    source: 'frontend/src/components/MailDraftFields.vue:72',
    role: 'status',
    original:
      'class="wide-field" data-testid="recipient-count" role="status" > <template v-if="recipientSummary.count !== null"> 当前收件人数：{{ recipientSummary.count }} 位 </template> <template v-else> 收件人数待核对。 <span v-if="recipientSummary.inputCount !== null">当前输入地址：{{ recipientSummary.inputCount }} 项。</span>',
    currentComponent: 'MailDraftFields',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'NeedsAttentionPanel:123',
    scenario: 'attention',
    source: 'frontend/src/components/NeedsAttentionPanel.vue:123',
    role: 'status',
    original:
      '<p v-if="busy" role="status" > 正在记录核对请求… </p> <div v-if="error" role="alert" class="error"',
    currentComponent: 'NeedsAttentionPanel',
    migration: '15c5154',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'NeedsAttentionPanel:129',
    scenario: 'attention',
    source: 'frontend/src/components/NeedsAttentionPanel.vue:129',
    role: 'alert',
    original:
      '<div v-if="error" role="alert" class="error" > <p> {{ error.message }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span> </p> <button',
    currentComponent: 'NeedsAttentionPanel',
    migration: '15c5154',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'NeedsAttentionPanel:151',
    scenario: 'attention',
    source: 'frontend/src/components/NeedsAttentionPanel.vue:151',
    role: 'alert',
    original:
      '@confirm="act(choice)" > <p role="alert"> 此决定只记录人工结果，不会调用供应商写接口。确认未执行不会自动重发；如需再次执行，必须创建新草稿或提案并重新审批。 </p> </ActionConfirmationDialog> </section> </template> <style scoped> .needs-attention {',
    currentComponent: 'NeedsAttentionPanel',
    migration: '15c5154',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TaskTimeline:77',
    scenario: 'timeline',
    source: 'frontend/src/components/TaskTimeline.vue:77',
    role: 'status',
    original:
      '<p v-if="!task" role="status" > 请选择一个任务查看执行时间线。 </p> <template v-else> <header> <h2>执行时间线</h2> <p>任务状态：{{ task.status }}</p>',
    currentComponent: 'TaskTimeline',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TaskTimeline:89',
    scenario: 'timeline',
    source: 'frontend/src/components/TaskTimeline.vue:89',
    role: 'alert',
    original:
      'v-if="task.error_code" class="error" role="alert" > 错误代码：{{ task.error_code }} </p> <!-- 仅使用服务端确认的配置错误码提供恢复指引，不显示供应商原始响应。 --> <p v-if="task.error_code === \'google_api_not_enabled\'"> 请在 Google Cloud 中为当前 OAuth 应用所属项目启用对应的 Gmail API 或 Google Calendar API，启用后重试同步。 </p>',
    currentComponent: 'TaskTimeline',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TaskTimeline:131',
    scenario: 'timeline',
    source: 'frontend/src/components/TaskTimeline.vue:131',
    role: 'alert',
    original:
      'v-if="retryError" class="error" role="alert" > {{ retryError }} </p> </template> </aside> </template> ',
    currentComponent: 'TaskTimeline',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:35',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:35',
    role: 'status',
    original:
      '<p v-if="loading" role="status" > 正在加载设置… </p> <form v-if="form" @submit.prevent="save" >',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:161',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:161',
    role: 'status',
    original:
      '<p v-if="catalog.loading.value" role="status" > 正在读取可用账户与日历… </p> <p v-if=" catalog.error.value || catalog.entries.value.some((entry) => entry.error)',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:170',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:170',
    role: 'alert',
    original:
      'catalog.entries.value.some((entry) => entry.error) " role="alert" > 部分账户目录加载失败，已保留原默认选择。<button type="button" @click="catalog.load" > 重试目录 </button>',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:196',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:196',
    role: 'status',
    original:
      '<p v-if="saved" role="status" > 已保存 </p> <p v-if="validation" role="alert" >',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:202',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:202',
    role: 'alert',
    original:
      '<p v-if="validation" role="alert" > {{ validation }} </p> <div v-if="error" role="alert" >',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '原区间格式文案由真实zod提前投影到字段Message；旧hook兜底不重复触发，仍为单个alert。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:208',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:208',
    role: 'alert',
    original:
      '<div v-if="error" role="alert" > <p> {{ error.message }} <span v-if="error.traceId">追踪编号：{{ error.traceId }}</span> </p> <button type="button"',
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'WorkSettingsForm:240',
    scenario: 'settingsForm',
    source: 'frontend/src/components/WorkSettingsForm.vue:240',
    role: 'alert',
    original:
      "min-width: 0; } [role='alert'] { color: #a61b1b; } button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid #164e9c; outline-offset: 3px;",
    currentComponent: 'WorkSettingsForm',
    migration: '7a138f3',
    change: 'CSS 选择器非模板节点，排除且不建立替代 alert。',
    excluded: true,
  },
  {
    id: 'ActionsPage:106',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:106',
    role: 'status',
    original:
      '<p v-if="creating" role="status" > 正在创建本地编辑对象… </p> <EditorRecovery :error="creationError" :busy="creating" @reload="refresh"',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionsPage:164',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:164',
    role: 'status',
    original:
      '</form> <p role="status" aria-live="polite" > {{ actions.loading || (!actions.loaded && !actions.listError) ? \'正在加载操作…\' : actions.loaded ? `当前页 ${actions.items.length} 项操作`',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionsPage:178',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:178',
    role: 'alert',
    original:
      'v-if="actions.listError" class="error" role="alert" > {{ actions.listError.message }}<span v-if="actions.listError.trace_id"> 追踪编号：{{ actions.listError.trace_id }}</span><span v-if="actions.items.length"> 当前保留上次成功读取的列表。</span> </p> <p',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionsPage:302',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:302',
    role: 'status',
    original:
      '</button> <p role="status" aria-live="polite" > {{ connectionLabels[connectionState] }}<span v-if="selected"> · {{ actionStatusLabel(selected.status) }}</span> </p> <p',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionsPage:311',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:311',
    role: 'status',
    original:
      '<p v-if="actions.snapshotLoading[selectedTaskId]" role="status" > 正在恢复操作快照… </p> <p v-if="actions.snapshotErrors[selectedTaskId]" class="error" role="alert"',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ActionsPage:318',
    scenario: 'actionsPage',
    source: 'frontend/src/pages/ActionsPage.vue:318',
    role: 'alert',
    original:
      'v-if="actions.snapshotErrors[selectedTaskId]" class="error" role="alert" > {{ actions.snapshotErrors[selectedTaskId]?.message }}<span v-if="actions.snapshotErrors[selectedTaskId]?.trace_id"> 追踪编号：{{ actions.snapshotErrors[selectedTaskId]?.trace_id }}</span><button type="button"',
    currentComponent: 'ActionCenterWorkspace',
    migration: '2314cda',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:124',
    scenario: 'loading',
    source: 'frontend/src/pages/CalendarProposalPage.vue:124',
    role: 'status',
    original:
      '<p v-if="loading || catalogLoading" role="status" > 正在加载提案与日历目录… </p> <EditorRecovery :error=" error || reprepareError || restoreError || catalogError || creationError "',
    currentComponent: 'CalendarProposalPage',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:184',
    scenario: 'calendarFacts',
    source: 'frontend/src/pages/CalendarProposalPage.vue:184',
    role: 'status',
    original:
      '<p v-else role="status" > 恢复来源已不可用，请核对历史快照保留期和目标日程。 </p> </section> <CalendarTargetFields :model-value="form" :proposal="proposal"',
    currentComponent: 'CalendarProposalPage',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:203',
    scenario: 'calendarForm',
    source: 'frontend/src/pages/CalendarProposalPage.vue:203',
    role: 'status',
    original:
      '<p v-if="dirty" role="status" > 有未保存或未确认的修改，请保存后重新检查。 </p> <p v-if="sourceDirty" role="status" >',
    currentComponent: 'CalendarEditorForm',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:209',
    scenario: 'calendarForm',
    source: 'frontend/src/pages/CalendarProposalPage.vue:209',
    role: 'status',
    original:
      '<p v-if="sourceDirty" role="status" > 请先确认目标日历，再编辑其他字段。 </p> <div class="editor-controls"> <button type="button" name="save-proposal"',
    currentComponent: 'CalendarEditorForm',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:250',
    scenario: 'calendarFacts',
    source: 'frontend/src/pages/CalendarProposalPage.vue:250',
    role: 'alert',
    original:
      '<p v-if="proposal.editor_facts?.before_status === \'unavailable\'" role="alert" > 原始修改前快照已不可用，无法核对前后差异。请返回来源重新创建提案。 </p> <!-- 修改或恢复缺少原快照时只展示输入与错误，不能把 null before 误标为新建。 --> <CalendarFieldsComparison v-if=" after &&',
    currentComponent: 'CalendarProposalPage',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:272',
    scenario: 'calendarFacts',
    source: 'frontend/src/pages/CalendarProposalPage.vue:272',
    role: 'status',
    original:
      '<p v-else role="status" > 尚无当前输入的冲突检查，请补齐并保存后重新检查。未检查参会人可用性。 </p> <button type="button" name="suggest-times" :disabled="locked || dirty || !after || form.all_day"',
    currentComponent: 'CalendarProposalPage',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'CalendarProposalPage:290',
    scenario: 'calendarFacts',
    source: 'frontend/src/pages/CalendarProposalPage.vue:290',
    role: 'status',
    original:
      '<p v-if="candidates.completeness === \'partial\'" role="status" > 部分日历来源缺失（{{ candidates.missing_connections.length }} 个连接）。 </p> <MissingCalendarConnections v-if="candidates.completeness === \'partial\'" :connection-ids="candidates.missing_connections"',
    currentComponent: 'CalendarProposalPage',
    migration: 'ee0ad29',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ChatPage:41',
    scenario: 'chatPage',
    source: 'frontend/src/pages/ChatPage.vue:41',
    role: 'status',
    original:
      '<p v-if="loading" role="status" > 正在加载会话… </p> <div v-if="error" role="alert" >',
    currentComponent: 'ChatPage',
    migration: '4a11592',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ChatPage:47',
    scenario: 'chatPage',
    source: 'frontend/src/pages/ChatPage.vue:47',
    role: 'alert',
    original:
      '<div v-if="error" role="alert" > <p> {{ error.message }}<span v-if="error.traceId"> 追踪编号：{{ error.traceId }}</span> </p> <button type="button"',
    currentComponent: 'ChatPage',
    migration: '4a11592',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ChatPage:90',
    scenario: 'chatPage',
    source: 'frontend/src/pages/ChatPage.vue:90',
    role: 'status',
    original:
      'v-if="task" data-testid="chat-task-status" role="status" > 任务：{{ actionStatusLabel(task.status) }}{{ connectionState === \'connected\' ? \'\' : \' · 正在恢复实时连接\' }} </p> <p v-if="trustedId && actions.snapshotErrors[trustedId]" role="alert"',
    currentComponent: 'ChatPage',
    migration: '4a11592',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ChatPage:97',
    scenario: 'chatPage',
    source: 'frontend/src/pages/ChatPage.vue:97',
    role: 'alert',
    original:
      '<p v-if="trustedId && actions.snapshotErrors[trustedId]" role="alert" > 操作快照读取失败，请在操作中心重新加载。 </p> <ActionDetail v-if="action" :snapshot="action" :timezone="auth.user?.timezone ?? \'UTC\'"',
    currentComponent: 'ChatPage',
    migration: '4a11592',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:64',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:64',
    role: 'status',
    original:
      '<p v-if="loading" role="status" > 正在加载连接能力… </p> <p v-if="loaded && !entries.length" role="status" >',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:70',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:70',
    role: 'status',
    original:
      '<p v-if="loaded && !entries.length" role="status" > 尚未连接账户。请选择供应商开始只读授权。 </p> <p v-if="error || actionError" role="alert" >',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:76',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:76',
    role: 'alert',
    original:
      '<p v-if="error || actionError" role="alert" > {{ (actionError || error)?.message }} <span v-if="(actionError || error)?.traceId">追踪编号：{{ (actionError || error)?.traceId }}</span> </p> <p v-if="notice" role="status"',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:83',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:83',
    role: 'status',
    original:
      '<p v-if="notice" role="status" > {{ notice }} </p> <a v-if="authorization" ref="authorizationLink" data-testid="authorization-link"',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:102',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:102',
    role: 'status',
    original:
      '{{ entry.connection.account_email }} </h2> <p role="status"> 连接状态：{{ entry.connection.status }} </p> <p v-if="entry.error" role="alert" > 能力加载失败，请刷新后重试。<span v-if="entry.error.traceId">追踪编号：{{ entry.error.traceId }}</span>',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:107',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:107',
    role: 'alert',
    original:
      '<p v-if="entry.error" role="alert" > 能力加载失败，请刷新后重试。<span v-if="entry.error.traceId">追踪编号：{{ entry.error.traceId }}</span> </p> <CapabilityRows v-if="entry.capabilities" :capabilities="entry.capabilities" :busy="busy"',
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'ConnectionsPage:166',
    scenario: 'connectionsPage',
    source: 'frontend/src/pages/ConnectionsPage.vue:166',
    role: 'alert',
    original:
      "overflow-wrap: anywhere; } [role='alert'] { color: #a61b1b; } button:focus-visible, a:focus-visible { outline: 3px solid #164e9c; outline-offset: 3px; }",
    currentComponent: 'ConnectionsPage',
    migration: '51d025b',
    change: 'CSS 选择器非模板节点，排除且不建立替代 alert。',
    excluded: true,
  },
  {
    id: 'LoginPage:82',
    scenario: 'loginPage',
    source: 'frontend/src/pages/LoginPage.vue:82',
    role: 'alert',
    original:
      '<p v-if="error" role="alert" > {{ error }} </p> </form> </main> </template> ',
    currentComponent: 'LoginPage',
    migration: '110103d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'MailDraftPage:72',
    scenario: 'loading',
    source: 'frontend/src/pages/MailDraftPage.vue:72',
    role: 'status',
    original:
      '<p v-if="loading || catalogLoading" role="status" > 正在加载草稿与账户… </p> <EditorRecovery :error="error || catalogError || creationError" :busy="busy || creating" @reload="reload"',
    currentComponent: 'MailDraftPage',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'MailDraftPage:101',
    scenario: 'mailForm',
    source: 'frontend/src/pages/MailDraftPage.vue:101',
    role: 'status',
    original:
      '<p v-if="dirty" role="status" > 有未保存的输入，请先保存后审阅。 </p> <div class="editor-controls"> <button type="button" name="save-draft"',
    currentComponent: 'MailEditorForm',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'MailDraftPage:144',
    scenario: 'mailForm',
    source: 'frontend/src/pages/MailDraftPage.vue:144',
    role: 'status',
    original:
      "<p v-if=\"generationRunning\" role=\"status\" > 正在草拟，请等待服务端保存结果。{{ generationConnection === 'connected' ? '' : '连接恢复中，任务状态以服务端为准。' }} </p>",
    currentComponent: 'MailEditorForm',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'MailDraftPage:154',
    scenario: 'mailForm',
    source: 'frontend/src/pages/MailDraftPage.vue:154',
    role: 'alert',
    original:
      '<p v-if="generationFailed" role="alert" > 草拟失败，原草稿仍保留。请检查任务历史并重新加载后再试。 </p> <p v-if="draft.status !== \'editing\'" class="editor-note" >',
    currentComponent: 'MailEditorForm',
    migration: '674ae1d',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'SettingsPage:187',
    scenario: 'settingsPage',
    source: 'frontend/src/pages/SettingsPage.vue:187',
    role: 'alert',
    original:
      '<p v-if="error" role="alert" > {{ error }} </p>  <section aria-labelledby="privacy-heading"> <h2 id="privacy-heading"> 隐私与数据',
    currentComponent: 'SettingsPage',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'SettingsPage:236',
    scenario: 'settingsPage',
    source: 'frontend/src/pages/SettingsPage.vue:236',
    role: 'alert',
    original:
      '<p v-if="deletionError" role="alert" > {{ deletionError }} </p> </section>  <h2>活动会话</h2> <ul>',
    currentComponent: 'SettingsPage',
    migration: '7a138f3',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TasksPage:124',
    scenario: 'tasksPage',
    source: 'frontend/src/pages/TasksPage.vue:124',
    role: 'status',
    original:
      '</p> <template v-else> <p role="status"> 实时连接：{{ connectionState }} </p> <p v-if="loading" role="status" > 正在恢复任务快照…',
    currentComponent: 'TasksPage',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TasksPage:129',
    scenario: 'tasksPage',
    source: 'frontend/src/pages/TasksPage.vue:129',
    role: 'status',
    original:
      '<p v-if="loading" role="status" > 正在恢复任务快照… </p> <p v-if="error" role="alert" >',
    currentComponent: 'TasksPage',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TasksPage:135',
    scenario: 'tasksPage',
    source: 'frontend/src/pages/TasksPage.vue:135',
    role: 'alert',
    original:
      '<p v-if="error" role="alert" > {{ error }} </p> <template v-if="task"> <h2>{{ task.kind }}</h2> <p>当前状态：{{ task.status }}</p> <template v-if="task.kind === \'calendar.restore.prepare\'">',
    currentComponent: 'TasksPage',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TasksPage:143',
    scenario: 'tasksPage',
    source: 'frontend/src/pages/TasksPage.vue:143',
    role: 'status',
    original:
      '<p>当前状态：{{ task.status }}</p> <template v-if="task.kind === \'calendar.restore.prepare\'"> <p role="status"> 准备完成后仍需核对并提交新的审批，日程尚未因此恢复。 </p> <RouterLink v-if="restoreEditorUrl" :to="restoreEditorUrl" data-testid="restore-result" >',
    currentComponent: 'TasksPage',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TasksPage:155',
    scenario: 'tasksPage',
    source: 'frontend/src/pages/TasksPage.vue:155',
    role: 'alert',
    original:
      '<p v-else-if="task.status === \'succeeded\'" role="alert" > 恢复提案结果已不可用，请返回操作中心核对原修改和保留期。 </p> </template> <button v-if="canCancel" type="button"',
    currentComponent: 'TasksPage',
    migration: 'addc635',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TodayBriefPage:51',
    scenario: 'briefPage',
    source: 'frontend/src/pages/TodayBriefPage.vue:51',
    role: 'status',
    original:
      '<p v-if="loading" role="status" > 正在加载… </p> <p v-if="error" role="alert" >',
    currentComponent: 'TodayBriefPage',
    migration: '98b4cf6',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TodayBriefPage:57',
    scenario: 'briefPage',
    source: 'frontend/src/pages/TodayBriefPage.vue:57',
    role: 'alert',
    original:
      '<p v-if="error" role="alert" > {{ error }} </p> <EditorRecovery :error="creationError" :busy="creating" @reload="load"',
    currentComponent: 'TodayBriefPage',
    migration: '98b4cf6',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
  {
    id: 'TodayBriefPage:68',
    scenario: 'briefPage',
    source: 'frontend/src/pages/TodayBriefPage.vue:68',
    role: 'status',
    original:
      '<p v-if="creating" role="status" > 正在创建本地编辑对象… </p> <label v-if="versions.length">历史版本 <select :value="selected?.id" @change="select(($event.target as HTMLSelectElement).value)"',
    currentComponent: 'TodayBriefPage',
    migration: '98b4cf6',
    change:
      '保留原语义与文案；Message／抽取组件只有单个播报根，动态条件由运行时场景触发。',
    excluded: false,
  },
]

/** 新增与组件内置公告：与旧清单分列，必须实际触发后才进入 observed 集合。 */
export const additionalInventory = [
  {
    id: 'CalendarConflictNotice:polite',
    role: 'region',
    scenario: 'calendarSources',
    currentComponent: 'CalendarConflictNotice',
    change:
      '4ce84ae:15原section polite迁至唯一Message；缺失来源说明保持note/off。',
  },
  {
    id: 'AppShell:disconnect',
    role: 'status',
    scenario: 'shellAdditions',
    currentComponent: 'AppShell',
    change: '68f7dfe新增全局断线status，仅消费原连接投影。',
  },
  {
    id: 'AppShell:recoveryToast',
    role: 'status',
    scenario: 'shellAdditions',
    currentComponent: 'Toast',
    change: '68f7dfe重连成功产生一次polite提示；重复connected不新增。',
  },
  {
    id: 'AppShell:errorToast',
    role: 'alert',
    scenario: 'shellAdditions',
    currentComponent: 'Toast',
    change: '68f7dfe错误Toast仍assertive；不替代审批与人工决定。',
  },
  {
    id: 'AppNavigation:drawer',
    role: 'dialog',
    scenario: 'shellAdditions',
    currentComponent: 'AppNavigation',
    change: '68f7dfe窄屏导航按需挂载一个真实模态Drawer。',
  },
  {
    id: 'TimelineDrawer:drawer',
    role: 'dialog',
    scenario: 'shellAdditions',
    currentComponent: 'TimelineDrawer',
    change: '68f7dfe窄屏时间线按需挂载一个真实模态Drawer；无第二份时间线。',
  },
  {
    id: 'AppShell:confirmation',
    role: 'alertdialog',
    scenario: 'shellAdditions',
    currentComponent: 'ConfirmDialog',
    change: '68f7dfe唯一确认出口，页面请求UI确认时才出现。',
  },
  {
    id: 'TaskHistoryList:loading',
    role: 'status',
    scenario: 'historyAdditions',
    currentComponent: 'TaskHistoryList',
    change: 'addc635列表新增独立加载提示，不覆盖完整任务快照。',
  },
  {
    id: 'TaskHistoryList:newTask',
    role: 'status',
    scenario: 'historyAdditions',
    currentComponent: 'TaskHistoryList',
    change: 'addc635新增任务提示只提供显式刷新。',
  },
  {
    id: 'TaskHistoryList:error',
    role: 'alert',
    scenario: 'historyAdditions',
    currentComponent: 'TaskHistoryList',
    change: 'addc635列表错误独立于详情错误，保留旧页与恢复动作。',
  },
  {
    id: 'ApprovalCard:countdown',
    role: 'status',
    scenario: 'approval',
    currentComponent: 'ApprovalCard',
    change: 'd447eb9新增审批倒计时polite，不重复静态风险说明。',
  },
  {
    id: 'Select:selected',
    role: 'status',
    scenario: 'sources',
    currentComponent: 'EditorConnectionSelect/Select',
    change: '选项弹层新增内置选择反馈；仅弹层打开时存在。',
  },
  {
    id: 'Select:filter',
    role: 'status',
    scenario: 'settingsForm',
    currentComponent: 'WorkSettingsForm/Select',
    change: '有filter时增加局部结果反馈，空搜索不伪造业务空态。',
  },
  {
    id: 'AutoComplete:search',
    role: 'status',
    scenario: 'mailForm',
    currentComponent: 'MailDraftFields/AutoComplete',
    change: '674ae1d三收件人字段各一个搜索status；只查询本草稿。',
  },
  {
    id: 'AutoComplete:selected',
    role: 'status',
    scenario: 'mailForm',
    currentComponent: 'MailDraftFields/AutoComplete',
    change: '674ae1d当前打开的建议弹层增加一个选择反馈，不复制人数提示。',
  },
  {
    id: 'MailEditorForm:fieldErrors',
    role: 'alert',
    scenario: 'mailFieldAdditions',
    currentComponent: 'MailEditorForm/MailDraftFields',
    change: '674ae1d五个格式字段各一个Message，来自实际Form/zod提交校验。',
  },
  {
    id: 'WorkSettingsForm:fieldErrors',
    role: 'alert',
    scenario: 'settingsFieldAdditions',
    currentComponent: 'WorkSettingsForm',
    change:
      '7a138f3八个格式字段的错误Message；包含旧validation语义的提前投影，不能与旧兜底重复计数。',
  },
  {
    id: 'CalendarEditorForm:fieldErrors',
    role: 'alert',
    scenario: 'calendarFieldAdditions',
    currentComponent: 'CalendarEditorForm/CalendarProposalFields',
    change:
      'ee0ad29五字段格式错误；非法时区时不猜测日期错误，修正时区后再验证两个日期字段。',
  },
] as const

/**
 * 模块传输及页面确认公告沿用既有真实浏览器E2E，在本轮完整E2E结果中逐项验收。
 * 这里只登记追溯，不把“测试存在”或库存单测通过描述为这些场景已运行。
 */
export const browserInventory = [
  {
    component: 'ActionsPage',
    roles: ['status', 'alert'],
    messages: [
      '正在加载操作中心…',
      '操作中心加载失败，请重试。重试会重新加载页面。',
    ],
    test: 'e2e/actions.spec.ts: action workspace module failure exposes a full reload retry',
  },
  {
    component: 'SettingsPage',
    roles: ['status', 'alert'],
    messages: [
      '正在加载工作设置表单…',
      '工作设置表单加载失败，请重试。重试会重新加载页面。',
    ],
    test: 'e2e/settings.spec.ts: the heavy form is loaded on settings demand with visible loading and retry after module fetch failure',
  },
  {
    component: 'MailDraftPage',
    roles: ['status', 'alert'],
    messages: [
      '正在加载邮件表单…',
      '邮件表单加载失败，请重试。重试会重新加载页面。',
    ],
    test: 'e2e/mail-editor.spec.ts: mail form loads on demand and retries a failed module without stale loading or implicit writes',
  },
  {
    component: 'CalendarProposalPage',
    roles: ['status', 'alert'],
    messages: [
      '正在加载日程表单…',
      '日程表单加载失败，请重试。重试会重新加载页面。',
    ],
    test: 'e2e/calendar-editor.spec.ts: calendar form loads on demand and recovers only after an explicit retry',
  },
  {
    component: 'SettingsPage',
    roles: ['dialog'],
    messages: [
      '删除全部数据',
      '删除本地数据不能撤回已发送的邮件或已生效的日程变更。',
    ],
    test: 'e2e/settings.spec.ts: deletion Dialog traps keyboard focus, isolates the shell and restores the trigger on Escape',
  },
  {
    component: 'SettingsPage/ConfirmDialog',
    roles: ['alertdialog'],
    messages: ['工作设置尚未保存，确定离开？'],
    test: 'e2e/settings.spec.ts: dirty settings use the global confirmation and native unload boundary without losing input on cancel',
  },
  {
    component: 'ConnectionsPage/ConfirmDialog',
    roles: ['alertdialog'],
    messages: [
      '确定断开此连接？未认领操作会停止；已执行的邮件或日程不会撤回。',
    ],
    test: 'e2e/connections.spec.ts: disconnect confirmation keeps keyboard focus and only sends the confirmed account',
  },
  {
    component: 'CalendarRepreparePanel/ConfirmDialog',
    roles: ['alertdialog'],
    messages: [
      '准备新版本',
      '将重新读取来源并创建本地新提案。原提案和审批保留不变。新提案需要独立核对、逐项确认并提交审批。',
    ],
    test: 'e2e/calendar-reprepare.spec.ts: preparation confirmation cancels without requests and restores focus after Escape',
  },
  {
    component: 'NeedsAttentionPanel/ActionConfirmationDialog',
    roles: ['dialog', 'alert'],
    messages: [
      '此决定只记录人工结果，不会调用供应商写接口。',
      '版本或状态已变化，请重新加载后核对。',
    ],
    test: 'e2e/mail-editor.spec.ts: manual conflict stays perceptible in the active dialog and reloads the authoritative result',
  },
] as const

/** 页面离开确认使用同一全局出口；这两个既有真实组件测试由最终完整单测执行，非库存场景。 */
export const pageConfirmationInventory = [
  {
    component: 'MailDraftPage/ConfirmDialog',
    role: 'alertdialog',
    message: '草稿尚未保存，确定离开？',
    test: 'src/pages/MailDraftPage.spec.ts: cancels dirty navigation without requests, restores focus and allows explicit discard',
  },
  {
    component: 'CalendarProposalPage/ConfirmDialog',
    role: 'alertdialog',
    message: '提案尚未保存或确认，确定离开？',
    test: 'src/pages/CalendarPresentation.spec.ts: confirms discarding edits without sending requests and returns focus on Escape',
  },
] as const
