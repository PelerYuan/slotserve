<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { Check, CircleAlert, CircleDashed, CircleX, ListChecks } from 'lucide-vue-next'
import type { HealthCheck } from '../api'
import { REQUIRED_SETUP, isDone } from '../checks'
import CheckActions from './CheckActions.vue'
import InviteDialog from './InviteDialog.vue'

// The setup checklist (F06): the onboarding checks, each ticking itself when it works, with its buttons. Collapses
// to one line once everything required is done.
const props = defineProps<{ checks: HealthCheck[] }>()
const emit = defineEmits<{ updated: [check: HealthCheck]; invited: [] }>()
const { t, te } = useI18n()

const STORE = 'slotserve.setupChecklist.open'
const items = computed(() => props.checks.filter((c) => c.onboarding))
const required = computed(() => items.value.filter((c) => REQUIRED_SETUP.has(c.id)))
const optional = computed(() => items.value.filter((c) => !REQUIRED_SETUP.has(c.id)))
const doneCount = computed(() => items.value.filter(isDone).length)
const requiredDone = computed(() => required.value.every(isDone))

function remembered(): boolean | null {
  try {
    const v = localStorage.getItem(STORE)
    return v === null ? null : v === '1'
  } catch {
    return null
  }
}
const choice = ref<boolean | null>(remembered())
const open = computed(() => choice.value ?? !requiredDone.value)
function setOpen(v: boolean) {
  choice.value = v
  try {
    localStorage.setItem(STORE, v ? '1' : '0')
  } catch {
    /* storage unavailable */
  }
}

const name = (c: HealthCheck) =>
  te(`adminSetup.checklist.items.${c.id}`) ? t(`adminSetup.checklist.items.${c.id}`) : c.title
const icon = (c: HealthCheck) =>
  isDone(c) ? Check : c.level === 'error' ? CircleX : c.level === 'warn' ? CircleAlert : CircleDashed

const inviteOpen = ref(false)
</script>

<template>
  <section v-if="items.length" class="card checklist" data-testid="setup-checklist">
    <div class="head">
      <ListChecks :size="18" class="head-icon" />
      <div class="head-text">
        <h3 class="card-title">
          {{ requiredDone && !open ? t('adminSetup.checklist.complete') : t('adminSetup.checklist.title') }}
          <Check v-if="requiredDone && !open" :size="16" class="ok-mark" />
        </h3>
        <div v-if="open" class="card-sub">{{ t('adminSetup.checklist.sub') }}</div>
      </div>
      <span class="progress-text num" data-testid="setup-progress">
        {{ t('adminSetup.checklist.progress', { done: doneCount, total: items.length }) }}
      </span>
      <NButton size="small" quaternary @click="setOpen(!open)">
        {{ open ? t('adminSetup.checklist.hide') : t('adminSetup.checklist.showAgain') }}
      </NButton>
    </div>
    <div v-if="open" class="bar"><span :style="{ width: `${(100 * doneCount) / items.length}%` }" /></div>
    <template v-if="open">
      <ul class="items">
        <li v-for="c in required" :key="c.id" :class="['item', c.level, { done: isDone(c) }]" :data-check="c.id">
          <component :is="icon(c)" :size="18" class="state" />
          <div class="text">
            <div class="name">{{ name(c) }}</div>
            <div class="title">{{ c.dismissed ? t('adminSetup.health.dismissedNoName', { reason: c.dismissed.reason }) : c.title }}</div>
            <pre v-if="!isDone(c) && c.evidence" class="evidence">{{ c.evidence }}</pre>
          </div>
          <CheckActions v-if="!isDone(c) || c.dismissed" :check="c" :name="name(c)" size="small" @updated="(x) => emit('updated', x)">
            <NButton v-if="c.id === 'invite'" size="small" type="primary" secondary @click="inviteOpen = true">
              {{ t('adminSetup.checklist.createInvite') }}
            </NButton>
          </CheckActions>
          <NButton v-else-if="c.id === 'invite'" size="small" quaternary @click="inviteOpen = true">
            {{ t('adminSetup.checklist.createInvite') }}
          </NButton>
        </li>
      </ul>
      <template v-if="optional.length">
        <div class="optional-title">{{ t('adminSetup.checklist.optional') }}</div>
        <ul class="items">
          <li v-for="c in optional" :key="c.id" :class="['item', c.level, { done: isDone(c) }]" :data-check="c.id">
            <component :is="icon(c)" :size="18" class="state" />
            <div class="text">
              <div class="name">{{ name(c) }}</div>
              <div class="title">{{ c.dismissed ? t('adminSetup.health.dismissedNoName', { reason: c.dismissed.reason }) : c.title }}</div>
            </div>
            <CheckActions v-if="!isDone(c) || c.dismissed" :check="c" :name="name(c)" size="small" @updated="(x) => emit('updated', x)" />
          </li>
        </ul>
      </template>
    </template>
    <InviteDialog v-model:show="inviteOpen" @created="emit('invited')" />
  </section>
</template>

<style scoped>
.checklist {
  margin-bottom: 16px;
}
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 20px;
}
.head-icon {
  color: var(--brand);
  flex: none;
}
.head-text {
  flex: 1;
  min-width: 0;
}
.head .card-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ok-mark {
  color: var(--ok);
}
.progress-text {
  color: var(--text-2);
  font-size: 13px;
  white-space: nowrap;
}
.bar {
  height: 4px;
  margin: 0 20px 6px;
  border-radius: 4px;
  background: var(--track);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  background: var(--ok);
  transition: width 0.4s;
}
.items {
  list-style: none;
  margin: 0;
  padding: 0;
}
.item {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  gap: 12px;
  align-items: start;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
.state {
  margin-top: 2px;
  color: var(--text-3);
}
.item.error .state {
  color: var(--danger);
}
.item.warn .state {
  color: var(--warn);
}
.item.done .state {
  color: var(--ok);
}
.name {
  font-weight: 600;
}
.item.done .name {
  color: var(--text-2);
}
.text {
  min-width: 0;
}
.title {
  color: var(--text-2);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.evidence {
  margin: 6px 0 0;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--code-bg);
  color: var(--code-text);
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}
.optional-title {
  padding: 10px 20px 6px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-3);
  background: var(--surface-muted);
}
.optional-title + .items .item:first-child {
  border-top: none;
}
@media (max-width: 760px) {
  .head {
    flex-wrap: wrap;
  }
  .item {
    grid-template-columns: 22px minmax(0, 1fr);
  }
  .item > :last-child:not(.text) {
    grid-column: 2;
  }
}
</style>
