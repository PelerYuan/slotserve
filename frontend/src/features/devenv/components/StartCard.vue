<script setup lang="ts">
// "Open an environment" in three steps (F03): 1 pick a tool (VS Code in the browser by default), 2 a GPU or not
// (not now = start right away on the CPU; a GPU = QuickBook), 3 open. Web interfaces need no credentials; the SSH
// key step appears inline only on the terminal / SSH path.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NRadio, NRadioGroup, useMessage } from 'naive-ui'
import { BookOpen, ChevronDown, Code, Monitor, Terminal } from 'lucide-vue-next'
import { cpuApi } from '@/api/devenvCpu'
import type { DevInfo, DevProject, DevSession, DevTemplate } from '@/api/types'
import { cpuInfo, loadCpu } from '@/composables/devenvCpu'
import { useErrorMessage } from '@/composables/useErrorMessage'
import PreviewLine from './PreviewLine.vue'
import SshCredentials from './SshCredentials.vue'

const props = defineProps<{
  templates: DevTemplate[]
  projects: DevProject[]
  info: DevInfo
  canLogIn: boolean
}>()
const emit = defineEmits<{
  started: [session: DevSession]
  book: [target: { templateId: number | null; projectId: number | null }]
  credentials: []
}>()

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()

type Pick = { kind: 't' | 'p'; id: number }
const TOOLS = [
  { key: 'vscode', builtin: 'vscode', icon: Code },
  { key: 'jupyter', builtin: 'jupyter-pytorch', icon: BookOpen },
  { key: 'desktop', builtin: 'desktop', icon: Monitor },
] as const

const usable = computed(() => props.templates.filter((x) => x.enabled || x.kind !== 'builtin'))
// The three browser tools: the built-in templates, else any template serving that kind of web interface.
const tools = computed(() =>
  TOOLS.map((tool) => {
    const tpl =
      usable.value.find((x) => x.builtin_key === tool.builtin) ??
      usable.value.find((x) => x.kind === 'builtin' && x.web_ports.some((p) => p.kind === tool.key))
    return tpl ? { ...tool, template: tpl } : null
  }).filter((x) => x !== null),
)
const toolIds = computed(() => new Set(tools.value.map((x) => x.template.id)))
const sshTemplates = computed(() => usable.value.filter((x) => !x.web_ports.length && x.kind === 'builtin'))
const otherTemplates = computed(() => usable.value.filter((x) => !toolIds.value.has(x.id) && !sshTemplates.value.includes(x)))
const kept = computed(() => props.projects.filter((p) => p.mine && p.state !== 'running'))

const moreOptions = computed(() => {
  const out: Record<string, unknown>[] = []
  const group = (key: string, label: string, children: { key: string; label: string }[]) => {
    if (children.length) out.push({ type: 'group', key, label, children })
  }
  group('ssh', t('devenvUi.start.sshGroup'), sshTemplates.value.map((x) => ({ key: `t:${x.id}`, label: x.name })))
  group('kept', t('devenvCpu.keptGroup'), kept.value.map((p) => ({ key: `p:${p.id}`, label: p.name })))
  group('other', t('devenvUi.start.otherGroup'), otherTemplates.value.map((x) => ({ key: `t:${x.id}`, label: x.name })))
  return out
})

const pick = ref<Pick | null>(null)
watch(
  tools,
  (list) => {
    if (!pick.value && list.length) pick.value = { kind: 't', id: list[0].template.id }
    else if (!pick.value && sshTemplates.value.length) pick.value = { kind: 't', id: sshTemplates.value[0].id }
  },
  { immediate: true },
)
function choose(key: string) {
  const [kind, id] = key.split(':')
  pick.value = { kind: kind as 't' | 'p', id: Number(id) }
}

const pickedTemplate = computed(() => {
  const p = pick.value
  if (!p) return null
  if (p.kind === 't') return props.templates.find((x) => x.id === p.id) ?? null
  const project = props.projects.find((x) => x.id === p.id)
  return props.templates.find((x) => x.id === project?.template_id) ?? null
})
const pickedProject = computed(() => (pick.value?.kind === 'p' ? props.projects.find((x) => x.id === pick.value?.id) ?? null : null))
const isTool = computed(() => pick.value?.kind === 't' && toolIds.value.has(pick.value.id))
const moreLabel = computed(() => {
  if (!pick.value || isTool.value) return t('devenvUi.start.tool.ssh')
  return pickedProject.value?.name ?? pickedTemplate.value?.name ?? t('devenvUi.start.tool.ssh')
})
// Only an environment without a web interface needs a way to log in over SSH.
const needsSsh = computed(() => !!pickedTemplate.value && !pickedTemplate.value.web_ports.length)
const needsCredentials = computed(() => needsSsh.value && !props.canLogIn)

// ---- GPU or not -------------------------------------------------------------------------------------------------
const cpu = computed(() => cpuInfo.value)
const cpuOk = computed(() => !!cpu.value?.enabled)
const cpuFull = computed(() => !!cpu.value && cpu.value.running >= cpu.value.per_user)
const gpu = ref<'no' | 'yes'>('no')
watch(
  [cpuOk, cpuFull],
  () => {
    if (cpu.value && (!cpuOk.value || cpuFull.value)) gpu.value = 'yes'
  },
  { immediate: true },
)

