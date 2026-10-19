<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NInputNumber, NSwitch, useMessage } from 'naive-ui'
import { api } from '@/api/client'
import type { AdminSetting, SettingValue } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()

const settings = ref<AdminSetting[]>([])
const draft = reactive<Record<string, SettingValue>>({})
const busy = ref(false)

function adopt(list: AdminSetting[]) {
  settings.value = list
  for (const s of list) draft[s.key] = s.value
}

onMounted(async () => {
  try {
    adopt(await api.admin.settings())
  } catch (e) {
    showError(e)
  }
})

const changes = computed(() => {
  const out: Record<string, SettingValue> = {}
  for (const s of settings.value) if (draft[s.key] !== s.value && draft[s.key] !== null) out[s.key] = draft[s.key]
  return out
})
const dirty = computed(() => Object.keys(changes.value).length > 0)

async function save(payload: Record<string, SettingValue | null>) {
  busy.value = true
  try {
    adopt(await api.admin.saveSettings(payload))
    session.config = await api.config().catch(() => session.config)
    message.success(t('admin.settings.saved'))
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

const display = (s: AdminSetting, v: SettingValue) =>
  typeof v === 'boolean' ? (v ? t('admin.settings.on') : t('admin.settings.off')) : s.secret ? '••••••' : v === '' ? '""' : String(v)
</script>

<template>
  <p class="muted intro">{{ t('admin.settings.intro') }}</p>
  <section class="card">
    <div v-for="s in settings" :key="s.key" class="item">
      <div class="text">
        <div class="title">
          {{ t(`admin.settings.${s.key}`) }}
          <span v-if="s.overridden" class="badge brand">{{ t('admin.settings.overridden') }}</span>
        </div>
        <div class="muted desc">{{ t(`admin.settings.${s.key}_desc`) }}</div>
        <div class="faint def">
          {{ t('admin.settings.default', { value: display(s, s.default) }) }}
          <a v-if="s.overridden" href="#" @click.prevent="save({ [s.key]: null })">{{ t('admin.settings.reset') }}</a>
        </div>
      </div>
      <div class="control">
        <NSwitch v-if="typeof s.value === 'boolean'" v-model:value="draft[s.key] as boolean" />
        <NInputNumber
          v-else-if="typeof s.value === 'number'"
          v-model:value="draft[s.key] as number"
          :min="1"
          :precision="0"
          class="num-input"
        />
        <NInput
          v-else-if="s.secret"
          v-model:value="draft[s.key] as string"
          type="password"
          show-password-on="click"
        />
        <NInput v-else v-model:value="draft[s.key] as string" class="mono-input" />
      </div>
    </div>
    <div class="actions">
      <NButton :disabled="!dirty" @click="adopt(settings)">{{ t('common.cancel') }}</NButton>
      <NButton type="primary" :disabled="!dirty" :loading="busy" @click="save(changes)">{{ t('common.save') }}</NButton>
    </div>
  </section>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
}
.item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 24px;
  align-items: center;
  padding: 18px 20px;
  border-bottom: 1px solid var(--border);
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.desc {
  margin-top: 2px;
  font-size: 13px;
}
.def {
  margin-top: 4px;
  font-size: 12px;
  display: flex;
  gap: 10px;
}
.control {
  display: flex;
  justify-content: flex-end;
}
.num-input {
  width: 140px;
}
.mono-input :deep(input) {
  font-family: var(--mono);
  font-size: 12.5px;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 14px 20px;
  background: var(--surface-muted);
}
@media (max-width: 760px) {
  .item {
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 16px;
  }
  .control {
    justify-content: flex-start;
  }
}
</style>
