<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import Button from 'primevue/button'
import DataView from 'primevue/dataview'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import type {
  TaskHistoryFilters,
  TaskHistoryItem,
  TaskHistoryPage,
} from '@/api/taskHistory'
import { TASK_STATUSES, taskStatusPresentation } from '@/design/status'
import EmptyState from './EmptyState.vue'
import StatusTag from './StatusTag.vue'

/** 只读展示输入；错误已由页面转换为安全文案，链接只允许页面提供的站内地址。 */
const props = defineProps<{
  page: TaskHistoryPage | null
  filters: TaskHistoryFilters
  loading: boolean
  error: string | null
  newTaskHint: boolean
  taskHref?: (id: string) => string
}>()
/** 组件不读取 API、Router 或 Store；动作仅表达用户意图，不推断详情授权。 */
const emit = defineEmits<{
  filter: [filters: TaskHistoryFilters]
  next: []
  previous: []
  refresh: []
  select: [id: string]
}>()

/** 固定安全标签不读取命令或邮件正文；未知种类用普通文本保留兼容性。 */
const kinds = [
  { value: 'daily_brief', label: '每日简报', business: true },
  { value: 'conversation.respond', label: '对话回复', business: true },
  { value: 'mail_draft.generate', label: '邮件正文生成', business: true },
  { value: 'trusted_action', label: '可信操作（邮件／日程）', business: true },
  { value: 'calendar.restore.prepare', label: '恢复提案准备', business: true },
  { value: 'sync_mail', label: '邮件同步', business: false },
  { value: 'sync_gmail', label: '邮件同步（旧记录）', business: false },
  { value: 'sync_calendar', label: '日历同步', business: false },
  { value: 'brief.overdue_diagnostic', label: '简报逾期诊断', business: false },
  {
    value: 'privacy.clear_source_cache',
    label: '来源缓存清理',
    business: false,
  },
  { value: 'privacy.delete_all_data', label: '隐私数据清理', business: false },
  { value: 'other', label: '其他任务', business: false },
]
const draft = reactive<TaskHistoryFilters>({ ...props.filters })
watch(
  () => props.filters,
  (filters) => Object.assign(draft, filters),
)
const kindOptions = computed(() => [
  { value: null, label: '全部类型' },
  ...kinds.filter((kind) =>
    draft.scope === 'business'
      ? kind.business
      : draft.scope === 'background'
        ? !kind.business
        : true,
  ),
])
const statusOptions = [
  { value: null, label: '全部状态' },
  ...TASK_STATUSES.map((value) => ({
    value,
    label: taskStatusPresentation(value).label,
  })),
]
const filtered = computed(
  () =>
    props.filters.scope !== 'business' ||
    props.filters.kind !== null ||
    props.filters.status !== null ||
    props.filters.created_from_date !== null ||
    props.filters.created_to_date !== null,
)
const emptyTitle = computed(() =>
  props.page?.previous_cursor
    ? '本页已无可用记录'
    : filtered.value
      ? '没有符合筛选条件的任务'
      : '暂无任务历史',
)

/** 提交民用日期字符串；空输入代表无边界，由既有状态层/服务端验证真实日期与时区。 */
function applyFilters(): void {
  emit('filter', {
    ...draft,
    scope: draft.kind === 'other' ? 'all' : draft.scope,
    created_from_date: draft.created_from_date || null,
    created_to_date: draft.created_to_date || null,
  })
}
/** 主视图切换清除可能互斥的类型，保留当前已应用状态/日期。 */
function changeScope(scope: 'business' | 'all'): void {
  emit('filter', { ...props.filters, scope, kind: null })
}
/** 失败计数的范围独立于业务类型/状态，后台范围明确含 other；不触发重试。 */
function showBackgroundFailures(): void {
  emit('filter', {
    ...props.filters,
    scope: 'background',
    kind: null,
    status: 'failed',
  })
}
/** @param kind 已收窄的服务端任务种类。@returns 固定标签或纯文本兼容说明，不读取敏感载荷。 */
function taskKindLabel(kind: string): string {
  return (
    kinds.find((entry) => entry.value === kind)?.label ?? `其他任务（${kind}）`
  )
}
/** 链接以完整身份定位；短前缀仅供视觉浏览，辅助技术名称不会产生前缀歧义。 */
function linkHref(id: string): string {
  return props.taskHref?.(id) ?? `/tasks?task_id=${encodeURIComponent(id)}`
}
/** 创建/读取时间始终显式使用响应中的用户时区，格式失败不退回宿主时区。 */
function timeText(value: string): string {
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: props.page?.filter_timezone ?? 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(new Date(value))
  } catch {
    return '时间未知'
  }
}
/** 执行时长以开始时间为准；运行中只使用这次读取的 server_time，不启动行计时器。 */
function durationText(item: TaskHistoryItem): string {
  if (item.started_at === null) return '尚未开始'
  const end = item.finished_at ?? props.page?.server_time
  if (!end) return '耗时未知'
  const duration = Date.parse(end) - Date.parse(item.started_at)
  if (!Number.isFinite(duration) || duration < 0) return '耗时未知'
  if (item.finished_at !== null) return `耗时 ${Math.round(duration / 1000)} 秒`
  return ['succeeded', 'failed', 'cancelled'].includes(item.status)
    ? '耗时未知'
    : `已运行 ${Math.round(duration / 1000)} 秒`
}
</script>

