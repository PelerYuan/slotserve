<script setup lang="ts">
// The Overview's one "next step" slot (F01, D13). At most one card, by priority: My work > the setup checklist
// (admins, while required steps are open) > Start here (newcomers). The cards it pushes down fold into small
// links on the right. Admins' to-dos (bookings and sign-ups to approve, error checks) never take the slot: they
// are one line above it, in step with the bell.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { ArrowRight, Settings2, TriangleAlert } from 'lucide-vue-next'
import { session } from '@/composables/session'
import { useStatus } from '@/composables/useStatus'
import { availabilityOf } from '@/features/playground/api'
import SuggestionCard from '@/features/suggestions/components/SuggestionCard.vue'
import { offerSuggestion } from '@/features/suggestions/useSuggestions'
import { inbox } from '@/features/inbox/useInbox'
import { loadOnboarding, onboarding, onboardingApi, updateOnboarding, type Goal, type HealthCheck } from '../api'
import { loadWork, type WorkRow } from '../work'
import MyWork from './MyWork.vue'
import StartHere from './StartHere.vue'

const emit = defineEmits<{ playground: []; book: [intent: 'devenv' | 'model' | 'exclusive' | null] }>()
const { t } = useI18n()
const router = useRouter()
const { status } = useStatus()

type Card = 'work' | 'setup' | 'start'
const work = ref<WorkRow[]>([])
// Setup checklist (F06): the onboarding checks; these ones are optional (a feature map, not to-dos).
const OPTIONAL_SETUP = new Set(['chat', 'docker', 'backup'])
const setup = ref<HealthCheck[]>([])
const setupTotal = ref(0)
const pending = ref({ bookings: 0, signups: 0, errors: 0 })
const override = ref<Card | null>(null)
const isAdmin = computed(() => !!session.user?.is_admin)

async function loadAdmin() {
  if (!isAdmin.value) return
  const [health, bookings, signups] = await Promise.all([
    onboardingApi.health().catch(() => null),
    onboardingApi.pendingBookings().catch(() => []),
    onboardingApi.pendingSignups().catch(() => []),
  ])
  const checks = health?.checks ?? []
  const required = checks.filter((c) => c.onboarding && !OPTIONAL_SETUP.has(c.id))
  setup.value = required.filter((c) => c.level !== 'ok' && !c.dismissed)
  setupTotal.value = required.length
  pending.value = {
    bookings: bookings.length,
    signups: signups.length,
    errors: checks.filter((c) => c.level === 'error' && !c.dismissed && !c.onboarding).length,
  }
}

async function refresh() {
  const [rows] = await Promise.all([loadWork(), loadAdmin(), loadOnboarding(true)])
  work.value = rows
}

