import { createApp } from 'vue'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'

import 'primeicons/primeicons.css'
import './design/app.css'

import App from './App.vue'
import { primeVueOptions } from './design/primevue'
import router from './router'

/**
 * 创建应用根：先安装 Pinia，保证路由守卫可安全读取认证 Store；PrimeVue 及其
 * Toast/确认服务在挂载前全局安装，组件本身由 unplugin-vue-components 按需注册，
 * 不做 `app.component` 全局注册。Toast 与 ConfirmDialog 的出口组件由应用骨架挂载
 * （M2.1 实施计划 Task 2），在此之前页面不调用这两项服务。
 */
const app = createApp(App)
app.use(createPinia())
app.use(PrimeVue, primeVueOptions)
app.use(ToastService)
app.use(ConfirmationService)
app.use(router)
app.mount('#app')
