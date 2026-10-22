<script setup lang="ts">
// "Start here" (F01): three goals for newcomers, each saying whether it needs a booking and how soon it works.
// Ticks come from real activity (a successful call, an environment that started, a booking), not from clicks.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { CalendarPlus, CircleCheck, Circle, MessageSquare, Terminal } from 'lucide-vue-next'
import { session } from '@/composables/session'
import type { ModelAvailability } from '@/features/playground/api'
import type { Goal, Onboarding } from '../api'

const props = defineProps<{ state: Onboarding; callable: ModelAvailability | null }>()
const emit = defineEmits<{ goal: [goal: Goal]; dismiss: [] }>()
const { t } = useI18n()

const goals = computed(() => {
  const list: { id: Goal; icon: unknown; title: string; desc: string; hint: string; action: string }[] = [
    {
      id: 'call',
      icon: MessageSquare,
      title: t('onboarding.start.call.title'),
      desc: props.callable
        ? t('onboarding.start.call.nowModel', { model: props.callable.name })
        : t('onboarding.start.call.later'),
      hint: props.callable ? t('onboarding.start.call.now') : '',
      action: t('onboarding.start.call.action'),
    },
  ]
  if (session.config?.devenv_enabled)
    list.push({
      id: 'env',
      icon: Terminal,
      title: t('onboarding.start.env.title'),
      desc: t('onboarding.start.env.desc'),
      hint: t('onboarding.start.env.hint'),
      action: t('onboarding.start.env.action'),
    })
  list.push({
    id: 'book',
    icon: CalendarPlus,
    title: t('onboarding.start.book.title'),
    desc: t('onboarding.start.book.desc'),
    hint: t('onboarding.start.book.hint'),
    action: t('onboarding.start.book.action'),
  })
  return list
})
const doneCount = computed(() => goals.value.filter((g) => props.state.done[g.id]).length)
</script>

<template>
  <section class="card start" data-card="start">
    <div class="start-head">
      <h3 class="card-title">{{ t('onboarding.start.title', { name: session.user?.username }) }}</h3>
      <span class="faint small">{{ t('onboarding.start.progress', { done: doneCount, total: goals.length }) }}</span>
      <span class="spacer" />
      <NButton size="small" quaternary data-action="start-dismiss" @click="emit('dismiss')">
        {{ t('onboarding.start.dismiss') }}
      </NButton>
    </div>
    <div class="goals" :style="{ '--n': goals.length }">
      <div v-for="g in goals" :key="g.id" class="goal" :class="{ done: state.done[g.id] }" :data-goal="g.id">
        <div class="goal-top">
          <span class="goal-icon"><component :is="g.icon" :size="17" /></span>
          <span class="goal-title">{{ g.title }}</span>
          <span class="spacer" />
          <CircleCheck v-if="state.done[g.id]" :size="17" class="tick" :aria-label="t('onboarding.start.done')" />
          <Circle v-else :size="17" class="faint" />
        </div>
        <div class="goal-desc">{{ g.desc }}</div>
        <div v-if="g.hint" class="faint small">{{ g.hint }}</div>
        <NButton
          size="small"
          :type="state.done[g.id] ? 'default' : 'primary'"
          :secondary="!state.done[g.id]"
          :quaternary="state.done[g.id]"
          class="goal-action"
          :data-action="`start-${g.id}`"
          @click="emit('goal', g.id)"
        >
          {{ g.action }}
        </NButton>
      </div>
    </div>
  </section>
</template>

<style scoped>
.start {
  padding: 16px 20px 20px;
}
.start-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.start-head .card-title {
  margin: 0;
}
.goals {
  display: grid;
  grid-template-columns: repeat(var(--n), minmax(0, 1fr));
  gap: 12px;
}
.goal {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface-muted);
}
.goal.done {
  background: var(--ok-soft);
  border-color: transparent;
}
.goal-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.goal-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  flex: none;
}
.goal-title {
  font-weight: 650;
}
.goal-desc {
  font-size: 13px;
}
.tick {
  color: var(--ok-text);
}
.goal-action {
  margin-top: auto;
  align-self: flex-start;
}
.goal .small {
  margin-bottom: 10px;
}
@media (max-width: 900px) {
  .goals {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