let timer: number | undefined
onMounted(() => {
  void refresh()
  timer = window.setInterval(() => document.visibilityState === 'visible' && refresh(), 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
// a new admin message (an approval request, an alert) changes the line above the slot
watch(
  () => inbox.unread.admin,
  (n, before) => n > before && loadAdmin(),
)

const showStart = computed(() => !!onboarding.data?.start)
const available = computed<Card[]>(() => [
  ...(work.value.length ? (['work'] as const) : []),
  ...(setup.value.length ? (['setup'] as const) : []),
  ...(showStart.value ? (['start'] as const) : []),
])
const card = computed<Card | null>(() =>
  override.value && available.value.includes(override.value) ? override.value : (available.value[0] ?? null),
)
const folded = computed(() => available.value.filter((c) => c !== card.value))

const callable = computed(() => {
  const now = availabilityOf(status.value).filter((m) => m.state === 'now')
  return now.find((m) => m.is_default && !m.loading) ?? now.find((m) => !m.loading) ?? now[0] ?? null
})

// "start here" done (all goals reached): one "you can also…" instead
const allDone = computed(() => {
  const d = onboarding.data?.done
  if (!d) return false
  return d.call && d.book && (d.env || !session.config?.devenv_enabled)
})
watch(allDone, (done) => done && offerSuggestion('start_done'), { immediate: true })

function onGoal(goal: Goal) {
  if (goal === 'call') emit('playground')
  else if (goal === 'env') void router.push({ name: 'devenv' })
  else emit('book', 'exclusive')
}

const actionParts = computed(() =>
  [
    pending.value.bookings && {
      key: 'bookings',
      text: t('onboarding.action.bookings', { n: pending.value.bookings }, pending.value.bookings),
      to: { name: 'admin', query: { tab: 'reservations' } },
    },
    pending.value.signups && {
      key: 'signups',
      text: t('onboarding.action.signups', { n: pending.value.signups }, pending.value.signups),
      to: { name: 'admin', query: { tab: 'users' } },
    },
    pending.value.errors && {
      key: 'errors',
      text: t('onboarding.action.errors', { n: pending.value.errors }, pending.value.errors),
      to: { name: 'admin' },
    },
  ].filter(Boolean) as { key: string; text: string; to: object }[],
)

function foldedLabel(c: Card) {
  if (c === 'work') return t('onboarding.also.work', { n: work.value.length })
  if (c === 'setup') return t('onboarding.also.setup', { n: setup.value.length })
  return t('onboarding.also.start')
}

defineExpose({ refresh })
</script>

<template>
  <div class="next-step" data-component="next-step" :data-slot="card ?? 'none'">
    <div v-if="actionParts.length" class="action-bar" data-field="needs-action">
      <TriangleAlert :size="15" />
      <span class="lead">{{ t('onboarding.action.lead') }}</span>
      <template v-for="(p, i) in actionParts" :key="p.key">
        <span v-if="i" class="sep">·</span>
        <RouterLink :to="p.to">{{ p.text }}</RouterLink>
      </template>
      <ArrowRight :size="14" />
    </div>

    <div v-if="card" class="slot">
      <section v-if="card === 'work'" class="card slot-card" data-card="work">
        <div class="slot-head">
          <div>
            <h3 class="card-title">{{ t('onboarding.work.title') }}</h3>
            <div class="card-sub">{{ t('onboarding.work.subtitle') }}</div>
          </div>
          <span class="spacer" />
          <RouterLink v-if="session.config?.devenv_enabled" :to="{ name: 'devenv' }" class="small">{{ t('onboarding.work.all') }}</RouterLink>
          <RouterLink :to="{ name: 'schedule' }" class="small">{{ t('onboarding.work.schedule') }}</RouterLink>
        </div>
        <MyWork :rows="work" @changed="refresh" />
      </section>

      <section v-else-if="card === 'setup'" class="card slot-card setup" data-card="setup">
        <span class="setup-icon"><Settings2 :size="18" /></span>
        <div class="setup-main">
          <h3 class="card-title">{{ t('onboarding.setup.title') }}</h3>
          <div class="card-sub">
            {{ t('onboarding.setup.progress', { done: setupTotal - setup.length, total: setupTotal }) }} ·
            {{ t('onboarding.setup.subtitle', { n: setup.length }, setup.length) }}
          </div>
          <ul class="setup-list">
            <li v-for="c in setup.slice(0, 4)" :key="c.id" :class="c.level">{{ c.title }}</li>
          </ul>
        </div>
        <RouterLink :to="{ name: 'admin', query: { tab: 'overview' } }">
          <NButton type="primary" secondary>{{ t('onboarding.setup.continue') }} →</NButton>
        </RouterLink>
      </section>

      <StartHere
        v-else-if="card === 'start' && onboarding.data"
        :state="onboarding.data"
        :callable="callable"
        @goal="onGoal"
        @dismiss="updateOnboarding({ dismiss_start: true })"
      />

      <div v-if="folded.length" class="folded">
        <button v-for="c in folded" :key="c" class="fold-link" :data-fold="c" @click="override = c">
          {{ foldedLabel(c) }}
        </button>
        <button v-if="override && override !== available[0]" class="fold-link" @click="override = null">↺</button>
      </div>
    </div>

    <SuggestionCard
      id="start_done"
      :text="t('suggestions.startDone')"
      :action="t('suggestions.openSettings')"
      @action="router.push({ name: 'settings' })"
    />
  </div>
</template>

<style scoped>
.next-step {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
}
.next-step:not(:has(> *)) {
  display: none;
}
.action-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 9px 14px;
  border-radius: 10px;
  background: var(--orange-soft);
  color: var(--orange-text);
  font-size: 13px;
}
.action-bar a {
  color: inherit;
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.lead {
  font-weight: 600;
}
.sep {
  opacity: 0.6;
}
.slot {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.slot-card {
  padding: 16px 20px;
}
.slot-head {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.slot-head .card-title {
  margin: 0;
}
.setup {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.setup-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  flex: none;
}
.setup-main {
  flex: 1;
  min-width: 220px;
}
.setup-main .card-title {
  margin: 0;
}
.setup-list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--text-2);
}
.setup-list li.error::marker {
  color: var(--danger);
}
.folded {
  display: flex;
  justify-content: flex-end;
  gap: 14px;
  flex-wrap: wrap;
}
.fold-link {
  border: none;
  background: none;
  padding: 0;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text-3);
  cursor: pointer;
}
.fold-link:hover {
  color: var(--brand);
}
</style>
