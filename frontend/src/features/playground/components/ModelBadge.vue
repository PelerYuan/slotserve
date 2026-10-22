<script setup lang="ts">
// The one model availability badge (F05): callable now / available after booking / in use by someone. The state
// comes from /api/status (backend api/status.py), so every page that shows a model says the same.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CalendarClock, Lock, Zap } from 'lucide-vue-next'
import { formatTime } from '@/utils/format'
import type { ModelAvailability } from '../api'

const props = withDefaults(defineProps<{ model: ModelAvailability | null | undefined; short?: boolean }>(), {
  short: false,
})
const { t } = useI18n()

const view = computed(() => {
  const m = props.model
  if (!m) return null
  if (m.state === 'now')
    return { tone: m.loading ? 'loading' : 'now', icon: Zap, text: t(m.loading ? 'playground.badge.loading' : 'playground.badge.now'), title: '' }
  if (m.state === 'busy') {
    const user = m.holder ?? '?'
    const text =
      props.short || !m.until
        ? t('playground.badge.busyShort', { user })
        : t('playground.badge.busy', { user, time: formatTime(m.until) })
    return { tone: 'busy', icon: Lock, text, title: m.until ? t('playground.badge.busy', { user, time: formatTime(m.until) }) : '' }
  }
  return { tone: 'booking', icon: CalendarClock, text: t('playground.badge.booking'), title: t('playground.badge.bookingHint') }
})
</script>

<template>
  <span v-if="view" class="avail-wrap">
    <span class="avail" :class="view.tone" :title="view.title || undefined" :data-availability="model?.state">
      <component :is="view.icon" :size="12" :stroke-width="2.2" />
      <span>{{ view.text }}</span>
    </span>
    <span v-if="model?.args_pending && !short" class="avail pending" :title="t('playground.badge.argsPendingHint')">
      {{ t('playground.badge.argsPending') }}
    </span>
  </span>
</template>

<style scoped>
.avail-wrap {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.avail {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 550;
  line-height: 20px;
  padding: 0 8px;
  border-radius: 999px;
  white-space: nowrap;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.avail svg {
  flex: none;
}
.now {
  background: var(--ok-soft);
  color: var(--ok-text);
}
.loading {
  background: var(--ok-soft);
  color: var(--ok-text);
  opacity: 0.85;
}
.booking {
  background: var(--warn-soft);
  color: var(--warn-text);
}
.busy {
  background: var(--neutral-soft);
  color: var(--text-2);
}
.pending {
  background: var(--orange-soft);
  color: var(--orange-text);
}
</style>
