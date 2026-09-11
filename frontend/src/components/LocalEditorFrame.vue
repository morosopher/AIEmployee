<script setup lang="ts">
/** 邮件与日程编辑器共享语义布局及窄屏输入样式，业务状态由专用 feature 提供。 */
defineProps<{ title: string }>()
</script>
<template>
  <section class="local-editor">
    <h1>{{ title }}</h1>
    <slot />
  </section>
</template>
<style scoped>
.local-editor {
  max-width: 60rem;
  color: #1e293b;
  min-width: 0;
  overflow-wrap: anywhere;
}
.local-editor :deep(label) {
  display: grid;
  gap: 0.4rem;
  margin: 0.8rem 0;
}
.local-editor :deep(input),
.local-editor :deep(select),
.local-editor :deep(textarea),
.local-editor :deep(button) {
  box-sizing: border-box;
  font: inherit;
  max-width: 100%;
  padding: 0.55rem 0.7rem;
  border: 1px solid #a8b1bf;
  border-radius: 0.35rem;
  color: #1e293b;
  background: white;
}
.local-editor :deep(textarea) {
  width: 100%;
  min-height: 7rem;
  resize: vertical;
}
.local-editor :deep(button) {
  cursor: pointer;
  min-height: 2.5rem;
}
.local-editor :deep(button:disabled),
.local-editor :deep(input:disabled),
.local-editor :deep(select:disabled),
.local-editor :deep(textarea:disabled) {
  opacity: 0.65;
  cursor: default;
}
.local-editor :deep(:focus-visible) {
  outline: 3px solid #164e9c;
  outline-offset: 3px;
}
.local-editor :deep(.editor-controls) {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin: 1rem 0;
}
.local-editor :deep(.editor-fields) {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0 1rem;
}
.local-editor :deep(.wide-field) {
  grid-column: 1 / -1;
}
.local-editor :deep(.editor-note) {
  padding: 0.75rem;
  background: #fff5df;
  border-left: 3px solid #946200;
}
@media (max-width: 650px) {
  .local-editor :deep(.editor-fields) {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
