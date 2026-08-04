<script setup lang="ts">
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'
const props = defineProps<{ content: string }>()
const md = new MarkdownIt({ html: false, linkify: true, typographer: true })
const rendered = computed(() => DOMPurify.sanitize(md.render(props.content), { ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.-]|$))/i, FORBID_TAGS: ['style', 'script'], FORBID_ATTR: ['onerror', 'onclick'] }))
</script>
<template><div class="markdown-message" v-html="rendered" /></template>
