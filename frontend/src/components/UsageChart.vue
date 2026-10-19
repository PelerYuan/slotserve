<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { formatCompact, formatNumber } from '@/utils/format'

/** Stacked daily columns: one segment per series (person). */
const props = defineProps<{
  days: string[] // YYYY-MM-DD, oldest first
  series: { name: string; color: string; values: number[] }[]
}>()

const wrap = ref<HTMLElement | null>(null)
const width = ref(720)
const HEIGHT = 240
const PAD = { top: 12, right: 8, bottom: 26, left: 44 }
let ro: ResizeObserver | undefined

onMounted(() => {
  ro = new ResizeObserver(([entry]) => (width.value = Math.max(280, entry.contentRect.width)))
  if (wrap.value) ro.observe(wrap.value)
})
onBeforeUnmount(() => ro?.disconnect())

const totals = computed(() => props.days.map((_, i) => props.series.reduce((s, x) => s + (x.values[i] ?? 0), 0)))

function niceMax(v: number) {
  if (v <= 0) return 10
  const p = 10 ** Math.floor(Math.log10(v))
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p
  return 10 * p
}

const yMax = computed(() => niceMax(Math.max(...totals.value, 0)))
const ticks = computed(() => [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax.value))
const plotW = computed(() => width.value - PAD.left - PAD.right)
const plotH = HEIGHT - PAD.top - PAD.bottom
const band = computed(() => plotW.value / Math.max(1, props.days.length))
const barW = computed(() => Math.min(24, Math.max(3, band.value * 0.62)))
const y = (v: number) => PAD.top + plotH - (v / yMax.value) * plotH

const GAP = 2
const columns = computed(() =>
  props.days.map((day, i) => {
    const x = PAD.left + i * band.value + (band.value - barW.value) / 2
    let acc = 0
    const segs: { color: string; y: number; h: number; top: boolean }[] = []
    const nonZero = props.series.map((s, k) => ({ s, k, v: s.values[i] ?? 0 })).filter((e) => e.v > 0)
    nonZero.forEach((e, idx) => {
      const y0 = y(acc)
      acc += e.v
      const y1 = y(acc)
      const h = Math.max(1, y0 - y1 - (idx < nonZero.length - 1 ? GAP : 0))
      segs.push({ color: e.s.color, y: y1 + (idx < nonZero.length - 1 ? GAP : 0), h, top: idx === nonZero.length - 1 })
    })
    return { day, i, x, segs }
  }),
)

const labelEvery = computed(() => Math.max(1, Math.ceil(props.days.length / Math.floor(plotW.value / 64))))
function dayLabel(d: string) {
  const [, m, dd] = d.split('-')
  return `${Number(m)}/${Number(dd)}`
}

function segPath(x: number, w: number, top: number, h: number, round: boolean) {
  const r = round ? Math.min(4, h, w / 2) : 0
  return `M${x},${top + h}V${top + r}q0,-${r} ${r},-${r}H${x + w - r}q${r},0 ${r},${r}V${top + h}Z`
}

const hover = ref<number | null>(null)
const tip = computed(() => {
  if (hover.value == null) return null
  const i = hover.value
  const col = columns.value[i]
  const left = Math.min(Math.max(col.x + barW.value / 2, 90), width.value - 90)
  return {
    left,
    day: props.days[i],
    total: totals.value[i],
    rows: props.series.map((s) => ({ name: s.name, color: s.color, v: s.values[i] ?? 0 })).filter((r) => r.v > 0),
  }
})
</script>

<template>
  <div ref="wrap" class="chart" @mouseleave="hover = null">
    <svg :width="width" :height="HEIGHT" role="img" aria-label="Tokens per day">
      <g class="gridlines">
        <g v-for="tk in ticks" :key="tk">
          <line :x1="PAD.left" :x2="width - PAD.right" :y1="y(tk)" :y2="y(tk)" />
          <text :x="PAD.left - 8" :y="y(tk) + 4" text-anchor="end">{{ formatCompact(tk) }}</text>
        </g>
      </g>
      <g v-for="c in columns" :key="c.day">
        <path
          v-for="(s, k) in c.segs"
          :key="k"
          :d="segPath(c.x, barW, s.y, s.h, s.top)"
          :fill="s.color"
          :opacity="hover == null || hover === c.i ? 1 : 0.45"
        />
        <rect
          class="hit"
          :x="PAD.left + c.i * band"
          :y="PAD.top"
          :width="band"
          :height="plotH"
          @mouseenter="hover = c.i"
        />
        <text v-if="c.i % labelEvery === 0" class="xlab" :x="c.x + barW / 2" :y="HEIGHT - 6" text-anchor="middle">
          {{ dayLabel(c.day) }}
        </text>
      </g>
    </svg>
    <div v-if="tip" class="tip" :style="{ left: `${tip.left}px` }">
      <div class="tip-day">{{ tip.day }}</div>
      <div v-for="r in tip.rows" :key="r.name" class="tip-row">
        <span class="sw" :style="{ background: r.color }" />
        <span>{{ r.name }}</span>
        <span class="spacer" />
        <span class="num">{{ formatNumber(r.v) }}</span>
      </div>
      <div class="tip-row total">
        <span>Σ</span>
        <span class="spacer" />
        <span class="num">{{ formatNumber(tip.total) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chart {
  position: relative;
  width: 100%;
}
svg {
  display: block;
}
.gridlines line {
  stroke: #ececf1;
  stroke-width: 1;
}
.gridlines text,
.xlab {
  fill: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.hit {
  fill: transparent;
  cursor: default;
}
.tip {
  position: absolute;
  top: 4px;
  transform: translateX(-50%);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  padding: 10px 12px;
  min-width: 160px;
  font-size: 12.5px;
  pointer-events: none;
  z-index: 2;
}
.tip-day {
  font-weight: 600;
  margin-bottom: 6px;
}
.tip-row {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.tip-row.total {
  border-top: 1px solid var(--border);
  margin-top: 6px;
  padding-top: 6px;
  color: var(--text);
  font-weight: 600;
}
.sw {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
}
</style>
