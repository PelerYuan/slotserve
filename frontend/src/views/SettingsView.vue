<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NInput, NRadioButton, NRadioGroup, NSwitch, useDialog } from 'naive-ui'
import { Monitor, Moon, Send, Sun } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { CalendarLinks, TelegramInfo } from '@/api/types'
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

// ---- telegram ----------------------------------------------------------------
const tg = ref<TelegramInfo | null>(null)
const linking = ref(false)
let poll: number | undefined

const PREF_KEYS = computed(() => {
  const keys = ['reminders', 'model_ready', 'booking_changes', 'waitlist', 'downloads']
  return session.user?.is_admin ? [...keys, 'admin_alerts'] : keys
})

async function loadTelegram() {
  tg.value = await api.telegram().catch(() => tg.value)
}

async function link() {
  linking.value = true
  try {
    const res = await api.telegramLink()
    if (res.link_url) window.open(res.link_url, '_blank', 'noopener')
    // Wait for the user to press Start in Telegram.
    window.clearInterval(poll)
    let tries = 0
    poll = window.setInterval(async () => {
      await loadTelegram()
      if (tg.value?.linked || ++tries > 60) {
        window.clearInterval(poll)
        linking.value = false
        if (tg.value?.linked) message.success(t('settings.tg.linkedOk'))
      }
    }, 2000)
  } catch (e) {
    linking.value = false
    showError(e)
  }
}

function unlink() {
  dialog.warning({
    title: t('settings.tg.unlink'),
    content: t('settings.tg.unlinkConfirm'),
    positiveText: t('settings.tg.unlink'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      tg.value = await api.telegramUnlink().catch((e) => (showError(e), tg.value))
    },
  })
}

async function testMessage() {
  try {
    await api.telegramTest()
    message.success(t('settings.tg.testSent'))
  } catch (e) {
    showError(e)
  }
}

async function setPref(key: string, value: boolean) {
  if (!tg.value) return
  tg.value.notify_prefs = { ...tg.value.notify_prefs, [key]: value }
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
  await loadTelegram()
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
          <h3 class="card-title tg-title"><Send :size="15" /> Telegram</h3>
          <div class="card-sub">{{ t('settings.tg.subtitle') }}</div>
        </div>
        <span v-if="tg?.linked" class="badge ok">{{ t('settings.tg.linked') }}</span>
      </div>
      <div class="card-pad">
        <p v-if="!tg?.enabled" class="muted no-margin">{{ t('settings.tg.disabled') }}</p>
        <template v-else>
          <div class="tg-row">
            <span v-if="tg.linked" class="muted">
              {{ t('settings.tg.linkedAs', { name: tg.telegram_username ? '@' + tg.telegram_username : '—' }) }}
              <span v-if="tg.bot_username" class="faint"> · @{{ tg.bot_username }}</span>
            </span>
            <span v-else class="muted">{{ t('settings.tg.notLinked', { bot: tg.bot_username ? '@' + tg.bot_username : '' }) }}</span>
            <span class="spacer" />
            <template v-if="tg.linked">
              <NButton size="small" secondary @click="testMessage">{{ t('settings.tg.test') }}</NButton>
              <NButton size="small" quaternary type="error" @click="unlink">{{ t('settings.tg.unlink') }}</NButton>
            </template>
            <NButton v-else size="small" type="primary" :loading="linking" @click="link">{{ t('settings.tg.link') }}</NButton>
          </div>
          <div v-if="tg.linked" class="pref-grid">
            <label v-for="k in PREF_KEYS" :key="k" class="pref-item">
              <div>
                <div>{{ t(`settings.tg.prefs.${k}`) }}</div>
                <div class="faint small">{{ t(`settings.tg.prefs.${k}_desc`) }}</div>
              </div>
              <NSwitch :value="tg.notify_prefs[k] ?? true" size="small" @update:value="(v: boolean) => setPref(k, v)" />
            </label>
          </div>
        </template>
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
