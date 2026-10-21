<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NCheckboxGroup, NForm, NFormItem, NInput, NInputNumber, NModal, NSelect } from 'naive-ui'
import { Plus, Trash2 } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { CatalogEntry, CatalogEntryInput, CatalogTask } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'

const show = defineModel<boolean>('show', { required: true })
const props = defineProps<{ entry: CatalogEntry | null }>()
const emit = defineEmits<{ saved: [] }>()
const { t } = useI18n()
const { showError, message } = useErrorMessage()

const TASKS: CatalogTask[] = ['chat', 'code', 'reasoning', 'embedding', 'vision']

interface VariantRow {
  format: 'gguf' | 'safetensors'
  repo: string
  file: string
  mmproj: string
  quant: string
}

const form = reactive({
  id: '',
  name: '',
  publisher: '',
  license: '',
  family: '',
  tasks: ['chat'] as CatalogTask[],
  params_b: 7 as number | null,
  context_length: 32768 as number | null,
  en: '',
  zh: '',
  source_repo: '',
  tags: '',
  llamaArgs: '',
  vllmArgs: '',
  variants: [] as VariantRow[],
})
const saving = ref(false)
const editing = computed(() => !!props.entry)

const blankVariant = (): VariantRow => ({ format: 'gguf', repo: '', file: '', mmproj: '', quant: 'Q4_K_M' })

watch(show, (open) => {
  if (!open) return
  const e = props.entry
  Object.assign(form, {
    id: e?.id ?? '',
    name: e?.name ?? '',
    publisher: e?.publisher ?? '',
    license: e?.license ?? '',
    family: e?.family ?? '',
    tasks: e ? [...e.tasks] : ['chat'],
    params_b: e?.params_b ?? 7,
    context_length: e?.context_length ?? 32768,
    en: e?.description.en ?? '',
    zh: e?.description.zh ?? '',
    source_repo: e?.source_repo ?? '',
    tags: e?.tags.join(', ') ?? '',
    llamaArgs: e?.extra_args['llama.cpp'] ?? '',
    vllmArgs: e?.extra_args.vllm ?? '',
    variants: e
      ? e.variants.map((v) => ({ format: v.format, repo: v.repo, file: v.files[0] ?? '', mmproj: v.mmproj ?? '', quant: v.quant }))
      : [blankVariant()],
  })
})

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)

watch(
  () => form.name,
  (name) => {
    if (!editing.value) form.id = slug(name)
  },
)

const formatOptions = [
  { label: 'GGUF (llama.cpp)', value: 'gguf' },
  { label: 'safetensors (vLLM / SGLang)', value: 'safetensors' },
]

const valid = computed(
  () =>
    !!form.id &&
    !!form.name.trim() &&
    form.tasks.length > 0 &&
    !!form.params_b &&
    !!form.context_length &&
    form.variants.length > 0 &&
    form.variants.every((v) => v.repo.includes('/') && v.quant.trim() && (v.format === 'safetensors' || v.file.trim())),
)

function body(): CatalogEntryInput {
  const ids = new Set<string>()
  const extra: Record<string, string> = {}
  if (form.llamaArgs.trim()) extra['llama.cpp'] = form.llamaArgs.trim()
  if (form.vllmArgs.trim()) extra.vllm = form.vllmArgs.trim()
  return {
    id: form.id,
    name: form.name.trim(),
    family: form.family.trim(),
    publisher: form.publisher.trim(),
    license: form.license.trim(),
    tasks: form.tasks,
    params_b: form.params_b ?? 1,
    context_length: form.context_length ?? 4096,
    tags: form.tags
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    description: { en: form.en.trim(), zh: form.zh.trim() },
    source_repo: form.source_repo.trim() || null,
    extra_args: extra,
    variants: form.variants.map((v) => {
      let id = slug(v.quant) || v.format
      while (ids.has(id)) id += '-' + (v.format === 'gguf' ? 'gguf' : 'st')
      ids.add(id)
      return {
        id,
        format: v.format,
        repo: v.repo.trim(),
        quant: v.quant.trim(),
        files: v.format === 'gguf' ? [v.file.trim()] : [],
        mmproj: v.format === 'gguf' && v.mmproj.trim() ? v.mmproj.trim() : null,
      }
    }),
  }
}

