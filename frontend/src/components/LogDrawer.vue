<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'

const show = defineModel<boolean>('show', { default: false })
const { t } = useI18n()
const lines = ref<string[]>([])
const loading = ref(false)
const box = ref<HTMLElement | null>(null)
let timer: number | undefined

async function load() {
  loading.value = true
  try {
    lines.value = (await api.logs(400)).lines
    await nextTick()
    box.value?.scrollTo({ top: box.value.scrollHeight })
  } finally {
    loading.value = false
  }
}

watch(show, (open) => {
  window.clearInterval(timer)
  if (open) {
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
  background: #fbfbfc;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  color: #2f2f3a;
}
.mark {
  color: var(--brand);
  font-weight: 600;
  margin-top: 8px;
}
</style>
