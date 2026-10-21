<script setup lang="ts">
// "Start without GPU" for a template or a kept environment (no booking needed). With several machines the owner
// picks the machine first, seeing free cores and memory there (or lets "Pick for me" choose).
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NModal, useMessage } from 'naive-ui'
import { Cpu } from 'lucide-vue-next'
import { cpuApi } from '@/api/devenvCpu'
import type { Machine } from '@/api/machines'
import MachinePicker from '@/components/machines/MachinePicker.vue'
import { cpuCanStart, cpuInfo, loadCpu } from '@/composables/devenvCpu'
import { loadMachines, useMachines } from '@/composables/machines'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { cores, gb } from '@/utils/machines'

const props = defineProps<{ templateId?: number; projectId?: number; size?: 'tiny' | 'small' }>()
const emit = defineEmits<{ started: [] }>()

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const { machines, multi } = useMachines()
const busy = ref(false)
const full = computed(() => !!cpuInfo.value && cpuInfo.value.running >= cpuInfo.value.per_user)
const choosing = ref(false)
const nodeId = ref<number | null>(null)
const chosen = computed(() => machines.value.find((m) => m.id === nodeId.value) ?? null)

const unsupported = (m: Machine) => (m.capacity.cpu_envs ? null : t('machines.reasons.cpu_envs_unsupported'))
const need = () => ({
  gpu: false,
  purpose: 'cpu_env' as const,
  cores: cpuInfo.value?.cores ?? 0,
  memory_mb: Math.round((cpuInfo.value?.memory_gb ?? 0) * 1024),
})

async function start(node: number | null = null) {
  busy.value = true
  try {
    const target = props.projectId ? { project_id: props.projectId } : { template_id: props.templateId }
    await cpuApi.start(node == null ? target : { ...target, node_id: node })
    message.success(t('devenvCpu.starting'))
    choosing.value = false
    await loadCpu().catch(() => undefined)
    emit('started')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function onClick() {
  if (!multi.value) return start()
  await loadMachines(true)
  nodeId.value = machines.value.find((m) => m.is_local && m.capacity.cpu_envs)?.id ?? machines.value.find((m) => m.capacity.cpu_envs)?.id ?? null
  choosing.value = true
}
</script>

<template>
  <NButton
    v-if="cpuInfo?.enabled"
    :size="size ?? 'tiny'"
    secondary
    :loading="busy && !choosing"
    :disabled="!cpuCanStart()"
    :title="full ? t('devenvCpu.limitReached', { n: cpuInfo.per_user }) : t('devenvCpu.startHint')"
    data-test="cpu-start"
    @click="onClick"
  >
    <template #icon><Cpu :size="13" /></template>
    {{ t('devenvCpu.start') }}
  </NButton>
  <NModal
    v-model:show="choosing"
    preset="card"
    :title="t('machines.startWhere')"
    :bordered="false"
    style="width: 640px; max-width: calc(100vw - 32px)"
  >
    <MachinePicker v-model="nodeId" mode="cpu" :need="need" :disabled-reason="unsupported" data-test="cpu-machine" />
    <p v-if="chosen" class="faint small chosen">
      {{ t('machines.startOn', { name: chosen.name, cores: cores(chosen.capacity.cpu_cores_free), memory: gb(chosen.capacity.memory_free_mb) }) }}
    </p>
    <template #footer>
      <div class="footer">
        <NButton @click="choosing = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" :disabled="nodeId == null" data-action="cpu-start-confirm" @click="start(nodeId)">
          {{ t('devenvCpu.start') }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.chosen {
  margin: 12px 0 0;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
