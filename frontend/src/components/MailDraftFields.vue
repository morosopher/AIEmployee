<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import AutoComplete from 'primevue/autocomplete'
import Button from 'primevue/button'
import Chip from 'primevue/chip'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Message from 'primevue/message'
import type { MailDraft } from '@/api/types'
import type { ConnectionCatalogEntry } from '@/composables/useConnectionCatalog'
import type {
  MailEditorForm,
  MailRecipientSummary,
} from '@/features/mail/useMailDraftEditor'
import { recipientList } from '@/features/actions/editorInput'
import EditorConnectionSelect from './EditorConnectionSelect.vue'

/**
 * 纯展示字段：chips 与未确认文字同时投影到原字符串模型，不接管保存、数量或重复身份规则。
 * Live region：原实时人数 status 逐字保留；AutoComplete 每字段内置搜索结果 status 与打开建议后
 * 的选中反馈只说明该字段的局部交互，不替代跨字段人数核对，也不新增空 role 凑数。
 */
const props = defineProps<{
  draft: MailDraft
  entries: ConnectionCatalogEntry[]
  disabled: boolean
  recipientSummary: MailRecipientSummary
  errors?: Partial<Record<keyof MailEditorForm, string>>
}>()
const form = defineModel<MailEditorForm>({ required: true })
const recipientFields = [
  { name: 'to', label: '收件人 To' },
  { name: 'cc', label: '抄送 CC' },
  { name: 'bcc', label: '密送 BCC' },
] as const
type RecipientField = (typeof recipientFields)[number]['name']
const chips = reactive<Record<RecipientField, string[]>>({
  to: [],
  cc: [],
  bcc: [],
})
const pending = reactive<Record<RecipientField, string>>({
  to: '',
  cc: '',
  bcc: '',
})
const suggestions = reactive<Record<RecipientField, string[]>>({
  to: [],
  cc: [],
  bcc: [],
})
/** 建议只取当前服务端草稿已有地址，不读取历史 recipient_suggestions，也不触发请求。 */
const availableSuggestions = computed(() => [
  ...new Set([...props.draft.to, ...props.draft.cc, ...props.draft.bcc]),
])
watch(
  () => props.draft,
  (draft) => {
    for (const { name } of recipientFields) {
      chips[name] = [...draft[name]]
      pending[name] = ''
      suggestions[name] = []
    }
  },
  { immediate: true },
)

/** @param name 当前字段；原始未确认输入必须参与实时人数和保存，避免合法旧 chip 掩盖新错误。 */
function sync(name: RecipientField): void {
  form.value[name] = [
    ...chips[name],
    ...(pending[name] ? [pending[name]] : []),
  ].join(', ')
}
/** @param event 真实输入事件；只更新页面内存，不修剪或静默改写输入。 */
function input(name: RecipientField, event: Event): void {
  if (!(event.target instanceof HTMLInputElement) || props.disabled) return
  pending[name] = event.target.value
  sync(name)
}
/** @param values AutoComplete 公开数组值；删除 chip 时保留尚未确认的输入，选入建议时清空查询。 */
function updateChips(name: RecipientField, values: string[]): void {
  if (props.disabled) return
  if (values.length > chips[name].length) pending[name] = ''
  chips[name] = values
  sync(name)
}
/** 没有打开建议时 Enter 仅确认合法地址，不触发表单；格式不合法仍原样留在输入框供纠正。 */
function confirmInput(name: RecipientField, event: KeyboardEvent): void {
  if (
    event.key !== 'Enter' ||
    !(event.target instanceof HTMLInputElement) ||
    event.target.getAttribute('aria-expanded') === 'true'
  )
    return
  event.preventDefault()
  event.stopPropagation()
  if (props.disabled) return
  try {
    const addresses = recipientList(pending[name])
    chips[name] = [...chips[name], ...addresses]
    pending[name] = ''
    sync(name)
  } catch {
    // 错误内容保留；现有实时人数及 Form 提交校验给出固定说明，不吞掉地址。
  }
}
/** @param query 本地查询词；仅在当前草稿建议集合中过滤，绝不访问通讯录。 */
function search(name: RecipientField, query: string): void {
  suggestions[name] = availableSuggestions.value.filter((address) =>
    address.toLowerCase().includes(query.toLowerCase()),
  )
}
/**
 * PrimeVue 4.5.5 运行时公开 chip slot 的 class 未列入其声明；只收窄实际字符串，不断言未知形状。
 * @param slot 当前 chip 的公开 slot 数据。
 * @returns 主题活动样式类；缺少公开值时保持未设置，不拼接库内部类名。
 */
