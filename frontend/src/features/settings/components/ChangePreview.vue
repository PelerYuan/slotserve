<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NModal } from 'naive-ui'
import { ArrowRight, ShieldAlert } from 'lucide-vue-next'
import { formatRange } from '@/utils/format'
import type { Impact, ImpactUser, Setting, SettingsPreview } from '../api'
import { useSettingText } from '../text'

// What a save (or an undo) would do, before it is done (F07): every change from → to, who and which bookings the
// new limits affect (bookings are kept), an option to tell those people, and a note when the password is needed.
const show = defineModel<boolean>('show', { required: true })
const props = withDefaults(
  defineProps<{ preview: SettingsPreview | null; settings: Record<string, Setting>; undo?: boolean; busy?: boolean }>(),
  { undo: false, busy: false },
)
const emit = defineEmits<{ confirm: [notify: boolean] }>()
const { t, te } = useI18n()
const { label, display } = useSettingText()

const notify = ref(false)
watch(show, (v) => v && (notify.value = false))

const changes = computed(() => props.preview?.changes ?? [])
const impacts = computed(() => props.preview?.impacts ?? [])
const danger = computed(() => changes.value.some((c) => c.danger))
const notifyCount = computed(() => props.preview?.notify_user_ids.length ?? 0)

function value(key: string, v: unknown, reset = false): string {
  const s = props.settings[key]
  const text = s ? display(s, v as never) : String(v ?? '—')
  return reset ? `${text} ${t('settingsUi.preview.reset')}` : text
}

function impactText(i: Impact): string {
  const p: Record<string, unknown> = { ...i.params }
  const s = props.settings[i.key]
  if (s) {
    if ('after' in p) p.after = display(s, p.after as never)
    if ('before' in p) p.before = display(s, p.before as never)
  }
  let key = i.text_key
  if (i.key === 'approval_mode' && p.mode === 'off') key = `${i.text_key}_off`
  return te(key) ? t(key, p) : key
}

function userDetail(i: Impact, u: ImpactUser): string {
  if (typeof u.hours_this_week === 'number')
    return t('settingsUi.preview.hoursThisWeek', { hours: u.hours_this_week, limit: u.limit_this_week })
  if (typeof u.open_bookings === 'number') return t('settingsUi.preview.openBookings', { n: u.open_bookings })
  if (typeof u.bookings === 'number') return t('settingsUi.preview.bookingsN', { n: u.bookings })
  return ''
}
const reason = (r: string) => (te(`settingsUi.preview.reasons.${r}`) ? t(`settingsUi.preview.reasons.${r}`) : r)
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :bordered="false"
    :title="undo ? t('settingsUi.preview.undoTitle') : t('settingsUi.preview.title', { n: changes.length })"
    style="width: 600px; max-width: calc(100vw - 32px)"
    data-testid="settings-preview"
  >
    <div class="changes">
      <div v-for="c in changes" :key="c.key" class="change" :data-key="c.key">
        <div class="change-name">{{ settings[c.key] ? label(settings[c.key]) : c.key }}</div>
        <div class="change-values">
          <span class="before">{{ value(c.key, c.before) }}</span>
          <ArrowRight :size="14" class="arrow" />
          <span class="after">{{ value(c.key, c.after, c.reset) }}</span>
        </div>
      </div>
    </div>

    <template v-if="impacts.length">
      <div class="section-title">{{ t('settingsUi.preview.impact') }}</div>
      <div v-for="i in impacts" :key="i.key" class="impact" :data-impact="i.key">
        <p class="impact-text">{{ i.users.length || i.reservations.length ? impactText(i) : t('settingsUi.preview.none') }}</p>
        <ul v-if="i.users.length" class="users">
          <li v-for="u in i.users" :key="u.user_id" :class="{ over: u.over }">
            <span class="uname">{{ u.username }}</span>
            <span class="faint">{{ userDetail(i, u) }}</span>
            <span v-if="u.over" class="badge danger">{{ t('settingsUi.preview.over') }}</span>
          </li>
        </ul>
        <details v-if="i.reservations.length" class="bookings">
          <summary>{{ t('settingsUi.preview.bookings', { n: i.params.reservations ?? i.reservations.length }) }}</summary>
          <ul>
            <li v-for="r in i.reservations" :key="r.id">
              <span class="uname">{{ r.username }}</span>
              <span class="num muted">{{ formatRange(r.start_at, r.end_at) }}</span>
              <span class="faint">{{ reason(r.reason) }}</span>
            </li>
          </ul>
        </details>
      </div>
      <p class="faint small">{{ t('settingsUi.preview.kept') }}</p>
      <NCheckbox v-if="notifyCount" v-model:checked="notify" data-testid="preview-notify">
        {{ t('settingsUi.preview.notify', { n: notifyCount }) }}
      </NCheckbox>
    </template>

    <div v-if="danger" class="danger-note">
      <ShieldAlert :size="16" />
      <span>{{ t('settingsUi.preview.danger') }}</span>
    </div>

    <template #footer>
      <div class="footer">
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" data-testid="preview-confirm" @click="emit('confirm', notify)">
          {{ undo ? t('settingsUi.preview.confirmUndo') : t('settingsUi.preview.confirm') }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.changes {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.change {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr);
  gap: 12px;
  padding: 9px 12px;
  border-top: 1px solid var(--border);
  font-size: 13px;
}
.change:first-child {
  border-top: none;
}
.change-name {
  font-weight: 500;
}
.change-values {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.before {
  color: var(--text-3);
  text-decoration: line-through;
  overflow-wrap: anywhere;
}
.after {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.arrow {
  color: var(--text-3);
  flex: none;
}
.section-title {
  margin: 16px 0 6px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-3);
}
.impact + .impact {
  margin-top: 10px;
}
.impact-text {
  margin: 0 0 6px;
}
.users,
.bookings ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.users li,
.bookings li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding: 4px 0;
  font-size: 13px;
}
.uname {
  font-weight: 600;
}
.users li.over .uname {
  color: var(--danger-text);
}
.bookings {
  margin-top: 6px;
  font-size: 13px;
}
.bookings summary {
  cursor: pointer;
  color: var(--brand);
}
.small {
  font-size: 12px;
}
.danger-note {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-top: 14px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger-text);
  font-size: 13px;
}
.danger-note svg {
  flex: none;
  margin-top: 2px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 520px) {
  .change {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
</style>
