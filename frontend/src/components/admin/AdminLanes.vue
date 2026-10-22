<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NInput, NModal, NSelect, useDialog, useMessage } from 'naive-ui'
import { Cpu, Plus } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { DevTemplate, IdleMode, Lane, Model } from '@/api/types'
import { loadLanes, useLanes } from '@/composables/lanes'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useArgsGate } from '@/composables/argsGate'
import { useStatus } from '@/composables/useStatus'
import { session } from '@/composables/session'

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()
const { withArgsGate } = useArgsGate()
const { status, refresh } = useStatus()
const { lanes } = useLanes()

const models = ref<Model[]>([])
const templates = ref<DevTemplate[]>([])
const devenvEnabled = computed(() => !!session.config?.devenv_enabled)
onMounted(async () => {
  await loadLanes(true)
  models.value = await api.models().catch(() => [])
  templates.value = await api.devenv.templates().catch(() => [])
})
// The shared environment of an idle lane comes from a template offered to everyone.
const templateOptions = computed(() =>
  templates.value.filter((x) => x.kind === 'builtin').map((x) => ({ label: `${x.name}  ·  ${x.image}`, value: x.id })),
)
const idleOptions = computed(() =>
  (['model', 'devenv', 'none'] as IdleMode[]).map((m) => ({ label: t(`admin.lanes.idle.${m}`), value: m })),
)

const gpus = computed(() => status.value?.gpus ?? [])
const owner = (index: number) => lanes.value.find((l) => l.gpus.includes(index))
const modelOptions = computed(() => [
  { label: t('admin.lanes.noDefault'), value: null as unknown as number },
  ...models.value.filter((m) => m.status === 'ready').map((m) => ({ label: m.name, value: m.id })),
])

const form = reactive({
  open: false,
  id: null as number | null,
  name: '',
  gpus: [] as number[],
  model: null as number | null,
  idle: 'model' as IdleMode,
  template: null as number | null,
})
const busy = ref(false)

function openNew() {
  const free = gpus.value.map((g) => g.index).filter((i) => !owner(i))
  Object.assign(form, {
    open: true,
    id: null,
    name: `GPU ${free.join(',') || lanes.value.length + 1}`,
    gpus: free,
    model: null,
    idle: 'model',
    template: null,
  })
}

function openEdit(l: Lane) {
  Object.assign(form, {
    open: true,
    id: l.id,
    name: l.name,
    gpus: [...l.gpus],
    model: l.default_model_id,
    idle: l.idle_mode ?? 'model',
    template: l.idle_template_id,
  })
}

function toggleGpu(index: number, on: boolean) {
  form.gpus = on ? [...form.gpus, index].sort((a, b) => a - b) : form.gpus.filter((g) => g !== index)
}

const takenElsewhere = (index: number) => {
  const l = owner(index)
  return !!l && l.id !== form.id
}

async function save() {
  busy.value = true
  try {
    const body = {
      name: form.name.trim(),
      gpus: form.gpus,
      default_model_id: form.model,
      idle_mode: form.idle,
      idle_template_id: form.idle === 'devenv' ? form.template : null,
    }
    if (form.id === null) await api.admin.createLane(body)
    else await api.admin.updateLane(form.id, body)
    form.open = false
    message.success(t('admin.lanes.saved'))
    await loadLanes(true)
    window.setTimeout(refresh, 600)
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

function remove(l: Lane) {
  dialog.warning({
    title: t('admin.lanes.delete'),
    content: t('admin.lanes.deleteConfirm', { name: l.name }),
    positiveText: t('admin.lanes.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.admin.deleteLane(l.id)
        await loadLanes(true)
        refresh()
      } catch (e) {
        showError(e)
      }
    },
  })
}

const laneState = (id: number) => status.value?.lanes.find((l) => l.id === id)
const loadedModel = (id: number) => laneState(id)?.runtime.model_id ?? null

function reload(l: Lane) {
  dialog.warning({
    title: t('admin.lanes.reload'),
    content: t('admin.lanes.reloadConfirm', { name: l.name }),
    positiveText: t('admin.lanes.reload'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.restart(l.id)
        message.success(t('admin.lanes.reloading', { name: l.name }))
        setTimeout(refresh, 800)
      } catch (e) {
        showError(e)
      }
    },
  })
}

