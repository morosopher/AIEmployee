<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { createConversation, getConversation, sendMessage } from '@/api/conversations'
import type { Conversation, Message } from '@/api/types'
import MarkdownMessage from '@/components/MarkdownMessage.vue'
const conversation = ref<Conversation | null>(null); const messages = ref<Message[]>([]); const draft = ref(''); const loading = ref(false)
onMounted(async () => { conversation.value = await createConversation(); const value = await getConversation(conversation.value.id); messages.value = value.messages })
async function submit() { if (!conversation.value || !draft.value.trim()) return; const content = draft.value; draft.value = ''; messages.value.push({ id: crypto.randomUUID(), role: 'user', content_markdown: content, task_id: null, created_at: new Date().toISOString() }); loading.value = true; try { await sendMessage(conversation.value.id, content, crypto.randomUUID()) } finally { loading.value = false } }
</script>
<template><section><h1>聊天</h1><div v-for="message in messages" :key="message.id" :class="message.role"><MarkdownMessage :content="message.content_markdown" /></div><form @submit.prevent="submit"><label class="sr-only" for="message">消息</label><textarea id="message" v-model="draft" /><button type="submit" :disabled="loading">发送</button></form></section></template>
