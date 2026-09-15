<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import CapabilityRows from '@/components/CapabilityRows.vue'
import { useConnections } from '@/features/connections/useConnections'
import { providerLabel } from '@/features/actions/presentation'

/** 连接页面只组合目录与受控动作，不在模板里发送供应商请求或推断授权结果。 */
const {
  entries,
  loading,
  loaded,
  error,
  busy,
  notice,
  actionError,
  authorization,
  load,
  connect,
  enable,
  disable,
  sync,
  disconnect,
} = useConnections()

const authorizationLink = ref<HTMLAnchorElement | null>(null)
// 用户可能在页面下方启用能力；等新链接渲染后移入焦点，使下一步进入视野和键盘顺序。
// 这里只改变展示焦点，不自动跳转、保存授权 URL 或推断供应商已同意。
watch(authorization, async (value) => {
  if (!value) return
  await nextTick()
  authorizationLink.value?.focus()
})
</script>
<template>
  <section class="connections-page">
    <h1>连接与能力</h1>
    <p>每个账户单独授权。发送邮件或修改日程前，仍需逐次审阅并批准精确内容。</p>
    <div class="controls">
      <button
        type="button"
        name="connect-google"
        :disabled="busy"
        @click="connect('google')"
      >
        连接 Google
      </button><button
        type="button"
        name="connect-microsoft"
        :disabled="busy"
        @click="connect('microsoft')"
      >
        连接 Microsoft
      </button><button
        type="button"
        name="refresh-connections"
        :disabled="loading || busy"
        @click="load"
      >
        刷新连接
      </button>
    </div>
    <p
      v-if="loading"
      role="status"
    >
      正在加载连接能力…
    </p>
    <p
      v-if="loaded && !entries.length"
      role="status"
    >
      尚未连接账户。请选择供应商开始只读授权。
    </p>
    <p
      v-if="error || actionError"
      role="alert"
    >
      {{ (actionError || error)?.message }}
      <span v-if="(actionError || error)?.traceId">追踪编号：{{ (actionError || error)?.traceId }}</span>
    </p>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
    <a
      v-if="authorization"
      ref="authorizationLink"
      data-testid="authorization-link"
      :href="authorization.url"
    >继续 {{ providerLabel(authorization.provider) }} 授权</a>
    <article
      v-for="entry in entries"
      :key="entry.connection.id"
      :data-connection-id="entry.connection.id"
    >
      <h2>
        {{ providerLabel(entry.connection.provider) }} ·
        {{ entry.connection.account_email }}
      </h2>
      <p role="status">
        连接状态：{{ entry.connection.status }}
      </p>
      <p
        v-if="entry.error"
        role="alert"
      >
        能力加载失败，请刷新后重试。<span v-if="entry.error.traceId">追踪编号：{{ entry.error.traceId }}</span>
      </p>
      <CapabilityRows
        v-if="entry.capabilities"
        :capabilities="entry.capabilities"
        :busy="busy"
        :disconnected="entry.connection.status === 'disconnected'"
        @enable="enable(entry.connection, $event)"
        @disable="disable(entry.connection, $event)"
      />
      <p>关闭能力不会撤回已执行操作，也不保证供应商移除单项授权范围。</p>
      <div class="controls">
        <button
          v-if="['connected', 'degraded'].includes(entry.connection.status)"
          type="button"
          :disabled="busy"
          @click="sync(entry.connection)"
        >
          立即同步
        </button><button
          v-if="entry.connection.status !== 'connected'"
          type="button"
          :disabled="busy"
          @click="connect(entry.connection.provider)"
        >
          重新连接
        </button><button
          v-if="entry.connection.status !== 'disconnected'"
          type="button"
          :disabled="busy"
          @click="disconnect(entry.connection)"
        >
          断开
        </button>
      </div>
    </article>
  </section>
</template>
<style scoped>
.connections-page {
  max-width: 64rem;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}
article {
  margin: 1.5rem 0;
  padding: 1rem;
  border: 1px solid #dce2ea;
  border-radius: 0.65rem;
}
h2 {
  font-size: 1.15rem;
  overflow-wrap: anywhere;
}
[role='alert'] {
  color: #a61b1b;
}
button:focus-visible,
a:focus-visible {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
</style>
