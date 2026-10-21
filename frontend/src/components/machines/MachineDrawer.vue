<script setup lang="ts">
// Everything about one machine: hardware, per-GPU bars, lanes with today's and tomorrow's bookings, CPU compute
// left, disks, Docker, models and a small load history. Used by the Machines page and Admin -> Nodes.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NDrawer, NDrawerContent } from 'naive-ui'
import { machinesApi, type Machine, type MachineHistory } from '@/api/machines'
import LineChart from '@/components/LineChart.vue'
import MeterBar from './MeterBar.vue'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { isDark } from '@/composables/theme'
import { STATUS_CLASS, cores, gb, pct, rate } from '@/utils/machines'
import { formatBytes, formatDateTime, formatRange, relativeFromNow } from '@/utils/format'

const props = defineProps<{ machineId: number | null }>()
const emit = defineEmits<{ close: [] }>()
const { t } = useI18n()
const { showError } = useErrorMessage()

const machine = ref<Machine | null>(null)
const history = ref<MachineHistory | null>(null)
const width = ref(Math.min(620, window.innerWidth))
let timer: number | undefined

async function load(id: number) {
  try {
    const [m, h] = await Promise.all([machinesApi.get(id), machinesApi.history(id)])
    if (props.machineId === id) {
      machine.value = m
      history.value = h
    }
  } catch (e) {
    showError(e)
  }
}

