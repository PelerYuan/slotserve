<script setup lang="ts">
// "Call a model" (F05, D16): a chat in the browser on the session (no key; POST /api/playground/chat, metered
// as the person with source=playground) next to the same request in code. Only models that are callable now
// are offered; the server never switches a model for it. "Copy code to run locally" creates a key on request
// (name "playground", 90 days, shown once, kept only in this component) and fills it into the code.
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NButton,
  NCollapse,
  NCollapseItem,
  NDrawer,
  NDrawerContent,
  NInput,
  NInputNumber,
  NSelect,
  NSlider,
  NTab,
  NTabs,
  useDialog,
  type SelectOption,
} from 'naive-ui'
import { CalendarPlus, RotateCcw, SendHorizontal, ShieldCheck } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import QuickBook from '@/features/quickbook/components/QuickBook.vue'
import { ApiError } from '@/api/client'
import { useStatus } from '@/composables/useStatus'
import { useErrorMessage } from '@/composables/useErrorMessage'
import SuggestionCard from '@/features/suggestions/components/SuggestionCard.vue'
import { offerSuggestion } from '@/features/suggestions/useSuggestions'
import { loadOnboarding } from '@/features/onboarding/api'
import {
  availabilityOf,
  playgroundApi,
  PLAYGROUND_KEY_DAYS,
  PLAYGROUND_KEY_NAME,
  type ChatMessage,
  type ChatRequest,
  type KeyInfo,
  type ModelAvailability,
  type PlaygroundState,
} from '../api'
import { codeSamples, endpoint, type CodeLang } from '../code'
import ModelBadge from './ModelBadge.vue'

const show = defineModel<boolean>('show', { default: false })
const props = defineProps<{ model?: string | null }>()

const { t, te } = useI18n()
const dialog = useDialog()
const { describe, message } = useErrorMessage()
const { status, refresh } = useStatus()

// ---- layout ---------------------------------------------------------------------------------------------------
const narrow = ref(false)
let media: MediaQueryList | null = null
const onMedia = () => (narrow.value = !!media?.matches)
onMounted(() => {
  media = window.matchMedia('(max-width: 760px)')
  onMedia()
  media.addEventListener('change', onMedia)
})
onBeforeUnmount(() => media?.removeEventListener('change', onMedia))
const pane = ref<'chat' | 'code'>('chat')

// ---- state ----------------------------------------------------------------------------------------------------
const state = ref<PlaygroundState | null>(null)
const keys = ref<KeyInfo[]>([])
const available = computed(() => availabilityOf(status.value))
const callable = computed(() => available.value.filter((m) => m.state === 'now'))
const modelName = ref<string | null>(null)
const selected = computed<ModelAvailability | undefined>(() => available.value.find((m) => m.name === modelName.value))

function pickModel() {
  const wanted = props.model ? available.value.find((m) => m.name === props.model && m.state === 'now') : undefined
  const onlyDefault = state.value?.default_model_only ? state.value.default_model : null
  const fallback = onlyDefault
    ? callable.value.find((m) => m.name === onlyDefault)
    : (callable.value.find((m) => m.is_default && !m.loading) ?? callable.value.find((m) => !m.loading) ?? callable.value[0])
  modelName.value = (wanted ?? fallback)?.name ?? null
}

const picked = ref(false) // the model was chosen after opening (the "no model" hint waits for it)
watch(show, async (open) => {
  if (!open) return
  picked.value = false
  pane.value = 'chat'
  error.value = null
  const [s, k] = await Promise.all([playgroundApi.state().catch(() => null), playgroundApi.keys().catch(() => [])])
  state.value = s
  keys.value = k.filter((key) => !key.expired)
  await refresh()
  pickModel()
  picked.value = true
})
watch(callable, () => {
  if (show.value && (!selected.value || selected.value.state !== 'now')) pickModel()
})

const modelOptions = computed<SelectOption[]>(() =>
  available.value.map((m) => ({
    label: m.name,
    value: m.name,
    disabled:
      m.state !== 'now' || (!!state.value?.default_model_only && state.value.default_model !== m.name),
  })),
)
function renderLabel(option: SelectOption) {
  const m = available.value.find((x) => x.name === option.value)
  return h('span', { class: 'pg-option' }, [h('span', { class: 'pg-option-name' }, String(option.label)), h(ModelBadge, { model: m, short: true })])
}