<template>
  <section
    aria-label="任务历史列表"
    class="flex min-w-0 flex-col gap-4"
  >
    <div
      class="flex flex-wrap gap-2"
      role="group"
      aria-label="任务范围"
    >
      <Button
        label="业务任务"
        :outlined="filters.scope !== 'business'"
        :aria-pressed="filters.scope === 'business'"
        :disabled="loading"
        @click="changeScope('business')"
      />
      <Button
        label="全部任务"
        :outlined="filters.scope === 'business'"
        :aria-pressed="filters.scope !== 'business'"
        :disabled="loading"
        @click="changeScope('all')"
      />
      <Button
        label="刷新任务历史"
        severity="secondary"
        :disabled="loading"
        @click="emit('refresh')"
      />
    </div>
    <p
      v-if="filters.scope === 'background'"
      class="text-muted-color"
    >
      当前范围：后台任务（包括其他任务）
    </p>
    <form
      class="grid grid-cols-1 gap-3 sm:grid-cols-2"
      aria-label="筛选任务历史"
      @submit.prevent="applyFilters"
    >
      <div class="flex min-w-0 flex-col gap-1">
        <label for="history-kind">任务类型</label>
        <Select
          v-model="draft.kind"
          input-id="history-kind"
          aria-label="任务类型"
          :options="kindOptions"
          option-label="label"
          option-value="value"
        />
      </div>
      <div class="flex min-w-0 flex-col gap-1">
        <label for="history-status">任务状态</label>
        <Select
          v-model="draft.status"
          input-id="history-status"
          aria-label="任务状态"
          :options="statusOptions"
          option-label="label"
          option-value="value"
        />
      </div>
      <div class="flex min-w-0 flex-col gap-1">
        <label for="history-from">创建起始日期</label>
        <InputText
          id="history-from"
          v-model="draft.created_from_date"
          type="date"
        />
      </div>
      <div class="flex min-w-0 flex-col gap-1">
        <label for="history-to">创建结束日期</label>
        <InputText
          id="history-to"
          v-model="draft.created_to_date"
          type="date"
        />
      </div>
      <Button
        type="submit"
        label="应用筛选"
        :disabled="loading"
        class="justify-self-start"
      />
    </form>
    <div
      v-if="
        filters.scope === 'business' && page && page.background_failed_count > 0
      "
      class="flex flex-col items-start gap-2"
    >
      <Button
        :label="`当前时间范围内有 ${page.background_failed_count} 条后台失败记录`"
        severity="secondary"
        text
        :disabled="loading"
        @click="showBackgroundFailures"
      />
      <p class="text-sm text-muted-color">
        包括其他任务，不受类型和状态筛选限制；这是失败记录数，不代表未处理故障，也不会自动重试。
      </p>
    </div>
    <Message
      v-if="loading"
      role="status"
      aria-live="polite"
      severity="info"
    >
      正在加载任务历史…
    </Message>
    <Message
      v-if="newTaskHint"
      role="status"
      aria-live="polite"
      severity="info"
    >
      <Button
        label="有新任务，点击刷新"
        text
        :disabled="loading"
        @click="emit('refresh')"
      />
    </Message>
    <Message
      v-if="error"
      severity="error"
      role="alert"
    >
      {{ page ? '刷新失败，仍显示上次读取的数据。' : '任务历史加载失败。'
      }}{{ error }}
      <Button
        label="重新加载第一页"
        severity="secondary"
        :disabled="loading"
        @click="emit('refresh')"
      />
    </Message>
    <p
      v-if="page"
      class="text-sm text-muted-color"
    >
      列表读取时间：{{ timeText(page.server_time) }}（{{
        page.filter_timezone
      }}）；创建日期按此时区筛选。
    </p>
    <DataView
      v-if="page?.items.length"
      :value="page.items"
      data-key="id"
    >
      <template #list="{ items }: { items: TaskHistoryItem[] }">
        <ul
          aria-label="任务历史"
          class="m-0 flex list-none flex-col divide-y divide-surface-200 p-0"
        >
          <li
            v-for="item in items"
            :key="item.id"
            class="flex min-w-0 flex-col gap-2 py-4"
          >
            <div class="flex flex-wrap items-center gap-2">
              <a
                :href="linkHref(item.id)"
                :aria-label="`查看任务 ${item.id}`"
                class="rounded text-primary underline focus-visible:outline-2"
                @click.prevent="emit('select', item.id)"
              >{{ taskKindLabel(item.kind) }}</a>
              <StatusTag
                kind="task"
                :value="item.status"
              />
              <span class="font-mono text-sm text-muted-color">{{
                item.id.slice(0, 8)
              }}</span>
            </div>
            <p class="text-sm">
              创建时间：<time :datetime="item.created_at">{{
                timeText(item.created_at)
              }}</time>
              · <span>{{ durationText(item) }}</span>
            </p>
            <a
              v-if="item.retry_of_task_id"
              :href="linkHref(item.retry_of_task_id)"
              :aria-label="`查看重试来源 ${item.retry_of_task_id}`"
              class="rounded text-primary underline focus-visible:outline-2"
              @click.prevent="emit('select', item.retry_of_task_id)"
            >重试自 {{ item.retry_of_task_id.slice(0, 8) }}</a>
          </li>
        </ul>
      </template>
    </DataView>
    <EmptyState
      v-else-if="page && !loading && !error"
      :title="emptyTitle"
      description="请选择其他筛选条件，或重新加载最新任务历史。"
      action-label="重新加载第一页"
      @action="emit('refresh')"
    />
    <nav
      aria-label="任务历史分页"
      class="flex flex-wrap gap-3"
    >
      <Button
        label="上一页"
        severity="secondary"
        :disabled="loading || !!error || !page?.previous_cursor"
        @click="emit('previous')"
      />
      <Button
        label="下一页"
        severity="secondary"
        :disabled="loading || !!error || !page?.next_cursor"
        @click="emit('next')"
      />
    </nav>
  </section>
</template>
