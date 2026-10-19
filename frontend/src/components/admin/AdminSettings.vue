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

const GROUPS: { id: string; keys: string[] }[] = [
  { id: 'access', keys: ['registration_open', 'invite_code'] },
  { id: 'booking', keys: ['max_reservation_hours', 'max_advance_days', 'quota_weekly_hours', 'quota_upcoming'] },
  { id: 'models', keys: ['default_llama_args', 'hf_endpoint', 'hf_token'] },
  { id: 'notify', keys: ['telegram_bot_token', 'telegram_proxy', 'public_url', 'timezone'] },
  { id: 'monitor', keys: ['metrics_token'] },
  { id: 'maintenance', keys: ['drain_seconds', 'usage_retention_days', 'audit_retention_days', 'backup_keep'] },
]
const groups = computed(() =>
  GROUPS.map((g) => ({
    id: g.id,
    items: g.keys.map((k) => settings.value.find((s) => s.key === k)).filter((s): s is AdminSetting => !!s),
  })).filter((g) => g.items.length),
)
const ZERO_OK = new Set(['drain_seconds', 'usage_retention_days', 'audit_retention_days', 'backup_keep'])
const minFor = (key: string) => (key.startsWith('quota_') || ZERO_OK.has(key) ? 0 : 1)

const display = (s: AdminSetting, v: SettingValue) =>
  typeof v === 'boolean'
    ? v
      ? t('admin.settings.on')
      : t('admin.settings.off')
    : v === ''
      ? t('admin.settings.notSet')
      : s.secret
        ? '••••••'
        : String(v)
</script>

<template>
  <p class="muted intro">{{ t('admin.settings.intro') }}</p>
  <section v-for="g in groups" :key="g.id" class="card group">
    <div class="group-title">{{ t(`admin.settings.groups.${g.id}`) }}</div>
    <div v-for="s in g.items" :key="s.key" class="item">
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
          :min="minFor(s.key)"
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
  </section>
  <div class="actions sticky" :class="{ dirty }">
    <span class="faint small">{{ dirty ? t('admin.settings.unsaved') : '' }}</span>
    <NButton :disabled="!dirty" @click="adopt(settings)">{{ t('common.cancel') }}</NButton>
    <NButton type="primary" :disabled="!dirty" :loading="busy" @click="save(changes)">{{ t('common.save') }}</NButton>
  </div>
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
.group {
  margin-bottom: 16px;
}
.group-title {
  padding: 12px 20px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
  background: var(--surface-muted);
  border-bottom: 1px solid var(--border);
}
.group .item:last-child {
  border-bottom: none;
}
.actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
}
.actions .small {
  margin-right: auto;
  font-size: 12.5px;
}
.sticky {
  position: sticky;
  bottom: 0;
  background: linear-gradient(to top, var(--bg) 70%, transparent);
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
