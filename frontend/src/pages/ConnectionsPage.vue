<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import { useConfirm } from 'primevue/useconfirm'
import CapabilityRows from '@/components/CapabilityRows.vue'
import StatusTag from '@/components/StatusTag.vue'
import { useConnections } from '@/features/connections/useConnections'
import { providerLabel } from '@/features/actions/presentation'
import { useAuthStore } from '@/stores/auth'

const confirmation = useConfirm()
const auth = useAuthStore()
const confirmingDisconnect = ref(false)
let pendingConfirmation: ((accepted: boolean) => void) | null = null

/** 同一个 UI 决定只能结算一次，避免 accept 后的 hide 再次产生决定或请求。 */
function settleConfirmation(accepted: boolean): void {
  const settle = pendingConfirmation
  pendingConfirmation = null
  confirmingDisconnect.value = false
  settle?.(accepted)
}

/**
 * 消费 AppShell 的唯一 ConfirmDialog 出口，只返回用户决定，不复制断开请求路径。
 * @param message 行为层提供的完整原警告。
 * @returns 同意、取消、Esc、关闭或卸载后的确认结果。
 */
function confirmDisconnect(message: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    pendingConfirmation = resolve
    confirmingDisconnect.value = true
    confirmation.require({
      header: '断开连接',
      message,
      icon: 'pi pi-exclamation-triangle',
      defaultFocus: 'reject',
      rejectProps: { label: '取消', severity: 'secondary', outlined: true },
      acceptProps: { label: '确认断开', severity: 'danger' },
      accept: () => settleConfirmation(true),
      reject: () => settleConfirmation(false),
      onHide: () => settleConfirmation(false),
    })
  })
}

/** 页面只组合目录与受控动作，不推断授权结果或直接发起供应商请求。 */
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
} = useConnections({ confirmDisconnect })
const currentError = computed(() => actionError.value || error.value)
const authorizationLink = ref<HTMLElement | null>(null)

// 只改变下一步的展示焦点；不自动跳转、持久化 URL 或推断供应商已经同意。
watch(authorization, async (value) => {
  if (!value) return
  await nextTick()
  authorizationLink.value?.focus()
})
onUnmounted(() => {
  if (!pendingConfirmation) return
  settleConfirmation(false)
  confirmation.close()
})
</script>

<template>
  <section
    class="mx-auto w-full max-w-5xl space-y-5"
    :aria-busy="busy || undefined"
  >
    <header class="space-y-2">
      <h1 class="text-2xl font-semibold">
        连接与能力
      </h1>
      <p class="text-muted-color">
        每个账户单独授权。发送邮件或修改日程前，仍需逐次审阅并批准精确内容。
      </p>
    </header>
    <div class="flex flex-wrap gap-3">
      <Button
        label="连接 Google"
        :disabled="busy"
        @click="connect('google')"
      />
      <Button
        label="连接 Microsoft"
        severity="secondary"
        :disabled="busy"
        @click="connect('microsoft')"
      />
      <Button
        label="刷新连接"
        icon="pi pi-refresh"
        severity="secondary"
        outlined
        :disabled="loading || busy"
        @click="load"
      />
    </div>
    <Message
      v-if="loading"
      severity="info"
      role="status"
      aria-live="polite"
    >
      正在加载连接能力…
    </Message>
    <Message
      v-if="loaded && !entries.length"
      severity="info"
      role="status"
      aria-live="polite"
    >
      尚未连接账户。请选择供应商开始只读授权。
    </Message>
    <Message
      v-if="currentError"
      severity="error"
      role="alert"
      aria-live="assertive"
    >
      {{ currentError.message }}
      <span v-if="currentError.traceId">追踪编号：{{ currentError.traceId }}</span>
    </Message>
    <Message
      v-if="notice"
      severity="info"
      role="status"
      aria-live="polite"
    >
      {{ notice }}
    </Message>
    <!-- 保留真正的链接节点，供原授权焦点规则和浏览器原生导航使用。 -->
    <a
      v-if="authorization"
      ref="authorizationLink"
      data-testid="authorization-link"
      :href="authorization.url"
      class="inline-flex min-h-11 items-center rounded-md bg-primary px-4 py-2 font-medium text-primary-contrast focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
    >继续 {{ providerLabel(authorization.provider) }} 授权</a>
    <Card
      v-for="entry in entries"
      :key="entry.connection.id"
      role="article"
      :aria-labelledby="`connection-title-${entry.connection.id}`"
      :data-testid="`connection-${entry.connection.id}`"
      class="min-w-0"
    >
      <template #title>
        <h2
          :id="`connection-title-${entry.connection.id}`"
          class="break-words text-lg font-semibold"
        >
          {{ providerLabel(entry.connection.provider) }} ·
          {{ entry.connection.account_email }}
        </h2>
      </template>
      <template #content>
        <div class="min-w-0 space-y-4">
          <p role="status">
            <span class="sr-only">连接状态：{{ entry.connection.status }}</span>
            <StatusTag
              kind="connection"
              :value="entry.connection.status"
              aria-hidden="true"
            />
          </p>
          <Message
            v-if="entry.error"
            severity="error"
            role="alert"
            aria-live="assertive"
          >
            能力加载失败，请刷新后重试。<span v-if="entry.error.traceId">追踪编号：{{ entry.error.traceId }}</span>
          </Message>
          <CapabilityRows
            v-if="entry.capabilities"
            :capabilities="entry.capabilities"
            :busy="busy"
            :disconnected="entry.connection.status === 'disconnected'"
            :timezone="auth.user?.timezone ?? 'UTC'"
            @enable="enable(entry.connection, $event)"
            @disable="disable(entry.connection, $event)"
          />
          <p class="text-sm text-muted-color">
            关闭能力不会撤回已执行操作，也不保证供应商移除单项授权范围。
          </p>
          <div class="flex flex-wrap gap-3">
            <Button
              v-if="['connected', 'degraded'].includes(entry.connection.status)"
              label="立即同步"
              severity="secondary"
              outlined
              :disabled="busy"
              @click="sync(entry.connection)"
            />
            <Button
              v-if="entry.connection.status !== 'connected'"
              label="重新连接"
              severity="secondary"
              :disabled="busy"
              @click="connect(entry.connection.provider)"
            />
            <!-- 确认期间由外壳 inert 和行为层 busy 互斥；保留触发节点供 Dialog 捕获返回焦点。 -->
            <Button
              v-if="entry.connection.status !== 'disconnected'"
              label="断开"
              severity="danger"
              outlined
              :disabled="busy && !confirmingDisconnect"
              @click="disconnect(entry.connection)"
            />
          </div>
        </div>
      </template>
    </Card>
  </section>
</template>
