<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NProgress, NTabPane, NTabs } from 'naive-ui'
import { CalendarPlus, Cpu, Hand, KeyRound, Lock, RotateCw, ScrollText, ShieldCheck, TriangleAlert } from 'lucide-vue-next'
import StatusDot from '@/components/StatusDot.vue'
import CodeBlock from '@/components/CodeBlock.vue'
import LogDrawer from '@/components/LogDrawer.vue'
import { api } from '@/api/client'
import { session } from '@/composables/session'
import { useStatus } from '@/composables/useStatus'
import { useNow } from '@/composables/useNow'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDuration, formatRange, formatTime, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const { status, refresh } = useStatus()
const now = useNow()
const { showError, message } = useErrorMessage()
const showLogs = ref(false)
const restarting = ref(false)

const runtime = computed(() => status.value?.runtime)
const holder = computed(() => status.value?.active_reservation ?? null)
const mine = computed(() => holder.value?.user_id === session.user?.id)
const canRestart = computed(() => !holder.value || mine.value || session.user?.is_admin)
const manual = computed(() => status.value?.manual ?? null)
const fallbackFrom = computed(() => (status.value?.reason === 'fallback' ? status.value.fallback_from : null))
const exclusive = computed(() => !!holder.value?.exclusive)
const modelLabel = computed(() => {
  const rt = runtime.value
  if (!rt?.model_name) return null
  return rt.preset_name ? `${rt.model_name} · ${rt.preset_name}` : rt.model_name
})

const extendOptions = computed(() => [
  { key: 30, label: t('schedule.extendBy', { n: 30 }) },
  { key: 60, label: t('schedule.extendBy', { n: 60 }) },
])

async function extend(minutes: number) {
  if (!holder.value) return
  try {
    await api.extendReservation(holder.value.id, minutes)
    message.success(t('schedule.extended'))
    refresh()
  } catch (e) {
    showError(e)
  }
}

const loadProgress = computed(() => {
  const rt = runtime.value
  if (!rt || rt.state !== 'loading' || !rt.load_started_at) return null
  const elapsed = (now.value - new Date(rt.load_started_at).getTime()) / 1000
  const est = rt.est_load_seconds || 60
  return { pct: Math.min(99, (elapsed / est) * 100), remaining: est - elapsed }
})

const baseUrl = computed(() => `${window.location.origin}/v1`)
const modelName = computed(() => runtime.value?.model_name ?? 'default')
const curl = computed(
  () => `curl ${baseUrl.value}/chat/completions \\
  -H "Authorization: Bearer $SLOTSERVE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${modelName.value}", "messages": [{"role": "user", "content": "Hello"}]}'`,
)
const python = computed(
  () => `from openai import OpenAI

client = OpenAI(base_url="${baseUrl.value}", api_key="sk-ss-...")
resp = client.chat.completions.create(
    model="${modelName.value}",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)`,
)

function memPct(used: number, total: number) {
  return total ? Math.round((used / total) * 100) : 0
}

