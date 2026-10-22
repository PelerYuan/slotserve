<script setup lang="ts">
// Files and storage of dev environments (F11): where /workspace lives and how full it is, the kept directories,
// the shared storages, and why one cannot be reached (members: a grey "not available" with what it means; admins
// also see the error). Used on each environment card (compact) and as a page section.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NTooltip } from 'naive-ui'
import { HardDrive } from 'lucide-vue-next'
import { storageApi, type MyStorage, type Storage } from '@/api/storage'
import type { DevProject } from '@/api/types'
import { session } from '@/composables/session'
import { formatBytes } from '@/utils/format'

const props = defineProps<{
  /** the kept environment of the card (its kept directories), if any */
  project?: DevProject | null
  /** on a card: no heading, no outer card */
  compact?: boolean
  /** this environment is kept (paused between bookings) */
  kept?: boolean
}>()

const { t, te } = useI18n()
const isAdmin = computed(() => !!session.user?.is_admin)

// One request for all cards of the page (refreshed at most every 30 s).
let cache: { at: number; mine: Promise<MyStorage[]>; admin: Promise<Storage[]> | null } | null = null
function fetchAll() {
  if (!cache || Date.now() - cache.at > 30_000) {
    cache = {
      at: Date.now(),
      mine: storageApi.mine().catch(() => []),
      admin: isAdmin.value ? storageApi.list().catch(() => []) : null,
    }
  }
  return cache
}

const rows = ref<MyStorage[]>([])
const adminRows = ref<Storage[]>([])
const loaded = ref(false)
onMounted(async () => {
  const c = fetchAll()
  rows.value = await c.mine
  adminRows.value = c.admin ? await c.admin : []
  loaded.value = true
})

const personal = computed(() => rows.value.find((s) => s.purpose === 'personal') ?? null)
const mounts = computed(() => rows.value.filter((s) => s.purpose !== 'personal' && s.path))
const usePct = computed(() => {
  const p = personal.value
  if (!p || p.used == null || !p.quota_gb) return null
  return Math.min(100, (p.used / (p.quota_gb * 1e9)) * 100)
})
function adminError(id: number): string | null {
  const err = adminRows.value.find((s) => s.id === id)?.status?.error ?? null
  if (!err) return null
  return /^[a-z_]+$/.test(err) && te(`errors.${err}`) ? t(`errors.${err}`) : err
}
function unreachable(s: MyStorage) {
  return s.ok === false
}
</script>

<template>
  <section v-if="loaded" class="storage" :class="{ compact, block: !compact }" data-test="devenv-storage">
    <h2 v-if="!compact" class="block-title row"><HardDrive :size="16" /> {{ t('devenvUi.storage.title') }}</h2>
    <div :class="{ card: !compact, 'card-pad': !compact }" class="list">
      <!-- /workspace -->
      <div class="item" data-path="/workspace">
        <div class="row wrap line">
          <span class="mono strong">/workspace</span>
          <span class="muted small">
            {{ personal ? t('devenvUi.storage.workspaceOn', { name: personal.name }) : t('devenvUi.storage.workspaceVolume') }}
          </span>
          <NTooltip v-if="personal && unreachable(personal)">
            <template #trigger>
              <span class="badge">{{ t('devenvUi.storage.unavailable') }}</span>
            </template>
            {{ t('devenvUi.storage.workspaceUnavailable') }}
          </NTooltip>
        </div>
        <div v-if="personal && personal.used != null" class="usage">
          <div v-if="usePct !== null" class="bar" :class="{ over: personal.over }">
            <span :style="{ width: `${usePct}%` }" />
          </div>
          <span class="small num" :class="{ 'over-text': personal.over }">
            {{ formatBytes(personal.used) }}<template v-if="personal.quota_gb"> / {{ personal.quota_gb }} GB</template>
          </span>
          <span v-if="personal.over" class="small over-text">{{ t('devenvUi.storage.over') }}</span>
        </div>
        <p class="faint small">{{ t('devenvUi.storage.workspaceKept') }}</p>
      </div>

      <!-- kept directories -->
      <div v-if="project && project.persist_paths.length" class="item">
        <div class="row wrap line">
          <span class="mono strong">{{ project.persist_paths.join('  ') }}</span>
          <span class="muted small">{{ t('devenvUi.storage.keptDirs') }}</span>
        </div>
      </div>

      <!-- shared storages -->
      <div v-for="s in mounts" :key="s.id" class="item" :data-path="s.path">
        <div class="row wrap line">
          <span class="mono strong">{{ s.path }}</span>
          <span class="muted small">{{ s.name }} · {{ t(`storage.purpose.${s.purpose}`) }}</span>
          <span v-if="s.read_only" class="badge">{{ t('storage.ro') }}</span>
          <NTooltip v-if="unreachable(s)">
            <template #trigger>
              <span class="badge" data-test="storage-unavailable">{{ t('devenvUi.storage.unavailable') }}</span>
            </template>
            {{ t('devenvUi.storage.unavailableTip', { path: s.path }) }}
          </NTooltip>
        </div>
        <p v-if="unreachable(s)" class="faint small">
          {{ t('devenvUi.storage.unavailableWhy', { path: s.path }) }}
          <template v-if="isAdmin">
            <span v-if="adminError(s.id)" class="mono"> {{ adminError(s.id) }}</span> ·
            <RouterLink :to="{ name: 'admin', query: { tab: 'storage' } }">{{ t('devenvUi.storage.fix') }}</RouterLink>
          </template>
        </p>
      </div>

      <p class="faint small foot">
        {{ kept ? t('devenvUi.storage.restKept') : t('devenvUi.storage.restGone') }}
        {{ t('devenvUi.storage.backup') }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.block {
  margin-bottom: 28px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
  gap: 6px;
  margin-bottom: 12px;
}
.list {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.item {
  display: grid;
  gap: 3px;
  min-width: 0;
}
.line {
  gap: 8px;
  align-items: center;
}
.mono.strong {
  overflow-wrap: anywhere;
}
p {
  margin: 0;
}
.usage {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.bar {
  flex: 1 1 140px;
  max-width: 260px;
  height: 6px;
  border-radius: 999px;
  background: var(--surface-muted);
  border: 1px solid var(--border);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  background: var(--brand);
}
.bar.over span {
  background: var(--warn);
}
.over-text {
  color: var(--warn-text);
  font-weight: 600;
}
.foot {
  margin-top: 2px;
}
</style>
