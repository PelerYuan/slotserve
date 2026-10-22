<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NModal } from 'naive-ui'
import { ArrowRight } from 'lucide-vue-next'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime } from '@/utils/format'
import { settingsApi, type Setting, type SettingHistoryEntry } from '../api'
import { useSettingText } from '../text'

// Who changed one setting when, from what to what; "Undo" goes through the same preview as a save. Secrets are
// only fingerprinted, so they cannot be undone: they are entered again.
const show = defineModel<boolean>('show', { required: true })
const props = defineProps<{ setting: Setting | null }>()
const emit = defineEmits<{ undo: [auditId: number] }>()
const { t } = useI18n()
const { label, display } = useSettingText()
const { showError } = useErrorMessage()

const entries = ref<SettingHistoryEntry[]>([])
const loaded = ref(false)

async function load() {
  if (!props.setting) return
  loaded.value = false
  try {
    entries.value = await settingsApi.history(props.setting.key)
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
watch(show, (v) => v && load())
defineExpose({ load })
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :bordered="false"
    :title="setting ? t('settingsUi.historyTitle', { name: label(setting) }) : ''"
    style="width: 560px; max-width: calc(100vw - 32px)"
    data-testid="setting-history"
  >
    <div v-if="loaded && !entries.length" class="empty">{{ t('settingsUi.historyEmpty') }}</div>
    <ul v-else-if="setting" class="entries">
      <li v-for="e in entries" :key="e.audit_id">
        <div class="when">
          <span class="num muted">{{ formatDateTime(e.created_at) }}</span>
          <span class="who">{{ e.actor_name || t('admin.audit.system') }}</span>
          <span v-if="e.action !== 'settings.update'" class="badge">{{ t(`admin.audit.actions.${e.action.replace('.', '_')}`) }}</span>
        </div>
        <div class="values">
          <span class="before">{{ display(setting, e.before) }}</span>
          <ArrowRight :size="13" class="arrow" />
          <span class="after">{{ e.reset ? t('settingsUi.historyReset') : display(setting, e.after) }}</span>
          <span class="spacer" />
          <NButton v-if="e.undoable" size="tiny" secondary @click="emit('undo', e.audit_id)">{{ t('settingsUi.undo') }}</NButton>
          <span v-else class="faint small">{{ t('settingsUi.reenter') }}</span>
        </div>
      </li>
    </ul>
  </NModal>
</template>

<style scoped>
.entries {
  list-style: none;
  margin: 0;
  padding: 0;
}
.entries li {
  padding: 10px 0;
  border-top: 1px solid var(--border);
}
.entries li:first-child {
  border-top: none;
  padding-top: 0;
}
.when {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 12.5px;
}
.who {
  font-weight: 600;
}
.values {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.before {
  color: var(--text-3);
}
.after {
  font-weight: 600;
}
.arrow {
  color: var(--text-3);
}
.small {
  font-size: 12px;
}
</style>