// Edit the launch arguments of the model a lane is running, then reload every lane running it.
const argsForm = reactive({ open: false, modelId: 0, name: '', args: '' })
function openArgs(l: Lane) {
  const id = loadedModel(l.id)
  const m = models.value.find((x) => x.id === id)
  if (!m) return
  Object.assign(argsForm, { open: true, modelId: m.id, name: m.name, args: m.args })
}
async function saveArgs() {
  busy.value = true
  try {
    const saved = await withArgsGate((x) => api.updateModel(argsForm.modelId, { args: argsForm.args.trim(), ...x }))
    if (!saved) return
    const affected = lanes.value.filter((x) => loadedModel(x.id) === argsForm.modelId)
    for (const x of affected) await api.restart(x.id)
    models.value = await api.models().catch(() => models.value)
    argsForm.open = false
    message.success(t('admin.lanes.argsSaved', { n: affected.length }))
    setTimeout(refresh, 800)
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <p class="muted intro">{{ t('admin.lanes.intro') }}</p>

  <section class="card">
    <div class="card-header">
      <h3 class="card-title">{{ t('admin.lanes.gpus') }}</h3>
    </div>
    <div v-if="!gpus.length" class="empty">{{ t('admin.lanes.noGpus') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <tbody>
          <tr v-for="g in gpus" :key="g.index">
            <td class="num nowrap"><Cpu :size="14" class="faint" /> GPU {{ g.index }}</td>
            <td>{{ g.name }}</td>
            <td class="muted num nowrap">{{ (g.memory_total_mb / 1024).toFixed(0) }} GB</td>
            <td class="right">
              <span v-if="owner(g.index)" class="badge brand">{{ owner(g.index)!.name }}</span>
              <span v-else class="badge">{{ t('admin.lanes.unassigned') }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card lanes">
    <div class="card-header">
      <h3 class="card-title">{{ t('admin.lanes.title') }}</h3>
      <NButton size="small" type="primary" @click="openNew">
        <template #icon><Plus :size="14" /></template>
        {{ t('admin.lanes.add') }}
      </NButton>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.lanes.name') }}</th>
            <th>GPU</th>
            <th>{{ t('admin.lanes.idleMode') }}</th>
            <th>{{ t('admin.lanes.now') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in lanes" :key="l.id">
            <td class="strong">{{ l.name }}</td>
            <td class="num">{{ l.gpus.length ? l.gpus.join(', ') : t('admin.lanes.allGpus') }}</td>
            <td class="small">
              <span v-if="l.idle_mode === 'devenv'">{{ t('admin.lanes.idle.devenv') }} · {{ l.idle_template_name ?? '—' }}</span>
              <span v-else-if="l.idle_mode === 'none'" class="muted">{{ t('admin.lanes.idle.none') }}</span>
              <span v-else class="mono">{{ l.default_model_name ?? '—' }}</span>
            </td>
            <td class="muted small">
              <template v-if="laneState(l.id)">
                {{ t(`state.${laneState(l.id)!.runtime.state}`) }}
                <span v-if="laneState(l.id)!.runtime.model_name" class="mono"> · {{ laneState(l.id)!.runtime.model_name }}</span>
                <span v-else-if="laneState(l.id)!.shared_state">
                  · {{ t('admin.lanes.idle.devenv') }} ({{ t(`devenv.state.${laneState(l.id)!.shared_state}`) }})
                </span>
              </template>
            </td>
            <td class="right nowrap">
              <NButton size="small" quaternary :disabled="!loadedModel(l.id)" @click="openArgs(l)">
                {{ t('admin.lanes.args') }}
              </NButton>
              <NButton size="small" quaternary @click="reload(l)">{{ t('admin.lanes.reload') }}</NButton>
              <NButton size="small" quaternary @click="openEdit(l)">{{ t('common.edit') }}</NButton>
              <NButton size="small" quaternary type="error" :disabled="lanes.length <= 1" @click="remove(l)">
                {{ t('common.delete') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <NModal
    v-model:show="form.open"
    preset="card"
    :title="form.id === null ? t('admin.lanes.add') : t('admin.lanes.edit')"
    :bordered="false"
    style="width: 480px; max-width: calc(100vw - 32px)"
  >
    <div class="fields">
      <label>
        <span class="field-label">{{ t('admin.lanes.name') }}</span>
        <NInput v-model:value="form.name" maxlength="64" />
      </label>
      <div>
        <span class="field-label">GPU</span>
        <p v-if="!gpus.length" class="faint small no-margin">{{ t('admin.lanes.noGpus') }}</p>
        <div v-else class="gpu-picks">
          <NCheckbox
            v-for="g in gpus"
            :key="g.index"
            :checked="form.gpus.includes(g.index)"
            :disabled="takenElsewhere(g.index)"
            @update:checked="(v: boolean) => toggleGpu(g.index, v)"
          >
            GPU {{ g.index }} <span class="faint">· {{ g.name }}</span>
            <span v-if="takenElsewhere(g.index)" class="faint"> ({{ owner(g.index)!.name }})</span>
          </NCheckbox>
        </div>
        <p class="faint small hint">{{ t('admin.lanes.gpuHint') }}</p>
      </div>
      <label>
        <span class="field-label">{{ t('admin.lanes.defaultModel') }}</span>
        <NSelect v-model:value="form.model" :options="modelOptions" filterable />
      </label>
      <label>
        <span class="field-label">{{ t('admin.lanes.idleMode') }}</span>
        <NSelect v-model:value="form.idle" :options="idleOptions" data-field="idle-mode" />
      </label>
      <label v-if="form.idle === 'devenv'">
        <span class="field-label">{{ t('admin.lanes.idleTemplate') }}</span>
        <NSelect v-model:value="form.template" :options="templateOptions" filterable data-field="idle-template" />
        <p class="faint small hint">{{ t('admin.lanes.idleHint') }}</p>
        <p v-if="!devenvEnabled" class="small hint warn-text">{{ t('admin.lanes.idleNoDevenv') }}</p>
      </label>
    </div>
    <template #footer>
      <div class="footer">
        <NButton @click="form.open = false">{{ t('common.cancel') }}</NButton>
        <NButton
          type="primary"
          :loading="busy"
          :disabled="!form.name.trim() || (form.idle === 'devenv' && !form.template)"
          @click="save"
        >
          {{ t('common.save') }}
        </NButton>
      </div>
    </template>
  </NModal>
  <NModal v-model:show="argsForm.open" preset="card" :title="t('admin.lanes.argsTitle', { model: argsForm.name })" style="max-width: 560px">
    <NInput
      v-model:value="argsForm.args"
      type="textarea"
      class="mono"
      :autosize="{ minRows: 3, maxRows: 8 }"
      placeholder="-ngl 99 -c 8192 --flash-attn on"
    />
    <p class="faint small args-hint">{{ t('admin.lanes.argsHint') }}</p>
    <template #footer>
      <div class="footer">
        <NButton @click="argsForm.open = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" @click="saveArgs">{{ t('admin.lanes.argsSave') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
}
.lanes {
  margin-top: 16px;
}
.strong {
  font-weight: 600;
}
.small {
  font-size: 12.5px;
}
.nowrap {
  white-space: nowrap;
}
.fields {
  display: grid;
  gap: 16px;
}
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 6px;
}
.gpu-picks {
  display: grid;
  gap: 6px;
}
.hint {
  margin: 6px 0 0;
}
.no-margin {
  margin: 0;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.args-hint {
  margin-top: 8px;
}
.warn-text {
  color: var(--warning, #b45309);
}
</style>
