<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NModal } from 'naive-ui'
import { ShieldAlert } from 'lucide-vue-next'
import { setSudoHandler } from '@/api/client'
import { securityApi } from '@/features/security/api'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { askForSudo, finishSudo, sudoPrompt } from './state'

// Mounted once in App.vue: any request answered with 403 `sudo_required` opens this dialog, and the request is
// repeated after the password was accepted (api/client.ts).
const { t } = useI18n()
const { describe } = useErrorMessage()

const password = ref('')
const error = ref('')
const busy = ref(false)
const input = ref<InstanceType<typeof NInput> | null>(null)

onMounted(() => setSudoHandler(askForSudo))
onBeforeUnmount(() => finishSudo(false))

watch(
  () => sudoPrompt.open,
  async (open) => {
    if (!open) return
    password.value = ''
    error.value = ''
    await nextTick()
    input.value?.focus()
  },
)

async function confirm() {
  if (!password.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await securityApi.enterSudo(password.value)
    password.value = ''
    finishSudo(true)
  } catch (e) {
    error.value = describe(e)
  } finally {
    busy.value = false
  }
}

function onShow(show: boolean) {
  if (!show && sudoPrompt.open) finishSudo(false)
}
</script>

<template>
  <NModal
    :show="sudoPrompt.open"
    preset="card"
    :bordered="false"
    :mask-closable="false"
    :title="t('security.sudoTitle')"
    class="sudo-modal"
    style="width: 400px; max-width: calc(100vw - 32px)"
    data-testid="sudo-prompt"
    @update:show="onShow"
  >
    <div class="sudo-intro">
      <ShieldAlert :size="18" class="sudo-icon" />
      <p>{{ t('security.sudoIntro') }}</p>
    </div>
    <form @submit.prevent="confirm">
      <input type="text" name="username" autocomplete="username" :value="session.user?.username ?? ''" hidden />
      <NInput
        ref="input"
        v-model:value="password"
        type="password"
        show-password-on="click"
        :placeholder="t('ph.currentPassword')"
        :input-props="{ autocomplete: 'current-password', name: 'password' }"
        @keyup.enter="confirm"
      />
      <div v-if="error" class="sudo-error">{{ error }}</div>
    </form>
    <template #footer>
      <div class="sudo-footer">
        <NButton @click="finishSudo(false)">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!password" :loading="busy" @click="confirm">
          {{ t('sudo.confirm') }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.sudo-intro {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-bottom: 14px;
  color: var(--text-2);
}
.sudo-intro p {
  margin: 0;
}
.sudo-icon {
  flex: none;
  margin-top: 2px;
  color: var(--warn);
}
.sudo-error {
  margin-top: 10px;
  color: var(--danger);
  font-size: 13px;
}
.sudo-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
