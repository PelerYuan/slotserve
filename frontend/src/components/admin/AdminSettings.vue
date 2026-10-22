<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NInputNumber, NSelect, NSwitch, useMessage } from 'naive-ui'
import { api } from '@/api/client'
import type { AdminSetting, SettingValue } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'

const { t, te } = useI18n()
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
  { id: 'access', keys: ['registration_open', 'invite_code', 'approval_mode', 'approval_hours', 'api_key_reveal'] },
  {
    id: 'devenv',
    keys: [
      'devenv_enabled',
      'devenv_ssh_host',
      'devenv_ssh_ports',
      'devenv_cpus',
      'devenv_memory_gb',
      'devenv_shm_gb',
      'devenv_custom_images',
      'devenv_allowed_registries',
      'devenv_saved_images',
      'devenv_pull_minutes',
      'devenv_password_login',
      'devenv_storage_dir',
      'devenv_projects',
      'devenv_project_days',
      // devenv_cpu: environments without a GPU
      'devenv_cpu_enabled',
      'devenv_cpu_per_user',
      'devenv_cpu_cores_per_gpu',
      'devenv_cpu_cores',
      'devenv_cpu_memory_gb',
      'devenv_cpu_memory_pool_gb',
      'devenv_public_ports_enabled',
      'devenv_public_ports',
      'devenv_public_port_limit',
      'devenv_forwarder_image',
      'devenv_web_url',
      'devenv_web_compat',
    ],
  },
  {
    id: 'booking',
    keys: [
      'slot_minutes',
      'max_reservation_hours',
      'max_advance_days',
      'quota_weekly_hours',
      'quota_upcoming',
      'reminder_minutes',
    ],
  },
  {
    id: 'runtime',
    keys: [
      'preload_min_minutes',
      'preload_buffer_seconds',
      'load_timeout_seconds',
      'stop_timeout_seconds',
      'drain_seconds',
      'disk_read_mbps',
      'scheduler_interval_seconds',
    ],
  },
  { id: 'models', keys: ['default_llama_args', 'download_min_free_gb', 'hf_endpoint', 'hf_token'] },
  { id: 'engines', keys: ['vllm_cmd', 'sglang_cmd', 'custom_engine_cmd', 'custom_engine_health'] },
  { id: 'notify', keys: ['telegram_bot_token', 'telegram_proxy', 'public_url', 'timezone', 'chat_default_locale'] },
  { id: 'feishu', keys: ['feishu_enabled', 'feishu_app_id', 'feishu_app_secret', 'feishu_domain'] },
  { id: 'dingtalk', keys: ['dingtalk_enabled', 'dingtalk_client_id', 'dingtalk_client_secret', 'dingtalk_robot_code'] },
  {
    id: 'wecom',
    keys: ['wecom_enabled', 'wecom_corp_id', 'wecom_agent_id', 'wecom_secret', 'wecom_token', 'wecom_aes_key'],
  },
  { id: 'qq', keys: ['qq_enabled', 'qq_app_id', 'qq_app_secret', 'qq_sandbox'] },
  {
    id: 'webhooks',
    keys: [
      'group_events',
      'feishu_webhook_url',
      'feishu_webhook_secret',
      'dingtalk_webhook_url',
      'dingtalk_webhook_secret',
      'wecom_webhook_url',
    ],
  },
  { id: 'monitor', keys: ['metrics_token', 'usage_busy_percent'] },
  { id: 'maintenance', keys: ['usage_retention_days', 'usage_sample_days', 'audit_retention_days', 'backup_keep'] },
]
const groups = computed(() => {
  const known = new Set(GROUPS.flatMap((g) => g.keys))
  const list = GROUPS.map((g) => ({
    id: g.id,
    items: g.keys.map((k) => settings.value.find((s) => s.key === k)).filter((s): s is AdminSetting => !!s),
  }))
  // Settings added on the server later still show up, under "Other".
  list.push({ id: 'other', items: settings.value.filter((s) => !known.has(s.key)) })
  return list.filter((g) => g.items.length)
})

