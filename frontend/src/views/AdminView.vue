<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NTabPane, NTabs } from 'naive-ui'
import AdminOverview from '@/components/admin/AdminOverview.vue'
import AdminUsers from '@/components/admin/AdminUsers.vue'
import AdminReservations from '@/components/admin/AdminReservations.vue'
import AdminKeys from '@/components/admin/AdminKeys.vue'
import AdminSettings from '@/components/admin/AdminSettings.vue'
import AdminAudit from '@/components/admin/AdminAudit.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const TABS = ['overview', 'users', 'reservations', 'keys', 'settings', 'audit'] as const
type Tab = (typeof TABS)[number]

const tab = computed<Tab>({
  get: () => (TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : 'overview'),
  set: (v) => router.replace({ query: v === 'overview' ? {} : { tab: v } }),
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('admin.title') }}</h1>
        <p>{{ t('admin.subtitle') }}</p>
      </div>
    </div>

    <NTabs v-model:value="tab" type="line" animated class="tabs" :tabs-padding="0">
      <NTabPane v-for="name in TABS" :key="name" :name="name" :tab="t(`admin.tabs.${name}`)" display-directive="if">
        <AdminOverview v-if="name === 'overview'" @navigate="(v: Tab) => (tab = v)" />
        <AdminUsers v-else-if="name === 'users'" />
        <AdminReservations v-else-if="name === 'reservations'" />
        <AdminKeys v-else-if="name === 'keys'" />
        <AdminSettings v-else-if="name === 'settings'" />
        <AdminAudit v-else />
      </NTabPane>
    </NTabs>
  </div>
</template>

<style scoped>
.tabs :deep(.n-tabs-nav) {
  margin-bottom: 20px;
}
.tabs :deep(.n-tabs-tab) {
  font-weight: 500;
}
</style>
