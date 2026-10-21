<script setup lang="ts">
// "Start without GPU" for a template or a kept environment (no booking needed).
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useMessage } from 'naive-ui'
import { Cpu } from 'lucide-vue-next'
import { cpuApi } from '@/api/devenvCpu'
import { cpuCanStart, cpuInfo, loadCpu } from '@/composables/devenvCpu'
import { useErrorMessage } from '@/composables/useErrorMessage'

const props = defineProps<{ templateId?: number; projectId?: number; size?: 'tiny' | 'small' }>()
const emit = defineEmits<{ started: [] }>()

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const busy = ref(false)
const full = computed(() => !!cpuInfo.value && cpuInfo.value.running >= cpuInfo.value.per_user)

async function start() {
  busy.value = true
  try {
    await cpuApi.start(props.projectId ? { project_id: props.projectId } : { template_id: props.templateId })
    message.success(t('devenvCpu.starting'))
    await loadCpu().catch(() => undefined)
    emit('started')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NButton
    v-if="cpuInfo?.enabled"
    :size="size ?? 'tiny'"
    secondary
    :loading="busy"
    :disabled="!cpuCanStart()"
    :title="full ? t('devenvCpu.limitReached', { n: cpuInfo.per_user }) : t('devenvCpu.startHint')"
    data-test="cpu-start"
    @click="start"
  >
    <template #icon><Cpu :size="13" /></template>
    {{ t('devenvCpu.start') }}
  </NButton>
</template>
