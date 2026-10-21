<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RuntimeState } from '@/api/types'

const props = withDefaults(defineProps<{ state: RuntimeState; label?: boolean }>(), { label: true })
const { t } = useI18n()

const tone = computed(
  () =>
    ({ ready: 'ok', loading: 'warn', stopping: 'warn', failed: 'danger', stopped: 'idle', offline: 'danger' })[props.state] ?? 'idle',
)
</script>

<template>
  <span class="status" :class="tone">
    <span class="dot" />
    <span v-if="label">{{ t(`state.${state}`) }}</span>
  </span>
</template>

<style scoped>
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 500;
  padding: 2px 9px 2px 8px;
  border-radius: 999px;
  line-height: 20px;
  white-space: nowrap;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.ok {
  color: var(--ok-text);
  background: var(--ok-soft);
}
.warn {
  color: var(--warn-text);
  background: var(--warn-soft);
}
.warn .dot {
  animation: pulse 1.4s ease-in-out infinite;
}
.danger {
  color: var(--danger-text);
  background: var(--danger-soft);
}
.idle {
  color: var(--text-2);
  background: var(--neutral-soft);
}
@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}
</style>
