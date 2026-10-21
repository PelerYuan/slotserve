<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NForm, NFormItem, NInput, NModal, NRadioButton, NRadioGroup, NSelect } from 'naive-ui'
import { api } from '@/api/client'
import type { CatalogDeploy, CatalogEngine, CatalogEntry, CatalogLane } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes } from '@/utils/format'
import { CONTEXT_OPTIONS, bestFor, contextLabel, engineLabel, fitClass, fitOn } from '@/utils/catalog'

const props = defineProps<{ entry: CatalogEntry; variant: string | null; lanes: CatalogLane[] }>()
const emit = defineEmits<{ close: []; deployed: [deploy: CatalogDeploy] }>()
const { t } = useI18n()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

const live = ref<CatalogEntry>(props.entry)
const laneId = ref<number | null>(props.lanes[0]?.id ?? null)
const variantId = ref<string>(props.variant ?? (laneId.value != null ? bestFor(props.entry, laneId.value).variant?.id : null) ?? props.entry.variants[0].id)
const variant = computed(() => live.value.variants.find((v) => v.id === variantId.value) ?? live.value.variants[0])
const engine = ref<CatalogEngine>(variant.value.engines[0])
const ctx = ref<number>(props.entry.default_ctx)
const setDefault = ref(false)
const name = ref('')
const busy = ref(false)

watch(variantId, () => {
  if (!variant.value.engines.includes(engine.value)) engine.value = variant.value.engines[0]
})

const ctxOptions = computed(() => {
  const values = new Set([...CONTEXT_OPTIONS.filter((n) => n <= props.entry.context_length), props.entry.default_ctx])
  return [...values].sort((a, b) => a - b).map((n) => ({ label: `${contextLabel(n)} ${t('catalog.tokens')}`, value: n }))
})

let seq = 0
watch(ctx, async (value) => {
  const mine = ++seq
  try {
    const fresh = await api.catalog.entry(props.entry.id, value)
    if (mine === seq) live.value = fresh
  } catch (e) {
    showError(e)
  }
})

const variantOptions = computed(() =>
  live.value.variants.map((v) => ({
    value: v.id,
    label: `${v.quant} · ${v.engines.map(engineLabel).join('/')} · ${formatBytes(v.download_bytes)}`,
  })),
)
const laneOptions = computed(() => props.lanes.map((l) => ({ value: l.id, label: l.name })))
const lane = computed(() => props.lanes.find((l) => l.id === laneId.value) ?? null)
const fit = computed(() => (laneId.value != null ? fitOn(variant.value, laneId.value) : 'unknown'))

const est = computed(() => variant.value.estimate)
const scale = computed(() => Math.max(est.value.total, lane.value?.memory_bytes ?? 0) || 1)
const seg = (n: number) => `${(n / scale.value) * 100}%`

const defaultName = computed(() => `${props.entry.id}-${variant.value.id}`)
const present = computed(() => variant.value.models.some((m) => m.engine === engine.value))

async function submit() {
  busy.value = true
  try {
    const d = await api.catalog.deploy({
      entry_id: props.entry.id,
      variant_id: variant.value.id,
      engine: engine.value,
      lane_id: laneId.value,
      set_default: isAdmin.value && setDefault.value,
      ctx: ctx.value,
      name: name.value.trim() || undefined,
    })
    emit('deployed', d)
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NModal
    :show="true"
    preset="card"
    :title="t('catalog.deploy.title', { name: entry.name })"
    style="width: 620px; max-width: calc(100vw - 32px)"
    :bordered="false"
    @update:show="(v: boolean) => !v && emit('close')"
  >
    <NForm :show-require-mark="false" label-placement="top">
      <NFormItem :label="t('catalog.deploy.variant')">
        <NSelect v-model:value="variantId" :options="variantOptions" />
      </NFormItem>
      <NFormItem v-if="variant.engines.length > 1" :label="t('models.engine')">
        <NRadioGroup v-model:value="engine" size="small">
          <NRadioButton v-for="e in variant.engines" :key="e" :value="e">{{ engineLabel(e) }}</NRadioButton>
        </NRadioGroup>
      </NFormItem>
      <div class="two">
        <NFormItem :label="t('catalog.deploy.context')">
          <NSelect v-model:value="ctx" :options="ctxOptions" />
        </NFormItem>
        <NFormItem v-if="lanes.length" :label="t('catalog.deploy.lane')">
          <NSelect v-model:value="laneId" :options="laneOptions" />
        </NFormItem>
      </div>

      <div class="mem">
        <div class="mem-head">
          <span class="small">{{ t('catalog.deploy.memory') }}</span>
          <span class="num"><b>≈ {{ formatBytes(est.total) }}</b>
            <template v-if="lane?.memory_bytes"> / {{ formatBytes(lane.memory_bytes) }}</template>
          </span>
          <span class="badge" :class="fitClass(fit)">{{ t(`catalog.fit.${fit}`) }}</span>
        </div>
        <div class="mem-bar">
          <div class="w" :style="{ width: seg(est.weights) }" />
          <div class="k" :style="{ width: seg(est.kv_cache) }" />
          <div class="o" :style="{ width: seg(est.overhead) }" />
          <div v-if="lane?.memory_bytes" class="cap" :style="{ left: seg(lane.memory_bytes) }" />
        </div>
        <div class="legend small faint">
          <span><i class="w" />{{ t('catalog.deploy.weights') }} {{ formatBytes(est.weights) }}</span>
          <span><i class="k" />{{ t('catalog.deploy.kv') }} {{ formatBytes(est.kv_cache) }}</span>
          <span><i class="o" />{{ t('catalog.deploy.overhead') }} {{ formatBytes(est.overhead) }}</span>
        </div>
        <p class="small faint hint">{{ t(`catalog.fitHint.${fit}`) }}</p>
      </div>

      <NFormItem :label="t('catalog.deploy.name')">
        <NInput v-model:value="name" :placeholder="defaultName" maxlength="128" />
      </NFormItem>
      <NCheckbox v-if="isAdmin && lanes.length" v-model:checked="setDefault">
        {{ t('catalog.deploy.setDefault', { lane: lane?.name ?? '' }) }}
      </NCheckbox>
      <p v-if="variant.gated" class="small warn-text">{{ t('catalog.gatedHint') }}</p>
      <p class="small faint">
        {{ present ? t('catalog.deploy.presentHint') : variant.downloaded ? t('catalog.deploy.downloadedHint') : t('catalog.deploy.downloadHint', { size: formatBytes(variant.download_bytes) }) }}
      </p>
    </NForm>
    <template #footer>
      <div class="footer">
        <NButton @click="emit('close')">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" @click="submit">{{ t('catalog.deploy.confirm') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.mem {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  margin-bottom: 16px;
  background: var(--surface-muted);
}
.mem-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.mem-head .num {
  margin-left: auto;
}
.mem-bar {
  position: relative;
  display: flex;
  height: 10px;
  border-radius: 5px;
  background: var(--rail);
}
.mem-bar > div:not(.cap) {
  height: 100%;
}
.mem-bar > div:first-child {
  border-radius: 5px 0 0 5px;
}
.w,
.legend i.w {
  background: var(--brand);
}
.k,
.legend i.k {
  background: var(--ok);
}
.o,
.legend i.o {
  background: var(--warn);
}
.cap {
  position: absolute;
  top: -3px;
  bottom: -3px;
  width: 2px;
  background: var(--text);
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 8px;
}
.legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 5px;
}
.hint {
  margin: 6px 0 0;
}
.warn-text {
  color: var(--warn-text);
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 560px) {
  .two {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
