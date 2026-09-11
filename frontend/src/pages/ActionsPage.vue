<script setup lang="ts">
import { nextTick, ref } from 'vue'
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
let selectionTrigger: HTMLElement | null = null
const connectionLabels = {
  connecting: '正在建立实时连接',
  connected: '实时连接正常',
  reconnecting: '正在重新连接',
  disconnected: '实时连接已断开',
}

/**
 * @param taskId 真实任务 ID。
 * @param event 选择触发器。
 * @returns 详情区域成为键盘焦点。
 */
async function openDetail(taskId: string, event: Event): Promise<void> {
  selectionTrigger =
    event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  selectTask(taskId)
  await nextTick()
  detailRegion.value?.focus()
}
/** 关闭独立详情后恢复原触发器焦点，键盘用户无需重新定位列表。 */
async function closeSelected(): Promise<void> {
  closeDetail()
  await nextTick()
  selectionTrigger?.focus()
}
</script>

<template>
  <section
    class="actions-page"
    :class="{ 'has-detail': selectedTaskId }"
    aria-labelledby="actions-title"
  >
    <header class="page-heading">
      <div>
        <h1 id="actions-title">
          操作中心
        </h1>
        <p>集中查看本地草稿、日程提案与执行进度。</p>
      </div>
      <button
        type="button"
        :disabled="actions.loading"
        @click="refresh"
      >
        刷新操作
      </button>
    </header>
    <div class="creation-controls">
      <button
        type="button"
        name="new-mail"
        :disabled="creating"
        @click="newMail"
      >
        新邮件
      </button><button
        type="button"
        name="new-calendar"
        :disabled="creating"
        @click="newCalendar"
      >
        新日程
      </button>
    </div>
    <p
      v-if="creating"
      role="status"
    >
      正在创建本地编辑对象…
    </p>
    <EditorRecovery
      :error="creationError"
      :busy="creating"
      @reload="refresh"
    />
    <div class="action-layout">
      <div class="action-list-region">
        <form
          class="filters"
          aria-label="操作筛选"
          @submit.prevent
        >
          <label>供应商<select
            v-model="provider"
            aria-label="供应商筛选"
          >
            <option value="">全部供应商</option>
            <option value="google">Google</option>
            <option value="microsoft">Microsoft</option>
          </select></label>
          <label>类型<select
            v-model="itemKind"
            aria-label="操作类型筛选"
          >
            <option value="">全部类型</option>
            <option value="mail_draft">邮件草稿</option>
            <option value="calendar_proposal">日程提案</option>
            <option value="trusted_task">可信任务</option>
          </select></label>
          <label>状态<select
            v-model="status"
            aria-label="操作状态筛选"
          >
            <option value="">全部状态</option>
            <option value="editing">编辑中</option>
            <option value="stale">需要重新检查</option>
            <option value="created">已创建</option>
            <option value="queued">已排队</option>
            <!-- 本地对象与可信任务保留各自状态值，筛选不把相似文案合成为领域状态。 -->
            <option value="awaiting_approval">本地对象待审批</option>
            <option value="waiting_approval">任务待审批</option>
            <option value="executing">本地对象执行中</option>
            <option value="running">任务执行中</option>
            <option value="retry_scheduled">等待安全重试</option>
            <option value="reconciling">正在核对</option>
            <option value="needs_attention">需要人工确认</option>
            <option value="succeeded">已完成</option>
            <option value="sent">已发送</option>
            <option value="applied">已应用</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select></label>
        </form>
        <p
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
        </p>
        <p
          v-if="actions.listError"
          class="error"
          role="alert"
        >
          {{ actions.listError.message
          }}<span v-if="actions.listError.trace_id">
            追踪编号：{{ actions.listError.trace_id }}</span><span v-if="actions.items.length">
            当前保留上次成功读取的列表。</span>
        </p>
        <p
          v-if="
            actions.loaded &&
              !actions.loading &&
              !actions.listError &&
              !actions.items.length
          "
          class="empty"
        >
          暂无操作。草稿、提案和任务将在这里集中展示。
        </p>
        <div class="action-groups">
          <section
            v-for="group in groups"
            :key="group.id"
            class="action-group"
            :aria-labelledby="`group-${group.id}`"
          >
            <h2 :id="`group-${group.id}`">
              {{ group.label }}
              <span
                v-if="actions.loaded"
                class="count"
              >{{
                group.items.length
              }}</span>
            </h2>
            <ul v-if="group.items.length">
              <li
                v-for="item in group.items"
                :key="`${item.item_kind}:${item.id}`"
                :data-action-id="item.id"
                :class="{
                  selected:
                    item.task_id === selectedTaskId && selectedTaskId !== null,
                }"
              >
                <div>
                  <strong>{{ actionLabel(item.action) }}</strong><span class="metadata">{{ providerLabel(item.provider) }} ·
                    {{ actionStatusLabel(item.status) }}</span>
                </div>
                <span
                  v-if="item.risk_level"
                  class="risk"
                >{{
                  item.risk_level === 'high' ? '高风险' : '中风险'
                }}</span>
                <time :datetime="item.updated_at">{{
                  formatActionTime(item.updated_at, timezone)
                }}</time>
                <button
                  v-if="item.item_kind === 'trusted_task'"
                  type="button"
                  :aria-label="`查看${actionLabel(item.action)}详情`"
                  :aria-pressed="item.task_id === selectedTaskId"
                  @click="openDetail(item.task_id, $event)"
                >
                  查看详情
                </button>
                <RouterLink
                  v-else
                  class="local-status"
                  :to="item.editor_url"
                >
                  {{
                    item.item_kind === 'mail_draft'
                      ? '编辑本地草稿'
                      : '编辑本地提案'
                  }}
                </RouterLink>
              </li>
            </ul>
            <p
              v-else-if="actions.loaded && !actions.listError"
              class="group-empty"
            >
              此分组暂无操作
            </p>
          </section>
        </div>
        <nav
          v-if="actions.offset > 0 || actions.items.length === actions.limit"
          class="pagination"
          aria-label="操作分页"
        >
          <button
            type="button"
            :disabled="actions.loading || actions.offset === 0"
            @click="changePage(Math.max(0, actions.offset - actions.limit))"
          >
            上一页
          </button>
          <button
            type="button"
            :disabled="actions.loading || actions.items.length < actions.limit"
            @click="changePage(actions.offset + actions.limit)"
          >
            下一页
          </button>
        </nav>
      </div>
      <!-- 窄屏时列表隐藏，详情独立占据完整内容区域，不压缩桌面多栏。 -->
      <aside
        v-if="selectedTaskId"
        ref="detailRegion"
        class="detail-region"
        tabindex="-1"
        aria-label="操作详情与时间线"
      >
        <button
          type="button"
          aria-label="关闭操作详情"
          @click="closeSelected"
        >
          返回操作列表
        </button>
        <p
          role="status"
          aria-live="polite"
        >
          {{ connectionLabels[connectionState]
          }}<span v-if="selected">
            · {{ actionStatusLabel(selected.status) }}</span>
        </p>
        <p
          v-if="actions.snapshotLoading[selectedTaskId]"
          role="status"
        >
          正在恢复操作快照…
        </p>
        <p
          v-if="actions.snapshotErrors[selectedTaskId]"
          class="error"
          role="alert"
        >
          {{ actions.snapshotErrors[selectedTaskId]?.message
          }}<span v-if="actions.snapshotErrors[selectedTaskId]?.trace_id">
            追踪编号：{{
              actions.snapshotErrors[selectedTaskId]?.trace_id
            }}</span><button
            type="button"
            @click="refresh"
          >
            重试详情
          </button>
        </p>
        <ActionDetail
          v-if="selected"
          :snapshot="selected"
          :timezone="timezone"
        />
      </aside>
    </div>
  </section>