async function save() {
  saving.value = true
  try {
    if (props.entry) await api.catalog.updateEntry(props.entry.id, body())
    else await api.catalog.createEntry(body())
    message.success(t('catalog.admin.saved'))
    show.value = false
    emit('saved')
  } catch (e) {
    showError(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :title="editing ? t('catalog.admin.editTitle') : t('catalog.admin.addTitle')"
    style="width: 720px; max-width: calc(100vw - 32px)"
    :bordered="false"
  >
    <NForm :show-require-mark="false" label-placement="top">
      <div class="grid2">
        <NFormItem :label="t('models.name')">
          <NInput v-model:value="form.name" maxlength="80" placeholder="My Model 7B" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.id')" :feedback="t('catalog.admin.idHint')">
          <NInput v-model:value="form.id" :disabled="editing" class="mono-input" maxlength="64" />
        </NFormItem>
        <NFormItem :label="t('catalog.publisher')">
          <NInput v-model:value="form.publisher" maxlength="60" />
        </NFormItem>
        <NFormItem :label="t('catalog.license')">
          <NInput v-model:value="form.license" maxlength="60" placeholder="Apache-2.0" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.paramsB')">
          <NInputNumber v-model:value="form.params_b" :min="0.01" :step="0.1" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.contextLength')">
          <NInputNumber v-model:value="form.context_length" :min="256" :step="1024" />
        </NFormItem>
      </div>
      <NFormItem :label="t('catalog.admin.tasks')">
        <NCheckboxGroup v-model:value="form.tasks">
          <NCheckbox v-for="task in TASKS" :key="task" :value="task" :label="t(`catalog.task.${task}`)" />
        </NCheckboxGroup>
      </NFormItem>
      <div class="grid2">
        <NFormItem :label="t('catalog.admin.descEn')">
          <NInput v-model:value="form.en" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="1000" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.descZh')">
          <NInput v-model:value="form.zh" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="1000" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.sourceRepo')" :feedback="t('catalog.admin.sourceRepoHint')">
          <NInput v-model:value="form.source_repo" placeholder="org/model" class="mono-input" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.tags')">
          <NInput v-model:value="form.tags" placeholder="multilingual, tools" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.llamaArgs')">
          <NInput v-model:value="form.llamaArgs" class="mono-input" placeholder="--jinja" />
        </NFormItem>
        <NFormItem :label="t('catalog.admin.vllmArgs')">
          <NInput v-model:value="form.vllmArgs" class="mono-input" placeholder="--enable-auto-tool-choice" />
        </NFormItem>
      </div>

      <div class="variants-head">
        <div>
          <div class="v-title">{{ t('catalog.variants') }}</div>
          <div class="faint small">{{ t('catalog.admin.variantsHint') }}</div>
        </div>
        <NButton size="small" secondary @click="form.variants.push(blankVariant())">
          <template #icon><Plus :size="14" /></template>
          {{ t('catalog.admin.addVariant') }}
        </NButton>
      </div>
      <div v-for="(v, i) in form.variants" :key="i" class="variant">
        <NSelect v-model:value="v.format" :options="formatOptions" size="small" class="fmt" />
        <NInput v-model:value="v.quant" size="small" :placeholder="t('catalog.admin.quant')" class="quant" />
        <NInput v-model:value="v.repo" size="small" placeholder="org/repo-GGUF" class="repo mono-input" />
        <template v-if="v.format === 'gguf'">
          <NInput v-model:value="v.file" size="small" placeholder="model-Q4_K_M.gguf" class="file mono-input" />
          <NInput v-model:value="v.mmproj" size="small" :placeholder="t('catalog.admin.mmproj')" class="file mono-input" />
        </template>
        <NButton size="small" quaternary circle :disabled="form.variants.length < 2" @click="form.variants.splice(i, 1)">
          <Trash2 :size="14" />
        </NButton>
      </div>
    </NForm>
    <template #footer>
      <div class="footer">
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="saving" :disabled="!valid" @click="save">{{ t('common.save') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 12px;
}
.variants-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  margin-bottom: 10px;
}
.v-title {
  font-weight: 600;
}
.variant {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  padding: 8px 0;
}
.variant + .variant {
  border-top: 1px dashed var(--border);
}
.fmt {
  width: 210px;
}
.quant {
  width: 110px;
}
.repo {
  flex: 1 1 200px;
}
.file {
  flex: 1 1 220px;
}
.mono-input :deep(input) {
  font-family: var(--mono);
  font-size: 12.5px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 640px) {
  .grid2 {
    grid-template-columns: 1fr;
  }
  .fmt,
  .quant {
    width: 100%;
  }
}
</style>
