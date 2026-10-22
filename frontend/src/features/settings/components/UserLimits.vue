<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDatePicker, NInput, NInputNumber, NModal } from 'naive-ui'
import { Plus } from 'lucide-vue-next'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDate, formatDateTime } from '@/utils/format'
import { settingsApi, type QuotaGrant, type UserPolicy } from '../api'

// The limits one person books under (global settings + temporary extra hours, F07): give extra hours until a day
// ("10 more hours a week until 10/29, reason: thesis defence") or withdraw them.
const show = defineModel<boolean>('show', { required: true })
const props = defineProps<{ userId: number | null; username: string }>()
const emit = defineEmits<{ changed: [policy: UserPolicy] }>()
const { t } = useI18n()
const { showError, message } = useErrorMessage()

const policy = ref<UserPolicy | null>(null)
const form = reactive({ open: false, hours: 10 as number | null, until: null as number | null, reason: '', busy: false })

async function load() {
  if (props.userId === null) return
  try {
    policy.value = await settingsApi.policy(props.userId)
    emit('changed', policy.value)
  } catch (e) {
    showError(e)
  }
}
watch(show, (v) => {
  if (!v) return
  policy.value = null
  const week = new Date()
  week.setDate(week.getDate() + 7)
  Object.assign(form, { open: false, hours: 10, until: week.getTime(), reason: '', busy: false })
  load()
})

const hours = (n: number) => (n ? t('settingsUi.limits.hours', { n: Math.round(n * 10) / 10 }) : t('settingsUi.limits.unlimited'))
const days = (n: number) => (n ? t('settingsUi.limits.days', { n }) : t('settingsUi.limits.unlimited'))
const grants = computed(() =>
  [...(policy.value?.grants ?? [])].sort((a, b) => Number(b.active) - Number(a.active) || b.created_at.localeCompare(a.created_at)),
)
const grantState = (g: QuotaGrant) =>
  g.revoked_at ? t('settingsUi.limits.revoked') : g.active ? t('settingsUi.limits.active') : t('settingsUi.limits.ended')
// the picked day counts in full
const disablePast = (ts: number) => ts < new Date().setHours(0, 0, 0, 0)

async function give() {
  if (props.userId === null || !form.hours || !form.until) return
  form.busy = true
  try {
    const end = new Date(form.until)
    end.setHours(23, 59, 0, 0)
    await settingsApi.addGrant(props.userId, {
      extra_hours: form.hours,
      until: end.toISOString(),
      reason: form.reason.trim(),
    })
    message.success(t('settingsUi.limits.given', { name: props.username }))
    form.open = false
    await load()
  } catch (e) {
    showError(e)
  } finally {
    form.busy = false
  }
}