async function restart() {
  restarting.value = true
  try {
    await api.restart()
    message.success(t('overview.restartDone'))
    window.setTimeout(refresh, 800)
  } catch (e) {
    showError(e)
  } finally {
    restarting.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('overview.title') }}</h1>
        <p>{{ t('overview.subtitle') }}</p>
      </div>
      <RouterLink :to="{ name: 'schedule' }">
        <NButton type="primary">
          <template #icon><CalendarPlus :size="16" /></template>
          {{ t('overview.book') }}
        </NButton>
      </RouterLink>
    </div>

    <div v-if="fallbackFrom" class="banner warn">
      <TriangleAlert :size="16" />
      <span>{{ t('overview.fallbackBanner', { model: fallbackFrom, current: runtime?.model_name }) }}</span>
    </div>
    <div v-else-if="manual" class="banner">
      <Hand :size="16" />
      <span>
        {{
          manual.model_name
            ? t('overview.manualLoad', { user: manual.by, model: manual.model_name })
            : t('overview.manualUnload', { user: manual.by })
        }}
        {{ manual.until ? t('overview.manualUntil', { time: formatTime(manual.until) }) : t('overview.manualUntilNext') }}
      </span>
    </div>

    <div class="top">
      <!-- Current model -->
      <section class="card card-pad model-card">
        <div class="row">
          <span class="label">{{ t('overview.currentModel') }}</span>
          <span class="spacer" />
          <StatusDot v-if="runtime" :state="runtime.state" />
        </div>
        <div class="model-name">{{ exclusive ? t('schedule.kindExclusive') : (modelLabel ?? t('overview.noModel')) }}</div>
        <div class="model-sub muted">
          <template v-if="loadProgress">
            {{
              loadProgress.remaining > 0
                ? t('overview.loadingEta', { eta: formatDuration(loadProgress.remaining) })
                : t('overview.loadingOverdue')
            }}
          </template>
          <template v-else-if="runtime?.state === 'ready' && runtime.ready_at">
            {{ t('overview.readySince', { time: formatTime(runtime.ready_at) }) }}
          </template>
          <template v-else-if="runtime?.state === 'failed'">
            <span class="err">{{ runtime.error }}</span>
          </template>
          <template v-else-if="exclusive">{{ t('overview.exclusiveHint') }}</template>
          <template v-else-if="!runtime?.model_name">{{ t('overview.noModelHint') }}</template>
        </div>
        <NProgress
          v-if="loadProgress"
          type="line"
          :percentage="loadProgress.pct"
          :show-indicator="false"
          :height="6"
          color="var(--brand)"
          rail-color="var(--rail)"
          class="progress"
        />
        <div class="actions">
          <NButton size="small" secondary :disabled="!canRestart || !runtime?.model_name" :loading="restarting" @click="restart">
            <template #icon><RotateCw :size="14" /></template>
            {{ t('overview.restart') }}
          </NButton>
          <NButton size="small" quaternary @click="showLogs = true">
            <template #icon><ScrollText :size="14" /></template>
            {{ t('overview.viewLogs') }}
          </NButton>
        </div>
      </section>

      <!-- Access -->
      <section class="card card-pad access" :class="{ reserved: holder }">
        <span class="label">{{ t('overview.access') }}</span>
        <div class="access-body">
          <div class="access-icon">
            <Lock v-if="holder" :size="20" :stroke-width="1.8" />
            <ShieldCheck v-else :size="20" :stroke-width="1.8" />
          </div>
          <div>
            <div class="access-title">
              <template v-if="holder">
                {{ mine ? t('overview.reservedByYou') : t('overview.reservedBy', { user: holder.username }) }}
              </template>
              <template v-else>{{ t('overview.freeAccess') }}</template>
            </div>
            <div class="muted small">
              <template v-if="holder">
                {{ t('overview.until', { time: formatTime(holder.end_at) }) }} · {{ relativeFromNow(holder.end_at) }}
              </template>
              <template v-else>{{ t('overview.freeAccessDesc') }}</template>
            </div>
          </div>
        </div>
        <NDropdown v-if="holder && mine" :options="extendOptions" trigger="click" @select="extend">
          <NButton size="small" secondary type="primary" class="extend">{{ t('schedule.extend') }}</NButton>
        </NDropdown>
      </section>

      <!-- GPU -->
      <section class="card card-pad gpu">
        <div class="row">
          <span class="label">{{ t('overview.gpu') }}</span>
          <span class="spacer" />
          <Cpu :size="16" class="faint" />
        </div>
        <template v-if="status?.gpus.length">
          <div v-for="g in status.gpus" :key="g.index" class="gpu-item">
            <div class="gpu-name">{{ g.name }}</div>
            <div class="mem-row">
              <span class="muted small">{{ t('overview.memory') }}</span>
              <span class="num small">
                {{ (g.memory_used_mb / 1024).toFixed(1) }} / {{ (g.memory_total_mb / 1024).toFixed(0) }} GB
              </span>
            </div>
            <NProgress
              type="line"
              :percentage="memPct(g.memory_used_mb, g.memory_total_mb)"
              :show-indicator="false"
              :height="6"
              color="var(--brand)"
              rail-color="var(--rail)"
            />
            <div class="metrics">
              <div>
                <div class="faint small">{{ t('overview.utilization') }}</div>
                <div class="metric num">{{ g.utilization_pct }}%</div>
              </div>
              <div>
                <div class="faint small">{{ t('overview.temperature') }}</div>
                <div class="metric num">{{ g.temperature_c }}°C</div>
              </div>
              <div v-if="g.power_w != null">
                <div class="faint small">{{ t('overview.power') }}</div>
                <div class="metric num">{{ Math.round(g.power_w) }} W</div>
              </div>
            </div>
          </div>
        </template>
        <div v-else class="muted small no-gpu">{{ t('overview.noGpu') }}</div>
      </section>
    </div>

    <div class="bottom">
      <section class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('overview.upcoming') }}</h3>
          <RouterLink :to="{ name: 'schedule' }" class="small">{{ t('nav.schedule') }} →</RouterLink>
        </div>
        <div v-if="!status?.upcoming.length" class="empty">{{ t('overview.noUpcoming') }}</div>
        <ul v-else class="upcoming">
          <li v-for="r in status.upcoming" :key="r.id">
            <div class="when">
              <div class="num">{{ formatRange(r.start_at, r.end_at) }}</div>
              <div class="faint small">{{ relativeFromNow(r.start_at) }}</div>
            </div>
            <div class="who">
              <div class="who-name">
                {{ r.username }}<span v-if="r.user_id === session.user?.id" class="you">{{ t('common.you') }}</span>
              </div>
              <div class="faint small mono">{{ r.model_name }}</div>
            </div>
          </li>
        </ul>
      </section>

      <section class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">{{ t('overview.quickStart') }}</h3>
            <div class="card-sub">{{ t('overview.quickStartHint') }}</div>
          </div>
          <RouterLink :to="{ name: 'keys' }">
            <NButton size="small" secondary>
              <template #icon><KeyRound :size="14" /></template>
              {{ t('overview.createKey') }}
            </NButton>
          </RouterLink>
        </div>
        <div class="card-pad quick">
          <div class="label">{{ t('keys.endpoint') }}</div>
          <CodeBlock :code="baseUrl" inline />
          <NTabs type="line" size="small" animated class="tabs">
            <NTabPane name="python" tab="Python">
              <CodeBlock :code="python" />
            </NTabPane>
            <NTabPane name="curl" tab="cURL">
              <CodeBlock :code="curl" />
            </NTabPane>
          </NTabs>
        </div>
      </section>
    </div>

    <LogDrawer v-model:show="showLogs" />
  </div>
