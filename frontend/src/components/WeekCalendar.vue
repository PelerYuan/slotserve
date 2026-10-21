<script setup lang="ts">
import { reservationWhat } from '@/utils/reservation'
import { computed, nextTick, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Reservation } from '@/api/types'
import { formatTime } from '@/utils/format'

const props = defineProps<{
  start: Date // local midnight of the first day
  days: number
  slotMinutes: number
  reservations: Reservation[]
  currentUserId: number | null
  now: number
}>()

const emit = defineEmits<{
  select: [start: Date, end: Date]
  open: [reservation: Reservation]
}>()

const { locale, t } = useI18n()
const label = (r: Reservation) =>
  reservationWhat(r, t)
const HOUR_PX = 48
const DAY_PX = 24 * HOUR_PX
const slotPx = computed(() => (props.slotMinutes / 60) * HOUR_PX)
const slotsPerDay = computed(() => Math.round((24 * 60) / props.slotMinutes))

const scroller = ref<HTMLElement | null>(null)

function addDays(d: Date, n: number) {
  const x = new Date(d)
  x.setDate(x.getDate() + n)
  return x
}

const dayList = computed(() =>
  Array.from({ length: props.days }, (_, i) => {
    const date = addDays(props.start, i)
    return {
      date,
      key: date.toDateString(),
      isToday: date.toDateString() === new Date(props.now).toDateString(),
      weekday: date.toLocaleDateString(locale.value, { weekday: 'short' }),
      day: date.getDate(),
    }
  }),
)

const hours = Array.from({ length: 24 }, (_, h) => h)
function hourLabel(h: number) {
  return `${String(h).padStart(2, '0')}:00`
}

interface Segment {
  r: Reservation
  top: number
  height: number
  first: boolean
  mine: boolean
  active: boolean
}

const segmentsByDay = computed(() =>
  dayList.value.map(({ date }) => {
    const dayStart = date.getTime()
    const dayEnd = addDays(date, 1).getTime()
    const out: Segment[] = []
    for (const r of props.reservations) {
      if (r.status !== 'booked') continue
      const rs = new Date(r.start_at).getTime()
      const re = new Date(r.end_at).getTime()
      const s = Math.max(rs, dayStart)
      const e = Math.min(re, dayEnd)
      if (e <= s) continue
      out.push({
        r,
        top: ((s - dayStart) / 3_600_000) * HOUR_PX,
        height: Math.max(((e - s) / 3_600_000) * HOUR_PX, 14),
        first: s === rs,
        mine: r.user_id === props.currentUserId,
        active: rs <= props.now && props.now < re,
      })
    }
    return out
  }),
)

const nowLine = computed(() => {
  const idx = dayList.value.findIndex((d) => d.isToday)
  if (idx < 0) return null
  const dayStart = dayList.value[idx].date.getTime()
  return { idx, top: ((props.now - dayStart) / 3_600_000) * HOUR_PX }
})

function pastHeight(date: Date) {
  const dayStart = date.getTime()
  const dayEnd = addDays(date, 1).getTime()
  if (props.now >= dayEnd) return DAY_PX
  if (props.now <= dayStart) return 0
  return ((props.now - dayStart) / 3_600_000) * HOUR_PX
}

// ---- drag to select --------------------------------------------------------

const drag = ref<{ day: number; anchor: number; current: number } | null>(null)
let moved = false

function slotAt(e: PointerEvent, el: HTMLElement) {
  const y = e.clientY - el.getBoundingClientRect().top
  return Math.min(slotsPerDay.value - 1, Math.max(0, Math.floor(y / slotPx.value)))
}

function slotEnd(day: number, slot: number) {
  return dayList.value[day].date.getTime() + (slot + 1) * props.slotMinutes * 60_000
}

