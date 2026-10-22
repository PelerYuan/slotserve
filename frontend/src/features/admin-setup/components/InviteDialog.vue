<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NInputNumber, NModal, NRadioButton, NRadioGroup, NSwitch } from 'naive-ui'
import { Copy } from 'lucide-vue-next'
import { i18n } from '@/i18n'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDate } from '@/utils/format'
import { setupApi, type InviteCreated } from '../api'

// New invite link (F06): 7 days / 50 sign-ups by default, optionally with approval; afterwards the link (shown once)
// and a ready-to-post group message in English or Chinese.
const show = defineModel<boolean>('show', { required: true })
const emit = defineEmits<{ created: [invite: InviteCreated] }>()
const { t, locale } = useI18n()
const { showError, message } = useErrorMessage()

const form = reactive({ note: '', days: 7, maxUses: 50, approval: false, busy: false })
const created = ref<InviteCreated | null>(null)
const msgLang = ref<'en' | 'zh-CN'>('en')

watch(show, (open) => {
  if (!open) return
  Object.assign(form, { note: '', days: 7, maxUses: 50, approval: false, busy: false })
  created.value = null
  msgLang.value = locale.value === 'zh-CN' ? 'zh-CN' : 'en'
})

async function create() {
  form.busy = true
  try {
    created.value = await setupApi.createInvite({
      note: form.note.trim(),
      days: form.days,
      max_uses: form.maxUses,
      requires_approval: form.approval,
    })
    emit('created', created.value)
  } catch (e) {
    showError(e)
  } finally {
    form.busy = false
  }
}

const url = computed(() => (created.value ? `${window.location.origin}${created.value.path}` : ''))

const groupMessage = computed(() => {
  const inv = created.value
  if (!inv) return ''
  const tr = (key: string, params: Record<string, unknown> = {}) =>
    i18n.global.t(key, params, { locale: msgLang.value })
  const date = new Date(inv.expires_at).toLocaleDateString(msgLang.value, { month: 'long', day: 'numeric' })
  let text = tr('adminSetup.invites.groupMessage', { date, url: url.value })
  if (inv.requires_approval) text += `\n${tr('adminSetup.invites.groupMessageApproval')}`
  return text
})

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('adminSetup.invites.copied'))
  } catch {
    message.error(t('diag.copyFailed'))
  }
}
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :bordered="false"
    :title="created ? t('adminSetup.invites.createdTitle') : t('adminSetup.invites.createTitle')"
    style="width: 520px; max-width: calc(100vw - 32px)"
    data-testid="invite-dialog"
  >
    <template v-if="!created">
      <div class="fields">
        <label class="field">
          <span class="field-label">{{ t('adminSetup.invites.note') }}</span>
          <NInput v-model:value="form.note" :maxlength="64" :placeholder="t('adminSetup.invites.notePh')" />
        </label>
        <div class="two">
          <label class="field">
            <span class="field-label">{{ t('adminSetup.invites.days') }}</span>
            <NInputNumber v-model:value="form.days" :min="1" :max="90" :precision="0" :placeholder="t('adminSetup.invites.daysPh')">
              <template #suffix>{{ t('adminSetup.invites.daysUnit') }}</template>
            </NInputNumber>
          </label>
          <label class="field">
            <span class="field-label">{{ t('adminSetup.invites.maxUses') }}</span>
            <NInputNumber
              v-model:value="form.maxUses"
              :min="1"
              :max="1000"
              :precision="0"
              :placeholder="t('adminSetup.invites.maxUsesPh')"
            />
          </label>
        </div>
        <label class="switch-row">
          <div>
            <div>{{ t('adminSetup.invites.approval') }}</div>
            <div class="faint small">{{ t('adminSetup.invites.approvalHint') }}</div>
          </div>
          <NSwitch v-model:value="form.approval" />
        </label>
      </div>
    </template>
    <template v-else>
      <p class="muted intro">{{ t('adminSetup.invites.createdIntro') }}</p>
      <div class="field">
        <span class="field-label">{{ t('adminSetup.invites.link') }}</span>
        <div class="copy-row">
          <code class="mono link" data-testid="invite-url">{{ url }}</code>
          <NButton size="small" type="primary" @click="copy(url)">
            <template #icon><Copy :size="14" /></template>
            {{ t('adminSetup.invites.copyLink') }}
          </NButton>
        </div>
      </div>
      <div class="field msg">
        <div class="row">
          <span class="field-label">{{ t('adminSetup.invites.message') }}</span>
          <span class="spacer" />
          <NRadioGroup v-model:value="msgLang" size="small" :aria-label="t('adminSetup.invites.messageLang')">
            <NRadioButton value="en">English</NRadioButton>
            <NRadioButton value="zh-CN">中文</NRadioButton>
          </NRadioGroup>
        </div>
        <pre class="message" data-testid="invite-message">{{ groupMessage }}</pre>
        <div>
          <NButton size="small" secondary @click="copy(groupMessage)">
            <template #icon><Copy :size="14" /></template>
            {{ t('adminSetup.invites.copyMessage') }}
          </NButton>
        </div>
      </div>
      <p class="faint small">
        {{ t('adminSetup.invites.expires') }}: {{ formatDate(created.expires_at) }} ·
        {{ t('adminSetup.invites.maxUses') }}: {{ created.max_uses }}
      </p>
    </template>
    <template #footer>
      <div class="footer">
        <template v-if="!created">
          <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="form.busy" :disabled="!form.days || !form.maxUses" @click="create">
            {{ t('common.create') }}
          </NButton>
        </template>
        <NButton v-else type="primary" @click="show = false">{{ t('common.close') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.fields {
  display: grid;
  gap: 14px;
}
.field {
  display: grid;
  gap: 6px;
}
.field-label {
  font-size: 13px;
  font-weight: 500;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.switch-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}
.small {
  font-size: 12px;
}
.intro {
  margin: 0 0 14px;
}
.copy-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.link {
  flex: 1;
  min-width: 0;
  padding: 7px 10px;
  border-radius: 6px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.msg {
  margin-top: 16px;
}
.message {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  color: var(--code-text);
  font-family: var(--font);
  font-size: 13px;
  white-space: pre-wrap;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 520px) {
  .two {
    grid-template-columns: 1fr;
  }
  .copy-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
