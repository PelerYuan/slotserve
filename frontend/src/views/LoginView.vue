<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NInput } from 'naive-ui'
import { CalendarClock, Moon, Plug, RefreshCw, Sun } from 'lucide-vue-next'
import BrandMark from '@/components/BrandMark.vue'
import AuthShowcase from '@/components/AuthShowcase.vue'
import { api, ApiError } from '@/api/client'
import { securityApi } from '@/features/security/api'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { setLocale } from '@/i18n'
import { isDark, setThemeMode } from '@/composables/theme'

const { t, locale } = useI18n()
const router = useRouter()
const route = useRoute()
const { describe } = useErrorMessage()

const mode = ref<'login' | 'register'>('login')
const form = reactive({ username: '', password: '', invite: '', setupCode: '' })
const busy = ref(false)
const error = ref('')
const shake = ref(0)

// No admin yet: the first account signs up with the setup code from the server log (not the invite code).
const setupRequired = ref(false)
const registrationOpen = computed(() => setupRequired.value || session.config?.registration_open !== false)

async function loadSetupState() {
  try {
    setupRequired.value = (await securityApi.setupState()).setup_required
  } catch {
    setupRequired.value = false
  }
  if (setupRequired.value) mode.value = 'register'
}
onMounted(loadSetupState)

const canSubmit = computed(
  () =>
    form.username.trim() &&
    form.password &&
    (mode.value === 'login' || (setupRequired.value ? form.setupCode.trim() : form.invite.trim())),
)

