<script setup lang="ts">
// One "you can also…" line with an optional action; renders only while its suggestion is the one on screen.
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { Lightbulb, X } from 'lucide-vue-next'
import { useSuggestion, type SuggestionId } from '../useSuggestions'

const props = defineProps<{ id: SuggestionId; text: string; action?: string }>()
const emit = defineEmits<{ action: [] }>()
const { t } = useI18n()
const { visible, close } = useSuggestion(props.id)

function act() {
  emit('action')
  close()
}
</script>

<template>
  <Transition name="suggest">
    <div v-if="visible" class="suggestion" role="status" :data-suggestion="id">
      <Lightbulb :size="16" class="icon" />
      <span class="text">
        <span class="lead">{{ t('suggestions.lead') }}</span>
        {{ text }}
      </span>
      <NButton v-if="action" size="small" type="primary" secondary @click="act">{{ action }}</NButton>
      <button class="close" :aria-label="t('suggestions.close')" :title="t('suggestions.close')" @click="close">
        <X :size="15" />
      </button>
    </div>
  </Transition>
</template>

<style scoped>
.suggestion {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 10px 10px 14px;
  border-radius: 10px;
  background: var(--brand-softer);
  border: 1px solid var(--brand-soft);
  font-size: 13px;
}
.icon {
  flex: none;
  color: var(--brand);
}
.text {
  flex: 1;
  min-width: 0;
}
.lead {
  font-weight: 600;
  color: var(--brand);
  margin-right: 4px;
}
.close {
  flex: none;
  border: none;
  background: none;
  padding: 4px;
  border-radius: 6px;
  color: var(--text-3);
  cursor: pointer;
  display: grid;
  place-items: center;
}
.close:hover {
  background: var(--hover);
  color: var(--text);
}
.suggest-enter-active,
.suggest-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}
.suggest-enter-from,
.suggest-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
@media (max-width: 640px) {
  .suggestion {
    flex-wrap: wrap;
  }
}
</style>