function onDown(e: PointerEvent, day: number) {
  if (e.button !== 0 || (e.target as HTMLElement).closest('.event')) return
  const el = e.currentTarget as HTMLElement
  const slot = slotAt(e, el)
  if (slotEnd(day, slot) <= props.now) return
  el.setPointerCapture(e.pointerId)
  drag.value = { day, anchor: slot, current: slot }
  moved = false
}

function onMove(e: PointerEvent) {
  if (!drag.value) return
  const el = e.currentTarget as HTMLElement
  const slot = slotAt(e, el)
  if (slot !== drag.value.current) moved = true
  drag.value.current = slot
}

function onUp() {
  const d = drag.value
  drag.value = null
  if (!d) return
  let a = Math.min(d.anchor, d.current)
  let b = Math.max(d.anchor, d.current) + 1
  if (!moved) b = Math.min(slotsPerDay.value, a + Math.max(1, Math.round(60 / props.slotMinutes)))
  // Skip slots that already passed.
  while (a < b - 1 && slotEnd(d.day, a) <= props.now) a++
  const dayStart = dayList.value[d.day].date.getTime()
  emit('select', new Date(dayStart + a * props.slotMinutes * 60_000), new Date(dayStart + b * props.slotMinutes * 60_000))
}

const selection = computed(() => {
  const d = drag.value
  if (!d) return null
  const a = Math.min(d.anchor, d.current)
  const b = Math.max(d.anchor, d.current) + 1
  const dayStart = dayList.value[d.day].date.getTime()
  return {
    day: d.day,
    top: a * slotPx.value,
    height: (b - a) * slotPx.value,
    label: `${formatTime(new Date(dayStart + a * props.slotMinutes * 60_000))} – ${formatTime(
      new Date(dayStart + b * props.slotMinutes * 60_000),
    )}`,
  }
})

onMounted(async () => {
  await nextTick()
  const hour = Math.max(0, new Date(props.now).getHours() - 2)
  scroller.value?.scrollTo({ top: Math.min(hour, 8) * HOUR_PX })
})
</script>