</template>

<style scoped>
.creation-controls {
  display: flex;
  gap: 0.75rem;
  margin: 1rem 0;
  flex-wrap: wrap;
}
.actions-page {
  color: #1e293b;
  min-width: 0;
}
.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1.5rem;
}
h1 {
  margin: 0;
  font-size: 1.75rem;
}
.page-heading p {
  margin-bottom: 0;
  color: #485365;
}
.action-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1.5rem;
}
.has-detail .action-layout {
  grid-template-columns: minmax(0, 1fr) minmax(18rem, 24rem);
}
.filters {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
label {
  display: grid;
  gap: 0.35rem;
  font-size: 0.875rem;
}
select,
button {
  font: inherit;
  min-height: 2.5rem;
  padding: 0.4rem 0.65rem;
  border: 1px solid #a8b1bf;
  border-radius: 0.35rem;
  background: #fff;
  color: #1e293b;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.65;
  cursor: default;
}
button:focus-visible,
select:focus-visible,
.detail-region:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
.action-group {
  border: 1px solid #d7dce5;
  border-radius: 0.5rem;
  margin-bottom: 1rem;
  overflow: hidden;
}
.action-group h2 {
  display: flex;
  justify-content: space-between;
  margin: 0;
  padding: 0.8rem 1rem;
  font-size: 1rem;
  background: #f6f8fb;
}
.count {
  color: #485365;
  font-weight: normal;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.65rem 1rem;
  padding: 1rem;
  border-top: 1px solid #d7dce5;
}
li > div {
  flex: 1 1 10rem;
}
.metadata {
  display: block;
  margin-top: 0.3rem;
  color: #485365;
  font-size: 0.875rem;
}
time,
.local-status {
  color: #485365;
  font-size: 0.8rem;
}
.risk {
  font-size: 0.75rem;
  padding: 0.2rem 0.4rem;
  border: 1px solid #c6aa72;
  border-radius: 0.25rem;
}
.selected {
  background: #eef5ff;
}
.group-empty {
  padding: 0.5rem 1rem;
  color: #485365;
  font-size: 0.875rem;
}
.empty {
  padding: 1rem;
  border: 1px dashed #a8b1bf;
  border-radius: 0.5rem;
}
.error {
  color: #a61b1b;
}
.detail-region {
  border-left: 1px solid #d7dce5;
  padding-left: 1.5rem;
  min-width: 0;
}
.pagination {
  display: flex;
  gap: 0.75rem;
}
@media (max-width: 1000px) {
  .has-detail .action-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .detail-region {
    border-left: 0;
    border-top: 1px solid #d7dce5;
    padding: 1rem 0 0;
  }
}
@media (max-width: 800px) {
  .has-detail .action-list-region {
    display: none;
  }
  .page-heading {
    align-items: flex-start;
  }
  .filters label {
    flex: 1 1 8rem;
  }
  .detail-region {
    border-top: 0;
  }
}
</style>
