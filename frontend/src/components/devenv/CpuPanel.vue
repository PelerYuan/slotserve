<script setup lang="ts">
// "Without a GPU" on the Dev environments page: limits, starting one, and (admins) every CPU-only environment.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NModal, NSelect, useMessage } from 'naive-ui'
import { Cpu } from 'lucide-vue-next'
import { cpuApi } from '@/api/devenvCpu'
import type { DevProject, DevTemplate } from '@/api/types'
import { session } from '@/composables/session'
import { cpuCanStart, cpuInfo, formatCores, loadCpu } from '@/composables/devenvCpu'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, relativeFromNow } from '@/utils/format'

const props = defineProps<{ templates: DevTemplate[]; projects: DevProject[]; canLogIn: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

async function refresh() {
  try {
    await loadCpu()
  } catch {
    /* the page shows its own errors */
  }
}
let timer: number | undefined
onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 4000)
})
onBeforeUnmount(() => window.clearInterval(timer))

const info = computed(() => cpuInfo.value)
const others = computed(() => (info.value?.sessions ?? []).filter((s) => !s.mine))

// ---- starting ----------------------------------------------------------------
const picker = ref({ open: false, value: null as string | null, node: null as number | null })
// with other machines connected, people pick where it runs (the server's own machine by default)
const nodeOptions = computed(() =>
  (info.value?.nodes ?? []).map((n) => ({
    label:
      n.cpus !== null
        ? t('devenvCpu.nodeOption', { name: n.name, n: n.cpus }) + (n.online ? '' : ` (${t('devenvCpu.nodeOffline')})`)
        : n.name,
    value: n.id ?? 0,
    disabled: !n.online,
  })),
)
const busy = ref(false)
const options = computed(() => {
  const kept = props.projects
    .filter((p) => p.mine && p.state !== 'running')
    .map((p) => ({ label: p.name, value: `p:${p.id}` }))
  const templates = props.templates
    .filter((x) => x.enabled || x.kind !== 'builtin')
    .map((x) => ({ label: x.name, value: `t:${x.id}` }))
  const out = []
  if (kept.length) out.push({ type: 'group', label: t('devenvCpu.keptGroup'), key: 'kept', children: kept })
  out.push({ type: 'group', label: t('devenvCpu.templateGroup'), key: 'templates', children: templates })
  return out
})
function openPicker() {
  picker.value = { open: true, value: null, node: null }
}
async function start() {
  const [kind, id] = (picker.value.value ?? '').split(':')
  if (!id) return
  busy.value = true
  try {
    const where = picker.value.node ? { node_id: picker.value.node } : {}
    await cpuApi.start(kind === 'p' ? { project_id: Number(id), ...where } : { template_id: Number(id), ...where })
    picker.value.open = false
    message.success(t('devenvCpu.starting'))
    await refresh()
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function stop(id: number) {
  try {
    await cpuApi.stop(id)
    await refresh()
    emit('changed')
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <section v-if="info" class="block cpu-panel" data-test="cpu-panel">
    <div class="block-head">
      <h2 class="block-title row"><Cpu :size="16" /> {{ t('devenvCpu.title') }}</h2>
      <NButton
        v-if="info.enabled"
        size="small"
        type="primary"
        secondary
        :disabled="!cpuCanStart() || !canLogIn"
        data-test="cpu-open"
        @click="openPicker"
      >
        {{ t('devenvCpu.start') }}
      </NButton>
    </div>
    <p v-if="!info.enabled" class="faint small">{{ t('devenvCpu.disabled') }}</p>
    <template v-else>
      <p class="muted small">{{ t('devenvCpu.hint') }}</p>
      <ul class="facts small">
        <li>{{ t('devenvCpu.running', { n: info.running, max: info.per_user }) }}</li>
        <li>
          {{ info.cores > 0 ? t('devenvCpu.coresEach', { n: info.cores }) : t('devenvCpu.coresPool') }}
          ·
          {{ info.memory_gb > 0 ? t('devenvCpu.memoryEach', { n: info.memory_gb }) : t('devenvCpu.memoryAny') }}
        </li>
        <li v-if="info.pool_cpus !== null">
          {{ t('devenvCpu.pool', { n: info.pool_cpus, total: info.host_cpus ?? '?', cpus: info.pool ?? '—' }) }}
        </li>
        <li v-if="info.lanes.length">
          {{ t('devenvCpu.reserved') }}
          <span v-for="(l, i) in info.lanes" :key="l.lane_id">
            {{ i ? ', ' : '' }}{{ t('devenvCpu.laneCores', { name: l.name, n: l.cpus, cpus: l.cpuset }) }}
          </span>
        </li>
        <li v-if="info.memory_pool_gb > 0">
          {{ t('devenvCpu.memoryPool', { used: info.memory_claimed_gb, n: info.memory_pool_gb }) }}
        </li>
      </ul>
    </template>

    <!-- admins: everyone's CPU-only environments -->
    <div v-if="isAdmin && others.length" class="card admin-list" data-test="cpu-admin">
      <table class="table small">
        <thead>
          <tr>
            <th>{{ t('devenvCpu.owner') }}</th>
            <th>{{ t('devenvCpu.environment') }}</th>
            <th>{{ t('devenvCpu.stateCol') }}</th>
            <th class="right">{{ t('devenvCpu.cpuCol') }}</th>
            <th class="right">{{ t('devenvCpu.memoryCol') }}</th>
            <th>{{ t('devenvCpu.since') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in others" :key="s.id">
            <td>{{ s.username ?? '—' }}</td>
            <td>{{ s.name }}<span v-if="s.node_name" class="faint"> · {{ s.node_name }}</span></td>
            <td>{{ t(`devenv.state.${s.state}`) }}</td>
            <td class="right num">{{ s.live ? formatCores(s.live.cpu) : '—' }}</td>
            <td class="right num">{{ s.live ? formatBytes(s.live.memory) : '—' }}</td>
            <td class="faint">{{ s.started_at ? relativeFromNow(s.started_at) : '—' }}</td>
            <td class="right">
              <NButton size="tiny" quaternary type="warning" @click="stop(s.id)">
                {{ s.project_id ? t('devenvCpu.pause') : t('devenvCpu.stop') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <NModal v-model:show="picker.open" preset="card" :title="t('devenvCpu.start')" style="max-width: 460px">
      <p class="muted small">{{ t('devenvCpu.pickHint') }}</p>
      <NSelect
        v-model:value="picker.value"
        :options="options"
        :placeholder="t('devenvCpu.pick')"
        filterable
        data-test="cpu-pick"
      />
      <template v-if="nodeOptions.length > 1">
        <p class="muted small node-hint">{{ t('devenvCpu.nodeHint') }}</p>
        <NSelect v-model:value="picker.node" :options="nodeOptions" :placeholder="t('devenvCpu.nodeDefault')" clearable data-test="cpu-node" />
      </template>
      <template #footer>
        <div class="footer">
          <NButton @click="picker.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!picker.value" data-test="cpu-confirm" @click="start">
            {{ t('devenvCpu.start') }}
          </NButton>
        </div>
      </template>
    </NModal>
  </section>
</template>

<style scoped>
.block {
  margin-bottom: 28px;
}
.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
  gap: 6px;
}
.facts {
  margin: 6px 0 0;
  padding-left: 18px;
  color: var(--text-2);
  display: grid;
  gap: 2px;
}
.admin-list {
  margin-top: 12px;
  overflow-x: auto;
}
.table {
  width: 100%;
  border-collapse: collapse;
}
.table th,
.table td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.table .right {
  text-align: right;
}
.node-hint {
  margin-top: 12px;
  margin-bottom: 6px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
p {
  margin: 0;
}
</style>
