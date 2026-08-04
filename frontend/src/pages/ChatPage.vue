<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { createConversation, getConversation, sendMessage, listConversations, deleteConversation } from '@/api/conversations'
import type { Conversation, Message } from '@/api/types'
import MarkdownMessage from '@/components/MarkdownMessage.vue'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { useTasksStore } from '@/stores/tasks'
const conversation = ref<Conversation | null>(null); const conversations = ref<Conversation[]>([]); const messages = ref<Message[]>([]); const draft = ref(''); const loading = ref(false); const taskId = ref<string | null>(null)
useTaskEvents(taskId)
const task = computed(() => taskId.value ? useTasksStore().tasks[taskId.value] : undefined)
watch(() => task.value?.status, async (status) => { if ((status === 'succeeded' || status === 'failed') && conversation.value) messages.value = (await getConversation(conversation.value.id)).messages })
onMounted(async () => { try { conversations.value = await listConversations(); conversation.value = conversations.value[0] ?? await createConversation(); const value = await getConversation(conversation.value.id); messages.value = value.messages } catch { loading.value = false } })
async function selectConversation(value: Conversation) { conversation.value = value; messages.value = (await getConversation(value.id)).messages }
async function removeConversation(value: Conversation) { if (!window.confirm('确定删除会话？')) return; await deleteConversation(value.id); conversations.value = conversations.value.filter((item) => item.id !== value.id); if (conversation.value?.id === value.id) { conversation.value = conversations.value[0] ?? await createConversation(); messages.value = (await getConversation(conversation.value.id)).messages } }
async function submit() { if (!conversation.value || !draft.value.trim()) return; const content = draft.value; draft.value = ''; messages.value.push({ id: crypto.randomUUID(), role: 'user', content_markdown: content, task_id: null, created_at: new Date().toISOString() }); loading.value = true; try { const result = await sendMessage(conversation.value.id, content, crypto.randomUUID()); taskId.value = result.task_id; const value = await getConversation(conversation.value.id); messages.value = value.messages } finally { loading.value = false } }
</script>
<template><section><h1>聊天</h1><nav aria-label="会话历史"><div v-for="item in conversations" :key="item.id"><button type="button" @click="selectConversation(item)">{{ item.title }}</button><button type="button" @click="removeConversation(item)">删除</button></div></nav><div v-for="message in messages" :key="message.id" :class="message.role"><MarkdownMessage :content="message.content_markdown" /></div><form @submit.prevent="submit"><label class="sr-only" for="message">消息</label><textarea id="message" v-model="draft" /><button type="submit" :disabled="loading">发送</button></form></section></template>
