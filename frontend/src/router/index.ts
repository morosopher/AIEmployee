import {
  createRouter,
  createWebHistory,
  type RouteLocationNormalized,
} from 'vue-router'

import LoginPage from '@/pages/LoginPage.vue'
import TasksPage from '@/pages/TasksPage.vue'
import { useAuthStore } from '@/stores/auth'

/** 路由只公开登录入口，其他页面必须由安全 Cookie 会话恢复后访问。 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    {
      path: '/login',
      name: 'login',
      component: LoginPage,
      meta: { guestOnly: true },
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: TasksPage,
      meta: { requiresAuth: true },
    },
  ],
})

/**
 * 判断导航目标是否要求已恢复的会话，支持未来嵌套路由而不复制守卫。
 *
 * @param target Vue Router 已解析的目标。
 * @returns 目标是否受认证保护。
 */
function requiresAuthentication(target: RouteLocationNormalized): boolean {
  return target.matched.some((record) => record.meta.requiresAuth === true)
}

router.beforeEach(async (target) => {
  const auth = useAuthStore()
  await auth.loadCurrentUser()
  if (requiresAuthentication(target) && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: target.fullPath } }
  }
  if (target.meta.guestOnly === true && auth.isAuthenticated)
    return { name: 'tasks' }
  return true
})

export default router
