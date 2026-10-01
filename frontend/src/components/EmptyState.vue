<script setup lang="ts">
import Button from 'primevue/button'

/** 空数据的纯展示契约；可选说明与动作由页面提供，不推断业务状态或创建任何数据。 */
defineProps<{ title: string; description?: string; actionLabel?: string }>()
/** 用户主动点击时通知消费方；组件自身不发起请求。 */
defineEmits<{ action: [] }>()
</script>

<template>
  <section class="flex flex-col items-center gap-3 rounded-lg p-6 text-center">
    <i
      class="pi pi-inbox text-3xl text-muted-color"
      aria-hidden="true"
    />
    <h2 class="text-lg font-semibold">
      {{ title }}
    </h2>
    <p
      v-if="description"
      class="text-muted-color"
    >
      {{ description }}
    </p>
    <Button
      v-if="actionLabel"
      :label="actionLabel"
      @click="$emit('action')"
    />
  </section>
</template>