// Comma-separated settings edited as a multiple choice.
const MULTI: Record<string, string[]> = { group_events: ['admin_alerts', 'approvals', 'announcements'] }
const multiOptions = (key: string) => MULTI[key].map((v) => ({ label: t(`admin.channels.kinds.${v}`), value: v }))
const splitList = (v: SettingValue) => (typeof v === 'string' && v ? v.split(',').map((x) => x.trim()) : [])

const label = (s: AdminSetting) => (te(`admin.settings.${s.key}`) ? t(`admin.settings.${s.key}`) : s.key)
const desc = (s: AdminSetting) => (te(`admin.settings.${s.key}_desc`) ? t(`admin.settings.${s.key}_desc`) : '')
const unit = (s: AdminSetting) =>
  s.unit ? (te(`admin.settings.units.${s.unit}`) ? t(`admin.settings.units.${s.unit}`) : s.unit) : ''
const choiceOptions = (s: AdminSetting) =>
  (s.choices ?? []).map((c) => {
    const key = `admin.settings.choice.${s.key}.${c}`
    return { label: te(key) ? t(key) : `${c} ${unit(s)}`.trim(), value: c }
  })

// A value as people read it: choices and lists by their labels, never the raw stored text.
const display = (s: AdminSetting, v: SettingValue) => {
  if (typeof v === 'boolean') return v ? t('admin.settings.on') : t('admin.settings.off')
  if (v === '') return t('admin.settings.notSet')
  if (s.secret) return '••••••'
  if (MULTI[s.key]) return multiOptions(s.key).filter((o) => splitList(v).includes(o.value)).map((o) => o.label).join(', ')
  if (s.choices) return choiceOptions(s).find((o) => o.value === v)?.label ?? `${v}`
  return `${v}${s.unit ? ' ' + unit(s) : ''}`
}
// An empty field uses the default, so the default is what it shows.
const placeholder = (s: AdminSetting) => display(s, s.default)
</script>

<template>
  <p class="muted intro">{{ t('admin.settings.intro') }}</p>
  <section v-for="g in groups" :key="g.id" class="card group">
    <div class="group-title">{{ t(`admin.settings.groups.${g.id}`) }}</div>
    <div v-for="s in g.items" :key="s.key" class="item">
      <div class="text">
        <div class="title">
          {{ label(s) }}
          <span v-if="s.overridden" class="badge brand">{{ t('admin.settings.overridden') }}</span>
        </div>
        <div v-if="desc(s)" class="muted desc">{{ desc(s) }}</div>
        <div class="faint def">
          {{ t('admin.settings.default', { value: display(s, s.default) }) }}
          <a v-if="s.overridden" href="#" @click.prevent="save({ [s.key]: null })">{{ t('admin.settings.reset') }}</a>
        </div>
      </div>
      <div class="control">
        <NSelect
          v-if="MULTI[s.key]"
          multiple
          :value="splitList(draft[s.key])"
          :options="multiOptions(s.key)"
          :placeholder="placeholder(s)"
          @update:value="(v: string[]) => (draft[s.key] = v.join(','))"
        />
        <NSwitch v-else-if="s.kind === 'bool'" v-model:value="draft[s.key] as boolean" />
        <NSelect
          v-else-if="s.choices"
          v-model:value="draft[s.key] as number | string"
          :options="choiceOptions(s)"
          :placeholder="placeholder(s)"
          class="num-input"
        />
        <NInputNumber
          v-else-if="s.kind === 'int' || s.kind === 'float'"
          v-model:value="draft[s.key] as number"
          :min="s.min ?? undefined"
          :max="s.max ?? undefined"
          :precision="s.kind === 'int' ? 0 : undefined"
          :step="s.kind === 'float' ? 0.5 : 1"
          :placeholder="placeholder(s)"
          class="num-input"
        >
          <template v-if="s.unit" #suffix>{{ unit(s) }}</template>
        </NInputNumber>
        <NInput
          v-else-if="s.secret"
          v-model:value="draft[s.key] as string"
          type="password"
          show-password-on="click"
          :placeholder="placeholder(s)"
        />
        <NInput v-else v-model:value="draft[s.key] as string" class="mono-input" :placeholder="placeholder(s)" />
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
  width: 170px;
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
