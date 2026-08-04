<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import TaskTimeline from './TaskTimeline.vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useTasksStore } from '@/stores/tasks'
import ApprovalCard from './ApprovalCard.vue'
import { decideApproval } from '@/api/approvals'
const route = useRoute(); const tasks = useTasksStore(); const task = computed(() => typeof route.query.task_id === 'string' ? tasks.tasks[route.query.task_id] ?? null : null)
const retry = async (id: string) => { throw new Error(`请在任务页重试 ${id}`) }
const approval = computed(() => { const step = task.value?.steps.find((item) => item.status === 'waiting_approval'); const summary = step?.output_summary; if (!summary || typeof summary.approval_id !== 'string' || typeof summary.tool !== 'string' || typeof summary.version !== 'number' || typeof summary.payload_hash !== 'string') return null; return { id: summary.approval_id, tool: summary.tool, payload: typeof summary.payload === 'object' && summary.payload !== null ? summary.payload as Record<string, unknown> : {}, version: summary.version, payload_hash: summary.payload_hash, status: 'pending', expires_at: '' } })
</script>
<template><div class="app-shell"><nav aria-label="主导航"><RouterLink to="/chat">新聊天</RouterLink><RouterLink to="/brief">今日简报</RouterLink><RouterLink to="/tasks">任务历史</RouterLink><RouterLink to="/connections">连接</RouterLink><RouterLink to="/settings">设置</RouterLink></nav><main><RouterView /></main><aside><TaskTimeline :task="task" :retry="retry" :follow="(id) => undefined" /><ApprovalCard v-if="approval" :approval="approval" :decide="decideApproval" /></aside></div></template>
<style scoped>.app-shell{display:grid;grid-template-columns:12rem minmax(0,1fr) 20rem;min-height:100vh}nav{display:flex;flex-direction:column;gap:1rem;padding:1rem;border-right:1px solid #ddd}main{padding:1.5rem}@media(max-width:800px){.app-shell{display:block}nav{flex-direction:row;overflow:auto}.app-shell>aside{display:none}}</style>
