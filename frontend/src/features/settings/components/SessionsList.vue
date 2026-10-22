<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, useDialog } from 'naive-ui'
import { Laptop, Smartphone } from 'lucide-vue-next'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'
import { settingsApi, type SignInSession } from '../api'

// Settings → Signed-in devices: the browsers this account is signed in on; sign one or all others out.
const { t } = useI18n()
const router = useRouter()
const dialog = useDialog()
const { showError, message } = useErrorMessage()

const list = ref<SignInSession[]>([])
const loaded = ref(false)
const showAll = ref(false)
const SHOWN = 6
// this browser first, then the most recently active ones
const sorted = computed(() =>
  [...list.value].sort((a, b) => Number(b.current) - Number(a.current) || b.last_seen_at.localeCompare(a.last_seen_at)),
)
const shown = computed(() => (showAll.value ? sorted.value : sorted.value.slice(0, SHOWN)))

async function load() {
  try {
    list.value = await settingsApi.sessions()
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(load)

function describe(ua: string | null): { name: string; mobile: boolean } {
  if (!ua) return { name: t('settings.devices.unknown'), mobile: false }
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /OPR\//.test(ua)
      ? 'Opera'
      : /Firefox\//.test(ua)
        ? 'Firefox'
        : /Chrome\//.test(ua)
          ? 'Chrome'
          : /Safari\//.test(ua)
            ? 'Safari'
            : /curl|python|httpx/i.test(ua)
              ? ua.split(/[\s/]/)[0]
              : ''
  const os = /Windows/.test(ua)
    ? 'Windows'
    : /Android/.test(ua)
      ? 'Android'
      : /iPhone|iPad|iOS/.test(ua)
        ? 'iOS'
        : /Mac OS X|Macintosh/.test(ua)
          ? 'macOS'
          : /Linux/.test(ua)
            ? 'Linux'
            : ''
  const mobile = /Mobile|Android|iPhone|iPad/.test(ua)
  if (browser && os) return { name: t('settings.devices.browserOn', { browser, os }), mobile }
  return { name: browser || os || t('settings.devices.unknown'), mobile }
}

async function end(s: SignInSession) {
  try {
    await settingsApi.endSession(s.id)
    if (s.current) {
      session.user = null
      router.push({ name: 'login' })
      return
    }
    message.success(t('settings.devices.done'))
    await load()
  } catch (e) {
    showError(e)
  }
}

function endOthers() {
  dialog.warning({
    title: t('settings.devices.signOutOthers'),
    content: t('settings.devices.sub'),
    positiveText: t('settings.devices.signOutOthers'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        const { revoked } = await settingsApi.endOtherSessions()
        message.success(t('settings.devices.othersDone', { n: revoked }))
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <section class="card" data-testid="devices">
    <div class="card-header">
      <div>
        <h3 class="card-title">{{ t('settings.devices.title') }}</h3>
        <div class="card-sub">{{ t('settings.devices.sub') }}</div>
      </div>
      <NButton v-if="list.length > 1" size="small" secondary type="error" @click="endOthers">
        {{ t('settings.devices.signOutOthers') }}
      </NButton>
    </div>
    <ul class="devices">
      <li v-for="s in shown" :key="s.id" :class="{ current: s.current }" :data-current="s.current">
        <component :is="describe(s.user_agent).mobile ? Smartphone : Laptop" :size="18" class="icon" />
        <div class="info">
          <div class="name">
            {{ describe(s.user_agent).name }}
            <span v-if="s.current" class="badge ok">{{ t('settings.devices.current') }}</span>
          </div>
          <div class="faint small">
            <span v-if="s.ip" class="mono">{{ s.ip }}</span>
            <span>{{ t('settings.devices.lastSeen', { time: relativeFromNow(s.last_seen_at) }) }}</span>
            <span :title="formatDateTime(s.created_at)">{{ t('settings.devices.since', { time: formatDateTime(s.created_at) }) }}</span>
          </div>
        </div>
        <NButton size="small" quaternary :type="s.current ? 'default' : 'error'" @click="end(s)">{{ t('settings.devices.signOut') }}</NButton>
      </li>
    </ul>
    <div v-if="list.length > SHOWN" class="more">
      <NButton size="tiny" quaternary @click="showAll = !showAll">
        {{ showAll ? t('settings.devices.fewer') : t('settings.devices.all', { n: list.length }) }}
      </NButton>
    </div>
    <div v-if="loaded && list.length <= 1" class="faint small only">{{ t('settings.devices.onlyThis') }}</div>
  </section>
</template>

<style scoped>
.devices {
  list-style: none;
  margin: 0;
  padding: 0;
}
.devices li {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
.devices li:first-child {
  border-top: none;
}
.icon {
  flex: none;
  color: var(--text-3);
}
.current .icon {
  color: var(--ok);
}
.info {
  flex: 1;
  min-width: 0;
}
.name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.info .small {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
}
.more {
  padding: 6px 14px;
  border-top: 1px solid var(--border);
}
.only {
  padding: 0 20px 14px;
}
</style>
