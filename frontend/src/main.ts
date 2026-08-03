import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'

/** 创建应用根并先安装 Pinia，保证路由守卫可安全读取认证 Store。 */
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
