<script setup lang="ts">
// One machine at a glance: compute it offers (GPU or CPU only), memory, load and what is free on it now.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Cpu, HardDrive, MemoryStick, PauseCircle, Server, User } from 'lucide-vue-next'
import type { Machine } from '@/api/machines'
import MeterBar from './MeterBar.vue'
import { STATUS_CLASS, cores, gb, gpuSummary, pct } from '@/utils/machines'
import { formatBytes, formatTime, relativeFromNow } from '@/utils/format'

const props = defineProps<{ machine: Machine }>()
defineEmits<{ open: [machine: Machine] }>()
const { t } = useI18n()

const m = computed(() => props.machine)
const cap = computed(() => m.value.capacity)
const memUsed = computed(() => pct(m.value.memory_used_mb, m.value.memory_total_mb))
const modelsDisk = computed(() => m.value.disks.find((d) => d.label === 'models') ?? m.value.disks[0] ?? null)
</script>

<template>
  <article class="card machine" :class="{ offline: !m.online }" :data-machine="m.name">
    <header class="head">
      <Server :size="16" class="faint" />
      <button class="name" type="button" @click="$emit('open', m)">{{ m.name }}</button>
      <span class="badge" :class="STATUS_CLASS[m.status]">{{ t(`cluster.statuses.${m.status}`) }}</span>
      <span class="badge" :class="m.kind === 'gpu' ? 'brand' : ''">
        {{ m.kind === 'gpu' ? t('machines.gpuKind', { n: m.gpus.length }) : t('machines.cpuOnly') }}
      </span>
      <span v-if="m.busy" class="badge warn">{{ t('machines.busy') }}</span>
      <span v-if="m.sharing_paused" class="badge warn"><PauseCircle :size="12" /> {{ t('machines.sharingPaused') }}</span>
    </header>
    <p class="sub faint">
      <span v-if="m.is_local">{{ t('machines.controlPlane') }} · </span>
      <span>{{ [m.hostname, m.os].filter(Boolean).join(' · ') || '—' }}</span>
      <span v-if="!m.online && m.last_seen_at"> · {{ t('cluster.lastSeen', { when: relativeFromNow(m.last_seen_at) }) }}</span>
    </p>
    <p v-if="m.owner || m.shared_hours" class="sub faint owner">
      <User :size="12" />
      <span v-if="m.owner">{{ t('machines.owner', { name: m.owner }) }}</span>
      <span v-if="m.shared_hours"> · {{ t('machines.sharedHours', { hours: m.shared_hours }) }}</span>
    </p>

    <div class="facts">
      <div class="fact">
        <Cpu :size="14" class="faint" />
        <span class="ellipsis" :title="m.cpu_model ?? ''">{{ m.cpu_model ?? t('machines.cpu') }}</span>
        <span class="faint num">
          {{ t('machines.coresShort', { n: cores(m.cpu_logical_cores) }) }}<template v-if="m.cpu_physical_cores"> / {{ t('machines.physicalShort', { n: m.cpu_physical_cores }) }}</template>
        </span>
      </div>
      <div v-if="m.gpus.length" class="fact">
        <span class="gpu-dot" />
        <span class="ellipsis" :title="gpuSummary(m)">{{ gpuSummary(m) }}</span>
        <span class="faint num">{{ gb(m.gpus[0]?.memory_total_mb) }}</span>
      </div>
    </div>

    <div class="meters">
      <MeterBar
        :label="t('machines.cpuLoad')"
        :percent="m.cpu_percent"
        :sub="t('machines.coresFree', { n: cores(cap.cpu_cores_free), total: cores(cap.cpu_cores) })"
      />
      <MeterBar
        :label="t('machines.memory')"
        :percent="m.memory_used_mb == null ? null : memUsed"
        :text="m.memory_used_mb == null ? gb(m.memory_total_mb) : `${gb(m.memory_used_mb)} / ${gb(m.memory_total_mb)}`"
        :sub="t('machines.memoryFree', { n: gb(cap.memory_free_mb) })"
      />
      <MeterBar
        v-for="g in m.gpus"
        :key="g.index"
        :label="`GPU ${g.index}`"
        :percent="g.memory_used_mb == null ? null : pct(g.memory_used_mb, g.memory_total_mb)"
        :text="g.memory_used_mb == null ? gb(g.memory_total_mb) : `${gb(g.memory_used_mb)} / ${gb(g.memory_total_mb)}`"
        :sub="g.utilization_pct == null ? undefined : t('machines.gpuUtil', { n: g.utilization_pct })"
      />
    </div>

    <ul v-if="m.lanes.length" class="lanes">
      <li v-for="l in m.lanes" :key="l.id" :class="{ free: l.free_now }">
        <span class="lane-name">{{ l.name }}</span>
        <span class="faint num small">{{ gb(l.gpu_memory_mb) }}</span>
        <span class="spacer" />
        <span v-if="l.free_now" class="state ok">
          {{ t('machines.freeNow') }}<template v-if="l.next_start_at"> · {{ t('machines.until', { time: formatTime(l.next_start_at) }) }}</template>
        </span>
        <span v-else-if="l.holder" class="state" :class="{ mine: l.holder_mine }">
          {{ l.holder_mine ? t('machines.heldByYou') : t('machines.heldBy', { name: l.holder }) }}<template v-if="l.held_until">
            · {{ t('machines.until', { time: formatTime(l.held_until) }) }}</template>
        </span>
        <span v-else class="state faint">{{ t('machines.unavailable') }}</span>
      </li>
    </ul>
    <p v-else class="no-lanes faint small">{{ m.kind === 'gpu' ? t('machines.noLanes') : t('machines.cpuOnlyHint') }}</p>

    <footer class="foot faint small">
      <span><MemoryStick :size="12" /> {{ t('machines.modelsHere', { n: m.models.length }) }}</span>
      <span v-if="modelsDisk"><HardDrive :size="12" /> {{ t('machines.diskFree', { n: formatBytes(modelsDisk.free_bytes) }) }}</span>
      <span v-if="cap.cpu_envs_running">{{ t('machines.cpuEnvs', { n: cap.cpu_envs_running }) }}</span>
      <span class="spacer" />
      <button class="link" type="button" data-action="machine-details" @click="$emit('open', m)">{{ t('machines.details') }}</button>
    </footer>
  </article>
</template>

<style scoped>
.machine {
  padding: 16px 18px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.machine.offline {
  opacity: 0.78;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.name {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  font-weight: 650;
  font-size: 15px;
  color: var(--text);
  cursor: pointer;
}
.name:hover {
  color: var(--brand);
}
.sub {
  margin: -6px 0 0;
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.owner {
  display: flex;
  align-items: center;
  gap: 4px;
}
.facts {
  display: grid;
  gap: 4px;
  font-size: 13px;
}
.fact {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.gpu-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  background: var(--brand);
  flex: none;
  margin: 0 2px;
}
.meters {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px 14px;
}
.lanes {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.lanes li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  font-size: 12.5px;
  min-width: 0;
}
.lanes li + li {
  border-top: 1px solid var(--border);
}
.lane-name {
  font-weight: 600;
}
.state {
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.state.ok {
  color: var(--ok-text);
  font-weight: 600;
}
.state.mine {
  color: var(--brand);
  font-weight: 600;
}
.no-lanes {
  margin: 0;
}
.small {
  font-size: 12px;
}
.foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.foot span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  color: var(--brand);
  cursor: pointer;
}
</style>
