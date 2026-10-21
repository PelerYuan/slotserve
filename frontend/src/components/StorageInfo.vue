<script setup lang="ts">
// The storages a person's environments get (Admin -> Storage), with their own usage of a personal space.
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { storageApi, type MyStorage } from '@/api/storage'
import { formatBytes } from '@/utils/format'

const { t } = useI18n()
const rows = ref<MyStorage[]>([])
onMounted(async () => {
  rows.value = await storageApi.mine().catch(() => [])
})
</script>

<template>
  <section v-if="rows.length" class="block storage-info">
    <h2 class="block-title">{{ t('storage.mineTitle') }}</h2>
    <div class="card">
      <div class="table-wrap">
        <table class="table">
          <tbody>
            <tr v-for="s in rows" :key="s.id">
              <td class="strong">{{ s.name }}</td>
              <td class="small muted">{{ t(`storage.purpose.${s.purpose}`) }}</td>
              <td class="small">
                <span v-if="s.path" class="mono">{{ s.path }}</span>
                <span v-else class="muted">{{ t('storage.keptDirs') }}</span>
                <span v-if="s.read_only" class="badge"> {{ t('storage.ro') }}</span>
                <span v-if="s.ok === false" class="badge bad"> {{ t('storage.unreachable') }}</span>
              </td>
              <td class="small right">
                <template v-if="s.purpose === 'personal' && s.used != null">
                  <span :class="{ over: s.over }">{{ formatBytes(s.used) }}</span>
                  <span v-if="s.quota_gb" class="faint"> / {{ s.quota_gb }} GB</span>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <p class="faint small hint">{{ t('storage.mineHint') }}</p>
  </section>
</template>

<style scoped>
.strong {
  font-weight: 600;
}
.small {
  font-size: 12.5px;
}
.over {
  color: var(--danger-text);
  font-weight: 600;
}
.hint {
  margin: 8px 0 0;
}
</style>
