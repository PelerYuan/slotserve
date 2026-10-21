<script setup lang="ts">
// Every machine people can use: CPU or GPU compute, memory, current load and what is free now.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInputNumber, NRadioButton, NRadioGroup, NSelect, NSwitch } from 'naive-ui'
import { Sparkles } from 'lucide-vue-next'
import { machinesApi, type Machine, type Recommendation } from '@/api/machines'
import MachineCard from '@/components/machines/MachineCard.vue'
import MachineDrawer from '@/components/machines/MachineDrawer.vue'
import { loadMachines, useMachines } from '@/composables/machines'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { maxGpuMemory } from '@/utils/machines'

const { t, te } = useI18n()
const { showError } = useErrorMessage()
const { machines, loaded } = useMachines()

let timer: number | undefined
onMounted(() => {
  void loadMachines(true)
  timer = window.setInterval(() => loadMachines(true), 10_000)
})
onBeforeUnmount(() => window.clearInterval(timer))

// ---- filters and sorting ---------------------------------------------------------------------------

const kind = ref<'all' | 'gpu' | 'cpu'>('all')
const minGpuGb = ref<number | null>(null)
const minRamGb = ref<number | null>(null)
const minCores = ref<number | null>(null)
const freeNow = ref(false)
const sort = ref<'name' | 'gpu' | 'cores' | 'memory' | 'load'>('name')

const gbOptions = (values: number[]) => values.map((v) => ({ label: `≥ ${v} GB`, value: v }))
const gpuOptions = computed(() => gbOptions([8, 12, 16, 24, 32, 48, 80]))
const ramOptions = computed(() => gbOptions([4, 8, 16, 32, 64, 128]))
const sortOptions = computed(() =>
  (['name', 'gpu', 'cores', 'memory', 'load'] as const).map((k) => ({ label: t(`machines.sort.${k}`), value: k })),
)

function matches(m: Machine): boolean {
  if (kind.value === 'gpu' && m.kind !== 'gpu') return false
  if (kind.value === 'cpu' && m.kind !== 'cpu') return false
  if (minGpuGb.value && (m.capacity.max_lane_gpu_memory_mb ?? maxGpuMemory(m)) < minGpuGb.value * 1024) return false
  if (minRamGb.value && (m.capacity.memory_free_mb ?? 0) < minRamGb.value * 1024) return false
  if (minCores.value && (m.capacity.cpu_cores_free ?? 0) < minCores.value) return false
  if (freeNow.value) {
    if (!m.online) return false
    const gpuFree = m.capacity.lanes_free > 0
    const cpuFree = (m.capacity.cpu_cores_free ?? 0) > 0
    if (kind.value === 'gpu' ? !gpuFree : kind.value === 'cpu' ? !cpuFree : !(gpuFree || cpuFree)) return false
  }
  return true
}

const shown = computed(() => {
  const list = machines.value.filter(matches)
  const key: Record<typeof sort.value, (m: Machine) => number> = {
    name: () => 0,
    gpu: (m) => -(m.capacity.gpu_memory_free_mb ?? (m.online ? m.capacity.gpu_memory_total_mb : 0)),
    cores: (m) => -(m.capacity.cpu_cores_free ?? 0),
    memory: (m) => -(m.capacity.memory_free_mb ?? 0),
    load: (m) => m.cpu_percent ?? 101,
  }
  const k = key[sort.value]
  return [...list].sort(
    (a, b) =>
      Number(b.online) - Number(a.online) || k(a) - k(b) || Number(b.is_local) - Number(a.is_local) || a.name.localeCompare(b.name),
  )
})

const totals = computed(() => {
  const on = machines.value.filter((m) => m.online)
  return {
    machines: machines.value.length,
    online: on.length,
    gpusFree: on.reduce((n, m) => n + m.capacity.gpus_free, 0),
    gpus: machines.value.reduce((n, m) => n + m.capacity.gpus, 0),
    cores: on.reduce((n, m) => n + (m.capacity.cpu_cores_free ?? 0), 0),
  }
})

function reset() {
  kind.value = 'all'
  minGpuGb.value = minRamGb.value = minCores.value = null
  freeNow.value = false
}

// ---- pick for me (uses the filters as the requirements) ------------------------------------------------

const picking = ref(false)
const pick = ref<Recommendation | null>(null)
async function pickForMe() {
  picking.value = true
  try {
    pick.value = await machinesApi.recommend({
      gpu: kind.value === 'all' ? (minGpuGb.value ? true : null) : kind.value === 'gpu',
      min_gpu_memory_mb: (minGpuGb.value ?? 0) * 1024,
      memory_mb: (minRamGb.value ?? 0) * 1024,
      cores: minCores.value ?? 0,
      purpose: 'any',
    })
  } catch (e) {
    showError(e)
  } finally {
    picking.value = false
  }
}
const reasonText = (code: string) => (te(`machines.reasons.${code}`) ? t(`machines.reasons.${code}`) : code)

