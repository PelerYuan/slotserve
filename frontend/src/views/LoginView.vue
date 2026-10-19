<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NInput } from 'naive-ui'
import { CalendarClock, Plug, RefreshCw } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import { api } from '@/api/client'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { setLocale } from '@/i18n'

const { t, locale } = useI18n()
const router = useRouter()
const route = useRoute()
const { describe } = useErrorMessage()

const mode = ref<'login' | 'register'>('login')
const form = reactive({ username: '', password: '', invite: '' })
const busy = ref(false)
const error = ref('')

const canSubmit = computed(
  () => form.username.trim() && form.password && (mode.value === 'login' || form.invite.trim()),
)

async function submit() {
  if (!canSubmit.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    session.user =
      mode.value === 'login'
        ? await api.login(form.username.trim(), form.password)
        : await api.register(form.username.trim(), form.password, form.invite.trim())
    const next = typeof route.query.next === 'string' && route.query.next.startsWith('/') ? route.query.next : '/'
    router.replace(next)
  } catch (e) {
    error.value = describe(e)
  } finally {
    busy.value = false
  }
}

function toggle() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}
</script>

<template>
  <div class="auth">
    <section class="hero">
      <BrandMark />
      <div class="hero-body">
        <h2>{{ t('app.tagline') }}</h2>
        <p>{{ t('auth.subtitle') }}</p>
        <ul>
          <li><CalendarClock :size="18" :stroke-width="1.8" /><span>{{ t('auth.feature1') }}</span></li>
          <li><RefreshCw :size="18" :stroke-width="1.8" /><span>{{ t('auth.feature2') }}</span></li>
          <li><Plug :size="18" :stroke-width="1.8" /><span>{{ t('auth.feature3') }}</span></li>
        </ul>
      </div>
      <div class="slots" aria-hidden="true">
        <span v-for="i in 7" :key="i" :class="['slot', `s${i}`]" />
      </div>
    </section>

    <section class="panel">
      <div class="lang">
        <button :class="{ on: locale === 'zh-CN' }" @click="setLocale('zh-CN')">中文</button>
        <span>/</span>
        <button :class="{ on: locale === 'en' }" @click="setLocale('en')">EN</button>
      </div>
      <div class="form-wrap">
        <BrandMark class="mobile-brand" />
        <h1>{{ mode === 'login' ? t('auth.welcome') : t('auth.createAccount') }}</h1>
        <NForm class="form" :show-require-mark="false" @submit.prevent="submit">
          <NFormItem :label="t('auth.username')" :feedback="mode === 'register' ? t('auth.usernameRule') : undefined">
            <NInput v-model:value="form.username" size="large" autofocus :input-props="{ autocomplete: 'username' }" />
          </NFormItem>
          <NFormItem :label="t('auth.password')" :feedback="mode === 'register' ? t('auth.passwordRule') : undefined">
            <NInput
              v-model:value="form.password"
              type="password"
              size="large"
              show-password-on="click"
              :input-props="{ autocomplete: mode === 'login' ? 'current-password' : 'new-password' }"
              @keyup.enter="submit"
            />
          </NFormItem>
          <NFormItem v-if="mode === 'register'" :label="t('auth.inviteCode')">
            <NInput v-model:value="form.invite" size="large" @keyup.enter="submit" />
          </NFormItem>
          <div v-if="error" class="error">{{ error }}</div>
          <NButton type="primary" size="large" block attr-type="submit" :loading="busy" :disabled="!canSubmit">
            {{ mode === 'login' ? t('auth.login') : t('auth.register') }}
          </NButton>
        </NForm>
        <p class="switch">
          {{ mode === 'login' ? t('auth.noAccount') : t('auth.haveAccount') }}
          <a href="#" @click.prevent="toggle">{{ mode === 'login' ? t('auth.register') : t('auth.login') }}</a>
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  min-height: 100vh;
  background: var(--surface);
}
.hero {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 40px 48px;
  background: radial-gradient(1200px 600px at 0% 0%, #eef0ff 0%, transparent 60%),
    radial-gradient(900px 500px at 100% 100%, #f1efff 0%, transparent 55%), var(--bg);
  border-right: 1px solid var(--border);
}
.hero-body {
  margin: auto 0;
  max-width: 440px;
  position: relative;
  z-index: 1;
}
.hero h2 {
  font-size: 34px;
  line-height: 1.15;
  letter-spacing: -0.03em;
  font-weight: 680;
  margin: 0 0 14px;
}
.hero p {
  color: var(--text-2);
  font-size: 15px;
  margin: 0 0 28px;
}
.hero ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 14px;
}
.hero li {
  display: flex;
  gap: 12px;
  align-items: center;
  color: var(--text);
  font-weight: 500;
}
.hero li svg {
  color: var(--brand);
  flex: none;
}
.slots {
  position: absolute;
  right: -40px;
  bottom: 56px;
  width: 340px;
  display: grid;
  gap: 10px;
  opacity: 0.9;
}
.slot {
  height: 14px;
  border-radius: 7px;
  background: #e4e6fb;
}
.s2 {
  width: 70%;
  margin-left: 20%;
  background: var(--brand);
  opacity: 0.85;
}
.s3 {
  width: 50%;
}
.s4 {
  width: 80%;
  margin-left: 10%;
  background: #c7cbf7;
}
.s5 {
  width: 40%;
  margin-left: 45%;
}
.s6 {
  width: 65%;
  background: #a5abf3;
}
.s7 {
  width: 30%;
  margin-left: 30%;
}
.panel {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
}
.lang {
  position: absolute;
  top: 24px;
  right: 28px;
  display: flex;
  gap: 6px;
  color: var(--text-3);
  font-size: 13px;
}
.lang button {
  border: none;
  background: none;
  color: var(--text-3);
  cursor: pointer;
  font: inherit;
  padding: 0;
}
.lang button.on {
  color: var(--text);
  font-weight: 600;
}
.form-wrap {
  width: 100%;
  max-width: 360px;
}
.mobile-brand {
  display: none !important;
  margin-bottom: 28px;
}
h1 {
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.02em;
  margin: 0 0 28px;
}
.error {
  color: var(--danger);
  background: var(--danger-soft);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 16px;
  font-size: 13px;
}
.switch {
  margin-top: 24px;
  color: var(--text-2);
  text-align: center;
}

@media (max-width: 900px) {
  .auth {
    grid-template-columns: 1fr;
  }
  .hero {
    display: none;
  }
  .mobile-brand {
    display: inline-flex !important;
  }
}
</style>
