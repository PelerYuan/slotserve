<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Server } from 'lucide-vue-next'
import { sharingApi, type NodeSharing } from '@/api/sharing'
import SharingPanel from '@/components/sharing/SharingPanel.vue'
import { useErrorMessage } from '@/composables/useErrorMessage'

const { t } = useI18n()
const { showError } = useErrorMessage()
const nodes = ref<NodeSharing[]>([])
const loaded = ref(false)

onMounted(async () => {
  try {
    nodes.value = await sharingApi.mine()
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('sharing.title') }}</h1>
        <p>{{ t('sharing.subtitle') }}</p>
      </div>
    </div>
    <div v-if="loaded && !nodes.length" class="card empty">{{ t('sharing.empty') }}</div>
    <section v-for="n in nodes" :key="n.id" class="card machine">
      <div class="card-header">
        <h3 class="card-title"><Server :size="15" class="faint" /> {{ n.name }}</h3>
      </div>
      <div class="card-pad">
        <SharingPanel :node-id="n.id" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.machine {
  margin-bottom: 16px;
}
</style>