watch(
  () => props.machineId,
  (id) => {
    window.clearInterval(timer)
    machine.value = null
    history.value = null
    if (id == null) return
    width.value = Math.min(620, window.innerWidth)
    void load(id)
    timer = window.setInterval(() => load(id), 10_000)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.clearInterval(timer))

// the usage page's categorical palette (stepped per theme)
const COLORS = computed(() =>
  isDark.value ? { cpu: '#3987e5', mem: '#c98500', gpu: '#199e70' } : { cpu: '#2a78d6', mem: '#eda100', gpu: '#1baf7a' },
)

const m = computed(() => machine.value)
const cap = computed(() => m.value?.capacity)

function localLabel(iso: string): string {
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

const recent = computed(() => {
  const h = history.value
  if (!h || !h.points.length) return null
  const total = m.value?.memory_total_mb ?? null
  return {
    labels: h.points.map((p) => localLabel(p.at)),
    series: [
      { name: t('machines.cpuLoad'), color: COLORS.value.cpu, values: h.points.map((p) => p.cpu_percent) },
      {
        name: t('machines.memory'),
        color: COLORS.value.mem,
        values: h.points.map((p) => (p.memory_used_mb == null || !total ? null : Math.round(pct(p.memory_used_mb, total)))),
      },
      ...(m.value?.gpus.length
        ? [{ name: t('machines.gpuUtilShort'), color: COLORS.value.gpu, values: h.points.map((p) => p.gpu_util_pct) }]
        : []),
    ],
  }
})

const day = computed(() => {
  const h = history.value
  if (!h || !h.gpu_hours.length || !h.gpu_hours.some((x) => x.util_pct != null)) return null
  return {
    labels: h.gpu_hours.map((x) => localLabel(x.start)),
    series: [{ name: t('machines.gpuUtilShort'), color: COLORS.value.gpu, values: h.gpu_hours.map((x) => x.util_pct) }],
  }
})

function dayOf(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return d.getTime() >= today.getTime() + 86_400_000 ? t('machines.tomorrow') : t('machines.today')
}
</script>

<template>
  <NDrawer :show="machineId != null" :width="width" placement="right" @update:show="(v: boolean) => !v && emit('close')">
    <NDrawerContent :title="m?.name ?? '…'" closable :native-scrollbar="false">
      <div v-if="m && cap" class="drawer" data-test="machine-drawer">
        <div class="row wrap">
          <span class="badge" :class="STATUS_CLASS[m.status]">{{ t(`cluster.statuses.${m.status}`) }}</span>
          <span class="badge" :class="m.kind === 'gpu' ? 'brand' : ''">
            {{ m.kind === 'gpu' ? t('machines.gpuKind', { n: m.gpus.length }) : t('machines.cpuOnly') }}
          </span>
          <span v-if="m.is_local" class="badge">{{ t('machines.controlPlane') }}</span>
          <span v-if="m.sharing_paused" class="badge warn">{{ t('machines.sharingPaused') }}</span>
          <span class="spacer" />
          <span v-if="m.last_seen_at && !m.online" class="faint small" :title="formatDateTime(m.last_seen_at)">
            {{ t('cluster.lastSeen', { when: relativeFromNow(m.last_seen_at) }) }}
          </span>
        </div>

        <section>
          <h4>{{ t('machines.hardware') }}</h4>
          <dl class="dl">
            <dt>{{ t('machines.host') }}</dt>
            <dd>{{ m.hostname ?? '—' }}</dd>
            <dt>{{ t('machines.system') }}</dt>
            <dd>{{ [m.os, m.arch].filter(Boolean).join(' · ') || '—' }}</dd>
            <dt>{{ t('machines.cpu') }}</dt>
            <dd>
              {{ m.cpu_model ?? '—' }}
              <span class="faint">· {{ t('machines.coresLong', { logical: cores(m.cpu_logical_cores), physical: m.cpu_physical_cores ?? '—' }) }}</span>
            </dd>
            <dt>{{ t('machines.memory') }}</dt>
            <dd class="num">{{ gb(m.memory_total_mb) }}</dd>
            <template v-if="m.gpu_driver">
              <dt>{{ t('machines.driver') }}</dt>
              <dd class="num">
                {{ m.gpu_driver.vendor ?? '' }} {{ m.gpu_driver.driver_version ?? '—' }}
                <span v-if="m.gpu_driver.cuda_version" class="faint">· CUDA {{ m.gpu_driver.cuda_version }}</span>
              </dd>
            </template>
            <dt>Docker</dt>
            <dd>
              <template v-if="m.docker.available">
                {{ m.docker.version ?? '?' }}
                <span class="faint">· {{ m.docker.runtimes.join(', ') || '—' }}</span>
                <span v-if="m.gpus.length && !m.docker.gpu_runtime" class="warn-text"> · {{ t('machines.noGpuRuntime') }}</span>
              </template>
              <span v-else class="faint">{{ t('machines.dockerUnknown') }}</span>
            </dd>
            <template v-if="m.net_rx_bps != null">
              <dt>{{ t('machines.network') }}</dt>
              <dd class="num">↓ {{ rate(m.net_rx_bps) }} · ↑ {{ rate(m.net_tx_bps) }}</dd>
            </template>
            <template v-if="m.load != null">
              <dt>{{ t('machines.loadAvg') }}</dt>
              <dd class="num">{{ m.load.toFixed(2) }}</dd>
            </template>
          </dl>
        </section>

        <section>
          <h4>{{ t('machines.now') }}</h4>
          <div class="meters">
            <MeterBar :label="t('machines.cpuLoad')" :percent="m.cpu_percent" />
            <MeterBar
              :label="t('machines.memory')"
              :percent="m.memory_used_mb == null ? null : pct(m.memory_used_mb, m.memory_total_mb)"
              :text="`${gb(m.memory_used_mb)} / ${gb(m.memory_total_mb)}`"
            />
          </div>
          <div v-if="m.gpus.length" class="gpus">
            <div v-for="g in m.gpus" :key="g.index" class="gpu" :data-gpu="g.index">
              <div class="gpu-head">
                <b>GPU {{ g.index }}</b>
                <span class="ellipsis">{{ g.name }}</span>
                <span v-if="g.lane_name" class="badge brand lane">{{ g.lane_name }}</span>
              </div>
              <div class="meters">
                <MeterBar
                  :label="t('machines.gpuMemory')"
                  :percent="g.memory_used_mb == null ? null : pct(g.memory_used_mb, g.memory_total_mb)"
                  :text="`${gb(g.memory_used_mb)} / ${gb(g.memory_total_mb)}`"
                />
                <MeterBar
                  :label="t('machines.gpuUtilShort')"
                  :percent="g.utilization_pct"
                  :sub="[g.temperature_c != null ? `${g.temperature_c} °C` : '', g.power_w != null ? `${g.power_w.toFixed(0)} W` : ''].filter(Boolean).join(' · ') || undefined"
                />
              </div>
            </div>
          </div>
        </section>

        <section>
          <h4>{{ t('machines.cpuCompute') }}</h4>
          <dl class="dl">
            <dt>{{ t('machines.coresTotal') }}</dt>
            <dd class="num">{{ cores(cap.cpu_cores) }}</dd>
            <dt>{{ t('machines.coresLanes') }}</dt>
            <dd class="num">{{ cap.cpu_cores_lanes }}</dd>
            <dt>{{ t('machines.coresEnvs') }}</dt>
            <dd class="num">{{ cores(cap.cpu_cores_envs) }} <span class="faint">({{ t('machines.cpuEnvs', { n: cap.cpu_envs_running }) }})</span></dd>
            <dt>{{ t('machines.coresFreeLabel') }}</dt>
            <dd class="num strong">{{ cores(cap.cpu_cores_free) }}</dd>
            <dt>{{ t('machines.memoryFreeLabel') }}</dt>
            <dd class="num strong">{{ gb(cap.memory_free_mb) }}</dd>
          </dl>
          <p class="faint small">{{ cap.cpu_envs ? t('machines.cpuEnvsHere') : t('machines.cpuEnvsElsewhere') }}</p>
        </section>

        <section v-if="m.lanes.length">
          <h4>{{ t('machines.lanes') }}</h4>
          <div v-for="l in m.lanes" :key="l.id" class="lane-block" :data-lane="l.name">
            <div class="row wrap">
              <b>{{ l.name }}</b>
              <span class="faint small">GPU {{ l.gpus.join(',') || '*' }} · {{ gb(l.gpu_memory_mb) }}</span>
              <span class="spacer" />
              <span v-if="l.free_now" class="badge ok">{{ t('machines.freeNow') }}</span>
              <span v-else-if="l.holder" class="badge">{{ l.holder_mine ? t('machines.heldByYou') : t('machines.heldBy', { name: l.holder }) }}</span>
            </div>
            <div v-if="l.model_name" class="faint small mono">{{ l.model_name }} · {{ t(`state.${l.state}`) }}</div>
            <ul v-if="l.bookings.length" class="bookings">
              <li v-for="b in l.bookings" :key="b.id" :class="{ mine: b.mine, pending: b.status === 'pending' }">
                <span class="faint">{{ dayOf(b.start_at) }}</span>
                <span class="num">{{ formatRange(b.start_at, b.end_at) }}</span>
                <span>{{ b.mine ? t('common.you') : b.username }}</span>
                <span class="faint">{{ t(`machines.kinds.${b.kind}`) }}</span>
                <span v-if="b.status === 'pending'" class="badge warn">{{ t('machines.pending') }}</span>
              </li>
            </ul>
            <p v-else class="faint small">{{ t('machines.noBookings') }}</p>
          </div>
        </section>

        <section>
          <h4>{{ t('machines.history') }}</h4>
          <template v-if="recent">
            <div class="legend faint small">
              <span v-for="x in recent.series" :key="x.name"><i :style="{ background: x.color }" />{{ x.name }}</span>
            </div>
            <LineChart :labels="recent.labels" :series="recent.series" :label="t('machines.history')" />
          </template>
          <p v-else class="faint small">{{ t('machines.noHistory') }}</p>
          <template v-if="day">
            <h5>{{ t('machines.gpuDay') }}</h5>
            <LineChart :labels="day.labels" :series="day.series" :label="t('machines.gpuDay')" />
          </template>
        </section>

        <section>
          <h4>{{ t('machines.storage') }}</h4>
          <div v-if="m.disks.length" class="meters">
            <MeterBar
              v-for="d in m.disks"
              :key="d.label"
              :label="t(`machines.disk.${d.label}`)"
              :percent="d.total_bytes ? 100 - pct(d.free_bytes, d.total_bytes) : null"
              :text="t('machines.diskFree', { n: formatBytes(d.free_bytes) })"
              :sub="d.path"
            />
          </div>
          <p v-else class="faint small">—</p>
        </section>

        <section>
          <h4>{{ t('machines.modelsTitle', { n: m.models.length }) }}</h4>
          <ul v-if="m.models.length" class="models">
            <li v-for="x in m.models" :key="x.id"><span class="mono">{{ x.name }}</span> <span class="faint">{{ formatBytes(x.size_bytes) }}</span></li>
          </ul>
          <p v-else class="faint small">{{ t('machines.noModels') }}</p>
        </section>
      </div>
      <div v-else class="faint">…</div>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.drawer {
  display: grid;
  gap: 18px;
}
h4 {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 650;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
h5 {
  margin: 10px 0 4px;
  font-size: 12.5px;
  color: var(--text-2);
}
.wrap {
  flex-wrap: wrap;
}
.small {
  font-size: 12.5px;
}
.strong {
  font-weight: 650;
}
.dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 16px;
  margin: 0;
  font-size: 13px;
}
.dl dt {
  color: var(--text-3);
}
.dl dd {
  margin: 0;
  min-width: 0;
  word-break: break-word;
}
.meters {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px 16px;
}
.gpus {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}
.gpu {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  background: var(--surface-muted);
}
.gpu-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 6px;
  min-width: 0;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
  color: var(--text-2);
}
.lane {
  height: 20px;
  font-size: 11px;
}
.lane-block {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}
.lane-block + .lane-block {
  margin-top: 8px;
}
.bookings {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: grid;
  gap: 4px;
  font-size: 12.5px;
}
.bookings li {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  align-items: center;
  padding: 3px 8px;
  border-radius: 6px;
  background: var(--event-bg);
  color: var(--event-text);
}
.bookings li.mine {
  background: var(--event-mine-bg);
  color: var(--event-mine-text);
}
.models {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  display: grid;
  gap: 2px;
}
.warn-text {
  color: var(--warn-text);
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-bottom: 4px;
}
.legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 5px;
}
</style>
