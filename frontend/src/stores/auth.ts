import { defineStore } from 'pinia'

import { getCurrentUser, login, ProblemError } from '@/api/client'
import type { AuthUser } from '@/api/types'

/** 认证状态只保存公开用户投影；安全 Cookie 始终由浏览器和服务端管理。 */
export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as AuthUser | null,
    resolved: false,
    loading: false,
    error: null as string | null,
  }),
  getters: {
    /** 当前是否已有经 `/auth/me` 或登录确认的会话。 */
    isAuthenticated: (state): boolean => state.user !== null,
  },
  actions: {
    /**
     * 从持久 Cookie 会话恢复用户，401 是正常的未登录分支而非页面错误。
     *
     * @returns 完成后可由路由守卫读取 `isAuthenticated`。
     */
    async loadCurrentUser(): Promise<void> {
      if (this.resolved || this.loading) return
      this.loading = true
      this.error = null
      try {
        this.user = await getCurrentUser()
      } catch (error: unknown) {
        this.user = null
        if (!(error instanceof ProblemError && error.problem.status === 401)) {
          this.error = '无法恢复登录状态，请稍后重试。'
        }
      } finally {
        this.loading = false
        this.resolved = true
      }
    },
    /**
     * 提交登录凭据并把公开用户资料写入内存，绝不缓存密码。
     *
     * @param email 管理员邮箱。
     * @param password 本次登录密码。
     * @returns 登录成功后解析的用户资料。
     * @throws ProblemError 当凭据或请求无效时交由页面展示可恢复提示。
     */
    async login(email: string, password: string): Promise<AuthUser> {
      this.loading = true
      this.error = null
      try {
        const user = await login(email, password)
        this.user = user
        this.resolved = true
        return user
      } finally {
        this.loading = false
      }
    },
  },
})