const openId = ref<number | null>(null)
</script>

<template>
  <div class="page wide">
    <div class="page-header">
      <div>
        <h1>{{ t('machines.title') }}</h1>
        <p>{{ t('machines.subtitle') }}</p>
      </div>
      <div class="totals faint num" data-test="machine-totals">
        {{ t('machines.totals', { online: totals.online, n: totals.machines, free: totals.gpusFree, gpus: totals.gpus, cores: Math.round(totals.cores) }) }}
      </div>
    </div>

    <section class="card filters">
      <NRadioGroup v-model:value="kind" size="small">
        <NRadioButton value="all">{{ t('machines.kindAll') }}</NRadioButton>
        <NRadioButton value="gpu">{{ t('machines.hasGpu') }}</NRadioButton>
        <NRadioButton value="cpu">{{ t('machines.cpuOnly') }}</NRadioButton>
      </NRadioGroup>
      <label class="f">
        <span>{{ t('machines.minGpuMemory') }}</span>
        <NSelect v-model:value="minGpuGb" :options="gpuOptions" :placeholder="t('machines.any')" clearable size="small" class="sel" data-filter="gpu-memory" />
      </label>
      <label class="f">
        <span>{{ t('machines.minFreeRam') }}</span>
        <NSelect v-model:value="minRamGb" :options="ramOptions" :placeholder="t('machines.any')" clearable size="small" class="sel" data-filter="ram" />
      </label>
      <label class="f">
        <span>{{ t('machines.minCores') }}</span>
        <NInputNumber v-model:value="minCores" size="small" :min="0" :max="512" :placeholder="t('machines.any')" clearable class="num-in" />
      </label>
      <label class="f switch">
        <NSwitch v-model:value="freeNow" size="small" data-filter="free-now" />
        <span>{{ t('machines.freeNowFilter') }}</span>
      </label>
      <span class="spacer" />
      <label class="f">
        <span>{{ t('machines.sortBy') }}</span>
        <NSelect v-model:value="sort" :options="sortOptions" size="small" class="sel" />
      </label>
      <NButton size="small" secondary type="primary" :loading="picking" data-action="machines-pick" @click="pickForMe">
        <template #icon><Sparkles :size="14" /></template>
        {{ t('machines.pickForMe') }}
      </NButton>
    </section>

    <div v-if="pick" class="pick card" data-test="pick-result">
      <template v-if="pick.best">
        <span>{{ t('machines.recommended') }}</span>
        <b>{{ pick.best.node_name }}<template v-if="pick.best.lane_name"> · {{ pick.best.lane_name }}</template></b>
        <span v-if="pick.best.reasons.length" class="faint">({{ pick.best.reasons.map(reasonText).join(' · ') }})</span>
        <NButton size="tiny" quaternary @click="openId = pick.best.node_id">{{ t('machines.details') }}</NButton>
      </template>
      <span v-else class="warn-text">{{ t('machines.nothingFits') }}</span>
      <span class="spacer" />
      <NButton size="tiny" quaternary @click="pick = null">{{ t('common.close') }}</NButton>
    </div>

    <div v-if="loaded && !machines.length" class="card empty">{{ t('machines.empty') }}</div>
    <div v-else-if="loaded && !shown.length" class="card empty">
      {{ t('machines.noMatch') }} <NButton text type="primary" @click="reset">{{ t('machines.resetFilters') }}</NButton>
    </div>
    <div v-else class="grid">
      <MachineCard
        v-for="m in shown"
        :key="m.id"
        :machine="m"
        :class="{ recommended: pick?.best?.node_id === m.id }"
        @open="openId = $event.id"
      />
    </div>

    <MachineDrawer :machine-id="openId" @close="openId = null" />
  </div>
</template>

<style scoped>
.page.wide {
  max-width: 1400px;
}
.totals {
  font-size: 13px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  padding: 12px 16px;
  margin-bottom: 16px;
  overflow: visible;
}
.f {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-2);
}
.sel {
  width: 130px;
}
.num-in {
  width: 110px;
}
.pick {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  padding: 10px 16px;
  margin-bottom: 16px;
  font-size: 13px;
  background: var(--brand-softer);
  border-color: var(--brand-soft);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  align-items: start;
}
.grid :deep(.recommended) {
  border-color: var(--brand);
  box-shadow: 0 0 0 1px var(--brand);
}
.warn-text {
  color: var(--warn-text);
}
@media (max-width: 600px) {
  .grid {
    grid-template-columns: 1fr;
  }
  .f {
    width: 100%;
    justify-content: space-between;
  }
  .f.switch {
    justify-content: flex-start;
  }
  .sel,
  .num-in {
    width: 60%;
  }
  .filters .spacer {
    display: none;
  }
}
</style>
