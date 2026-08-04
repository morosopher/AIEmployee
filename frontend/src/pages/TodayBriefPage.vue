<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getBrief, getTodayBrief, listBriefs, generateBrief } from '@/api/briefs'
import type { Brief } from '@/api/types'
import BriefView from '@/components/BriefView.vue'
const latest = ref<Brief | null>(null); const versions = ref<Brief[]>([]); const selected = ref<Brief | null>(null); const loading = ref(true); const error = ref<string | null>(null)
async function load() { loading.value = true; error.value = null; try { const value = await getTodayBrief(); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid'); latest.value = value; versions.value = await listBriefs(value.local_date); selected.value = latest.value } catch { latest.value = null; versions.value = []; selected.value = null; error.value = '暂无今日简报或加载失败。' } finally { loading.value = false } }
async function select(id: string) { selected.value = id === latest.value?.id ? latest.value : await getBrief(id) }
async function refresh() { await generateBrief(); await load() }
onMounted(() => void load())
</script>
<template><section><h1>今日简报</h1><button type="button" @click="refresh">生成新版本</button><p v-if="loading" role="status">正在加载…</p><p v-if="error" role="alert">{{ error }}</p><label v-if="versions.length">历史版本 <select :value="selected?.id" @change="select(($event.target as HTMLSelectElement).value)"><option v-for="version in versions" :key="version.id" :value="version.id">版本 {{ version.version }}{{ version.id === latest?.id ? '（最新）' : '' }}</option></select></label><p v-if="selected && selected.id !== latest?.id">正在查看历史版本 <button type="button" @click="selected = latest">返回最新</button></p><BriefView v-if="selected" :brief="selected" /></section></template>
