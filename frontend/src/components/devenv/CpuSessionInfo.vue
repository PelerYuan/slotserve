<script setup lang="ts">
// The extras of an environment running without a GPU: live CPU and memory, and what happens at a booking.
// (Stopping / pausing is on the environment card's header: features/devenv/components/EnvCard.vue.)
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Cpu } from 'lucide-vue-next'
import type { DevProject, DevSession } from '@/api/types'
import { cpuLive, formatCores } from '@/composables/devenvCpu'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ session: DevSession; projects: DevProject[] }>()

const { t } = useI18n()

const live = computed(() => cpuLive(props.session.id))
const project = computed(() => props.projects.find((p) => p.id === props.session.project_id) ?? null)
const kept = computed(() => props.session.project_id !== null)
</script>

<template>
  <div class="cpu-info" data-test="cpu-session">
    <div v-if="session.state === 'running' && live" class="row wrap line">
      <Cpu :size="13" class="faint" />
      <template v-if="live">
        <span class="small num">{{ t('devenvCpu.liveCpu', { n: formatCores(live.cpu) }) }}</span>
        <span class="small num">
          {{ t('devenvCpu.liveMemory', { used: formatBytes(live.memory) }) }}
          <template v-if="live.memory_limit"> / {{ formatBytes(live.memory_limit) }}</template>
        </span>
      </template>
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