// ---- open --------------------------------------------------------------------------------------------------------
const busy = ref(false)
async function open() {
  const p = pick.value
  if (!p) return
  if (gpu.value === 'yes') {
    emit('book', { templateId: p.kind === 't' ? p.id : null, projectId: p.kind === 'p' ? p.id : null })
    return
  }
  busy.value = true
  try {
    const s = await cpuApi.start(p.kind === 'p' ? { project_id: p.id } : { template_id: p.id })
    message.success(t('devenvUi.start.starting'))
    await loadCpu().catch(() => undefined)
    emit('started', s)
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
const openLabel = computed(() => (gpu.value === 'yes' ? t('devenvUi.start.chooseTime') : t('devenvUi.start.open')))
</script>

<template>
  <section class="card start" data-test="start-card">
    <h2 class="title">{{ t('devenvUi.start.title') }}</h2>

    <!-- 1 -->
    <div class="step">
      <span class="n">1</span>
      <div class="body">
        <div class="step-label">{{ t('devenvUi.start.step1') }}</div>
        <div class="tools" role="radiogroup">
          <button
            v-for="tool in tools"
            :key="tool.key"
            type="button"
            class="tool"
            role="radio"
            :aria-checked="pick?.kind === 't' && pick.id === tool.template.id"
            :class="{ on: pick?.kind === 't' && pick.id === tool.template.id }"
            :data-tool="tool.key"
            @click="pick = { kind: 't', id: tool.template.id }"
          >
            <component :is="tool.icon" :size="16" />
            <span>{{ t(`devenvUi.start.tool.${tool.key}`) }}</span>
          </button>
          <NDropdown v-if="moreOptions.length" trigger="click" :options="moreOptions" scrollable @select="choose">
            <button type="button" class="tool" :class="{ on: pick && !isTool }" data-tool="ssh">
              <Terminal :size="16" />
              <span class="ellipsis">{{ moreLabel }}</span>
              <ChevronDown :size="14" />
            </button>
          </NDropdown>
        </div>
        <p v-if="pickedTemplate" class="faint small what">
          {{ pickedProject ? t('devenvUi.start.continueKept') : pickedTemplate.builtin_key ? t(`devenv.builtin.${pickedTemplate.builtin_key}`) : pickedTemplate.description }}
        </p>
        <SshCredentials v-if="needsCredentials" :password-login="info.password_login" @done="emit('credentials')" />
      </div>
    </div>

    <!-- 2 -->
    <div class="step">
      <span class="n">2</span>
      <div class="body">
        <div class="step-label">{{ t('devenvUi.start.step2') }}</div>
        <NRadioGroup v-model:value="gpu" class="gpu" name="gpu">
          <NRadio value="no" :disabled="!cpuOk || cpuFull" data-test="gpu-no">
            {{ t('devenvUi.start.gpuNo') }} <span class="faint small">{{ t('devenvUi.start.gpuNoHint') }}</span>
          </NRadio>
          <NRadio value="yes" data-test="gpu-yes">
            {{ t('devenvUi.start.gpuYes') }} <span class="faint small">{{ t('devenvUi.start.gpuYesHint') }}</span>
          </NRadio>
        </NRadioGroup>
        <p v-if="cpu && !cpuOk" class="faint small">{{ t('devenvCpu.disabled') }}</p>
        <p v-else-if="cpuFull" class="faint small">{{ t('devenvCpu.limitReached', { n: cpu?.per_user ?? 0 }) }}</p>
      </div>
    </div>

    <!-- 3 -->
    <div class="step">
      <span class="n">3</span>
      <div class="body">
        <div class="row wrap go">
          <NButton
            type="primary"
            :loading="busy"
            :disabled="!pick || needsCredentials"
            data-test="start-open"
            @click="open"
          >
            {{ openLabel }}
          </NButton>
          <span class="small muted">{{ t('devenvUi.start.filesKept') }}</span>
        </div>
        <PreviewLine
          v-if="pick && gpu === 'no' && cpuOk && !needsCredentials"
          :template-id="pick.kind === 't' ? pick.id : null"
          :project-id="pick.kind === 'p' ? pick.id : null"
          :gpu="false"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.start {
  padding: 16px 18px;
  display: grid;
  gap: 14px;
  margin-bottom: 24px;
  min-width: 0;
}
.title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.step {
  display: grid;
  grid-template-columns: 24px 1fr;
  gap: 10px;
  align-items: start;
  min-width: 0;
}
.n {
  width: 22px;
  height: 22px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12px;
  font-weight: 700;
}
.body {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.step-label {
  font-weight: 500;
  color: var(--text-2);
  line-height: 22px;
}
.tools {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.tool {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  max-width: 100%;
}
.tool:hover {
  border-color: var(--brand);
}
.tool.on {
  border-color: var(--brand);
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
}
.what {
  margin: 0;
}
.gpu {
  display: grid;
  gap: 6px;
}
.go {
  gap: 12px;
  align-items: center;
}
p {
  margin: 0;
}
</style>
