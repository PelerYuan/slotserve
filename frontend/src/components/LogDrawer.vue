<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDrawer, NDrawerContent, NSelect } from 'naive-ui'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'
import { useLanes } from '@/composables/lanes'

const show = defineModel<boolean>('show', { default: false })
const props = defineProps<{ laneId?: number | null }>()
const { t } = useI18n()
const { lanes, multi } = useLanes()
const lane = ref<number | null>(null)
const laneOptions = () => lanes.value.map((l) => ({ label: l.name, value: l.id }))
const lines = ref<string[]>([])
const loading = ref(false)
const box = ref<HTMLElement | null>(null)
let timer: number | undefined

async function load() {
  loading.value = true
  try {
    lines.value = (await api.logs(400, lane.value)).lines
    await nextTick()
    box.value?.scrollTo({ top: box.value.scrollHeight })
  } finally {
    loading.value = false
  }
}

watch(show, (open) => {
  window.clearInterval(timer)
  if (open) {
    lane.value = props.laneId ?? lanes.value[0]?.id ?? null
    load()
    timer = window.setInterval(load, 4000)
  }
})
</script>

<template>
  <NDrawer v-model:show="show" :width="760" placement="right" style="max-width: 100vw">
    <NDrawerContent :title="t('models.logsTitle')" closable :native-scrollbar="false" body-content-style="padding:0">
      <template #header>
        <div class="row">
          <span>{{ t('models.logsTitle') }}</span>
          <NSelect
            v-if="multi"
            v-model:value="lane"
            :options="laneOptions()"
            :placeholder="t('ph.lane')"
            size="small"
            style="width: 160px"
            @update:value="load"
          />
          <NButton quaternary circle size="small" :loading="loading" @click="load">
            <template #icon><RefreshCw :size="15" /></template>
          </NButton>
        </div>
      </template>
      <div ref="box" class="log">
        <div v-if="!lines.length" class="faint">{{ t('models.noLogs') }}</div>
        <div v-for="(line, i) in lines" :key="i" :class="{ mark: line.startsWith('=====') }">{{ line || ' ' }}</div>
      </div>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.log {
  height: calc(100vh - 70px);
  overflow: auto;
  padding: 16px 20px;
  background: var(--code-bg);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--code-text);
}
.mark {
  color: var(--brand);
  font-weight: 600;
  margin-top: 8px;
}
</style>