<template>
  <div class="cal" :style="{ '--days': days }">
    <div class="head">
      <div class="gutter" />
      <div v-for="d in dayList" :key="d.key" class="head-day" :class="{ today: d.isToday }">
        <span class="wd">{{ d.weekday }}</span>
        <span class="dn">{{ d.day }}</span>
      </div>
    </div>

    <div ref="scroller" class="body">
      <div class="cal-grid" :style="{ height: `${DAY_PX}px` }">
        <div class="gutter times">
          <div v-for="h in hours" :key="h" class="time" :style="{ top: `${h * HOUR_PX}px` }">
            <span v-if="h > 0">{{ hourLabel(h) }}</span>
          </div>
        </div>

        <div
          v-for="(d, i) in dayList"
          :key="d.key"
          class="col"
          :class="{ today: d.isToday }"
          @pointerdown="onDown($event, i)"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="drag = null"
        >
          <div class="past" :style="{ height: `${pastHeight(d.date)}px` }" />
          <div v-for="h in hours" :key="h" class="hline" :style="{ top: `${h * HOUR_PX}px` }" />

          <button
            v-for="s in segmentsByDay[i]"
            :key="s.r.id"
            class="event"
            :class="{ mine: s.mine, active: s.active, short: s.height < 40, exclusive: s.r.exclusive, pending: s.r.status === 'pending' }"
            :style="{ top: `${s.top}px`, height: `${s.height}px` }"
            :title="`${s.r.username} · ${label(s.r)}`"
            @click.stop="emit('open', s.r)"
          >
            <span class="ev-title">{{ s.r.username }}</span>
            <span v-if="s.height >= 40" class="ev-sub">{{ label(s.r) }}</span>
            <span v-if="s.height >= 58 && s.first" class="ev-time">{{ formatTime(s.r.start_at) }} – {{ formatTime(s.r.end_at) }}</span>
          </button>

          <div v-if="selection && selection.day === i" class="selection" :style="{ top: `${selection.top}px`, height: `${selection.height}px` }">
            {{ selection.label }}
          </div>

          <div v-if="nowLine && nowLine.idx === i" class="now" :style="{ top: `${nowLine.top}px` }" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cal {
  --gutter: 56px;
  display: flex;
  flex-direction: column;
  user-select: none;
}
.head {
  display: grid;
  grid-template-columns: var(--gutter) repeat(var(--days), minmax(0, 1fr));
  border-bottom: 1px solid var(--border);
  padding-right: 8px;
}
.head-day {
  padding: 10px 0 10px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.wd {
  font-size: 11.5px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 500;
}
.dn {
  font-size: 17px;
  font-weight: 600;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  font-variant-numeric: tabular-nums;
}
.head-day.today .dn {
  background: var(--brand);
  color: #fff;
}
.head-day.today .wd {
  color: var(--brand);
}
.body {
  overflow-y: auto;
  height: min(680px, calc(100vh - 290px));
  min-height: 380px;
  scrollbar-gutter: stable;
}
.cal-grid {
  position: relative;
  display: grid;
  grid-template-columns: var(--gutter) repeat(var(--days), minmax(0, 1fr));
}
.times {
  position: relative;
}
.time {
  position: absolute;
  right: 8px;
  transform: translateY(-50%);
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.col {
  position: relative;
  border-left: 1px solid var(--border);
  cursor: cell;
  touch-action: pan-y;
}
.col.today {
  background: var(--brand-softer);
}
.past {
  position: absolute;
  inset: 0 0 auto 0;
  background: repeating-linear-gradient(-45deg, var(--past-hatch) 0 6px, transparent 6px 12px);
  pointer-events: none;
}
.hline {
  position: absolute;
  left: 0;
  right: 0;
  border-top: 1px solid var(--grid-line);
  pointer-events: none;
}
.event {
  position: absolute;
  left: 3px;
  right: 4px;
  padding: 4px 8px;
  border-radius: 7px;
  border: 1px solid var(--event-border);
  border-left: 3px solid var(--event-accent);
  background: var(--event-bg);
  color: var(--event-text);
  text-align: left;
  font-family: inherit;
  cursor: pointer;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: 1.3;
  transition: box-shadow 0.12s;
  z-index: 1;
}
.event:hover {
  box-shadow: var(--shadow-lg);
  z-index: 2;
}
.event.mine {
  background: var(--event-mine-bg);
  border-color: var(--event-mine-border);
  border-left-color: var(--brand);
  color: var(--event-mine-text);
}
.event.pending {
  border-style: dashed;
  opacity: 0.75;
}
.event.exclusive {
  background-image: repeating-linear-gradient(-45deg, var(--event-hatch) 0 5px, transparent 5px 10px);
}
.event.active {
  box-shadow: 0 0 0 2px rgba(79, 70, 229, 0.18);
}
.event.short {
  padding-top: 1px;
  padding-bottom: 1px;
  justify-content: center;
}
.ev-title {
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ev-sub,
.ev-time {
  font-size: 11px;
  opacity: 0.8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ev-sub {
  font-family: var(--mono);
  font-size: 10.5px;
}
.selection {
  position: absolute;
  left: 3px;
  right: 4px;
  border-radius: 7px;
  background: rgba(79, 70, 229, 0.14);
  border: 1.5px dashed var(--brand);
  color: var(--brand);
  font-size: 11.5px;
  font-weight: 600;
  padding: 4px 8px;
  pointer-events: none;
  z-index: 3;
}
.now {
  position: absolute;
  left: -1px;
  right: 0;
  height: 2px;
  background: #e5484d;
  z-index: 4;
  pointer-events: none;
}
.now::before {
  content: '';
  position: absolute;
  left: -5px;
  top: -4px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #e5484d;
}
@media (max-width: 640px) {
  .cal {
    --gutter: 44px;
  }
}
</style>
