<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { getSettings, updateSettings } from '@/api/settings'
import { listSessions, revokeSession } from '@/api/auth'
import type { Session, UserSettings } from '@/api/types'
const form = reactive<Partial<UserSettings>>({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365 }); const sessions = ref<Session[]>([]); const saved = ref(false); const error = ref<string | null>(null)
const timezones = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']
async function load() { try { Object.assign(form, await getSettings()); sessions.value = await listSessions() } catch { error.value = '无法加载设置。' } }
async function save() { saved.value = false; error.value = null; try { Object.assign(form, await updateSettings(form)); saved.value = true } catch { error.value = '设置无效，请检查输入。' } }
async function revoke(id: string) { if (!window.confirm('确定撤销该会话？')) return; try { await revokeSession(id); sessions.value = sessions.value.filter((session) => session.id !== id) } catch { error.value = '撤销失败。' } }
onMounted(() => void load())
</script>
<template><section><h1>设置</h1><form @submit.prevent="save"><label>时区 <select v-model="form.timezone"><option v-for="timezone in timezones" :key="timezone">{{ timezone }}</option></select></label><label>语言 <input v-model="form.locale" /></label><label>简报时间 <input v-model="form.brief_time" type="time" /></label><label>邮件正文保留天数 <input v-model.number="form.email_body_retention_days" type="number" /></label><label>来源元数据保留天数 <input v-model.number="form.source_metadata_retention_days" type="number" /></label><label>工作区历史保留天数 <input v-model.number="form.workspace_history_retention_days" type="number" /></label><button type="submit">保存</button></form><p v-if="saved" role="status">已保存</p><p v-if="error" role="alert">{{ error }}</p><h2>活动会话</h2><ul><li v-for="session in sessions" :key="session.id">{{ session.created_at }} · {{ session.expires_at }} <button type="button" @click="revoke(session.id)">撤销</button></li></ul></section></template>
