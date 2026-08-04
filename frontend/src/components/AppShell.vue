<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import TaskTimeline from './TaskTimeline.vue'
</script>
<template><div class="app-shell"><nav aria-label="主导航"><RouterLink to="/chat">新聊天</RouterLink><RouterLink to="/brief">今日简报</RouterLink><RouterLink to="/tasks">任务历史</RouterLink><RouterLink to="/connections">连接</RouterLink><RouterLink to="/settings">设置</RouterLink></nav><main><RouterView /></main><TaskTimeline :task="null" :retry="async () => { throw new Error('无任务') }" :follow="() => undefined" /></div></template>
<style scoped>.app-shell{display:grid;grid-template-columns:12rem minmax(0,1fr) 20rem;min-height:100vh}nav{display:flex;flex-direction:column;gap:1rem;padding:1rem;border-right:1px solid #ddd}main{padding:1.5rem}@media(max-width:800px){.app-shell{display:block}nav{flex-direction:row;overflow:auto}.app-shell>aside{display:none}}</style>
