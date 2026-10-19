<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

// A looping, purely decorative simulation of a day on the GPU: bookings, preloads,
// and gateway traffic. Nothing here talks to the backend.
const { t } = useI18n()

const LOOP_MS = 18000
const PRELOAD = 0.06
const DEFAULT_MODEL = 'Qwen3-8B'

const bookings = [
  { user: 'alice', model: 'Qwen3-32B', s: 0.1, e: 0.36, color: '#4f46e5' },
  { user: 'bob', model: 'gemma-3-27b', s: 0.46, e: 0.66, color: '#8b5cf6' },
  { user: 'peler', model: 'Qwen3-32B', s: 0.76, e: 0.93, color: '#0ea5e9' },
]
const users = bookings.map((b) => b.user)
const hours = ['08', '10', '12', '14', '16', '18', '20']

const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
const p = ref(0.22)
const tokens = ref(18240)
const util = ref(12)
const log = ref<{ id: number; user: string; code: number; note: string }[]>([])

const active = computed(() => bookings.find((b) => p.value >= b.s && p.value < b.e))
const preloading = computed(() => bookings.find((b) => p.value >= b.s - PRELOAD && p.value < b.s))
const status = computed(() => {
  if (active.value) return { kind: 'busy', text: t('auth.demoInUse', { user: active.value.user, model: active.value.model }) }
  if (preloading.value) return { kind: 'load', text: t('auth.demoPreload', { model: preloading.value.model }) }
  return { kind: 'idle', text: t('auth.demoIdle', { model: DEFAULT_MODEL }) }
})

let raf = 0
let start = 0
let lastTick = 0
let lastReq = 0
let seq = 0

function request() {
  const user = users[Math.floor(Math.random() * users.length)]
  const owner = active.value?.user
  let code = 200
  let note = owner ? active.value!.model : DEFAULT_MODEL
  if (preloading.value && !owner) {
    code = 503
    note = 'loading'
  } else if (owner && owner !== user) {
    code = 403
    note = 'gpu_reserved'
  }
  if (code === 200) tokens.value += 200 + Math.floor(Math.random() * 900)
  log.value = [{ id: seq++, user, code, note }, ...log.value].slice(0, 3)
}

function frame(now: number) {
  if (!start) start = now
  p.value = ((now - start) % LOOP_MS) / LOOP_MS
  if (now - lastTick > 600) {
    lastTick = now
    const target = active.value ? 84 : preloading.value ? 38 : 16
    util.value = Math.round(target + (Math.random() - 0.5) * 10)
  }
  if (now - lastReq > 1500) {
    lastReq = now
    request()
  }
  raf = requestAnimationFrame(frame)
}

onMounted(() => {
  for (let i = 0; i < 3; i++) request()
  if (!reduced) raf = requestAnimationFrame(frame)
})
onBeforeUnmount(() => cancelAnimationFrame(raf))

const pct = (v: number) => `${(v * 100).toFixed(2)}%`
</script>

<template>
  <div class="showcase" aria-hidden="true">
    <div class="card timeline">
      <div class="head">
        <span class="live"><i />{{ t('auth.demoTitle') }}</span>
        <span :class="['chip', status.kind]">
          <Transition name="swap" mode="out-in">
            <span :key="status.text">{{ status.text }}</span>
          </Transition>
        </span>
      </div>
      <div class="axis">
        <span v-for="h in hours" :key="h">{{ h }}</span>
      </div>
      <div class="rows">
        <div v-for="b in bookings" :key="b.user" class="row">
          <span class="who">{{ b.user }}</span>
          <div class="track">
            <span class="pre" :style="{ left: pct(b.s - PRELOAD), width: pct(PRELOAD) }" />
            <span
              :class="['blk', { on: active === b, done: p >= b.e }]"
              :style="{ left: pct(b.s), width: pct(b.e - b.s), '--c': b.color }"
            >
              {{ b.model }}
            </span>
          </div>
        </div>
        <div class="now" :style="{ left: `calc(56px + (100% - 56px) * ${p})` }" />
      </div>
      <div class="stats">
        <div>
          <label>{{ t('auth.demoUtil') }}</label>
          <div class="bar"><span :style="{ width: `${util}%` }" /></div>
          <b class="num">{{ util }}%</b>
        </div>
        <div>
          <label>{{ t('auth.demoTokens') }}</label>
          <b class="num">{{ tokens.toLocaleString('en-US') }}</b>
        </div>
      </div>
    </div>

    <div class="card requests">
      <TransitionGroup name="log" tag="ul">
        <li v-for="r in log" :key="r.id">
          <span class="mono path">POST /v1/chat/completions</span>
          <span class="u">{{ r.user }}</span>
          <span :class="['code', `c${r.code}`]">{{ r.code }}</span>
          <span class="mono note">{{ r.note }}</span>
        </li>
      </TransitionGroup>
    </div>
  </div>
</template>