// ---- chat -----------------------------------------------------------------------------------------------------
interface Shown extends ChatMessage {
  tokens?: { prompt: number; completion: number }
}
const messages = ref<Shown[]>([])
const input = ref('')
const system = ref('')
const temperature = ref<number>(0.7)
const maxTokens = ref<number | null>(null)
const sending = ref(false)
const error = ref<ApiError | null>(null)
const retryLeft = ref(0)
let retryTimer: number | undefined
const lastRequest = ref<ChatRequest | null>(null)
const lastResponse = ref<unknown>(null)
const scroller = ref<HTMLElement | null>(null)

const lastPrompt = computed(
  () => [...messages.value].reverse().find((m) => m.role === 'user')?.content || t('playground.codePrompt'),
)

async function send() {
  const text = input.value.trim()
  if (!text || sending.value || !modelName.value) return
  error.value = null
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  const body: ChatRequest = {
    model: modelName.value,
    messages: [
      ...(system.value.trim() ? [{ role: 'system' as const, content: system.value.trim() }] : []),
      ...messages.value.map(({ role, content }) => ({ role, content })),
    ],
    temperature: temperature.value,
    ...(maxTokens.value ? { max_tokens: maxTokens.value } : {}),
  }
  lastRequest.value = body
  sending.value = true
  scrollDown()
  try {
    const resp = await playgroundApi.chat(body)
    lastResponse.value = resp
    const reply = resp.choices?.[0]?.message?.content ?? ''
    messages.value.push({
      role: 'assistant',
      content: reply,
      tokens: resp.usage ? { prompt: resp.usage.prompt_tokens, completion: resp.usage.completion_tokens } : undefined,
    })
    void loadOnboarding(true) // the "call a model" goal is reached now
    void offerSuggestion('playground_first_reply')
  } catch (e) {
    messages.value.pop()
    input.value = text
    lastResponse.value = e instanceof ApiError ? { status: e.status, detail: { code: e.code, params: e.params } } : null
    if (e instanceof ApiError) {
      error.value = e
      if (e.code === 'playground_rate_limited') startRetry(Number(e.params.seconds) || 5)
      else if (e.code === 'model_unavailable' && e.params.retry_after) startRetry(Number(e.params.retry_after))
      if (e.code === 'playground_disabled' && state.value) state.value = { ...state.value, enabled: false }
      void refresh()
    } else message.error(describe(e))
  } finally {
    sending.value = false
    scrollDown()
  }
}

function startRetry(seconds: number) {
  window.clearInterval(retryTimer)
  retryLeft.value = Math.max(1, Math.ceil(seconds))
  retryTimer = window.setInterval(() => {
    retryLeft.value -= 1
    if (retryLeft.value <= 0) window.clearInterval(retryTimer)
  }, 1000)
}
onBeforeUnmount(() => window.clearInterval(retryTimer))

function scrollDown() {
  void nextTick(() => scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' }))
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void send()
  }
}

function clearChat() {
  messages.value = []
  error.value = null
  lastRequest.value = null
  lastResponse.value = null
}

const whyText = computed(() => {
  const e = error.value
  if (!e) return ''
  const key = `playground.why.${e.code}`
  return te(key) ? t(key, e.params as Record<string, unknown>) : describe(e)
})
const canBook = computed(() =>
  ['gpu_reserved', 'model_not_loaded', 'gpu_exclusive', 'model_switching'].includes(error.value?.code ?? ''),
)
const otherFree = computed(() => callable.value.find((m) => m.name !== modelName.value && !m.loading))

// ---- booking (QuickBook) --------------------------------------------------------------------------------------
const quickBook = ref(false)
const bookModelId = ref<number | null>(null)
function book(model?: ModelAvailability | null) {
  bookModelId.value = model?.id ?? null
  quickBook.value = true
}
const biggerModel = computed(() => available.value.find((m) => m.state !== 'now' && m.name !== modelName.value) ?? null)

