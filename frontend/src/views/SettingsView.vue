<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NInput, NRadioButton, NRadioGroup } from 'naive-ui'
import { api } from '@/api/client'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { setLocale, type Locale } from '@/i18n'
import { formatDateTime } from '@/utils/format'

const { t, locale } = useI18n()
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
      <div class="card-header"><h3 class="card-title">{{ t('settings.language') }}</h3></div>
      <div class="card-pad">
        <NRadioGroup :value="locale" @update:value="(v: string) => setLocale(v as Locale)">
          <NRadioButton value="zh-CN">简体中文</NRadioButton>
          <NRadioButton value="en">English</NRadioButton>
        </NRadioGroup>
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
</style>
