<script setup lang="ts">
import { nextTick, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'
import { useRoute, useRouter } from 'vue-router'

import { ProblemError } from '@/api/client'
import ProblemMessage from '@/components/ProblemMessage.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const email = ref('')
const password = ref('')
const error = ref<ProblemError | string | null>(null)
const passwordToggle = ref<HTMLButtonElement | null>(null)

/**
 * Password 的公开图标槽默认不是按钮；用语义按钮触发其回调，并在槽切换后恢复焦点。
 * @param toggleCallback PrimeVue 公开插槽提供的显隐回调，不访问组件内部状态。
 * @returns 新按钮完成渲染并重新获得焦点后结束，保证连续键盘切换。
 */
async function togglePassword(toggleCallback: () => void): Promise<void> {
  toggleCallback()
  await nextTick()
  passwordToggle.value?.focus()
}

/**
 * 登录成功后只跳转站内受保护路径，避免通过 query 产生开放重定向。
 *
 * @returns 已验证的站内目标路径。
 */
function destination(): string {
  const redirect = route.query.redirect
  return typeof redirect === 'string' &&
    redirect.startsWith('/') &&
    !redirect.startsWith('//')
    ? redirect
    : '/tasks'
}

/**
 * 提交管理员登录，并由认证 Store 建立内存用户投影。
 *
 * @returns Promise 在导航完成或错误显示后结束。
 */
async function submit(): Promise<void> {
  error.value = null
  try {
    await auth.login(email.value, password.value)
    password.value = ''
    await router.replace(destination())
  } catch (cause: unknown) {
    error.value =
      cause instanceof ProblemError ? cause : '登录暂时不可用，请稍后重试。'
  }
}
</script>

<template>
  <main class="grid min-h-dvh place-items-center bg-surface-50 p-4">
    <Card class="w-full max-w-sm">
      <template #title>
        <h1
          id="login-title"
          class="text-2xl font-semibold"
        >
          登录 AI Employee
        </h1>
      </template>
      <template #content>
        <form
          aria-labelledby="login-title"
          class="grid gap-4"
          @submit.prevent="submit"
        >
          <div class="grid gap-1">
            <label for="login-email">邮箱</label>
            <InputText
              id="login-email"
              v-model="email"
              type="email"
              autocomplete="username"
              required
              fluid
            />
          </div>
          <div class="grid gap-1">
            <label for="login-password">密码</label>
            <!-- 关闭强度提示；隐藏内置强度播报，避免无反馈模式仍宣告无关的密码提示。 -->
            <Password
              v-model="password"
              input-id="login-password"
              :input-props="{ autocomplete: 'current-password' }"
              :feedback="false"
              toggle-mask
              required
              fluid
              :pt="{
                hiddenAccesible: { 'aria-live': 'off', 'aria-hidden': true },
              }"
            >
              <!-- 两个公开槽均使用原生按钮，Enter/Space 的激活由浏览器提供，不模拟按键。 -->
              <template #unmaskicon="{ toggleCallback }">
                <button
                  ref="passwordToggle"
                  type="button"
                  aria-label="显示密码"
                  :aria-pressed="false"
                  class="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-surface-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  @click="togglePassword(toggleCallback)"
                >
                  <i
                    class="pi pi-eye"
                    aria-hidden="true"
                  />
                </button>
              </template>
              <template #maskicon="{ toggleCallback }">
                <button
                  ref="passwordToggle"
                  type="button"
                  aria-label="显示密码"
                  :aria-pressed="true"
                  class="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-surface-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                  @click="togglePassword(toggleCallback)"
                >
                  <i
                    class="pi pi-eye-slash"
                    aria-hidden="true"
                  />
                </button>
              </template>
            </Password>
          </div>
          <Button
            type="submit"
            :label="auth.loading ? '登录中…' : '登录'"
            :disabled="auth.loading"
            :loading="auth.loading"
          />
          <!-- 原有一个 alert 移交 Message；已知 401 保留真实基线原文并增加本地恢复说明。
               其余服务端错误使用共享安全映射；网络错误保留既有固定文案，不虚构 ProblemDetails。 -->
          <ProblemMessage
            v-if="error instanceof ProblemError"
            :problem="error"
            :description="
              error.problem.error_code === 'invalid_credentials'
                ? 'Invalid credentials，请检查邮箱和密码后重试。'
                : undefined
            "
          />
          <Message
            v-else-if="error"
            severity="error"
            role="alert"
          >
            {{ error }}
          </Message>
        </form>
      </template>
    </Card>
  </main>
</template>