async function revoke(g: QuotaGrant) {
  if (props.userId === null) return
  try {
    await settingsApi.revokeGrant(props.userId, g.id)
    message.success(t('settingsUi.limits.withdrawn'))
    await load()
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :bordered="false"
    :title="t('settingsUi.limits.title', { name: username })"
    style="width: 560px; max-width: calc(100vw - 32px)"
    data-testid="user-limits"
  >
    <template v-if="policy">
      <p v-if="policy.exempt" class="muted">{{ t('settingsUi.limits.adminExempt') }}</p>
      <dl class="facts">
        <div>
          <dt>{{ t('settingsUi.limits.weekly') }}</dt>
          <dd>
            <strong>{{ hours(policy.quota_weekly_hours) }}</strong>
            <span v-if="policy.extra_hours" class="faint">
              · {{ t('settingsUi.limits.weeklyDetail', { base: policy.base_weekly_hours, extra: policy.extra_hours }) }}
            </span>
          </dd>
        </div>
        <div>
          <dt>{{ t('settingsUi.limits.booked') }}</dt>
          <dd class="num">{{ t('settingsUi.limits.hours', { n: policy.booked_hours_this_week }) }}</dd>
        </div>
        <div>
          <dt>{{ t('settingsUi.limits.open') }}</dt>
          <dd class="num">{{ policy.open_bookings }}<span v-if="policy.quota_upcoming" class="faint"> / {{ policy.quota_upcoming }}</span></dd>
        </div>
        <div>
          <dt>{{ t('settingsUi.limits.longest') }}</dt>
          <dd>{{ hours(policy.max_reservation_hours) }}</dd>
        </div>
        <div>
          <dt>{{ t('settingsUi.limits.ahead') }}</dt>
          <dd>{{ days(policy.max_advance_days) }}</dd>
        </div>
      </dl>

      <div class="grants-head">
        <h4>{{ t('settingsUi.limits.grants') }}</h4>
        <NButton v-if="!form.open" size="small" secondary type="primary" data-testid="give-hours" @click="form.open = true">
          <template #icon><Plus :size="14" /></template>
          {{ t('settingsUi.limits.give') }}
        </NButton>
      </div>
      <div v-if="form.open" class="grant-form">
        <label class="field">
          <span class="field-label">{{ t('settingsUi.limits.extraHours') }}</span>
          <NInputNumber
            v-model:value="form.hours"
            :min="0.5"
            :max="168"
            :step="1"
            :placeholder="t('settingsUi.limits.extraHoursPh')"
            data-field="grant-hours"
          />
        </label>
        <label class="field">
          <span class="field-label">{{ t('settingsUi.limits.until') }}</span>
          <NDatePicker
            v-model:value="form.until"
            type="date"
            :is-date-disabled="disablePast"
            :placeholder="t('settingsUi.limits.untilPh')"
            data-field="grant-until"
          />
        </label>
        <label class="field wide">
          <span class="field-label">{{ t('settingsUi.limits.reason') }}</span>
          <NInput v-model:value="form.reason" :maxlength="200" :placeholder="t('settingsUi.limits.reasonPh')" data-field="grant-reason" />
        </label>
        <div class="form-actions wide">
          <NButton size="small" @click="form.open = false">{{ t('common.cancel') }}</NButton>
          <NButton size="small" type="primary" :disabled="!form.hours || !form.until" :loading="form.busy" data-testid="grant-save" @click="give">
            {{ t('settingsUi.limits.give') }}
          </NButton>
        </div>
      </div>
      <div v-if="!grants.length" class="faint small">{{ t('settingsUi.limits.noGrants') }}</div>
      <ul v-else class="grants">
        <li v-for="g in grants" :key="g.id" :class="{ off: !g.active }">
          <div>
            <div class="strong">{{ t('settingsUi.limits.grantLine', { hours: g.extra_hours, until: formatDate(g.until) }) }}</div>
            <div class="faint small">
              <span class="badge" :class="{ ok: g.active }">{{ grantState(g) }}</span>
              {{ g.reason }} · {{ t('settingsUi.limits.grantBy', { by: g.by }) }} · {{ formatDateTime(g.created_at) }}
            </div>
          </div>
          <NButton v-if="g.active" size="tiny" quaternary type="error" @click="revoke(g)">{{ t('settingsUi.limits.revoke') }}</NButton>
        </li>
      </ul>
    </template>
  </NModal>
</template>

<style scoped>
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px 16px;
  margin: 0 0 18px;
}
.facts dt {
  font-size: 12px;
  color: var(--text-3);
}
.facts dd {
  margin: 2px 0 0;
}
.grants-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.grants-head h4 {
  margin: 0;
  font-size: 14px;
}
.grant-form {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 12px;
  padding: 12px;
  margin-bottom: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-muted);
}
.field {
  display: grid;
  gap: 4px;
}
.field-label {
  font-size: 12.5px;
  font-weight: 500;
}
.wide {
  grid-column: 1 / -1;
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.grants {
  list-style: none;
  margin: 0;
  padding: 0;
}
.grants li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid var(--border);
}
.grants li.off {
  opacity: 0.65;
}
.strong {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
@media (max-width: 520px) {
  .grant-form {
    grid-template-columns: 1fr;
  }
}
</style>
