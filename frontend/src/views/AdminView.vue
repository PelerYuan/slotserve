<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NSelect } from 'naive-ui'
import AdminOverview from '@/components/admin/AdminOverview.vue'
import AdminUsers from '@/components/admin/AdminUsers.vue'
import AdminReservations from '@/components/admin/AdminReservations.vue'
import AdminKeys from '@/components/admin/AdminKeys.vue'
import AdminSettings from '@/components/admin/AdminSettings.vue'
import AdminAudit from '@/components/admin/AdminAudit.vue'
import AdminRequests from '@/components/admin/AdminRequests.vue'
import AdminLanes from '@/components/admin/AdminLanes.vue'
import AdminRuntime from '@/components/admin/AdminRuntime.vue'
import AdminChannels from '@/components/admin/AdminChannels.vue'
import AdminStorage from '@/components/admin/AdminStorage.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

// Four groups (F08), so the sections fit at 1440 px and a narrow screen shows a list with the current one.
const GROUPS = [
  { id: 'run', tabs: ['overview', 'runtime', 'lanes', 'storage'] },
  { id: 'people', tabs: ['users', 'reservations', 'keys'] },
  { id: 'talk', tabs: ['channels'] },
  { id: 'system', tabs: ['settings', 'audit', 'requests'] },
] as const
type Tab = (typeof GROUPS)[number]['tabs'][number]
const TABS: readonly Tab[] = GROUPS.flatMap((g) => g.tabs)

const tab = computed<Tab>({
  get: () => (TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : 'overview'),
  set: (v) => router.replace({ query: v === 'overview' ? {} : { tab: v } }),
})

const options = computed(() =>
  GROUPS.map((g) => ({
    type: 'group' as const,
    label: t(`adminSetup.nav.groups.${g.id}`),
    key: g.id,
    children: g.tabs.map((name) => ({ label: t(`adminSetup.nav.tabs.${name}`), value: name })),
  })),
)

// sections that link to other sections
const NAVIGATES = new Set<Tab>(['overview', 'channels'])
const onNavigate = (v: Tab) => (tab.value = v)

const views = {
  overview: AdminOverview,
  runtime: AdminRuntime,
  lanes: AdminLanes,
  storage: AdminStorage,
  users: AdminUsers,
  reservations: AdminReservations,
  keys: AdminKeys,
  channels: AdminChannels,
  settings: AdminSettings,
  audit: AdminAudit,
  requests: AdminRequests,
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('admin.title') }}</h1>
        <p>{{ t('admin.subtitle') }}</p>
      </div>
    </div>

    <nav class="admin-nav" role="tablist" :aria-label="t('adminSetup.nav.choose')">
      <div v-for="g in GROUPS" :key="g.id" class="nav-group" :class="{ current: (g.tabs as readonly string[]).includes(tab) }">
        <span class="caption">{{ t(`adminSetup.nav.groups.${g.id}`) }}</span>
        <div class="items">
          <button
            v-for="name in g.tabs"
            :key="name"
            role="tab"
            type="button"
            class="nav-tab"
            :class="{ active: tab === name }"
            :aria-selected="tab === name"
            :data-tab="name"
            @click="tab = name"
          >
            {{ t(`adminSetup.nav.tabs.${name}`) }}
          </button>
        </div>
      </div>
    </nav>
    <div class="nav-select">
      <NSelect
        :value="tab"
        :options="options"
        :placeholder="t('adminSetup.nav.choose')"
        :aria-label="t('adminSetup.nav.choose')"
        @update:value="(v: Tab) => (tab = v)"
      />
    </div>

    <div class="admin-body" role="tabpanel">
      <component :is="views[tab]" :key="tab" v-bind="NAVIGATES.has(tab) ? { onNavigate } : {}" />
    </div>
  </div>
</template>

<style scoped>
.admin-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 22px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
}
.nav-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.caption {
  padding: 0 10px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
}
.nav-group.current .caption {
  color: var(--brand);
}
.items {
  display: flex;
  flex-wrap: wrap;
}
.nav-tab {
  position: relative;
  padding: 6px 10px 10px;
  border: none;
  background: none;
  color: var(--text-2);
  font: inherit;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
  border-radius: 6px 6px 0 0;
}
.nav-tab:hover {
  color: var(--text);
}
.nav-tab.active {
  color: var(--brand);
}
.nav-tab.active::after {
  content: '';
  position: absolute;
  left: 10px;
  right: 10px;
  bottom: -1px;
  height: 2px;
  border-radius: 2px;
  background: var(--brand);
}
.nav-tab:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: -2px;
}
.nav-select {
  display: none;
  margin-bottom: 16px;
}
@media (max-width: 760px) {
  .admin-nav {
    display: none;
  }
  .nav-select {
    display: block;
  }
}
</style>
