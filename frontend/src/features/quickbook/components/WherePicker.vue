<script setup lang="ts">
// QuickBook's "Change": every place placement.fit judged, with why it fits or not. On one machine the places
// are lanes; with several machines (W4) the machine picker takes this slot and gets the same candidates.
import { useI18n } from 'vue-i18n'
import { laneLabel } from '@/utils/naming'
import type { Candidate } from '../api'
import { useReasonText } from '../reasons'

/** the chosen lane; null = automatic (the best candidate) */
const selected = defineModel<number | null>({ default: null })
defineProps<{ candidates: Candidate[] }>()

const { t } = useI18n()
const reasonText = useReasonText()

const name = (c: Candidate) => laneLabel({ name: c.lane_name ?? '', gpus: c.gpus })
const why = (c: Candidate) =>
  (c.fits ? c.reasons_ok.filter((r) => r.code !== 'quota_left' && r.code !== 'needs_approval') : c.reasons_no.slice(0, 1))
    .map(reasonText)
    .join(' · ')
</script>

<template>
  <div class="where-picker" role="radiogroup">
    <button
      type="button"
      role="radio"
      class="option"
      :class="{ on: selected === null }"
      :aria-checked="selected === null"
      data-lane="auto"
      @click="selected = null"
    >
      <b>{{ t('quickbook.keepAuto') }}</b>
    </button>
    <button
      v-for="c in candidates"
      :key="c.lane_id ?? -1"
      type="button"
      role="radio"
      class="option"
      :class="{ on: selected === c.lane_id, off: !c.fits }"
      :aria-checked="selected === c.lane_id"
      :disabled="!c.fits"
      :data-lane="c.lane_name"
      @click="selected = c.lane_id"
    >
      <b class="name">{{ name(c) }}</b>
      <span class="why" :class="{ warn: !c.fits }">{{ why(c) }}</span>
    </button>
  </div>
</template>

<style scoped>
.where-picker {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 8px;
  width: 100%;
}
.option {
  display: grid;
  gap: 3px;
  text-align: left;
  font: inherit;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  cursor: pointer;
  min-width: 0;
}
.option:hover:not(:disabled) {
  border-color: var(--border-strong);
}
.option.on {
  border-color: var(--brand);
  background: var(--brand-softer);
  box-shadow: 0 0 0 1px var(--brand) inset;
}
.option.off {
  opacity: 0.6;
  cursor: not-allowed;
}
.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.why {
  font-size: 11.5px;
  color: var(--text-2);
}
.why.warn {
  color: var(--warn);
}
</style>
