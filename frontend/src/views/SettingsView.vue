<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NInput, NRadioButton, NRadioGroup, NSwitch, useDialog } from 'naive-ui'
import { MessageCircle, Monitor, Moon, Send, Sun } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { CalendarLinks, ChannelLink, ChatChannel, ChatChannels } from '@/api/types'
import { session } from '@/composables/session'
import { setThemeMode, themeMode, type ThemeMode } from '@/composables/theme'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { setLocale, type Locale } from '@/i18n'
import { formatDateTime } from '@/utils/format'

const { t, locale } = useI18n()
const dialog = useDialog()
const { showError, message } = useErrorMessage()
const pw = reactive({ old: '', next: '' })
const saving = ref(false)

async function changePassword() {
  saving.value = true
  try {
    await api.changePassword(pw.old, pw.next)
    pw.old = ''
    pw.next = ''
    message.success(t('settings.passwordChanged'))
  } catch (e) {
    showError(e)
  } finally {
    saving.value = false
  }
}

// ---- chat channels (Telegram, Feishu, DingTalk, WeCom, QQ) ------------------------
const chat = ref<ChatChannels | null>(null)
const codes = ref<Record<string, ChannelLink>>({})
const linking = ref<string | null>(null)
let poll: number | undefined

const PREF_KEYS = computed(() => {
  const keys = ['reminders', 'model_ready', 'booking_changes', 'waitlist', 'downloads']
  return session.user?.is_admin ? [...keys, 'admin_alerts'] : keys
})
const anyLinked = computed(() => !!chat.value?.channels.some((c) => c.linked))

async function loadChannels() {
  chat.value = await api.channels().catch(() => chat.value)
}

function replace(updated: ChatChannel) {
  if (!chat.value) return
  chat.value.channels = chat.value.channels.map((c) => (c.id === updated.id ? updated : c))
}

async function link(c: ChatChannel) {
  linking.value = c.id
  try {
    const res = await api.channelLink(c.id)
    if (c.link_kind === 'url') {
      if (res.link_url) window.open(res.link_url, '_blank', 'noopener')
    } else {
      codes.value = { ...codes.value, [c.id]: res }
    }
    waitForLink(c.id, c.link_kind === 'url' ? 60 : 450)
  } catch (e) {
    linking.value = null
    showError(e)
  }
}

// Wait until the person presses Start (Telegram) or sends the code to the bot.
function waitForLink(id: string, maxTries: number) {
  window.clearInterval(poll)
  let tries = 0
  poll = window.setInterval(async () => {
    await loadChannels()
    const now = chat.value?.channels.find((c) => c.id === id)
    if (now?.linked || ++tries > maxTries) {
      window.clearInterval(poll)
      linking.value = null
      const rest = { ...codes.value }
      delete rest[id]
      codes.value = rest
      if (now?.linked) message.success(t('settings.chat.linkedOk', { channel: now.label }))
    }
  }, 2000)
}

onBeforeUnmount(() => window.clearInterval(poll))

function unlink(c: ChatChannel) {
  dialog.warning({
    title: t('settings.chat.unlink'),
    content: t('settings.chat.unlinkConfirm', { channel: c.label }),
    positiveText: t('settings.chat.unlink'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        replace(await api.channelUnlink(c.id))
      } catch (e) {
        showError(e)
      }
    },
  })
}

async function testMessage(c: ChatChannel) {
  try {
    await api.channelTest(c.id)
    message.success(t('settings.chat.testSent'))
  } catch (e) {
    showError(e)
  }
}

async function setNotify(c: ChatChannel, value: boolean) {
  try {
    replace(await api.channelNotify(c.id, value))
  } catch (e) {
    showError(e)
  }
}

async function setPref(key: string, value: boolean) {
  if (!chat.value) return
  chat.value.notify_prefs = { ...chat.value.notify_prefs, [key]: value }
  try {
    await api.preferences({ notify_prefs: { [key]: value } })
  } catch (e) {
    showError(e)
  }
}

// ---- calendar ------------------------------------------------------------------
const cal = ref<CalendarLinks | null>(null)

function resetCalendar() {
  dialog.warning({
    title: t('settings.cal.reset'),
    content: t('settings.cal.resetConfirm'),
    positiveText: t('settings.cal.reset'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      cal.value = await api.calendarReset().catch((e) => (showError(e), cal.value))
    },
  })
}

