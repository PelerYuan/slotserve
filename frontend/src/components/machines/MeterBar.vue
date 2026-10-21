<script setup lang="ts">
// A labelled bar: how full something is (memory, CPU, a GPU), coloured by level.
import { computed } from 'vue'
import { level } from '@/utils/machines'

const props = defineProps<{ label: string; percent: number | null; text?: string; sub?: string }>()
const tone = computed(() => level(props.percent))
</script>

<template>
  <div class="meter">
    <div class="meter-head">
      <span class="meter-label">{{ label }}</span>
      <span class="meter-text num">{{ text ?? (percent == null ? '—' : `${percent.toFixed(0)}%`) }}</span>
    </div>
    <div class="meter-track" role="meter" :aria-label="label" :aria-valuenow="percent ?? undefined" aria-valuemin="0" aria-valuemax="100">
      <div class="meter-fill" :class="tone" :style="{ width: `${percent ?? 0}%` }" />
    </div>
    <div v-if="sub" class="meter-sub faint">{{ sub }}</div>
  </div>
</template>

<style scoped>
.meter {
  min-width: 0;
}
.meter-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}
.meter-label {
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meter-text {
  font-weight: 600;
  white-space: nowrap;
}
.meter-track {
  height: 6px;
  border-radius: 3px;
  background: var(--rail);
  margin-top: 4px;
  overflow: hidden;
}
.meter-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--brand);
  transition: width 0.3s;
}
.meter-fill.ok {
  background: var(--ok);
}
.meter-fill.warn {
  background: var(--warn);
}
.meter-fill.danger {
  background: var(--danger);
}
.meter-sub {
  font-size: 11.5px;
  margin-top: 3px;
}
</style>
