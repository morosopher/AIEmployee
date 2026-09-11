<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import type { CalendarConfirmation } from '@/api/types'
import { useConnectionCatalog } from '@/composables/useConnectionCatalog'
import { useCalendarProposalEditor } from '@/features/calendar/useCalendarProposalEditor'
import { useCalendarRestore } from '@/features/calendar/useCalendarRestore'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'
import {
  actionStatusLabel,
  formatActionTime,
} from '@/features/actions/presentation'
import LocalEditorFrame from '@/components/LocalEditorFrame.vue'
import CalendarProposalFields from '@/components/CalendarProposalFields.vue'
import CalendarTargetFields from '@/components/CalendarTargetFields.vue'
import CalendarFieldsComparison from '@/components/CalendarFieldsComparison.vue'
import CalendarConflictNotice from '@/components/CalendarConflictNotice.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import LinkedActionPanel from '@/components/LinkedActionPanel.vue'

/** 页面只组合服务端前后值、确认与冲突事实；不实现排程算法或本地审批状态机。 */
const route = useRoute(),
  router = useRouter()
const proposalId = computed(() =>
  typeof route.params.proposalId === 'string' ? route.params.proposalId : '',
)
const {
  entries,
  loading: catalogLoading,
  error: catalogError,
  load: loadCatalog,
} = useConnectionCatalog()
const {
  proposal,
  form,
  loading,
  busy,
  locked,
  error,
  dirty,
  fieldsDirty,
  sourceDirty,
  conflicts,
  candidates,
  after,
  canSubmit,
  reload,
  save,
  confirm,
  suggest,
  choose,
  submit,
} = useCalendarProposalEditor(proposalId, (taskId) =>
  router.push({ path: '/actions', query: { task: taskId } }),
)
const {
  source: restoreSource,
  busy: restoreBusy,
  error: restoreError,
  prepare: prepareRestore,
} = useCalendarRestore(proposal, busy, (taskId) =>
  router.push({ path: '/tasks', query: { task_id: taskId } }),
)
const {
  newCalendar,
  error: creationError,
  busy: creating,
} = useLocalActionCreation()
const confirmations: Array<{ kind: CalendarConfirmation; label: string }> = [
  { kind: 'time', label: '时间' },
  { kind: 'attendees', label: '参会人' },
  { kind: 'notification_policy', label: '通知策略' },
]
onMounted(() => {
  void loadCatalog()
})
</script>
<template>
  <LocalEditorFrame title="日程提案">
    <RouterLink to="/actions">
      返回操作中心
    </RouterLink>
    <p class="editor-note">
      保存和确认只修改本地提案。提交审批后，请核对冻结的时间、目标日历和通知策略。
    </p>
    <p
      v-if="loading || catalogLoading"
      role="status"
    >
      正在加载提案与日历目录…
    </p>
    <EditorRecovery
      :error="error || restoreError || catalogError || creationError"
      :busy="busy || restoreBusy || creating"
      @reload="reload"
      @new-object="newCalendar"
    />
    <button
      type="button"
      name="reload-editor"
      :disabled="busy || restoreBusy"
      @click="reload"
    >
      重新加载提案
    </button>
    <template v-if="proposal">
      <p>
        版本 {{ proposal.version }} · {{ actionStatusLabel(proposal.status) }}
      </p>
      <section
        v-if="
          proposal.operation_kind === 'update' && proposal.status === 'applied'
        "
        aria-label="恢复已应用修改"
      >
        <p>恢复会准备新的提案，读取当前日程后仍需核对通知策略并重新审批。</p>
        <button
          v-if="restoreSource"
          type="button"
          name="prepare-restore"
          :disabled="busy || restoreBusy"
          @click="prepareRestore"
        >
          {{ restoreBusy ? '正在创建准备任务…' : '准备恢复提案' }}
        </button>
        <p
          v-else
          role="status"
        >
          恢复来源已不可用，请核对历史快照保留期和目标日程。
        </p>
      </section>
      <CalendarTargetFields
        :model-value="form"
        :proposal="proposal"
        :entries="entries"
        :disabled="locked"
        :fields-dirty="fieldsDirty"
        @confirm="confirm('calendar')"
      />
      <CalendarProposalFields
        :model-value="form"
        :disabled="locked"
      />
      <p
        v-if="dirty"
        role="status"
      >
        有未保存或未确认的修改，请保存后重新检查。
      </p>
      <p
        v-if="sourceDirty"
        role="status"
      >
        请先确认目标日历，再编辑其他字段。
      </p>
      <div class="editor-controls">
        <button
          type="button"
          name="save-proposal"
          :disabled="locked || !fieldsDirty || sourceDirty"
          @click="save"
        >
          保存提案
        </button>
      </div>
      <section aria-label="逐项确认">
        <h2>确认当前已保存版本</h2>
        <p>待确认：{{ proposal.required_confirmations.length }} 项</p>
        <div class="editor-controls">
          <button
            v-for="item in confirmations"
            :key="item.kind"
            type="button"
            :name="`confirm-${item.kind}`"
            :disabled="
              locked ||
                dirty ||
                !proposal.required_confirmations.includes(item.kind)
            "
            @click="confirm(item.kind)"
          >
            确认{{ item.label
            }}{{
              proposal.required_confirmations.includes(item.kind)
                ? ''
                : '（已确认）'
            }}
          </button>
        </div>
      </section>
      <p
        v-if="proposal.editor_facts?.before_status === 'unavailable'"
        role="alert"
      >
        原始修改前快照已不可用，无法核对前后差异。请返回来源重新创建提案。
      </p>
      <!-- 修改或恢复缺少原快照时只展示输入与错误，不能把 null before 误标为新建。 -->
      <CalendarFieldsComparison
        v-if="
          after &&
            (proposal.operation_kind === 'create' ||
              proposal.editor_facts?.before_status === 'available')
        "
        :before="proposal.editor_facts?.before ?? null"
        :after="after"
      />
      <CalendarConflictNotice
        v-if="conflicts !== null"
        data-testid="calendar-conflicts"
        :conflicts="conflicts"
      />
      <p
        v-else
        role="status"
      >
        尚无当前输入的冲突检查，请补齐并保存后重新检查。未检查参会人可用性。
      </p>
      <button
        type="button"
        name="suggest-times"
        :disabled="locked || dirty || !after || form.all_day"
        @click="suggest"
      >
        查询三个候选时间
      </button>
      <section
        v-if="candidates"
        aria-label="服务端候选时间"
      >
        <p
          v-if="candidates.completeness === 'partial'"
          role="status"
        >
          部分日历来源缺失（{{ candidates.missing_connections.length }}
          个连接）。
        </p>
        <p>未检查参会人可用性。选择候选后仍需保存并确认。</p>
        <p v-if="!candidates.candidates.length">
          当前范围没有可用候选，可调整时间后重试。
        </p>
        <div class="editor-controls">
          <button
            v-for="candidate in candidates.candidates"
            :key="candidate.starts_at"
            type="button"
            name="choose-candidate"
            :disabled="locked"
            @click="choose(candidate)"
          >
            {{ formatActionTime(candidate.starts_at, form.timezone) }} 至
            {{ formatActionTime(candidate.ends_at, form.timezone) }}（{{
              form.timezone
            }}）
          </button>
        </div>
      </section>
      <p
        v-if="form.notification_policy === 'none'"
        class="editor-note"
      >
        不发送通知仍可能触发供应商的外部同步；请在审批预览中核对供应商警告。
      </p>
      <p
        v-if="
          proposal.operation_kind !== 'create' &&
            !proposal.changed_fields.length
        "
      >
        尚无真实字段变更，不能提交空修改。
      </p>
      <button
        type="button"
        name="submit-proposal"
        :disabled="!canSubmit"
        @click="submit"
      >
        提交审批
      </button>
      <LinkedActionPanel
        :local-id="proposal.id"
        item-kind="calendar_proposal"
        :task-query="route.query.task"
        @changed="reload"
      />
    </template>
  </LocalEditorFrame>
</template>