async function submit() {
  if (!canSubmit.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    session.user =
      mode.value === 'login'
        ? await api.login(form.username.trim(), form.password)
        : await securityApi.register(
            form.username.trim(),
            form.password,
            setupRequired.value ? { setup_code: form.setupCode.trim() } : { invite_code: form.invite.trim() },
          )
    const next =
      typeof route.query.next === 'string' && route.query.next.startsWith('/') && !route.query.next.startsWith('//')
        ? route.query.next
        : '/'
    // a dev environment's web interface is served by the server (it hands over to the web port), not the app
    if (/^\/devenv\/\d+\/port\//.test(next)) window.location.assign(next)
    else router.replace(next)
  } catch (e) {
    error.value = describe(e)
    shake.value++
    // someone else finished the setup meanwhile, or it is needed after all
    if (e instanceof ApiError && ['setup_code_required', 'invalid_invite_code'].includes(e.code)) loadSetupState()
  } finally {
    busy.value = false
  }
}

function setMode(m: 'login' | 'register') {
  mode.value = m
  error.value = ''
}
</script>

<template>
  <div class="auth">
    <section class="hero">
      <div class="aurora" aria-hidden="true">
        <span class="blob b1" />
        <span class="blob b2" />
        <span class="blob b3" />
      </div>
      <div class="dots" aria-hidden="true" />
      <div class="hero-brand"><BrandMark class="rise" /></div>
      <div class="hero-body">
        <h2 class="rise" style="--d: 0.05s">{{ t('app.tagline') }}</h2>
        <p class="rise" style="--d: 0.1s">{{ t('auth.subtitle') }}</p>
        <ul>
          <li class="rise" style="--d: 0.15s">
            <span class="ic"><CalendarClock :size="16" :stroke-width="1.9" /></span>{{ t('auth.feature1') }}
          </li>
          <li class="rise" style="--d: 0.2s">
            <span class="ic"><RefreshCw :size="16" :stroke-width="1.9" /></span>{{ t('auth.feature2') }}
          </li>
          <li class="rise" style="--d: 0.25s">
            <span class="ic"><Plug :size="16" :stroke-width="1.9" /></span>{{ t('auth.feature3') }}
          </li>
        </ul>
        <AuthShowcase class="rise showcase" style="--d: 0.35s" />
      </div>
    </section>

    <section class="panel">
      <div class="lang">
        <button :class="{ on: locale === 'zh-CN' }" @click="setLocale('zh-CN')">中文</button>
        <span>/</span>
        <button :class="{ on: locale === 'en' }" @click="setLocale('en')">EN</button>
        <button
          class="theme-toggle"
          :aria-label="t(`settings.themes.${isDark ? 'light' : 'dark'}`)"
          :title="t(`settings.themes.${isDark ? 'light' : 'dark'}`)"
          @click="setThemeMode(isDark ? 'light' : 'dark')"
        >
          <Sun v-if="isDark" :size="15" />
          <Moon v-else :size="15" />
        </button>
      </div>
      <div class="form-wrap">
        <BrandMark class="mobile-brand rise" />
        <div class="title rise" style="--d: 0.05s">
          <Transition name="title" mode="out-in">
            <h1 :key="mode + setupRequired">
              {{
                mode === 'login' ? t('auth.welcome') : setupRequired ? t('security.setupTitle') : t('auth.createAccount')
              }}
            </h1>
          </Transition>
        </div>
        <p v-if="setupRequired" class="setup faint rise" style="--d: 0.08s">{{ t('security.setupIntro') }}</p>
        <div v-if="registrationOpen" class="seg rise" style="--d: 0.1s" role="tablist">
          <span class="pill" :class="mode" />
          <button role="tab" :aria-selected="mode === 'login'" :class="{ on: mode === 'login' }" @click="setMode('login')">
            {{ t('auth.login') }}
          </button>
          <button
            role="tab"
            :aria-selected="mode === 'register'"
            :class="{ on: mode === 'register' }"
            @click="setMode('register')"
          >
            {{ t('auth.register') }}
          </button>
        </div>
        <NForm class="form rise" style="--d: 0.15s" :show-require-mark="false" @submit.prevent="submit">
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
          <div class="collapse" :class="{ open: mode === 'register' }">
            <div>
              <NFormItem v-if="setupRequired" :label="t('security.setupCode')">
                <NInput
                  v-model:value="form.setupCode"
                  size="large"
                  :placeholder="t('security.setupCodePlaceholder')"
                  :disabled="mode !== 'register'"
                  :input-props="{ autocomplete: 'off', spellcheck: false }"
                  @keyup.enter="submit"
                />
              </NFormItem>
              <NFormItem v-else :label="t('auth.inviteCode')">
                <NInput
                  v-model:value="form.invite"
                  size="large"
                  :disabled="mode !== 'register'"
                  @keyup.enter="submit"
                />
              </NFormItem>
            </div>
          </div>
          <Transition name="err">
            <div v-if="error" :key="shake" class="error">{{ error }}</div>
          </Transition>
          <NButton
            class="submit"
            type="primary"
            size="large"
            block
            attr-type="submit"
            :loading="busy"
            :disabled="!canSubmit"
          >
            {{ mode === 'login' ? t('auth.login') : t('auth.register') }}
          </NButton>
        </NForm>
        <p v-if="!registrationOpen" class="closed faint rise" style="--d: 0.2s">{{ t('auth.registrationClosed') }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  min-height: 100vh;
  background: var(--surface);
}

/* ---------- hero ---------- */
.hero {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 40px clamp(56px, 6vw, 128px) 40px 56px;
  background: var(--hero-bg);
  border-right: 1px solid var(--border);
  isolation: isolate;
}
.aurora {
  position: absolute;
  inset: 0;
  z-index: -2;
  filter: blur(60px);
}
.blob {
  position: absolute;
  border-radius: 50%;
  opacity: 0.55;
  will-change: transform;
}
.b1 {
  width: 520px;
  height: 520px;
  left: -120px;
  top: -140px;
  background: #c7d2fe;
  animation: drift1 22s ease-in-out infinite;
}
.b2 {
  width: 460px;
  height: 460px;
  right: -140px;
  top: 30%;
  background: #ddd6fe;
  animation: drift2 26s ease-in-out infinite;
}
.b3 {
  width: 420px;
  height: 420px;
  left: 20%;
  bottom: -200px;
  background: #bae6fd;
  opacity: 0.45;
  animation: drift3 30s ease-in-out infinite;
}
.dots {
  position: absolute;
  inset: 0;
  z-index: -1;
  background-image: radial-gradient(rgba(79, 70, 229, 0.16) 1px, transparent 1px);
  background-size: 22px 22px;
  mask-image: radial-gradient(ellipse 70% 60% at 40% 40%, #000 20%, transparent 75%);
  -webkit-mask-image: radial-gradient(ellipse 70% 60% at 40% 40%, #000 20%, transparent 75%);
}
/* The logo lines up with the hero text instead of sitting in the far corner. */
.hero-brand {
  width: 100%;
  max-width: 480px;
  margin-left: auto;
}
/* On wide screens both columns keep their content next to the divider, so the page reads as one block. */
.hero-body {
  margin: auto 0 auto auto;
  width: 100%;
  max-width: 480px;
  padding: 32px 0 8px;
}
.hero h2 {
  font-size: 36px;
  line-height: 1.12;
  letter-spacing: -0.035em;
  font-weight: 700;
  margin: 0 0 14px;
  background: linear-gradient(120deg, var(--heading-from) 30%, var(--brand) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.hero p {
  color: var(--text-2);
  font-size: 15px;
  margin: 0 0 24px;
}
.hero ul {
  list-style: none;
  padding: 0;
  margin: 0 0 36px;
  display: grid;
  gap: 12px;
}
.hero li {
  display: flex;
  gap: 12px;
  align-items: center;
  color: var(--text);
  font-weight: 500;
}
.ic {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: 8px;
  color: var(--brand);
  background: var(--glass);
  box-shadow: 0 1px 2px rgba(40, 40, 110, 0.08), 0 0 0 1px rgba(79, 70, 229, 0.08);
}

/* ---------- panel ---------- */
.panel {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding: 48px 24px 48px clamp(48px, 6vw, 128px);
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
  transition: color 0.2s;
}
.lang button:hover,
.lang button.on {
  color: var(--text);
}
.theme-toggle {
  display: inline-flex;
  align-items: center;
  margin-left: 10px;
}
.lang button.on {
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
.title {
  min-height: 36px;
  margin-bottom: 20px;
}
h1 {
  font-size: 26px;
  font-weight: 680;
  letter-spacing: -0.025em;
  margin: 0;
}
.seg {
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  padding: 4px;
  margin-bottom: 24px;
  border-radius: 10px;
  background: var(--seg-bg);
}
.seg button {
  position: relative;
  z-index: 1;
  border: none;
  background: none;
  font: inherit;
  font-weight: 550;
  color: var(--text-2);
  padding: 7px 0;
  cursor: pointer;
  transition: color 0.25s;
}
.seg button.on {
  color: var(--text);
}
.pill {
  position: absolute;
  top: 4px;
  bottom: 4px;
  left: 4px;
  width: calc(50% - 4px);
  border-radius: 7px;
  background: var(--seg-pill);
  box-shadow: 0 1px 3px rgba(20, 20, 50, 0.1), 0 1px 1px rgba(20, 20, 50, 0.04);
  transition: transform 0.35s cubic-bezier(0.22, 1, 0.36, 1);
}
.pill.register {
  transform: translateX(100%);
}
.setup {
  margin: -8px 0 20px;
  font-size: 13px;
  line-height: 1.55;
}
.closed {
  margin-top: 20px;
  text-align: center;
  font-size: 13px;
}
.collapse {
  display: grid;
  grid-template-rows: 0fr;
  opacity: 0;
  transition: grid-template-rows 0.35s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.25s;
}
.collapse > div {
  overflow: hidden;
}
.collapse.open {
  grid-template-rows: 1fr;
  opacity: 1;
}
.error {
  color: var(--danger);
  background: var(--danger-soft);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 16px;
  font-size: 13px;
  animation: shake 0.4s cubic-bezier(0.36, 0.07, 0.19, 0.97);
}
.submit {
  transition: transform 0.15s, box-shadow 0.25s;
}
.submit:not(:disabled):hover {
  transform: translateY(-1px);
  box-shadow: 0 8px 20px -8px rgba(79, 70, 229, 0.6);
}
.submit:not(:disabled):active {
  transform: translateY(0);
}

/* ---------- motion ---------- */
.rise {
  animation: rise 0.7s cubic-bezier(0.22, 1, 0.36, 1) both;
  animation-delay: var(--d, 0s);
}
.title-enter-active,
.title-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}
.title-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.title-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
.err-leave-active {
  transition: opacity 0.2s;
}
.err-leave-to {
  opacity: 0;
}
@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@keyframes shake {
  10%,
  90% {
    transform: translateX(-1px);
  }
  20%,
  80% {
    transform: translateX(2px);
  }
  30%,
  50%,
  70% {
    transform: translateX(-4px);
  }
  40%,
  60% {
    transform: translateX(4px);
  }
}
@keyframes drift1 {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(120px, 80px) scale(1.15);
  }
}
@keyframes drift2 {
  0%,
  100% {
    transform: translate(0, 0) scale(1);
  }
  50% {
    transform: translate(-140px, -60px) scale(0.9);
  }
}
@keyframes drift3 {
  0%,
  100% {
    transform: translate(0, 0);
  }
  50% {
    transform: translate(100px, -90px);
  }
}
@media (prefers-reduced-motion: reduce) {
  .blob,
  .rise,
  .error {
    animation: none;
  }
}

@media (min-width: 1700px) {
  .hero-body,
  .hero-brand {
    max-width: 540px;
  }
  .hero h2 {
    font-size: 42px;
  }
}
@media (max-width: 900px) {
  .panel {
    justify-content: center;
    padding: 48px 24px;
  }
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
