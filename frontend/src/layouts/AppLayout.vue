<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NDrawer, NDropdown } from 'naive-ui'
import {
  BarChart3,
  Boxes,
  CalendarDays,
  ChevronsUpDown,
  KeyRound,
  LayoutDashboard,
  Menu,
  Settings,
} from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import StatusDot from '@/components/StatusDot.vue'
import { logout, session } from '@/composables/session'
import { useStatus } from '@/composables/useStatus'
import { setLocale, type Locale } from '@/i18n'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const { status } = useStatus()
const drawer = ref(false)

const nav = computed(() => [
  { name: 'overview', label: t('nav.overview'), icon: LayoutDashboard },
  { name: 'schedule', label: t('nav.schedule'), icon: CalendarDays },
  { name: 'models', label: t('nav.models'), icon: Boxes },
  { name: 'keys', label: t('nav.keys'), icon: KeyRound },
  { name: 'usage', label: t('nav.usage'), icon: BarChart3 },
  { name: 'settings', label: t('nav.settings'), icon: Settings },
])

watch(() => route.fullPath, () => (drawer.value = false))

const userMenu = computed(() => [
  { key: 'zh-CN', label: '简体中文', disabled: locale.value === 'zh-CN' },
  { key: 'en', label: 'English', disabled: locale.value === 'en' },
  { type: 'divider', key: 'd1' },
  { key: 'logout', label: t('auth.logout') },
])

async function onUserMenu(key: string) {
  if (key === 'logout') {
    await logout()
    router.push({ name: 'login' })
  } else {
    setLocale(key as Locale)
  }
}

const runtime = computed(() => status.value?.runtime)
const holder = computed(() => status.value?.active_reservation)
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="side-inner">
        <RouterLink to="/" class="brand-link"><BrandMark /></RouterLink>

        <nav class="nav">
          <RouterLink
            v-for="item in nav"
            :key="item.name"
            :to="{ name: item.name }"
            class="nav-item"
            :class="{ active: route.name === item.name }"
          >
            <component :is="item.icon" :size="17" :stroke-width="1.8" />
            <span>{{ item.label }}</span>
          </RouterLink>
        </nav>

        <div class="spacer" />

        <RouterLink v-if="runtime" to="/" class="gpu-mini">
          <div class="row">
            <span class="label">GPU</span>
            <span class="spacer" />
            <StatusDot :state="runtime.state" />
          </div>
          <div class="gpu-model" :title="runtime.model_name ?? ''">{{ runtime.model_name ?? t('overview.noModel') }}</div>
          <div class="gpu-holder faint">
            <template v-if="holder">{{
              holder.user_id === session.user?.id ? t('overview.reservedByYou') : t('overview.reservedBy', { user: holder.username })
            }}</template>
            <template v-else>{{ t('overview.freeAccess') }}</template>
          </div>
        </RouterLink>

        <NDropdown trigger="click" :options="userMenu" placement="top-start" @select="onUserMenu">
          <button class="user">
            <span class="avatar">{{ session.user?.username.slice(0, 1).toUpperCase() }}</span>
            <span class="user-meta">
              <span class="user-name">{{ session.user?.username }}</span>
              <span class="faint user-role">{{ session.user?.is_admin ? t('common.admin') : t('settings.member') }}</span>
            </span>
            <ChevronsUpDown :size="15" class="faint" />
          </button>
        </NDropdown>
      </div>
    </aside>

    <header class="topbar">
      <button class="icon-btn" aria-label="Menu" @click="drawer = true"><Menu :size="20" /></button>
      <BrandMark :size="24" />
      <span class="spacer" />
      <StatusDot v-if="runtime" :state="runtime.state" />
    </header>

    <NDrawer v-model:show="drawer" placement="left" :width="260">
      <div class="drawer">
        <BrandMark />
        <nav class="nav">
          <RouterLink
            v-for="item in nav"
            :key="item.name"
            :to="{ name: item.name }"
            class="nav-item"
            :class="{ active: route.name === item.name }"
          >
            <component :is="item.icon" :size="17" :stroke-width="1.8" />
            <span>{{ item.label }}</span>
          </RouterLink>
        </nav>
        <button class="nav-item" @click="onUserMenu(locale === 'zh-CN' ? 'en' : 'zh-CN')">
          {{ locale === 'zh-CN' ? 'English' : '简体中文' }}
        </button>
        <button class="nav-item" @click="onUserMenu('logout')">{{ t('auth.logout') }}</button>
      </div>
    </NDrawer>

    <main class="main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100%;
}
.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  width: var(--sidebar);
  background: var(--surface);
  border-right: 1px solid var(--border);
  z-index: 10;
}
.side-inner {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 20px 14px 14px;
}
.brand-link {
  padding: 2px 8px 20px;
}
.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--text-2);
  font-weight: 500;
  font-size: 13.5px;
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
  font-family: inherit;
  transition: background 0.12s, color 0.12s;
}
.nav-item:hover {
  background: #f3f3f6;
  color: var(--text);
}
.nav-item.active {
  background: var(--brand-soft);
  color: var(--brand);
}
.gpu-mini {
  display: block;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-muted);
  color: var(--text);
  margin-bottom: 10px;
}
.gpu-mini:hover {
  border-color: var(--border-strong);
  color: var(--text);
}
.gpu-model {
  margin-top: 8px;
  font-weight: 600;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.gpu-holder {
  font-size: 12px;
  margin-top: 1px;
}
.user {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px;
  border: none;
  background: none;
  border-radius: 10px;
  cursor: pointer;
  font-family: inherit;
  text-align: left;
}
.user:hover {
  background: #f3f3f6;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #6366f1, #4338ca);
  color: #fff;
  font-weight: 600;
  font-size: 13px;
  flex: none;
}
.user-meta {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  line-height: 1.3;
}
.user-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
}
.user-role {
  font-size: 12px;
}
.main {
  margin-left: var(--sidebar);
  min-height: 100vh;
}
.topbar {
  display: none;
}
.icon-btn {
  border: none;
  background: none;
  padding: 6px;
  margin-left: -6px;
  border-radius: 8px;
  color: var(--text);
  cursor: pointer;
  display: grid;
  place-items: center;
}
.drawer {
  padding: 20px 14px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

@media (max-width: 900px) {
  .sidebar {
    display: none;
  }
  .main {
    margin-left: 0;
  }
  .topbar {
    display: flex;
    align-items: center;
    gap: 10px;
    position: sticky;
    top: 0;
    z-index: 10;
    height: 56px;
    padding: 0 16px;
    background: rgba(255, 255, 255, 0.9);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
  }
}
</style>
