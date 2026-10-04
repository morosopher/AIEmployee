<script setup lang="ts">
import { nextTick, ref, computed } from 'vue'
import { breakpointsTailwind, useBreakpoints } from '@vueuse/core'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Paginator from 'primevue/paginator'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import Splitter from 'primevue/splitter'
import SplitterPanel from 'primevue/splitterpanel'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'
import Tag from 'primevue/tag'
import StatusTag from '@/components/StatusTag.vue'
import EmptyState from '@/components/EmptyState.vue'
import type {
  ActionListItem,
  ActionProvider,
  ActionItemKind,
  ActionStatus,
} from '@/api/types'
import { RouterLink } from 'vue-router'
import ActionDetail from '@/components/ActionDetail.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import { useActionCenter } from '@/composables/useActionCenter'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'
import {
  actionLabel,
  actionStatusLabel,
  formatActionTime,
  providerLabel,
} from '@/features/actions/presentation'

/** 操作中心页面只负责组合语义化区域、键盘焦点与窄屏独立详情。 */
const {
  actions,
  provider,
  itemKind,
  status,
  selectedTaskId,
  selected,
  timezone,
  groups,
  connectionState,
  refresh,
  selectTask,
  closeDetail,
  changePage,
} = useActionCenter()
const {
  busy: creating,
  error: creationError,
  newMail,
  newCalendar,
} = useLocalActionCreation()
const detailRegion = ref<HTMLElement | null>(null)
const listRegion = ref<HTMLElement | null>(null)
const pageHeading = ref<HTMLElement | null>(null)
const connectionLabels = {
  connecting: '正在建立实时连接',
  connected: '实时连接正常',
  reconnecting: '正在重新连接',
  disconnected: '实时连接已断开',
}

/**
 * @param taskId 真实任务 ID。
 * @returns 详情区域成为键盘焦点。
 */
async function openDetail(taskId: string): Promise<void> {
  selectTask(taskId)
  await nextTick()
  detailRegion.value?.focus()
}
/**
 * 关闭详情后按已验证 UUID 重新定位当前任务按钮，不能缓存 lazy Tabs 已卸载的 DOM。
 * nextTick 让窄屏列表先恢复可见；lazy 只挂载当前分组，因此查询不会命中后台隐藏分组。
 * 任务已移出当前页或用户保留其他分组时，聚焦始终可见的页面标题，不擅自切组或请求数据。
 * @returns 焦点归还当前任务按钮；没有可见按钮则落到操作中心标题，卸载后不再操作 DOM。
 */
async function closeSelected(): Promise<void> {
  const taskId = selectedTaskId.value
  closeDetail()
  await nextTick()
  const trigger = taskId
    ? listRegion.value?.querySelector<HTMLButtonElement>(
        `button[data-action-task="${taskId}"]`,
      )
    : null
  const focusTarget = trigger ?? pageHeading.value
  focusTarget?.focus()
}

