<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { Download, ExternalLink, Eye, EyeOff, Pencil, Trash2 } from 'lucide-vue-next'
import type { CatalogEntry, CatalogLane, CatalogVariant } from '@/api/types'
import { session } from '@/composables/session'
import { formatBytes } from '@/utils/format'
import { contextLabel, engineLabel, fitClass, fitOn, hfUrl, paramsLabel } from '@/utils/catalog'

const props = defineProps<{ entry: CatalogEntry | null; lanes: CatalogLane[] }>()
const emit = defineEmits<{ close: []; deploy: [variant: string]; hide: [hidden: boolean]; edit: []; remove: [] }>()
const { t, locale } = useI18n()
const isAdmin = computed(() => !!session.user?.is_admin)

const description = computed(() =>
  props.entry ? (locale.value === 'zh-CN' && props.entry.description.zh) || props.entry.description.en : '',
)
const registeredNames = (v: CatalogVariant) => v.models.map((m) => `${m.name} (${engineLabel(m.engine)})`).join(', ')
const breakdown = (v: CatalogVariant) =>
  t('catalog.breakdown', {
    weights: formatBytes(v.estimate.weights),
    kv: formatBytes(v.estimate.kv_cache),
    overhead: formatBytes(v.estimate.overhead),
    ctx: contextLabel(v.estimate.ctx),
  })
</script>

<template>
  <NDrawer :show="!!entry" :width="640" placement="right" style="max-width: 100vw" @update:show="(v: boolean) => !v && emit('close')">
    <NDrawerContent v-if="entry" :title="entry.name" closable :native-scrollbar="false">
      <div class="details">
        <p class="desc">{{ description }}</p>
        <dl class="meta">
          <div><dt>{{ t('catalog.publisher') }}</dt><dd>{{ entry.publisher || '—' }}</dd></div>
          <div><dt>{{ t('catalog.license') }}</dt><dd>{{ entry.license || '—' }}</dd></div>
          <div><dt>{{ t('catalog.params') }}</dt><dd class="num">{{ paramsLabel(entry) }}</dd></div>
          <div><dt>{{ t('catalog.context') }}</dt><dd class="num">{{ contextLabel(entry.context_length) }}</dd></div>
          <div><dt>{{ t('catalog.family') }}</dt><dd>{{ entry.family || '—' }}</dd></div>
          <div v-if="entry.arch">
            <dt>{{ t('catalog.arch') }}</dt>
            <dd class="num">{{ t('catalog.archValue', { layers: entry.arch.layers, kv: entry.arch.kv_heads }) }}</dd>
          </div>
        </dl>
        <div class="tags">
          <span v-for="task in entry.tasks" :key="task" class="badge brand">{{ t(`catalog.task.${task}`) }}</span>
          <span v-for="tag in entry.tags" :key="tag" class="badge">{{ tag }}</span>
        </div>
        <a v-if="entry.source_repo" :href="hfUrl(entry.source_repo)" target="_blank" rel="noopener" class="link small">
          <ExternalLink :size="13" /> {{ t('catalog.modelCard') }}
        </a>

        <h4>{{ t('catalog.variants') }}</h4>
        <p class="faint small">{{ t('catalog.estimateHint') }}</p>
        <ul class="variants">
          <li v-for="v in entry.variants" :key="v.id">
            <div class="v-top">
              <span class="v-quant mono">{{ v.quant }}</span>
              <span class="badge">{{ v.engines.map(engineLabel).join(' / ') }}</span>
              <span v-if="v.files.length > 1" class="badge">{{ t('models.hf.parts', { n: v.files.length }) }}</span>
              <span v-if="v.mmproj" class="badge">{{ t('catalog.withVision') }}</span>
              <span v-if="v.gated" class="badge warn" :title="t('catalog.gatedHint')">{{ t('catalog.gated') }}</span>
              <span v-if="v.models.length" class="badge ok">{{ t('catalog.installed') }}</span>
              <span v-else-if="v.downloaded" class="badge ok">{{ t('catalog.downloaded') }}</span>
              <span class="spacer" />
              <NButton size="small" type="primary" secondary @click="emit('deploy', v.id)">
                <template #icon><Download :size="14" /></template>
                {{ v.models.length ? t('catalog.deploy.again') : t('catalog.deploy.button') }}
              </NButton>
            </div>
            <div class="v-stats">
              <div>
                <div class="faint small">{{ t('catalog.download') }}</div>
                <div class="num stat">{{ formatBytes(v.download_bytes) }}</div>
              </div>
              <div :title="breakdown(v)">
                <div class="faint small">{{ t('catalog.memory', { ctx: contextLabel(v.estimate.ctx) }) }}</div>
                <div class="num stat">≈ {{ formatBytes(v.estimate.total) }}</div>
              </div>
              <div class="v-fits">
                <span v-for="lane in lanes" :key="lane.id" class="badge" :class="fitClass(fitOn(v, lane.id))" :title="t(`catalog.fitHint.${fitOn(v, lane.id)}`)">
                  {{ lane.name }}: {{ t(`catalog.fit.${fitOn(v, lane.id)}`) }}
                </span>
              </div>
            </div>
            <a :href="hfUrl(v.repo)" target="_blank" rel="noopener" class="mono faint repo">{{ v.repo }}</a>
            <div v-if="v.models.length" class="faint small">
              {{ t('catalog.registeredAs', { names: registeredNames(v) }) }}
            </div>
          </li>
        </ul>
      </div>
      <template v-if="isAdmin" #footer>
        <div class="admin-actions">
          <NButton quaternary size="small" @click="emit('hide', !entry.hidden)">
            <template #icon><component :is="entry.hidden ? Eye : EyeOff" :size="15" /></template>
            {{ entry.hidden ? t('catalog.admin.unhide') : t('catalog.admin.hide') }}
          </NButton>
          <template v-if="entry.source === 'custom'">
            <NButton quaternary size="small" @click="emit('edit')">
              <template #icon><Pencil :size="15" /></template>
              {{ t('common.edit') }}
            </NButton>
            <NButton quaternary size="small" type="error" @click="emit('remove')">
              <template #icon><Trash2 :size="15" /></template>
              {{ t('common.delete') }}
            </NButton>
          </template>
        </div>
      </template>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.details {
  display: grid;
  gap: 14px;
}
.desc {
  margin: 0;
  line-height: 1.6;
}
.meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 16px;
  margin: 0;
}
.meta dt {
  font-size: 12px;
  color: var(--text-3);
}
.meta dd {
  margin: 2px 0 0;
  font-weight: 600;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--brand);
  text-decoration: none;
  justify-self: start;
}
h4 {
  margin: 8px 0 -8px;
  font-size: 14px;
}
.variants {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.variants li {
  padding: 12px 14px;
  display: grid;
  gap: 8px;
}
.variants li + li {
  border-top: 1px solid var(--border);
}
.v-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.v-quant {
  font-weight: 650;
  font-size: 13.5px;
}
.v-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  align-items: flex-end;
}
.v-fits {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.stat {
  font-weight: 600;
}
.repo {
  font-size: 11.5px;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.admin-actions {
  display: flex;
  gap: 6px;
  width: 100%;
}
@media (max-width: 760px) {
  .meta {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
