<script setup lang="ts">
import { ref } from 'vue'
import { ProblemError } from '@/api/client'
interface Approval { id: string; tool: string; payload: Record<string, unknown>; version: number; payload_hash: string; status: string; expires_at: string }
const props = defineProps<{ approval: Approval; decide: (id: string, decision: 'approved' | 'rejected', version: number, payloadHash: string) => Promise<void> }>()
const busy = ref(false); const decided = ref(false); const error = ref<string | null>(null)
async function submitDecision(decision: 'approved' | 'rejected') { if (busy.value || decided.value || props.approval.status !== 'pending') return; busy.value = true; error.value = null; try { await props.decide(props.approval.id, decision, props.approval.version, props.approval.payload_hash); decided.value = true } catch (e) { error.value = e instanceof ProblemError && e.problem.status === 409 ? '审批已发生冲突，请刷新。' : '审批失败，请重试。' } finally { busy.value = false } }
</script>
<template><section class="approval-card" aria-label="人工审批"><h2>需要审批：{{ approval.tool }}</h2><pre>{{ JSON.stringify(approval.payload, null, 2) }}</pre><p>版本 {{ approval.version }} · {{ approval.status }}</p><button type="button" :disabled="busy || decided || approval.status !== 'pending'" @click="submitDecision('approved')">批准</button><button type="button" :disabled="busy || decided || approval.status !== 'pending'" @click="submitDecision('rejected')">拒绝</button><p v-if="error" role="alert">{{ error }}</p></section></template>