</template>

<style scoped>
.banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  margin-bottom: 16px;
  border-radius: 10px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 13px;
  font-weight: 500;
}
.banner.warn {
  background: var(--warn-soft);
  color: var(--warn-text);
}
.banner svg {
  flex: none;
}
.extend {
  margin-top: 14px;
}
.top {
  display: grid;
  grid-template-columns: 1.25fr 1fr 1fr;
  gap: 16px;
}
.bottom {
  display: grid;
  grid-template-columns: 1fr 1.25fr;
  gap: 16px;
  margin-top: 16px;
}
.small {
  font-size: 12.5px;
}
.model-card {
  display: flex;
  flex-direction: column;
}
.model-name {
  margin-top: 14px;
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.02em;
  word-break: break-all;
}
.model-sub {
  margin-top: 4px;
  min-height: 22px;
  font-size: 13px;
}
.err {
  color: var(--danger);
}
.progress {
  margin-top: 12px;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: auto;
  padding-top: 18px;
}
.access-body {
  display: flex;
  gap: 14px;
  margin-top: 16px;
  align-items: flex-start;
}
.access-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
  background: var(--ok-soft);
  color: var(--ok-text);
}
.reserved .access-icon {
  background: var(--brand-soft);
  color: var(--brand);
}
.access-title {
  font-weight: 600;
  font-size: 15px;
  margin-bottom: 2px;
}
.gpu-item + .gpu-item {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}
.gpu-name {
  font-weight: 600;
  margin: 12px 0 10px;
}
.mem-row {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}
.metrics {
  display: flex;
  gap: 28px;
  margin-top: 14px;
}
.metric {
  font-weight: 600;
  font-size: 16px;
}
.no-gpu {
  margin-top: 16px;
}
.upcoming {
  list-style: none;
  margin: 0;
  padding: 4px 0;
}
.upcoming li {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 20px;
}
.upcoming li + li {
  border-top: 1px solid var(--border);
}
.who {
  text-align: right;
  min-width: 0;
}
.who-name {
  font-weight: 600;
}
.you {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--brand);
  background: var(--brand-soft);
  padding: 1px 6px;
  border-radius: 5px;
}
.quick .label {
  margin-bottom: 8px;
}
.tabs {
  margin-top: 16px;
}

@media (max-width: 1100px) {
  .top {
    grid-template-columns: 1fr 1fr;
  }
  .model-card {
    grid-column: 1 / -1;
  }
  .bottom {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 640px) {
  .top {
    grid-template-columns: 1fr;
  }
}
</style>