onMounted(async () => {
  await loadChannels()
  cal.value = await api.calendar().catch(() => null)
})

const themes: { value: ThemeMode; icon: typeof Sun }[] = [
  { value: 'system', icon: Monitor },
  { value: 'light', icon: Sun },
  { value: 'dark', icon: Moon },
]
</script>

<template>
  <div class="page narrow">
    <div class="page-header">
      <div>
        <h1>{{ t('settings.title') }}</h1>
      </div>
    </div>

    <section class="card">
      <div class="card-header"><h3 class="card-title">{{ t('settings.account') }}</h3></div>
      <dl class="card-pad dl">
        <dt>{{ t('auth.username') }}</dt>
        <dd class="strong">{{ session.user?.username }}</dd>
        <dt>{{ t('settings.role') }}</dt>
        <dd>{{ session.user?.is_admin ? t('common.admin') : t('settings.member') }}</dd>
        <dt>{{ t('settings.memberSince') }}</dt>
        <dd>{{ formatDateTime(session.user?.created_at) }}</dd>
      </dl>
    </section>

    <section class="card">
      <div class="card-header"><h3 class="card-title">{{ t('settings.appearance') }}</h3></div>
      <div class="card-pad prefs">
        <div class="pref-row">
          <span class="muted">{{ t('settings.language') }}</span>
          <NRadioGroup :value="locale" size="small" @update:value="(v: string) => setLocale(v as Locale)">
            <NRadioButton value="zh-CN">简体中文</NRadioButton>
            <NRadioButton value="en">English</NRadioButton>
          </NRadioGroup>
        </div>
        <div class="pref-row">
          <span class="muted">{{ t('settings.theme') }}</span>
          <NRadioGroup :value="themeMode" size="small" @update:value="(v: ThemeMode) => setThemeMode(v)">
            <NRadioButton v-for="th in themes" :key="th.value" :value="th.value">
              <span class="theme-opt"><component :is="th.icon" :size="13" />{{ t(`settings.themes.${th.value}`) }}</span>
            </NRadioButton>
          </NRadioGroup>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title tg-title"><MessageCircle :size="15" /> {{ t('settings.chat.title') }}</h3>
          <div class="card-sub">{{ t('settings.chat.subtitle') }}</div>
        </div>
      </div>
      <div class="card-pad">
        <p v-if="chat && !chat.channels.length" class="muted no-margin">{{ t('settings.chat.none') }}</p>
        <div v-for="c in chat?.channels ?? []" :key="c.id" class="ch-row" :data-channel="c.id">
          <div class="tg-row">
            <div class="ch-main">
              <div class="ch-name">
                <Send v-if="c.id === 'telegram'" :size="13" />
                {{ c.label }}
                <span v-if="c.linked" class="badge ok">{{ t('settings.chat.linked') }}</span>
              </div>
              <div class="muted small">
                <template v-if="c.linked">{{ t('settings.chat.linkedAs', { name: c.account || '—' }) }}</template>
                <template v-else-if="!c.receives">{{ t('settings.chat.noReceive') }}</template>
                <template v-else>{{ t('settings.chat.notLinked') }}</template>
                <span v-if="c.bot_name" class="faint"> · {{ c.id === 'telegram' ? '@' + c.bot_name : c.bot_name }}</span>
              </div>
            </div>
            <span class="spacer" />
            <template v-if="c.linked">
              <label class="notify-switch">
                <span class="faint small">{{ t('settings.chat.notify') }}</span>
                <NSwitch :value="c.notify" size="small" @update:value="(v: boolean) => setNotify(c, v)" />
              </label>
              <NButton size="small" secondary @click="testMessage(c)">{{ t('settings.chat.test') }}</NButton>
              <NButton size="small" quaternary type="error" @click="unlink(c)">{{ t('settings.chat.unlink') }}</NButton>
            </template>
            <NButton
              v-else
              size="small"
              type="primary"
              :disabled="!c.receives"
              :loading="linking === c.id && c.link_kind === 'url'"
              @click="link(c)"
            >
              {{ t('settings.chat.link') }}
            </NButton>
          </div>
          <div v-if="codes[c.id] && !c.linked" class="code-box">
            <div>{{ t('settings.chat.codeHint', { channel: c.label, bot: c.bot_name || c.label }) }}</div>
            <CodeBlock :code="`bind ${codes[c.id].code}`" inline />
            <div class="faint small">
              {{ t('settings.chat.codeExpires', { time: formatDateTime(codes[c.id].expires_at) }) }}
              <a v-if="codes[c.id].link_url" :href="codes[c.id].link_url ?? undefined" target="_blank" rel="noopener">
                · {{ t('settings.chat.openApp', { channel: c.label }) }}
              </a>
            </div>
          </div>
        </div>
        <div v-if="chat && anyLinked" class="pref-grid">
          <div class="pref-title faint small">{{ t('settings.chat.prefsTitle') }}</div>
          <label v-for="k in PREF_KEYS" :key="k" class="pref-item">
            <div>
              <div>{{ t(`settings.tg.prefs.${k}`) }}</div>
              <div class="faint small">{{ t(`settings.tg.prefs.${k}_desc`) }}</div>
            </div>
            <NSwitch :value="chat.notify_prefs[k] ?? true" size="small" @update:value="(v: boolean) => setPref(k, v)" />
          </label>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title">{{ t('settings.cal.title') }}</h3>
          <div class="card-sub">{{ t('settings.cal.subtitle') }}</div>
        </div>
        <NButton size="small" quaternary @click="resetCalendar">{{ t('settings.cal.reset') }}</NButton>
      </div>
      <div v-if="cal" class="card-pad cal">
        <div class="label">{{ t('settings.cal.mine') }}</div>
        <CodeBlock :code="cal.mine_url" inline />
        <div class="label">{{ t('settings.cal.all') }}</div>
        <CodeBlock :code="cal.all_url" inline />
        <p class="faint small no-margin">{{ t('settings.cal.hint') }}</p>
      </div>
    </section>

    <section class="card">
      <div class="card-header"><h3 class="card-title">{{ t('settings.changePassword') }}</h3></div>
      <NForm class="card-pad" :show-require-mark="false" @submit.prevent="changePassword">
        <NFormItem :label="t('settings.oldPassword')">
          <NInput v-model:value="pw.old" type="password" show-password-on="click" :input-props="{ autocomplete: 'current-password' }" />
        </NFormItem>
        <NFormItem :label="t('settings.newPassword')" :feedback="t('auth.passwordRule')">
          <NInput v-model:value="pw.next" type="password" show-password-on="click" :input-props="{ autocomplete: 'new-password' }" />
        </NFormItem>
        <NButton type="primary" attr-type="submit" class="save-btn" :loading="saving" :disabled="!pw.old || pw.next.length < 6">
          {{ t('common.save') }}
        </NButton>
      </NForm>
    </section>

    <section class="card">
      <div class="card-header"><h3 class="card-title">{{ t('settings.about') }}</h3></div>
      <dl class="card-pad dl">
        <dt>{{ t('settings.version') }}</dt>
        <dd class="mono">SlotServe {{ session.config?.version }}</dd>
        <dt>{{ t('settings.apiDocs') }}</dt>
        <dd><a href="/api/docs" target="_blank" rel="noopener">/api/docs</a></dd>
        <dt>GitHub</dt>
        <dd><a href="https://github.com/PelerYuan/slotserve" target="_blank" rel="noopener">github.com/PelerYuan/slotserve</a></dd>
      </dl>
    </section>
  </div>
