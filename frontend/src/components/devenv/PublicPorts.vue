<script setup lang="ts">
// Direct ports (host ports published for dev environments): your own, or everyone's for admins, who can reclaim them.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NSwitch, useDialog } from 'naive-ui'
import { ExternalLink } from 'lucide-vue-next'
import { portsApi, type DevPublicPort } from '@/api/devenvPorts'
import { useErrorMessage } from '@/composables/useErrorMessage'

const props = defineProps<{ isAdmin: boolean }>()
const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()

const rows = ref<DevPublicPort[]>([])
const everyone = ref(false)

async function load() {
  try {
    rows.value = await portsApi.publicPorts(props.isAdmin && everyone.value)
  } catch (e) {
    showError(e)
  }
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(load, 10000)
})
onBeforeUnmount(() => window.clearInterval(timer))

function release(p: DevPublicPort) {
  const reclaim = everyone.value && p.owner !== null
  dialog.warning({
    title: reclaim ? t('devenv.ports.reclaim') : t('devenv.ports.release'),
    content: t('devenv.ports.releaseConfirm', { port: p.host_port, env: p.environment }),
    positiveText: reclaim ? t('devenv.ports.reclaim') : t('devenv.ports.release'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await portsApi.release(p.id)
        load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

const tone: Record<string, string> = { active: 'ok', starting: 'warn', error: 'danger', off: '', paused: '', login: '' }
</script>

<template>
  <section v-if="rows.length || isAdmin" class="block">
    <div class="block-head">
      <h2 class="block-title">{{ t('devenv.ports.publicTitle') }}</h2>
      <label v-if="isAdmin" class="row small toggle">
        <NSwitch v-model:value="everyone" size="small" @update:value="load" />{{ t('devenv.everyone') }}
      </label>
    </div>
    <p class="muted small">{{ t('devenv.ports.publicHint') }}</p>
    <div v-if="!rows.length" class="faint small">{{ t('devenv.ports.publicEmpty') }}</div>
    <div v-else class="card table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('devenv.ports.hostPort') }}</th>
            <th>{{ t('devenv.ports.port') }}</th>
            <th>{{ t('devenv.ports.environment') }}</th>
            <th v-if="everyone">{{ t('devenv.ports.owner') }}</th>
            <th>{{ t('devenv.ports.state') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in rows" :key="p.id" :data-host-port="p.host_port">
            <td class="mono">{{ p.host_port }}</td>
            <td class="mono">{{ p.container_port }} <span class="faint">{{ p.name }}</span></td>
            <td>
              {{ p.environment }}
              <span v-if="p.project_id" class="badge brand">{{ t('devenv.ports.kept') }}</span>
            </td>
            <td v-if="everyone">{{ p.owner ?? t('devenv.sharedBadge') }}</td>
            <td>
              <span class="badge" :class="tone[p.status]" :title="p.error ?? ''">{{ t(`devenv.ports.status.${p.status}`) }}</span>
            </td>
            <td class="actions">
              <NButton v-if="p.status === 'active'" tag="a" :href="p.url" target="_blank" rel="noopener" size="tiny" quaternary>
                <template #icon><ExternalLink :size="13" /></template>
              </NButton>
              <NButton size="tiny" quaternary type="error" @click="release(p)">
                {{ everyone && p.owner !== null ? t('devenv.ports.reclaim') : t('devenv.ports.release') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
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
  margin-bottom: 12px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
}
.toggle {
  gap: 6px;
  cursor: pointer;
}
.table-wrap {
  overflow-x: auto;
}
.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.table th,
.table td {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
.table th {
  color: var(--text-3);
  font-weight: 500;
}
.actions {
  text-align: right;
  white-space: nowrap;
}
</style>
