<script setup lang="ts">
// The inbox (F01): members see their own events; admins also the admin ones (alerts, approvals, other to-dos).
// Opening a message marks it read and follows its link.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NDrawer, NDrawerContent, NSwitch, NTab, NTabs } from 'naive-ui'
import { BellOff, CircleAlert, CircleCheck, Info, TriangleAlert } from 'lucide-vue-next'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'
import { inboxApi, type InboxCategory, type InboxItem } from '../api'
import { inbox, refreshUnread } from '../useInbox'

const { t } = useI18n()
const router = useRouter()
const { showError } = useErrorMessage()

const items = ref<InboxItem[]>([])
const hasMore = ref(false)
const loading = ref(false)
const unreadOnly = ref(false)
const tab = computed<'all' | InboxCategory>({
  get: () => inbox.category ?? 'all',
  set: (v) => (inbox.category = v === 'all' ? null : v),
})
const isAdmin = computed(() => !!session.user?.is_admin)

const narrow = ref(false)
let media: MediaQueryList | null = null
const onMedia = () => (narrow.value = !!media?.matches)
onMounted(() => {
  media = window.matchMedia('(max-width: 640px)')
  onMedia()
  media.addEventListener('change', onMedia)
})
onBeforeUnmount(() => media?.removeEventListener('change', onMedia))

async function load(more = false) {
  loading.value = true
  try {
    const page = await inboxApi.list({
      unread: unreadOnly.value,
      category: inbox.category,
      beforeId: more ? items.value[items.value.length - 1]?.id : null,
      limit: 30,
    })
    items.value = more ? [...items.value, ...page.items] : page.items
    hasMore.value = page.has_more
    inbox.unread = page.unread
  } catch (e) {
    showError(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => inbox.open,
  (open) => open && load(),
)
watch([() => inbox.category, unreadOnly], () => inbox.open && load())

async function markAll() {
  try {
    inbox.unread = await inboxApi.read(inbox.category ? { all: true, category: inbox.category } : { all: true })
    const now = new Date().toISOString()
    items.value = unreadOnly.value ? [] : items.value.map((i) => ({ ...i, read_at: i.read_at ?? now }))
  } catch (e) {
    showError(e)
  }
}

async function openItem(item: InboxItem) {
  if (!item.read_at) {
    item.read_at = new Date().toISOString()
    inboxApi
      .read({ ids: [item.id] })
      .then((counts) => (inbox.unread = counts))
      .catch(() => refreshUnread())
  }
  if (item.link && item.link.startsWith('/')) {
    inbox.open = false
    await router.push(item.link)
  }
}

const icons = { info: Info, success: CircleCheck, warn: TriangleAlert, error: CircleAlert }
</script>

<template>
  <NDrawer v-model:show="inbox.open" placement="right" :width="narrow ? '100%' : 440" :auto-focus="false">
    <NDrawerContent closable :native-scrollbar="false">
      <template #header>
        <span>{{ t('inbox.title') }}</span>
      </template>
      <div class="inbox" data-component="inbox">
        <NTabs v-if="isAdmin" v-model:value="tab" type="segment" size="small">
          <NTab name="all">
            {{ t('inbox.all') }}<span v-if="inbox.unread.total" class="n">{{ inbox.unread.total }}</span>
          </NTab>
          <NTab name="personal">
            {{ t('inbox.personal') }}<span v-if="inbox.unread.personal" class="n">{{ inbox.unread.personal }}</span>
          </NTab>
          <NTab name="admin">
            {{ t('inbox.admin') }}<span v-if="inbox.unread.admin" class="n alert">{{ inbox.unread.admin }}</span>
          </NTab>
        </NTabs>
        <div class="tools">
          <label class="unread-only">
            <NSwitch v-model:value="unreadOnly" size="small" />
            <span>{{ t('inbox.unreadOnly') }}</span>
          </label>
          <span class="spacer" />
          <NButton size="small" quaternary :disabled="!inbox.unread.total" data-action="inbox-read-all" @click="markAll">
            {{ t('inbox.markAll') }}
          </NButton>
        </div>

        <div v-if="!items.length && !loading" class="empty-state">
          <BellOff :size="26" :stroke-width="1.5" />
          <div>{{ unreadOnly ? t('inbox.emptyUnread') : t('inbox.empty') }}</div>
          <div class="faint small">{{ isAdmin ? t('inbox.emptyHintAdmin') : t('inbox.emptyHint') }}</div>
        </div>
        <ul v-else class="list">
          <li v-for="item in items" :key="item.id">
            <button
              class="item"
              :class="[item.level, { unread: !item.read_at, linked: !!item.link }]"
              :data-event="item.event"
              @click="openItem(item)"
            >
              <component :is="icons[item.level] ?? Info" :size="16" class="lvl" />
              <span class="body">
                <span class="msg">{{ item.message }}</span>
                <span class="meta faint">
                  <span :title="formatDateTime(item.created_at)">{{ relativeFromNow(item.created_at) }}</span>
                  <span v-if="isAdmin && item.category === 'admin'" class="tag">{{ t('inbox.admin') }}</span>
                </span>
              </span>
              <span v-if="!item.read_at" class="dot" :aria-label="t('inbox.unread')" />
            </button>
          </li>
        </ul>
        <NButton v-if="hasMore" block quaternary :loading="loading" @click="load(true)">{{ t('inbox.more') }}</NButton>

        <RouterLink :to="{ name: 'settings' }" class="settings small" @click="inbox.open = false">
          {{ t('inbox.settings') }}
        </RouterLink>
      </div>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.inbox {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.n {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 700;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand);
}
.n.alert {
  background: var(--danger-soft);
  color: var(--danger-text);
}
.tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.unread-only {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 12px;
  text-align: center;
  color: var(--text-2);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.item {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  width: 100%;
  text-align: left;
  border: none;
  background: none;
  padding: 10px;
  border-radius: 9px;
  font-family: inherit;
  color: var(--text-2);
  cursor: default;
}
.item.linked {
  cursor: pointer;
}
.item:hover {
  background: var(--hover);
}
.item.unread {
  color: var(--text);
  background: var(--brand-softer);
}
.item.unread:hover {
  background: var(--brand-soft);
}
.lvl {
  flex: none;
  margin-top: 2px;
  color: var(--text-3);
}
.success .lvl {
  color: var(--ok-text);
}
.warn .lvl {
  color: var(--warn-text);
}
.error .lvl {
  color: var(--danger);
}
.body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}
.msg {
  font-size: 13.5px;
  line-height: 1.5;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
.meta {
  display: flex;
  gap: 8px;
  font-size: 12px;
}
.tag {
  font-weight: 600;
  color: var(--danger-text);
}
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  margin-top: 6px;
  border-radius: 50%;
  background: var(--brand);
}
.settings {
  align-self: center;
  margin-top: 4px;
}
</style>
