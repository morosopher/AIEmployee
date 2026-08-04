import { requestJson } from './client'
import type { Conversation, Message } from './types'
export const listConversations = () => requestJson('/conversations', (v) => v as Conversation[])
export const createConversation = () => requestJson('/conversations', (v) => v as Conversation, { method: 'POST' })
export const getConversation = (id: string) => requestJson(`/conversations/${encodeURIComponent(id)}`, (v) => v as { conversation: Conversation; messages: Message[] })
export const sendMessage = (id: string, content_markdown: string, client_request_id: string) => requestJson(`/conversations/${encodeURIComponent(id)}/messages`, (v) => v as { task_id: string }, { method: 'POST', body: JSON.stringify({ content_markdown, client_request_id }) })
export const deleteConversation = (id: string) => requestJson(`/conversations/${encodeURIComponent(id)}`, () => null, { method: 'DELETE' })