// ---- code -----------------------------------------------------------------------------------------------------
const lang = ref<CodeLang>('python')
const createdKey = ref<string | null>(null) // only in memory, only while this page is open
const ownKey = computed(() => keys.value.find((k) => k.scope !== 'env') ?? null)
const earlier = computed(() => keys.value.find((k) => k.name === PLAYGROUND_KEY_NAME) ?? null)
const codeModel = computed(() => modelName.value ?? state.value?.default_model ?? available.value[0]?.name ?? 'default')
const samples = computed(() =>
  codeSamples({ baseUrl: endpoint(), key: createdKey.value, model: codeModel.value, prompt: lastPrompt.value }),
)
const creatingKey = ref(false)

async function writeClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
}

function copyCode() {
  if (createdKey.value) {
    void writeClipboard(samples.value[lang.value]).then(() => message.success(t('playground.codeCopied')))
    return
  }
  dialog.info({
    title: t('playground.keyConfirmTitle'),
    content: [
      t('playground.keyConfirm', { name: PLAYGROUND_KEY_NAME, days: PLAYGROUND_KEY_DAYS }),
      earlier.value ? t('playground.keyConfirmAgain', { name: PLAYGROUND_KEY_NAME, prefix: earlier.value.prefix }) : '',
    ]
      .filter(Boolean)
      .join(' '),
    positiveText: t('playground.keyCreate'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      creatingKey.value = true
      try {
        const key = await playgroundApi.createKey(PLAYGROUND_KEY_NAME, PLAYGROUND_KEY_DAYS)
        createdKey.value = key.key
        keys.value = [key, ...keys.value]
        await writeClipboard(samples.value[lang.value])
        message.success(t('playground.keyCreated'), { duration: 6000 })
      } catch (e) {
        message.error(describe(e))
      } finally {
        creatingKey.value = false
      }
    },
  })
}

const pretty = (v: unknown) => JSON.stringify(v, null, 2)
const enabled = computed(() => state.value?.enabled !== false)
</script>

