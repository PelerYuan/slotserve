<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { NInput, NInputNumber, NSelect, NSwitch, NTooltip } from 'naive-ui'
import { History, RotateCcw, TriangleAlert } from 'lucide-vue-next'
import type { SettingValue } from '@/api/types'
import type { Setting } from '../api'
import { MULTI, splitList, useSettingText } from '../text'

// One setting: its label with badges (changed, root on the host, tier), the control, and in the full view the
// default, the variable name, "use default" and the history.
// `value` is typed loosely on purpose: a prop that may be a boolean turns "" into true (Vue's boolean casting).
const props = withDefaults(
  defineProps<{
    setting: Setting
    value: unknown
    compact?: boolean
    dirty?: boolean
    highlight?: boolean
    inactive?: string // label of the setting that has to be on first
  }>(),
  { compact: false, dirty: false, highlight: false, inactive: '' },
)
const emit = defineEmits<{ 'update:value': [v: SettingValue | null]; reset: []; history: [] }>()
const { t } = useI18n()
const { label, desc, unit, display, choiceOptions, multiOptions } = useSettingText()

const s = computed(() => props.setting)
const value = computed(() => props.value as SettingValue | null)
const stacked = computed(() => props.compact && s.value.kind === 'str' && !s.value.choices && !MULTI[s.value.key])
const placeholder = computed(() =>
  s.value.secret
    ? s.value.has_value
      ? t('settingsUi.secretSet')
      : t('settingsUi.secretUnset')
    : display(s.value, s.value.default),
)
const set = (v: SettingValue | null) => emit('update:value', v)
</script>

<template>
  <div
    class="setting"
    :class="{ compact, stacked, dirty, highlight, expert: setting.tier === 'expert' }"
    :data-setting="setting.key"
    :id="`setting-${setting.key}`"
  >
    <div class="text">
      <div class="title">
        <span class="name" :title="compact ? desc(setting) : undefined">{{ label(setting) }}</span>
        <span v-if="dirty" class="dot" :title="t('settingsUi.unsaved', { n: 1 })" />
        <span v-if="setting.overridden && !compact" class="badge brand">{{ t('settingsUi.badges.changed') }}</span>
        <NTooltip v-if="setting.danger" trigger="hover">
          <template #trigger>
            <span class="badge danger" data-danger>
              <TriangleAlert :size="12" :stroke-width="2.2" />{{ compact ? '' : t('settingsUi.badges.danger') }}
            </span>
          </template>
          {{ t('settingsUi.badges.dangerHint') }}
        </NTooltip>
        <span v-if="setting.tier === 'expert' && !compact" class="badge">{{ t('settingsUi.badges.expert') }}</span>
        <a
          v-if="compact && setting.overridden"
          href="#"
          class="reset-icon"
          :title="t('settingsUi.useDefault')"
          :aria-label="t('settingsUi.useDefault')"
          @click.prevent="emit('reset')"
        >
          <RotateCcw :size="12" />
        </a>
      </div>
      <div v-if="!compact && desc(setting)" class="muted desc">{{ desc(setting) }}</div>
      <div v-if="inactive" class="inactive">{{ t('settingsUi.dependsOn', { name: inactive }) }}</div>
      <div v-if="!compact" class="faint meta">
        <span>{{ t('settingsUi.default', { value: display(setting, setting.default) }) }}</span>
        <code class="env" :title="t('settingsUi.variable')">{{ setting.env_var }}</code>
        <a v-if="setting.overridden" href="#" @click.prevent="emit('reset')">{{ t('settingsUi.useDefault') }}</a>
        <a href="#" class="hist" @click.prevent="emit('history')"><History :size="12" />{{ t('settingsUi.history') }}</a>
      </div>
    </div>
    <div class="control">
      <NSelect
        v-if="MULTI[setting.key]"
        multiple
        :value="splitList(value)"
        :options="multiOptions(setting.key)"
        :placeholder="placeholder"
        @update:value="(v: string[]) => set(v.join(','))"
      />
      <NSwitch v-else-if="setting.kind === 'bool'" :value="value as boolean" @update:value="set" />
      <NSelect
        v-else-if="setting.choices"
        :value="value as number | string"
        :options="choiceOptions(setting)"
        :placeholder="placeholder"
        class="num-input"
        @update:value="set"
      />
      <NInputNumber
        v-else-if="setting.kind === 'int' || setting.kind === 'float'"
        :value="value as number"
        :min="setting.min ?? undefined"
        :max="setting.max ?? undefined"
        :precision="setting.kind === 'int' ? 0 : undefined"
        :step="setting.kind === 'float' ? 0.5 : 1"
        :placeholder="placeholder"
        class="num-input"
        @update:value="set"
      >
        <template v-if="setting.unit" #suffix>{{ unit(setting) }}</template>
      </NInputNumber>
      <NInput
        v-else-if="setting.secret"
        :value="value as string"
        type="password"
        show-password-on="click"
        :placeholder="placeholder"
        :input-props="{ autocomplete: 'new-password' }"
        @update:value="set"
      />
      <NInput v-else :value="value as string" class="mono-input" :placeholder="placeholder" @update:value="set" />
    </div>
  </div>
</template>

<style scoped>
.setting {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 24px;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  transition: background 0.3s;
}
.setting.highlight {
  background: var(--brand-softer);
}
.setting.compact {
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  padding: 9px 16px;
}
.setting.compact .control {
  max-width: 180px;
}
.setting.stacked {
  grid-template-columns: 1fr;
  gap: 6px;
}
.setting.stacked .control {
  max-width: none;
  justify-content: stretch;
}
.setting.stacked .control > * {
  width: 100%;
}
.title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-weight: 600;
}
.compact .title {
  font-weight: 500;
  font-size: 13.5px;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warn);
}
.badge :deep(svg) {
  margin-right: 2px;
}
.reset-icon {
  display: inline-flex;
  color: var(--text-3);
}
.reset-icon:hover {
  color: var(--brand);
}
.desc {
  margin-top: 2px;
  font-size: 13px;
}
.inactive {
  margin-top: 2px;
  font-size: 12px;
  color: var(--warn-text);
}
.meta {
  margin-top: 4px;
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
}
.env {
  font-family: var(--mono);
  font-size: 11px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--neutral-soft);
  color: var(--text-2);
}
.hist {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.control {
  display: flex;
  justify-content: flex-end;
  min-width: 0;
}
.num-input {
  width: 170px;
}
.compact .num-input {
  width: 150px;
}
.mono-input :deep(input) {
  font-family: var(--mono);
  font-size: 12.5px;
}
@media (max-width: 760px) {
  .setting {
    grid-template-columns: 1fr;
    gap: 10px;
    padding: 14px 16px;
  }
  .setting.compact {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .control {
    justify-content: flex-start;
  }
  .compact .control {
    justify-content: flex-end;
  }
}
</style>
