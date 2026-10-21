<script setup lang="ts">
// A kept environment without a GPU: start it now, and whether it continues without a GPU after a booking.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { NSwitch } from 'naive-ui'
import { cpuApi } from '@/api/devenvCpu'
import type { DevProject } from '@/api/types'
import { cpuInfo, loadCpu } from '@/composables/devenvCpu'
import { useErrorMessage } from '@/composables/useErrorMessage'
import CpuStartButton from './CpuStartButton.vue'

const props = defineProps<{ project: DevProject }>()
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const { showError } = useErrorMessage()

const after = computed(() => !!cpuInfo.value?.after_booking.includes(props.project.id))

async function setAfter(value: boolean) {
  try {
    await cpuApi.afterBooking(props.project.id, value)
    await loadCpu()
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <div v-if="cpuInfo?.enabled && project.mine" class="cpu-project" data-test="cpu-project">
    <CpuStartButton v-if="project.state !== 'running'" :project-id="project.id" @started="emit('changed')" />
    <label class="row small toggle" :title="t('devenvCpu.afterBookingHint')">
      <NSwitch :value="after" size="small" @update:value="setAfter" />{{ t('devenvCpu.afterBooking') }}
    </label>
  </div>
</template>

<style scoped>
.cpu-project {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}
.toggle {
  gap: 6px;
  cursor: pointer;
}
</style>
