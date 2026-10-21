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
import AdminRequests from '@/components/admin/AdminRequests.vue'
import AdminLanes from '@/components/admin/AdminLanes.vue'
import AdminNodes from '@/components/admin/AdminNodes.vue'
import AdminRuntime from '@/components/admin/AdminRuntime.vue'
import AdminChannels from '@/components/admin/AdminChannels.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const TABS = ['overview', 'runtime', 'lanes', 'nodes', 'users', 'reservations', 'requests', 'keys', 'channels', 'settings', 'audit'] as const
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
        <AdminRuntime v-else-if="name === 'runtime'" />
        <AdminLanes v-else-if="name === 'lanes'" />
        <AdminNodes v-else-if="name === 'nodes'" />
        <AdminUsers v-else-if="name === 'users'" />
        <AdminReservations v-else-if="name === 'reservations'" />
        <AdminRequests v-else-if="name === 'requests'" />
        <AdminKeys v-else-if="name === 'keys'" />
        <AdminChannels v-else-if="name === 'channels'" @navigate="(v: Tab) => (tab = v)" />
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