<template>
  <NDrawer v-model:show="show" placement="right" :width="narrow ? '100%' : 980" :auto-focus="false">
    <NDrawerContent closable :native-scrollbar="false" body-content-class="pg-body">
      <template #header>
        <div class="pg-head">
          <span>{{ t('playground.title') }}</span>
          <span class="faint pg-sub">{{ enabled ? t('playground.subtitle') : '' }}</span>
        </div>
      </template>

      <div class="pg" :class="{ narrow }" data-component="playground">
        <div v-if="narrow && enabled" class="pg-switch" role="tablist">
          <button role="tab" :aria-selected="pane === 'chat'" :class="{ on: pane === 'chat' }" @click="pane = 'chat'">
            {{ t('playground.tabChat') }}
          </button>
          <button role="tab" :aria-selected="pane === 'code'" :class="{ on: pane === 'code' }" @click="pane = 'code'">
            {{ t('playground.tabCode') }}
          </button>
        </div>

        <!-- chat -->
        <section v-if="enabled" v-show="!narrow || pane === 'chat'" class="pg-chat">
          <div class="pg-safe">
            <ShieldCheck :size="15" />
            <span>{{ t('playground.safe') }}</span>
          </div>
          <label class="pg-model">
            <span class="field-label">{{ t('playground.model') }}</span>
            <NSelect
              v-model:value="modelName"
              :options="modelOptions"
              :render-label="renderLabel"
              :placeholder="t('playground.modelPlaceholder')"
              data-field="playground-model"
            />
          </label>
          <div v-if="selected && (selected.loading || selected.args_pending)" class="pg-badge"><ModelBadge :model="selected" /></div>
          <div v-if="picked && !modelName" class="pg-none">
            <span>{{ t('playground.noModel') }}</span>
            <NButton size="small" type="primary" secondary @click="book(biggerModel)">
              <template #icon><CalendarPlus :size="14" /></template>
              {{ t('playground.book') }}
            </NButton>
          </div>

          <div ref="scroller" class="pg-messages" data-field="playground-messages">
            <div v-if="!messages.length" class="faint small pg-empty">{{ t('playground.empty') }}</div>
            <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
              <div class="who">{{ m.role === 'user' ? t('playground.you') : codeModel }}</div>
              <div class="bubble">{{ m.content }}</div>
              <div v-if="m.tokens" class="faint tiny">
                {{ t('playground.tokens', { prompt: m.tokens.prompt, completion: m.tokens.completion }) }}
              </div>
            </div>
            <div v-if="sending" class="msg assistant">
              <div class="bubble typing">{{ t('playground.sending') }}</div>
            </div>
          </div>

          <div v-if="error" class="pg-error" role="alert" :data-error="error.code">
            <div>{{ whyText }}</div>
            <div class="pg-error-actions">
              <NButton v-if="canBook" size="small" type="primary" secondary @click="book(selected)">
                <template #icon><CalendarPlus :size="14" /></template>
                {{ t('playground.book') }}
              </NButton>
              <NButton v-if="canBook && otherFree" size="small" secondary @click="modelName = otherFree!.name">
                {{ t('playground.useModel', { model: otherFree.name }) }}
              </NButton>
              <span v-if="retryLeft > 0" class="faint small">{{ t('playground.retryIn', { n: retryLeft }) }}</span>
            </div>
          </div>

          <SuggestionCard
            id="playground_first_reply"
            :text="biggerModel ? t('suggestions.playgroundFirstReply', { model: biggerModel.name }) : t('suggestions.playgroundFirstReplyAny')"
            :action="t('suggestions.book')"
            @action="book(biggerModel)"
          />

          <div class="pg-input">
            <NInput
              v-model:value="input"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 6 }"
              :placeholder="t('playground.inputPlaceholder')"
              :disabled="!modelName"
              data-field="playground-input"
              @keydown="onKey"
            />
            <div class="pg-input-row">
              <NButton quaternary size="small" :disabled="!messages.length" @click="clearChat">
                <template #icon><RotateCcw :size="14" /></template>
                {{ t('playground.clear') }}
              </NButton>
              <span v-if="state?.rate_per_minute" class="faint tiny">{{ t('playground.rateHint', { n: state.rate_per_minute }) }}</span>
              <span class="spacer" />
              <NButton
                type="primary"
                :loading="sending"
                :disabled="!input.trim() || !modelName || retryLeft > 0"
                data-action="playground-send"
                @click="send"
              >
                <template #icon><SendHorizontal :size="15" /></template>
                {{ t('playground.send') }}
              </NButton>
            </div>
          </div>

          <NCollapse class="pg-more" :default-expanded-names="[]">
            <NCollapseItem :title="t('playground.params')" name="params">
              <div class="pg-params">
                <label>
                  <span class="field-label">{{ t('playground.system') }}</span>
                  <NInput v-model:value="system" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" :placeholder="t('playground.systemPlaceholder')" />
                </label>
                <label>
                  <span class="field-label">{{ t('playground.temperature') }} · {{ temperature.toFixed(1) }}</span>
                  <NSlider v-model:value="temperature" :min="0" :max="2" :step="0.1" />
                </label>
                <label>
                  <span class="field-label">{{ t('playground.maxTokens') }}</span>
                  <NInputNumber v-model:value="maxTokens" :min="1" :max="8192" clearable :placeholder="t('playground.maxTokensPlaceholder')" />
                </label>
              </div>
            </NCollapseItem>
            <NCollapseItem :title="t('playground.raw')" name="raw" :disabled="!lastRequest">
              <div class="pg-raw">
                <CodeBlock v-if="lastRequest" :code="`POST /api/playground/chat\n${pretty(lastRequest)}`" />
                <CodeBlock v-if="lastResponse" :code="pretty(lastResponse)" />
              </div>
            </NCollapseItem>
          </NCollapse>
        </section>

        <!-- code -->
        <section v-show="!narrow || pane === 'code' || !enabled" class="pg-code">
          <div v-if="!enabled" class="pg-disabled" data-field="playground-disabled">{{ t('playground.disabled') }}</div>
          <h3 class="card-title">{{ t('playground.codeTitle') }}</h3>
          <p class="faint small">
            {{ createdKey ? t('playground.codeHintKey') : ownKey ? t('playground.ownKey', { name: ownKey.name, prefix: ownKey.prefix }) : t('playground.codeHintPlaceholder') }}
          </p>
          <NTabs v-model:value="lang" type="line" size="small">
            <NTab name="python">Python</NTab>
            <NTab name="curl">cURL</NTab>
            <NTab name="js">JavaScript</NTab>
          </NTabs>
          <CodeBlock :code="samples[lang]" data-field="playground-code" />
          <NButton type="primary" :secondary="!!createdKey" block :loading="creatingKey" data-action="playground-copy-code" @click="copyCode">
            {{ t('playground.copyCode') }}
          </NButton>

          <div class="pg-tools">
            <div class="label">{{ t('playground.otherTools') }}</div>
            <p class="faint small">{{ t('playground.otherToolsHint') }}</p>
            <dl>
              <dt>{{ t('playground.baseUrl') }}</dt>
              <dd><code>{{ endpoint() }}</code></dd>
              <dt>{{ t('playground.apiKey') }}</dt>
              <dd>
                <code v-if="createdKey">{{ createdKey }}</code>
                <code v-else-if="ownKey">{{ ownKey.prefix }}••••</code>
                <span v-else class="faint">{{ t('playground.keyYours') }}</span>
              </dd>
              <dt>{{ t('playground.model') }}</dt>
              <dd><code>{{ codeModel }}</code></dd>
            </dl>
          </div>
        </section>
      </div>
    </NDrawerContent>
  </NDrawer>
  <QuickBook v-model:show="quickBook" :intent="bookModelId ? 'model' : null" :model-id="bookModelId" @done="refresh" />