<style scoped>
.showcase {
  position: relative;
  width: 100%;
  max-width: 480px;
}
.card {
  background: var(--glass);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.8) inset, 0 24px 48px -24px rgba(40, 40, 110, 0.28),
    0 2px 6px rgba(40, 40, 110, 0.05);
}
.timeline {
  padding: 16px 18px 14px;
  animation: float 9s ease-in-out infinite;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.live {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.live i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.5);
  animation: ping 2s ease-out infinite;
}
.chip {
  font-size: 12px;
  font-weight: 550;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
  overflow: hidden;
  transition: background 0.4s, color 0.4s;
}
.chip.idle {
  background: var(--ok-soft);
  color: var(--ok-text);
}
.chip.load {
  background: var(--warn-soft);
  color: var(--warn-text);
}
.chip.busy {
  background: var(--brand-soft);
  color: var(--brand);
}
.axis {
  display: flex;
  justify-content: space-between;
  margin-left: 56px;
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  margin-bottom: 6px;
}
.rows {
  position: relative;
  display: grid;
  gap: 8px;
}
.row {
  display: flex;
  align-items: center;
  height: 26px;
}
.who {
  width: 56px;
  flex: none;
  font-size: 12px;
  color: var(--text-2);
  font-weight: 500;
}
.track {
  position: relative;
  flex: 1;
  height: 100%;
  border-radius: 7px;
  background: repeating-linear-gradient(90deg, var(--track-line) 0 1px, transparent 1px calc(100% / 6)), var(--track);
}
.pre {
  position: absolute;
  top: 6px;
  bottom: 6px;
  border-radius: 4px 0 0 4px;
  background: repeating-linear-gradient(135deg, #fcd9a6 0 3px, transparent 3px 6px);
  opacity: 0.8;
}
.blk {
  position: absolute;
  top: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  padding: 0 8px;
  border-radius: 7px;
  font-size: 11px;
  font-weight: 600;
  color: var(--c);
  background: color-mix(in srgb, var(--c) 14%, var(--surface));
  border: 1px solid color-mix(in srgb, var(--c) 28%, var(--surface));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.4s, color 0.4s, box-shadow 0.4s, opacity 0.4s;
}
.blk.on {
  color: #fff;
  background: var(--c);
  border-color: var(--c);
  box-shadow: 0 6px 16px -6px var(--c);
}
.blk.done {
  opacity: 0.55;
}
.now {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  margin-left: -1px;
  background: #e5484d;
  border-radius: 1px;
  pointer-events: none;
}
.now::before {
  content: '';
  position: absolute;
  top: -3px;
  left: -3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #e5484d;
  box-shadow: 0 0 0 3px rgba(229, 72, 77, 0.18);
}
.stats {
  display: grid;
  grid-template-columns: 1.3fr 1fr;
  gap: 16px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.stats > div {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.stats label {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
}
.stats b {
  font-size: 13px;
  font-weight: 650;
  margin-left: auto;
}
.bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--rail);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg, #818cf8, var(--brand));
  transition: width 0.6s cubic-bezier(0.22, 1, 0.36, 1);
}
.requests {
  position: relative;
  margin: -14px -28px 0 48px;
  padding: 10px 14px;
  animation: float 9s ease-in-out -4.5s infinite;
}
.requests ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 6px;
  align-content: start;
  min-height: 82px;
}
.requests li {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  height: 22px;
}
.path {
  color: var(--text-2);
  font-size: 11px;
}
.u {
  width: 40px;
  color: var(--text);
  font-weight: 550;
}
.code {
  font-weight: 650;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 5px;
  font-variant-numeric: tabular-nums;
}
.c200 {
  color: var(--ok-text);
  background: var(--ok-soft);
}
.c403 {
  color: var(--orange-text);
  background: var(--orange-soft);
}
.c503 {
  color: var(--warn-text);
  background: var(--warn-soft);
}
.note {
  color: var(--text-3);
  font-size: 11px;
  margin-left: auto;
}

.swap-enter-active,
.swap-leave-active {
  transition: opacity 0.25s, transform 0.25s;
}
.swap-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
.swap-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
.log-enter-active {
  transition: opacity 0.4s, transform 0.4s cubic-bezier(0.22, 1, 0.36, 1);
}
.log-leave-active {
  transition: opacity 0.25s;
  position: absolute;
}
.log-move {
  transition: transform 0.4s cubic-bezier(0.22, 1, 0.36, 1);
}
.log-enter-from {
  opacity: 0;
  transform: translateY(-10px);
}
.log-leave-to {
  opacity: 0;
}

@keyframes float {
  0%,
  100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-6px);
  }
}
@keyframes ping {
  0% {
    box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.5);
  }
  80%,
  100% {
    box-shadow: 0 0 0 7px rgba(16, 185, 129, 0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .timeline,
  .requests,
  .live i {
    animation: none;
  }
}
</style>
