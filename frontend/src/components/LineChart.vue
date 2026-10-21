<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/** Lines over time buckets on a fixed 0–max scale (utilization %); gaps where a value is null. */
const props = withDefaults(
  defineProps<{
    labels: string[] // local bucket starts, YYYY-MM-DDTHH:MM, oldest first
    series: { name: string; color: string; values: (number | null)[] }[]
    max?: number
    unit?: string
    label?: string
  }>(),
  { max: 100, unit: '%', label: 'Utilization over time' },
)

const wrap = ref<HTMLElement | null>(null)
const width = ref(720)
const HEIGHT = 200
const PAD = { top: 12, right: 12, bottom: 26, left: 44 }
let ro: ResizeObserver | undefined

onMounted(() => {
  ro = new ResizeObserver(([entry]) => (width.value = Math.max(280, entry.contentRect.width)))
  if (wrap.value) ro.observe(wrap.value)
})
onBeforeUnmount(() => ro?.disconnect())

const plotW = computed(() => width.value - PAD.left - PAD.right)
const plotH = HEIGHT - PAD.top - PAD.bottom
const step = computed(() => plotW.value / Math.max(1, props.labels.length))
const x = (i: number) => PAD.left + (i + 0.5) * step.value
const y = (v: number) => PAD.top + plotH - (Math.min(v, props.max) / props.max) * plotH
const ticks = computed(() => [0, 0.25, 0.5, 0.75, 1].map((f) => f * props.max))

/** One path per series; a null value breaks the line, lone points get a dot. */
const lines = computed(() =>
  props.series.map((s) => {
    let d = ''
    const dots: { x: number; y: number }[] = []
    s.values.forEach((v, i) => {
      if (v == null) return
      const prev = i > 0 ? s.values[i - 1] : null
      const next = i < s.values.length - 1 ? s.values[i + 1] : null
      d += `${prev == null ? 'M' : 'L'}${x(i)},${y(v)}`
      if (prev == null && next == null) dots.push({ x: x(i), y: y(v) })
    })
    return { ...s, d, dots }
  }),
)

const labelEvery = computed(() => Math.max(1, Math.ceil(props.labels.length / Math.floor(plotW.value / 64))))
function short(d: string) {
  const [date, time] = d.split('T')
  const [, m, dd] = date.split('-')
  return time && time !== '00:00' ? time.slice(0, 2) + 'h' : `${Number(m)}/${Number(dd)}`
}

const hover = ref<number | null>(null)
function onMove(e: MouseEvent) {
  const rect = (e.currentTarget as SVGElement).getBoundingClientRect()
  const i = Math.floor((e.clientX - rect.left - PAD.left) / step.value)
  hover.value = i >= 0 && i < props.labels.length ? i : null
}
const tip = computed(() => {
  if (hover.value == null) return null
  const i = hover.value
  const rows = props.series
    .map((s) => ({ name: s.name, color: s.color, v: s.values[i] }))
    .filter((r): r is { name: string; color: string; v: number } => r.v != null)
  if (!rows.length) return null
  return {
    x: x(i),
    left: Math.min(Math.max(x(i), 90), width.value - 90),
    label: props.labels[i].replace('T00:00', '').replace('T', ' '),
    rows,
  }
})
</script>

<template>
  <div ref="wrap" class="chart" @mouseleave="hover = null">
    <svg :width="width" :height="HEIGHT" role="img" :aria-label="label" @mousemove="onMove">
      <g class="gridlines">
        <g v-for="tk in ticks" :key="tk">
          <line :x1="PAD.left" :x2="width - PAD.right" :y1="y(tk)" :y2="y(tk)" />
          <text :x="PAD.left - 8" :y="y(tk) + 4" text-anchor="end">{{ tk }}{{ unit }}</text>
        </g>
      </g>
      <line v-if="tip" class="cross" :x1="tip.x" :x2="tip.x" :y1="PAD.top" :y2="PAD.top + plotH" />
      <g v-for="l in lines" :key="l.name">
        <path :d="l.d" :stroke="l.color" class="line" />
        <circle v-for="(p, k) in l.dots" :key="k" :cx="p.x" :cy="p.y" r="3" :fill="l.color" />
        <template v-if="tip">
          <circle
            v-for="r in tip.rows.filter((r) => r.name === l.name)"
            :key="r.name"
            :cx="tip.x"
            :cy="y(r.v)"
            r="4"
            :fill="l.color"
            class="ring"
          />
        </template>
      </g>
      <text
        v-for="(d, i) in labels"
        v-show="i % labelEvery === 0"
        :key="d"
        class="xlab"
        :x="x(i)"
        :y="HEIGHT - 6"
        text-anchor="middle"
      >
        {{ short(d) }}
      </text>
    </svg>
    <div v-if="tip" class="tip" :style="{ left: `${tip.left}px` }">
      <div class="tip-day">{{ tip.label }}</div>
      <div v-for="r in tip.rows" :key="r.name" class="tip-row">
        <span class="sw" :style="{ background: r.color }" />
        <span>{{ r.name }}</span>
        <span class="spacer" />
        <span class="num">{{ r.v.toFixed(0) }}{{ unit }}</span>
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
  stroke: var(--grid-line);
  stroke-width: 1;
}
.gridlines text,
.xlab {
  fill: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.line {
  fill: none;
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.cross {
  stroke: var(--border-strong);
  stroke-width: 1;
}
.ring {
  stroke: var(--surface);
  stroke-width: 2;
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
  min-width: 150px;
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
.sw {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
}
</style>