function chipStyleClass(slot: object): string | undefined {
  return 'class' in slot && typeof slot.class === 'string' ? slot.class : undefined
}
</script>
<template>
  <div class="grid min-w-0 gap-4">
    <EditorConnectionSelect
      v-model="form.connection_id"
      :entries="entries"
      capability="mail.send"
      label="发送账户"
      :disabled="disabled || draft.mode !== 'new'"
    />
    <p
      v-if="draft.mode !== 'new'"
      class="text-sm text-muted-color"
    >
      回复已绑定来源线程：{{
        draft.source_thread_id
      }}。账户、线程和主题保持绑定。
    </p>
    <div
      v-for="field in recipientFields"
      :key="field.name"
      class="grid min-w-0 gap-1"
    >
      <label :for="`mail-${field.name}`">{{ field.label }}</label>
      <!-- multiple 内置 option 含按钮／输入会形成嵌套交互；已选区及每个 chip 用具名 group，保留 active-descendant 与原左右键／删除，建议弹层仍为 listbox。 -->
      <AutoComplete
        :input-id="`mail-${field.name}`"
        :model-value="chips[field.name]"
        :suggestions="suggestions[field.name]"
        multiple
        fluid
        :disabled="disabled"
        :invalid="Boolean(errors?.[field.name])"
        :form-control="{ novalidate: true }"
        :pt="{
          inputMultiple: {
            role: 'group',
            'aria-label': `${field.label}已确认地址`,
            'aria-orientation': null,
          },
          chipItem: {
            role: 'group',
            'aria-selected': null,
            'aria-setsize': null,
            'aria-posinset': null,
          },
          inputChip: { role: 'presentation' },
          input: {
            value: pending[field.name],
            'aria-describedby': errors?.[field.name]
              ? `mail-${field.name}-error`
              : undefined,
            onInput: (event: Event) => input(field.name, event),
          },
        }"
        @keydown.capture="confirmInput(field.name, $event)"
        @update:model-value="updateChips(field.name, $event)"
        @complete="search(field.name, $event.query)"
      >
        <template #chip="chip">
          <!-- 公开 slot class 连接活动项主题样式；普通及活动背景均由 Chip token 决定，避免工具类覆盖。 -->
          <Chip
            :class="chipStyleClass(chip)"
            class="max-w-full gap-1 pl-2"
          >
            <span class="min-w-0 break-all">{{ chip.value }}</span>
            <Button
              type="button"
              icon="pi pi-times"
              :aria-label="`移除${field.label}中的${chip.value}`"
              text
              rounded
              severity="secondary"
              size="small"
              :disabled="disabled"
              @click="chip.removeCallback"
            />
          </Chip>
        </template>
      </AutoComplete>
      <Message
        v-if="errors?.[field.name]"
        :id="`mail-${field.name}-error`"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors[field.name] }}
      </Message>
    </div>
    <p class="text-sm text-muted-color">
      多个邮箱用逗号分隔，To、CC、BCC 合计最多 50
      位。建议仅来自当前草稿已有收件人。
    </p>
    <!-- 实时人数沿用原 hook；不能把未核对项数宣告为已确认人数。 -->
    <p role="status">
      <template v-if="recipientSummary.count !== null">
        当前收件人数：{{ recipientSummary.count }} 位
      </template>
      <template v-else>
        收件人数待核对。
        <span v-if="recipientSummary.inputCount !== null">当前输入地址：{{ recipientSummary.inputCount }} 项。</span>
        {{ recipientSummary.error }}
      </template>
    </p>
    <div class="grid gap-1">
      <label for="mail-subject">主题</label>
      <InputText
        id="mail-subject"
        v-model="form.subject"
        name="subject"
        :disabled="disabled || draft.mode !== 'new'"
        :invalid="Boolean(errors?.subject)"
        :aria-invalid="Boolean(errors?.subject) || undefined"
        :aria-describedby="errors?.subject ? 'mail-subject-error' : undefined"
      />
      <Message
        v-if="errors?.subject"
        id="mail-subject-error"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors.subject }}
      </Message>
    </div>
    <div class="grid gap-1">
      <label for="mail-body">纯文本正文</label>
      <Textarea
        id="mail-body"
        v-model="form.body_text"
        name="body_text"
        rows="12"
        fluid
        class="resize-y"
        :disabled="disabled"
        :invalid="Boolean(errors?.body_text)"
        :aria-invalid="Boolean(errors?.body_text) || undefined"
        :aria-describedby="errors?.body_text ? 'mail-body-error' : undefined"
      />
      <Message
        v-if="errors?.body_text"
        id="mail-body-error"
        severity="error"
        size="small"
        variant="simple"
      >
        {{ errors.body_text }}
      </Message>
    </div>
  </div>
</template>
