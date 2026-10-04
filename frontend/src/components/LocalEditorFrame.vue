<script setup lang="ts">
/** 邮件与日程共享语义容器；仅提供 Tailwind 布局，状态、请求和输入规则由专用 feature 负责。 */
defineProps<{ title: string }>()
</script>
<template>
  <section
    class="min-w-0 max-w-5xl space-y-4 text-color wrap-anywhere [&_.editor-controls]:my-4 [&_.editor-controls]:flex [&_.editor-controls]:flex-wrap [&_.editor-controls]:gap-3 [&_.editor-fields]:grid [&_.editor-fields]:gap-4 md:[&_.editor-fields]:grid-cols-2 [&_.wide-field]:col-span-full [&_label]:grid [&_label]:gap-1 [&_input]:max-w-full [&_select]:max-w-full [&_textarea]:max-w-full"
  >
    <h1 class="text-2xl font-semibold">
      {{ title }}
    </h1>
    <slot />
  </section>
</template>
