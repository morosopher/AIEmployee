<script setup lang="ts">
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
  <section>
    <h1>今日简报</h1>
    <button
      type="button"
      :disabled="generating"
      @click="refresh"
    >
      生成新版本
    </button>
    <p
      v-if="loading"
      role="status"
    >
      正在加载…
    </p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
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
    <label v-if="versions.length">历史版本
      <select
        :value="selected?.id"
        @change="select(($event.target as HTMLSelectElement).value)"
      >
        <option
          v-for="version in versions"
          :key="version.id"
          :value="version.id"
        >
          版本 {{ version.version
          }}{{ version.id === latest?.id ? '（最新）' : '' }}
        </option>
      </select></label>
    <p v-if="selected && selected.id !== latest?.id">
      正在查看历史版本
      <button
        type="button"
        @click="selected = latest"
      >
        返回最新
      </button>
    </p>
    <BriefView
      v-if="selected"
      :brief="selected"
      :busy="creating"
      :propose="propose"
    />
  </section>
</template>
