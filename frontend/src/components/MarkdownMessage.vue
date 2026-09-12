<script setup lang="ts">
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'
import { focusedEditorPath } from '@/features/actions/editorLinks'

/** Markdown 只展示内容；内部编辑链接仅负责导航，不能调用创建、批准或写入接口。 */
const props = defineProps<{
  content: string
  openEditor?: (path: string) => void | Promise<unknown>
}>()
const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  breaks: true,
})
md.validateLink = (url: string) =>
  /^https?:\/\//i.test(url) || focusedEditorPath(url) !== null
md.renderer.rules.link_open = (tokens, index, options, _env, self) => {
  const token = tokens[index]
  if (token) {
    const local = focusedEditorPath(token.attrGet('href'))
    if (local) {
      token.attrSet('href', local)
      token.attrSet('data-editor-link', 'true')
    } else {
      token.attrSet('target', '_blank')
      token.attrSet('rel', 'noopener noreferrer')
    }
  }
  return self.renderToken(tokens, index, options)
}
// 原始 HTML 始终关闭。清洗器再次约束 href/src，不接受任意相对路径或脚本协议。
const rendered = computed(() =>
  DOMPurify.sanitize(md.render(props.content), {
    ALLOWED_URI_REGEXP:
      /^(?:https?:\/\/|\/(?:mail\/drafts|calendar\/proposals)\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$)/i,
    FORBID_TAGS: ['style', 'script'],
    FORBID_ATTR: ['onerror', 'onclick'],
    // 这两个非 URI 属性只由渲染器写入固定值，不能被 href/src 的严格 URI 正则误删。
    // 原始 HTML 仍被禁用；保留 _blank 时必须同时保留 noopener noreferrer。
    ADD_ATTR: ['target'],
    ADD_URI_SAFE_ATTR: ['target', 'rel'],
  }),
)

/** 浏览器修饰键仍保留原生导航；普通内部点击经路由进入聚焦编辑器。 */
function open(event: MouseEvent): void {
  if (
    !props.openEditor ||
    event.button !== 0 ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    event.altKey
  )
    return
  const link =
    event.target instanceof Element ? event.target.closest('a') : null
  const path = focusedEditorPath(link?.getAttribute('href'))
  if (path) {
    event.preventDefault()
    void props.openEditor(path)
  }
}
</script>
<template>
  <!-- eslint-disable vue/no-v-html -- rendered 已经过禁用原始 HTML、协议白名单和 DOMPurify 清洗；仅此输出点允许 HTML。 -->
  <div
    class="markdown-message"
    @click="open"
    v-html="rendered"
  />
  <!-- eslint-enable vue/no-v-html -->
</template>
