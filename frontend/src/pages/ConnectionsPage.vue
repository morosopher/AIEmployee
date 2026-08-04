<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { disconnectConnection, listConnections, startGoogleConnection, syncConnection } from '@/api/connections'
import type { Connection } from '@/api/types'
const connections = ref<Connection[]>([]); const error = ref<string | null>(null)
async function load() { try { connections.value = await listConnections() } catch { error.value = '无法加载连接。' } }
async function connect() { const value = await startGoogleConnection(); window.location.assign(value.authorization_url) }
async function sync(connection: Connection) { await syncConnection(connection.id) }
async function disconnect(connection: Connection) { if (window.confirm('确定断开连接？')) { await disconnectConnection(connection.id); await load() } }
onMounted(() => void load())
</script>
<template><section><h1>连接</h1><button type="button" @click="connect">连接 Google</button><p v-if="error" role="alert">{{ error }}</p><ul><li v-for="connection in connections" :key="connection.id"><strong>{{ connection.provider }}</strong> {{ connection.account_email }} · <span role="status">{{ connection.status }}</span><button v-if="['connecting','degraded','expired'].includes(connection.status)" type="button" @click="connect">重新连接</button><button v-if="['connected','degraded'].includes(connection.status)" type="button" @click="sync(connection)">立即同步</button><button v-if="connection.status !== 'disconnected'" type="button" @click="disconnect(connection)">断开</button></li></ul></section></template>
