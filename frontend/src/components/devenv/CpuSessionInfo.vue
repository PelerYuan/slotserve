<script setup lang="ts">
// The extras of an environment running without a GPU: live CPU and memory, pause, and what happens at a booking.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useMessage } from 'naive-ui'
import { Cpu } from 'lucide-vue-next'
import { cpuApi } from '@/api/devenvCpu'
import type { DevProject, DevSession } from '@/api/types'
import { cpuLive, formatCores, loadCpu } from '@/composables/devenvCpu'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ session: DevSession; projects: DevProject[] }>()
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const busy = ref(false)

const live = computed(() => cpuLive(props.session.id))
const project = computed(() => props.projects.find((p) => p.id === props.session.project_id) ?? null)
const kept = computed(() => props.session.project_id !== null)

async function pause() {
  busy.value = true
  try {
    await cpuApi.stop(props.session.id)
    message.success(kept.value ? t('devenvCpu.paused') : t('devenvCpu.stopped'))
    await loadCpu().catch(() => undefined)
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="cpu-info" data-test="cpu-session">
    <div class="row wrap line">
      <span class="badge brand"><Cpu :size="12" /> {{ t('devenvCpu.badge') }}</span>
      <template v-if="session.state === 'running' && live">
        <span class="small num">{{ t('devenvCpu.liveCpu', { n: formatCores(live.cpu) }) }}</span>
        <span class="small num">
          {{ t('devenvCpu.liveMemory', { used: formatBytes(live.memory) }) }}
          <template v-if="live.memory_limit"> / {{ formatBytes(live.memory_limit) }}</template>
        </span>
      </template>
      <span class="spacer" />
      <NButton
        v-if="session.state !== 'stopped' && session.state !== 'stopping'"
        size="small"
        secondary
        :loading="busy"
        data-test="cpu-pause"
        @click="pause"
      >
        {{ session.state === 'failed' ? t('devenvCpu.dismiss') : kept ? t('devenvCpu.pause') : t('devenvCpu.stop') }}
      </NButton>
    </div>
    <p class="faint small">
      <template v-if="project && project.upcoming">{{ t('devenvCpu.switchHint') }}</template>
      <template v-else-if="kept">{{ t('devenvCpu.keptHint') }}</template>
      <template v-else>{{ t('devenvCpu.throwawayHint') }}</template>
    </p>
  </div>
</template>

<style scoped>
.cpu-info {
  display: grid;
  gap: 4px;
}
.line {
  gap: 12px;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
p {
  margin: 0;
}
</style>
