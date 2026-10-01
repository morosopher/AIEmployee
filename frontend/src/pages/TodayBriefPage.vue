<script setup lang="ts">
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import EmptyState from '@/components/EmptyState.vue'
import BriefView from '@/components/BriefView.vue'
import EditorRecovery from '@/components/EditorRecovery.vue'
import { useBriefWorkspace } from '@/features/briefs/useBriefWorkspace'
import {
  sourceAction,
  type BriefActionKind,
  type BriefSource,
} from '@/features/briefs/sourceActions'
import { useLocalActionCreation } from '@/features/actions/useLocalActionCreation'

/** 简报生成沿用 M1；每个 M2 建议以用户点击的真实来源创建本地 editing 对象。 */
const {
  latest,
  selected,
  versions,
  loading,
  generating,
  error,
  load,
  select,
  refresh,
} = useBriefWorkspace()
const {
  busy: creating,
  error: creationError,
  reply,
  updateEvent,
} = useLocalActionCreation()
/**
 * 仅将用户明确选择的来源交给既有本地编辑器创建端口，不提交审批或执行真实写入。
 * @param kind 服务端建议的受限动作类型。
 * @param source 当前按钮绑定的真实来源；无效来源由既有规则拒绝。
 * @returns 本地对象创建与导航完成后结束。
 */
async function propose(
  kind: BriefActionKind,
  source: BriefSource,
): Promise<void> {
  const action = sourceAction(kind, source)
  if (action === 'mail.reply') await reply(source.source_id)
  if (action === 'calendar.update') await updateEvent(source.source_id)
}
</script>
<template>
  <section class="space-y-4 p-4 md:p-6">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <h1 class="text-2xl font-semibold">
        今日简报
      </h1>
      <Button
        :label="selected ? '生成新版本' : '生成简报'"
        :loading="generating"
        :disabled="generating"
        :aria-busy="generating"
        @click="refresh"
      />
    </header>
    <!-- 骨架仅辅助视觉等待，加载播报仍由原有且唯一的 status 文案承担。 -->
    <div
      v-if="loading"
      class="space-y-3"
    >
      <p role="status">
        正在加载…
      </p>
      <div
        data-testid="brief-loading-skeleton"
        aria-hidden="true"
        class="space-y-3"
      >
        <Skeleton class="h-8 w-2/3" />
        <Skeleton class="h-24" />
      </div>
    </div>
    <!-- feature 仅返回固定安全文案；保留失败与无数据的歧义，不伪造 ProblemDetails。 -->
    <Message
      v-if="error"
      severity="error"
      role="alert"
    >
      {{ error }}
    </Message>
    <EditorRecovery
      :error="creationError"
      :busy="creating"
      @reload="load"
    />
    <p
      v-if="creating"
      role="status"
    >
      正在创建本地编辑对象…
    </p>
    <Card v-if="versions.length">
      <template #content>
        <div class="flex flex-wrap items-center gap-3">
          <label
            id="brief-version-label"
            for="brief-version"
          >历史版本</label>
          <Select
            input-id="brief-version"
            aria-labelledby="brief-version-label"
            :model-value="selected?.id"
            :options="versions"
            option-value="id"
            :option-label="
              (version) =>
                `版本 ${version.version}${version.id === latest?.id ? '（最新）' : ''}`
            "
            @update:model-value="select"
          />
          <template v-if="selected && selected.id !== latest?.id">
            <span>正在查看历史版本</span>
            <Button
              label="返回最新"
              severity="secondary"
              @click="selected = latest"
            />
          </template>
        </div>
      </template>
    </Card>
    <BriefView
      v-if="selected"
      :brief="selected"
      :busy="creating"
      :propose="propose"
    />
    <EmptyState
      v-else-if="!loading"
      title="暂无可展示的简报"
      description="可点击生成简报；若加载失败，请稍后重试。"
    />
  </section>
</template>
