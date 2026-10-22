<script setup lang="ts">
// The SSH credentials step, inline where it is needed (F03): paste a public key (the main way), the two commands
// that make one on each system, a key pair made in the browser, and a password only while admins allow them.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NRadioButton, NRadioGroup } from 'naive-ui'
import { KeyRound } from 'lucide-vue-next'
import { api } from '@/api/client'
import CodeBlock from '@/components/CodeBlock.vue'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { canGenerateKeys, download, generateKeyPair } from '../keygen'

const props = defineProps<{ passwordLogin: boolean; keysOnly?: boolean }>()
const emit = defineEmits<{ done: [] }>()

const { t } = useI18n()
const { showError, message } = useErrorMessage()

const key = ref('')
const busy = ref(false)
const generated = ref<string | null>(null)
const canGenerate = ref(false)
const usePassword = ref(false)
const pw = ref({ a: '', b: '' })

type Os = 'windows' | 'mac' | 'linux'
const os = ref<Os>(/windows/i.test(navigator.userAgent) ? 'windows' : /mac/i.test(navigator.userAgent) ? 'mac' : 'linux')
const commands: Record<Os, string> = {
  windows: 'ssh-keygen -t ed25519\nGet-Content $env:USERPROFILE\\.ssh\\id_ed25519.pub | Set-Clipboard',
  mac: 'ssh-keygen -t ed25519\npbcopy < ~/.ssh/id_ed25519.pub',
  linux: 'ssh-keygen -t ed25519\ncat ~/.ssh/id_ed25519.pub',
}
const looksLikeKey = computed(() => /^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-|sk-)\S*\s+\S{20,}/.test(key.value.trim()))
const passwordOk = computed(() => pw.value.a.length >= 8 && pw.value.a === pw.value.b)

onMounted(async () => {
  canGenerate.value = await canGenerateKeys()
})

async function addKey(publicKey: string) {
  busy.value = true
  try {
    await api.addSshKey('', publicKey.trim())
    message.success(t('devenvUi.creds.keyAdded'))
    emit('done')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function generate() {
  busy.value = true
  try {
    const pair = await generateKeyPair('slotserve')
    download('slotserve_ed25519', pair.privateKey)
    generated.value = pair.publicKey
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
  if (generated.value) await addKey(generated.value)
}

async function savePassword() {
  busy.value = true
  try {
    await api.devenv.setPassword(pw.value.a)
    message.success(t('devenvUi.creds.passwordSet'))
    emit('done')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="creds" data-test="ssh-credentials">
    <div class="row head">
      <KeyRound :size="15" />
      <span class="strong">{{ t('devenvUi.creds.title') }}</span>
    </div>
    <p class="muted small">{{ keysOnly ? t('devenvUi.creds.introKeysOnly') : t('devenvUi.creds.intro') }}</p>

    <template v-if="!usePassword">
      <NInput
        v-model:value="key"
        type="textarea"
        class="mono"
        :autosize="{ minRows: 2, maxRows: 5 }"
        :placeholder="t('devenvUi.creds.keyPlaceholder')"
        data-test="ssh-key-input"
      />
      <div class="row wrap actions">
        <NButton type="primary" :loading="busy" :disabled="!looksLikeKey" data-test="ssh-key-save" @click="addKey(key)">
          {{ t('devenvUi.creds.addKey') }}
        </NButton>
        <span v-if="key.trim() && !looksLikeKey" class="small warn-text">{{ t('devenvUi.creds.notAKey') }}</span>
      </div>

      <details class="make">
        <summary class="small">{{ t('devenvUi.creds.noKey') }}</summary>
        <NRadioGroup v-model:value="os" size="small" class="tabs" data-test="ssh-os">
          <NRadioButton value="windows">{{ t('devenvUi.creds.os.windows') }}</NRadioButton>
          <NRadioButton value="mac">{{ t('devenvUi.creds.os.mac') }}</NRadioButton>
          <NRadioButton value="linux">{{ t('devenvUi.creds.os.linux') }}</NRadioButton>
        </NRadioGroup>
        <CodeBlock :code="commands[os]" />
        <p class="faint small">{{ t('devenvUi.creds.commandsHint') }}</p>
        <div v-if="canGenerate" class="gen">
          <NButton size="small" secondary :loading="busy" data-test="ssh-generate" @click="generate">
            {{ t('devenvUi.creds.generate') }}
          </NButton>
          <span class="faint small">{{ t('devenvUi.creds.generateHint') }}</span>
        </div>
        <p v-if="generated" class="small ok-text">{{ t('devenvUi.creds.generated') }}</p>
      </details>
    </template>

    <template v-else>
      <div class="pw">
        <NInput v-model:value="pw.a" type="password" show-password-on="click" :placeholder="t('devenvUi.creds.password')" />
        <NInput v-model:value="pw.b" type="password" show-password-on="click" :placeholder="t('devenvUi.creds.passwordAgain')" />
      </div>
      <div class="row wrap actions">
        <NButton type="primary" :loading="busy" :disabled="!passwordOk" @click="savePassword">
          {{ t('devenvUi.creds.setPassword') }}
        </NButton>
        <span class="faint small">{{ t('devenvUi.creds.passwordHint') }}</span>
      </div>
    </template>

    <button v-if="passwordLogin && !keysOnly" type="button" class="link small" @click="usePassword = !usePassword">
      {{ usePassword ? t('devenvUi.creds.useKey') : t('devenvUi.creds.usePassword') }}
    </button>
  </div>
</template>

<style scoped>
.creds {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-muted);
  min-width: 0;
}
.head {
  gap: 6px;
}
p {
  margin: 0;
}
.actions {
  gap: 10px;
  align-items: center;
}
.make summary {
  cursor: pointer;
  color: var(--text-2);
}
.make {
  display: grid;
  gap: 8px;
}
.make[open] {
  gap: 8px;
}
.tabs {
  margin: 8px 0 2px;
  display: flex;
  flex-wrap: wrap;
}
.gen {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 6px;
}
.pw {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.link {
  justify-self: start;
  background: none;
  border: 0;
  padding: 0;
  color: var(--brand);
  cursor: pointer;
}
.warn-text {
  color: var(--warn-text);
}
.ok-text {
  color: var(--ok-text);
}
</style>
