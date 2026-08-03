<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ProblemError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const email = ref('')
const password = ref('')
const error = ref<string | null>(null)

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
      cause instanceof ProblemError
        ? cause.problem.title
        : '登录暂时不可用，请稍后重试。'
  }
}
</script>

<template>
  <main class="login-page">
    <form aria-label="管理员登录" @submit.prevent="submit">
      <h1>登录 AI Employee</h1>
      <label>
        邮箱
        <input v-model="email" type="email" autocomplete="username" required />
      </label>
      <label>
        密码
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
        />
      </label>
      <button type="submit" :disabled="auth.loading">
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>
      <p v-if="error" role="alert">{{ error }}</p>
    </form>
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  place-items: center;
}
form {
  display: grid;
  gap: 1rem;
  width: min(24rem, 90vw);
}
label {
  display: grid;
  gap: 0.25rem;
}
</style>