</template>

<style scoped>
.pg-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pg-sub {
  font-size: 12.5px;
  font-weight: 400;
}
.pg {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.pg.narrow {
  grid-template-columns: minmax(0, 1fr);
  gap: 14px;
}
.pg-switch {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 3px;
  border-radius: 9px;
  background: var(--seg-bg);
}
.pg-switch button {
  border: none;
  background: none;
  padding: 6px 0;
  border-radius: 7px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 550;
  color: var(--text-2);
  cursor: pointer;
}
.pg-switch button.on {
  background: var(--seg-pill);
  color: var(--text);
  box-shadow: var(--shadow);
}
.pg-chat,
.pg-code {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.pg-safe {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 12.5px;
  color: var(--ok-text);
  background: var(--ok-soft);
  padding: 8px 12px;
  border-radius: 8px;
}
.pg-safe svg {
  flex: none;
  margin-top: 2px;
}
.field-label {
  font-size: 12.5px;
  font-weight: 550;
  color: var(--text-2);
}
.pg-model {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pg-none {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--warn-text);
  background: var(--warn-soft);
  padding: 10px 12px;
  border-radius: 8px;
}
.pg-messages {
  min-height: 160px;
  max-height: 46vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface-muted);
}
.pg-empty {
  margin: auto;
  text-align: center;
}
.msg {
  display: flex;
  flex-direction: column;
  gap: 3px;
  max-width: 88%;
}
.msg.user {
  align-self: flex-end;
  align-items: flex-end;
}
.who {
  font-size: 11.5px;
  color: var(--text-3);
}
.bubble {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13.5px;
  line-height: 1.55;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
}
.msg.user .bubble {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}
.typing {
  color: var(--text-3);
}
.tiny {
  font-size: 11.5px;
}
.pg-error {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--warn-soft);
  color: var(--warn-text);
}
.pg-error-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.pg-input {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pg-input-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.pg-params {
  display: grid;
  gap: 12px;
}
.pg-params label {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pg-raw {
  display: grid;
  gap: 8px;
}
.pg-disabled {
  font-size: 13px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--neutral-soft);
  color: var(--text-2);
}
.pg-code .card-title {
  margin: 0;
}
.pg-code p {
  margin: -6px 0 0;
}
.pg-tools {
  margin-top: 6px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.pg-tools p {
  margin: 4px 0 8px;
}
.pg-tools dl {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 14px;
  margin: 0;
  font-size: 13px;
}
.pg-tools dt {
  color: var(--text-3);
}
.pg-tools dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.pg-tools code {
  font-family: var(--mono);
  font-size: 12.5px;
}
:global(.pg-option) {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
:global(.pg-option-name) {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