/** 本地展示状态只控制当前页可见分组；服务端刷新不能改写它或移动键盘焦点。 */
const activeGroup = ref('drafts')
const desktop = useBreakpoints(breakpointsTailwind).greaterOrEqual('xl')
/** 小屏以独立区域呈现；隐藏分隔器而不卸载详情及审批控件。 */
const splitterPassThrough = computed(() => ({
  gutter: { class: desktop.value && selectedTaskId.value ? '' : 'hidden' },
  gutterHandle: {
    'aria-label': '调整操作列表与详情宽度',
    ...(desktop.value && selectedTaskId.value
      ? {}
      : { role: 'presentation', tabindex: -1 }),
  },
}))
/** @param item 已验证列表项。@returns 仅高亮当前任务，不引入额外选择或持久化。 */
function rowClass(item: ActionListItem): string {
  return item.task_id === selectedTaskId.value && selectedTaskId.value !== null
    ? 'bg-primary-50'
    : ''
}
/** @param item 已验证联合列表项。@returns 沿用旧列表的类型＋ID稳定键，刷新重排后保持任务按钮与焦点归属。 */
function rowKey(item: ActionListItem): string {
  return `${item.item_kind}:${item.id}`
}
const providerOptions: Array<{ label: string; value: ActionProvider | '' }> = [
  { label: '全部供应商', value: '' },
  { label: 'Google', value: 'google' },
  { label: 'Microsoft', value: 'microsoft' },
]
const kindOptions: Array<{ label: string; value: ActionItemKind | '' }> = [
  { label: '全部类型', value: '' },
  { label: '邮件草稿', value: 'mail_draft' },
  { label: '日程提案', value: 'calendar_proposal' },
  { label: '可信任务', value: 'trusted_task' },
]
/** 相同文案对应的本地对象/可信任务状态仍保留原精确值，不在 UI 合并领域状态。 */
const statusOptions: Array<{ label: string; value: ActionStatus | '' }> = [
  { label: '全部状态', value: '' },
  { label: '编辑中', value: 'editing' },
  { label: '需要重新检查', value: 'stale' },
  { label: '已创建', value: 'created' },
  { label: '已排队', value: 'queued' },
  { label: '本地对象待审批', value: 'awaiting_approval' },
  { label: '任务待审批', value: 'waiting_approval' },
  { label: '本地对象执行中', value: 'executing' },
  { label: '任务执行中', value: 'running' },
  { label: '等待安全重试', value: 'retry_scheduled' },
  { label: '正在核对', value: 'reconciling' },
  { label: '需要人工确认', value: 'needs_attention' },
  { label: '已完成', value: 'succeeded' },
  { label: '已发送', value: 'sent' },
  { label: '已应用', value: 'applied' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
]
/**
 * Live region 迁移记录：原 ActionsPage 的四个 status（创建、列表、连接、快照）和两个
 * alert（列表、快照错误）原文移入本组件并用 Message 承载；同一条消息不再套额外 role。
 * Skeleton/Tag/Panel 不增加 live region，分组只渲染当前标签，异步模块反馈由页面宿主负责。
 */
</script>
<template>
  <section
    class="min-w-0 space-y-4"
    aria-labelledby="actions-title"
  >
    <header class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1
          id="actions-title"
          ref="pageHeading"
          tabindex="-1"
          class="text-2xl font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          操作中心
        </h1>
        <p class="mt-2 text-muted-color">
          集中查看本地草稿、日程提案与执行进度。
        </p>
      </div>
      <Button
        label="刷新操作"
        :disabled="actions.loading"
        @click="refresh"
      />
    </header>
    <div class="flex flex-wrap gap-3">
      <Button
        label="新邮件"
        :disabled="creating"
        @click="newMail"
      />
      <Button
        label="新日程"
        severity="secondary"
        outlined
        :disabled="creating"
        @click="newCalendar"
      />
    </div>
    <Message
      v-if="creating"
      severity="secondary"
      role="status"
      aria-live="polite"
    >
      正在创建本地编辑对象…
    </Message>
    <EditorRecovery
      :error="creationError"
      :busy="creating"
      @reload="refresh"
    />
    <!-- 单一 Splitter/详情实例跨断点保留：只改变布局与可见性，绝不重挂 hooks 或复制审批控制。 -->
    <Splitter
      :layout="desktop ? 'horizontal' : 'vertical'"
      class="min-w-0 border-0 bg-transparent"
      :pt="splitterPassThrough"
    >
      <SplitterPanel
        :size="60"
        :min-size="30"
        class="min-w-0"
        :class="{
          'hidden md:block': selectedTaskId,
          '!basis-full': !selectedTaskId,
          '!basis-auto': !desktop,
        }"
      >
        <div
          ref="listRegion"
          class="min-w-0 space-y-4"
          :class="{ 'xl:pr-4': selectedTaskId }"
        >
          <form
            class="flex flex-wrap gap-3"
            aria-label="操作筛选"
            @submit.prevent
          >
            <div class="grid min-w-0 flex-1 gap-1">
              <label for="action-provider">供应商筛选</label>
              <Select
                v-model="provider"
                input-id="action-provider"
                aria-label="供应商筛选"
                placeholder="全部供应商"
                :options="providerOptions"
                option-label="label"
                option-value="value"
              />
            </div>
            <div class="grid min-w-0 flex-1 gap-1">
              <label for="action-kind">操作类型筛选</label>
              <Select
                v-model="itemKind"
                input-id="action-kind"
                aria-label="操作类型筛选"
                placeholder="全部类型"
                :options="kindOptions"
                option-label="label"
                option-value="value"
              />
            </div>
            <div class="grid min-w-0 flex-1 gap-1">
              <label for="action-status">操作状态筛选</label>
              <Select
                v-model="status"
                input-id="action-status"
                aria-label="操作状态筛选"
                placeholder="全部状态"
                :options="statusOptions"
                option-label="label"
                option-value="value"
              />
            </div>
          </form>
          <Message
            severity="secondary"
            role="status"
            aria-live="polite"
          >
            {{
              actions.loading || (!actions.loaded && !actions.listError)
                ? '正在加载操作…'
                : actions.loaded
                  ? `当前页 ${actions.items.length} 项操作`
                  : '尚未取得操作列表'
            }}
          </Message>
          <Skeleton
            v-if="actions.loading"
            height="4rem"
          />
          <Message
            v-if="actions.listError"
            severity="error"
            role="alert"
          >
            {{ actions.listError.message
            }}<span v-if="actions.listError.trace_id">
              追踪编号：{{ actions.listError.trace_id }}</span><span v-if="actions.items.length">
              当前保留上次成功读取的列表。</span>
          </Message>
          <EmptyState
            v-if="
              actions.loaded &&
                !actions.loading &&
                !actions.listError &&
                !actions.items.length
            "
            title="暂无操作。草稿、提案和任务将在这里集中展示。"
          />
          <!-- 标签计数只统计当前页。用户选定的标签不随 REST/SSE 刷新跳转，也不转写 API status。 -->
          <Tabs
            v-model:value="activeGroup"
            lazy
            scrollable
          >
            <TabList :pt="{ tabList: { 'aria-label': '当前页操作分组' } }">
              <Tab
                v-for="group in groups"
                :key="group.id"
                :value="group.id"
              >
                {{ group.label
                }}{{ actions.loaded ? ` ${group.items.length}` : '' }}
              </Tab>
            </TabList>
            <TabPanels class="px-0">
              <TabPanel
                v-for="group in groups"
                :key="group.id"
                :value="group.id"
              >
                <DataTable
                  v-if="group.items.length"
                  :value="group.items"
                  :data-key="rowKey"
                  :row-class="rowClass"
                  :table-props="{ 'aria-label': `${group.label}操作列表` }"
                  class="min-w-0"
                  :pt="{ tableContainer: { class: 'overflow-x-auto' } }"
                >
                  <Column
                    header="操作"
                    :pt="{ headerCell: { scope: 'col' } }"
                  >
                    <template #body="{ data }: { data: ActionListItem }">
                      <strong>{{ actionLabel(data.action) }}</strong><span class="mt-1 block text-sm text-muted-color">{{
                        providerLabel(data.provider)
                      }}</span>
                    </template>
                  </Column>
                  <Column
                    header="状态"
                    :pt="{ headerCell: { scope: 'col' } }"
                  >
                    <template #body="{ data }: { data: ActionListItem }">
                      <StatusTag
                        kind="action"
                        :value="data.status"
                      />
                    </template>
                  </Column>
                  <Column
                    header="风险"
                    :pt="{ headerCell: { scope: 'col' } }"
                  >
                    <template #body="{ data }: { data: ActionListItem }">
                      <Tag
                        v-if="data.risk_level"
                        :value="
                          data.risk_level === 'high' ? '高风险' : '中风险'
                        "
                        severity="warn"
                      /><span v-else>—</span>
                    </template>
                  </Column>
                  <Column
                    header="最近更新"
                    :pt="{ headerCell: { scope: 'col' } }"
                  >
                    <template #body="{ data }: { data: ActionListItem }">
                      <time
                        :datetime="data.updated_at"
                        class="text-sm text-muted-color"
                      >{{ formatActionTime(data.updated_at, timezone) }}</time>
                    </template>
                  </Column>
                  <Column
                    header="查看或编辑"
                    :pt="{ headerCell: { scope: 'col' } }"
                  >
                    <template #body="{ data }: { data: ActionListItem }">
                      <Button
                        v-if="data.item_kind === 'trusted_task'"
                        label="查看详情"
                        :aria-label="`查看${actionLabel(data.action)}详情`"
                        :aria-pressed="data.task_id === selectedTaskId"
                        :data-action-task="data.task_id"
                        link
                        @click="openDetail(data.task_id)"
                      />
                      <Button
                        v-else
                        :as="RouterLink"
                        :to="data.editor_url"
                        :label="
                          data.item_kind === 'mail_draft'
                            ? '编辑本地草稿'
                            : '编辑本地提案'
                        "
                        link
                      />
                    </template>
                  </Column>
                </DataTable>
                <EmptyState
                  v-else-if="actions.loaded && !actions.listError"
                  title="此分组暂无操作"
                />
              </TabPanel>
            </TabPanels>
          </Tabs>
          <!--
            API 没有 total，因此使用 Paginator 的 headless first/rows，完全不提供或显示总数。
            4.5.5 container slot 的 first 是 1-based，减一后才是原 offset；rows 就是原 limit。
            不调用预先移动内部页码的 callbacks：只有成功 REST 才更新 first，失败后重试仍读同一页。
            两个按钮沿用原 offset/items.length 边界，满页仍允许继续读取空页。
          -->
          <Paginator
            v-if="actions.offset > 0 || actions.items.length === actions.limit"
            :first="actions.offset"
            :rows="actions.limit"
            aria-label="操作分页"
          >
            <template #container="{ first, rows }">
              <div class="flex gap-3">
                <Button
                  label="上一页"
                  severity="secondary"
                  outlined
                  :disabled="actions.loading || actions.offset === 0"
                  @click="changePage(Math.max(0, first - 1 - rows))"
                />
                <Button
                  label="下一页"
                  severity="secondary"
                  outlined
                  :disabled="
                    actions.loading || actions.items.length < actions.limit
                  "
                  @click="changePage(first - 1 + rows)"
                />
              </div>
            </template>
          </Paginator>
        </div>
      </SplitterPanel>
      <SplitterPanel
        v-show="selectedTaskId"
        :size="40"
        :min-size="30"
        class="min-w-0"
        :class="{ '!basis-auto': !desktop }"
      >
        <aside
          v-if="selectedTaskId"
          ref="detailRegion"
          class="min-w-0 space-y-4 rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary md:mt-4 xl:mt-0 xl:pl-4"
          tabindex="-1"
          aria-label="操作详情与时间线"
        >
          <Button
            label="返回操作列表"
            aria-label="关闭操作详情"
            severity="secondary"
            outlined
            @click="closeSelected"
          />
          <Message
            severity="secondary"
            role="status"
            aria-live="polite"
          >
            {{ connectionLabels[connectionState]
            }}<span v-if="selected">
              · {{ actionStatusLabel(selected.status) }}</span>
          </Message>
          <Message
            v-if="actions.snapshotLoading[selectedTaskId]"
            severity="secondary"
            role="status"
            aria-live="polite"
          >
            正在恢复操作快照…
          </Message>
          <Message
            v-if="actions.snapshotErrors[selectedTaskId]"
            severity="error"
            role="alert"
          >
            {{ actions.snapshotErrors[selectedTaskId]?.message
            }}<span v-if="actions.snapshotErrors[selectedTaskId]?.trace_id">
              追踪编号：{{
                actions.snapshotErrors[selectedTaskId]?.trace_id
              }}</span><Button
              label="重试详情"
              severity="secondary"
              @click="refresh"
            />
          </Message>
          <ActionDetail
            v-if="selected"
            :snapshot="selected"
            :timezone="timezone"
          />
        </aside>
      </SplitterPanel>
    </Splitter>
  </section>
</template>
