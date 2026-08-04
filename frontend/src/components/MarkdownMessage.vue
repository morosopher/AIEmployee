<script setup lang="ts">
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'
const props = defineProps<{ content: string }>()
const md = new MarkdownIt({ html: false, linkify: true, typographer: true, breaks: true })
md.validateLink = (url: string) => /^https?:\/\//i.test(url)
md.renderer.rules.link_open = (tokens, index, options, _env, self) => { const token = tokens[index]; token.attrSet('target', '_blank'); token.attrSet('rel', 'noopener noreferrer'); return self.renderToken(tokens, index, options) }
const rendered = computed(() => DOMPurify.sanitize(md.render(props.content), { ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.-]|$))/i, FORBID_TAGS: ['style', 'script'], FORBID_ATTR: ['onerror', 'onclick'] }))
</script>
<template><div class="markdown-message" v-html="rendered" /></template>
