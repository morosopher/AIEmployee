import { requestJson } from './client'
import type { Conversation, Message } from './types'
const parseConversation = (v: unknown): Conversation => { if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('Invalid conversation'); const o = v as Record<string, unknown>; if (typeof o.id !== 'string' || typeof o.title !== 'string' || typeof o.created_at !== 'string' || typeof o.updated_at !== 'string') throw new Error('Invalid conversation'); return o as unknown as Conversation }
export const listConversations = () => requestJson('/conversations', (v) => { if (!Array.isArray(v)) throw new Error('Invalid conversations'); return v.map(parseConversation) })
export const createConversation = () => requestJson('/conversations', (v) => v as Conversation, { method: 'POST' })
export const getConversation = (id: string) => requestJson(`/conversations/${encodeURIComponent(id)}`, (v) => v as { conversation: Conversation; messages: Message[] })
export const sendMessage = (id: string, content_markdown: string, client_request_id: string) => requestJson(`/conversations/${encodeURIComponent(id)}/messages`, (v) => v as { task_id: string }, { method: 'POST', body: JSON.stringify({ content_markdown, client_request_id }) })
export const deleteConversation = (id: string) => requestJson(`/conversations/${encodeURIComponent(id)}`, () => null, { method: 'DELETE' })