</template>

<style scoped>
.page.narrow > * {
  max-width: 760px;
}
.card + .card {
  margin-top: 16px;
}
.dl {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 12px 16px;
  margin: 0;
}
.dl dt {
  color: var(--text-3);
}
.dl dd {
  margin: 0;
}
.strong {
  font-weight: 600;
}
.save-btn {
  margin-top: 8px;
}
.prefs {
  display: grid;
  gap: 14px;
}
.pref-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.theme-opt {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.tg-title {
  display: flex;
  align-items: center;
  gap: 7px;
}
.tg-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.pref-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 24px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.pref-title {
  grid-column: 1 / -1;
}
.ch-row + .ch-row {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}
.ch-main {
  min-width: 0;
}
.ch-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.notify-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: 4px;
  cursor: pointer;
}
.code-box {
  display: grid;
  gap: 6px;
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--surface-muted);
}
.pref-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  cursor: pointer;
}
.small {
  font-size: 12px;
}
.no-margin {
  margin: 0;
}
.cal {
  display: grid;
  gap: 8px;
}
.cal .label + * {
  margin-bottom: 6px;
}
@media (max-width: 640px) {
  .pref-grid {
    grid-template-columns: 1fr;
  }
  .dl {
    grid-template-columns: 120px 1fr;
  }
}
</style>
