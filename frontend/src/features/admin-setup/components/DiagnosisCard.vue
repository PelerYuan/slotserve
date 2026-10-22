<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useMessage } from 'naive-ui'
import { BookOpen, Copy, RotateCcw, Stethoscope } from 'lucide-vue-next'
import { docsUrl, type Diagnosis } from '../api'

// The fixed shape of an error (F08): cause (recognised automatically) · evidence (log lines) · what to try ·
// [Retry] [Copy diagnosis] [Full log] [Docs]. When no pattern matched, the raw exit code and log tail stay visible.
const props = withDefaults(
  defineProps<{ diagnosis: Diagnosis; title?: string; retry?: boolean; retrying?: boolean; logUrl?: string | null }>(),
  { title: '', retry: false, retrying: false, logUrl: null },
)
const emit = defineEmits<{ retry: [] }>()
const { t, locale } = useI18n()
const message = useMessage()

const text = computed(() => props.diagnosis.text[locale.value] ?? props.diagnosis.text.en ?? { cause: '', steps: [] })
const showTail = ref(false)

function plain(): string {
  const d = props.diagnosis
  const lines = [props.title, `${t('diag.cause')}: ${text.value.cause}`]
  if (text.value.steps.length) lines.push(`${t('diag.steps')}:`, ...text.value.steps.map((s, i) => `  ${i + 1}. ${s}`))
  if (d.exit_code !== null) lines.push(t('diag.exitCode', { code: d.exit_code }))
  if (d.error) lines.push(`${t('diag.error')}: ${d.error}`)
  if (d.evidence.length) lines.push(`${t('diag.evidence')}:`, ...d.evidence.map((l) => `  ${l}`))
  if (d.log_tail.length) lines.push(`${t('diag.logTail')}:`, ...d.log_tail.map((l) => `  ${l}`))
  lines.push(`(${d.id}, ${d.category})`)
  return lines.filter((l) => l !== '').join('\n')
}

async function copy() {
  try {
    await navigator.clipboard.writeText(plain())
    message.success(t('diag.copied'))
  } catch {
    message.error(t('diag.copyFailed'))
  }
}
</script>

<template>
  <div class="diag" :class="{ known: diagnosis.recognized }" data-testid="diagnosis">
    <div class="diag-head">
      <Stethoscope :size="15" />
      <span class="diag-label">{{ diagnosis.recognized ? t('diag.recognized') : t('diag.unknown') }}</span>
    </div>
    <dl>
      <div>
        <dt>{{ t('diag.cause') }}</dt>
        <dd class="cause">{{ text.cause }}</dd>
      </div>
      <div v-if="diagnosis.evidence.length">
        <dt>{{ t('diag.evidence') }}</dt>
        <dd><pre class="lines">{{ diagnosis.evidence.join('\n') }}</pre></dd>
      </div>
      <div v-if="text.steps.length">
        <dt>{{ t('diag.steps') }}</dt>
        <dd>
          <ol class="steps">
            <li v-for="(s, i) in text.steps" :key="i">{{ s }}</li>
          </ol>
        </dd>
      </div>
      <div v-if="diagnosis.exit_code !== null || diagnosis.error">
        <dt>{{ t('diag.raw') }}</dt>
        <dd class="raw">
          <span v-if="diagnosis.exit_code !== null">{{ t('diag.exitCode', { code: diagnosis.exit_code }) }}</span>
          <span v-if="diagnosis.error" class="mono">{{ diagnosis.error }}</span>
        </dd>
      </div>
    </dl>
    <pre v-if="showTail && diagnosis.log_tail.length" class="lines tail">{{ diagnosis.log_tail.join('\n') }}</pre>
    <div class="diag-actions">
      <NButton v-if="retry" size="small" type="primary" secondary :loading="retrying" @click="emit('retry')">
        <template #icon><RotateCcw :size="14" /></template>
        {{ t('diag.retry') }}
      </NButton>
      <NButton size="small" secondary @click="copy">
        <template #icon><Copy :size="14" /></template>
        {{ t('diag.copy') }}
      </NButton>
      <NButton v-if="diagnosis.log_tail.length" size="small" quaternary @click="showTail = !showTail">
        {{ showTail ? t('diag.hideLog') : t('diag.showLog') }}
      </NButton>
      <NButton v-if="logUrl" size="small" quaternary tag="a" :href="logUrl" target="_blank">
        {{ t('diag.fullLog') }}
      </NButton>
      <NButton
        v-if="diagnosis.doc"
        size="small"
        quaternary
        tag="a"
        :href="docsUrl(diagnosis.doc, locale)"
        target="_blank"
        rel="noopener"
      >
        <template #icon><BookOpen :size="14" /></template>
        {{ t('diag.docs') }}
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.diag {
  border: 1px solid var(--border);
  border-left: 3px solid var(--text-3);
  border-radius: var(--radius-sm);
  background: var(--surface);
  padding: 12px 14px;
  font-size: 13px;
}
.diag.known {
  border-left-color: var(--danger);
}
.diag-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 8px;
}
dl {
  margin: 0;
  display: grid;
  gap: 8px;
}
dl > div {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 12px;
}
dt {
  color: var(--text-3);
}
dd {
  margin: 0;
  min-width: 0;
}
.cause {
  font-weight: 600;
  color: var(--text);
}
.steps {
  margin: 0;
  padding-left: 18px;
}
.steps li + li {
  margin-top: 2px;
}
.raw {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  color: var(--text-2);
}
.lines {
  margin: 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--code-bg);
  color: var(--code-text);
  font-family: var(--mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}
.tail {
  margin-top: 10px;
}
.diag-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}
@media (max-width: 640px) {
  dl > div {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
</style>
