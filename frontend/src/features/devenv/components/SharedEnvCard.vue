<script setup lang="ts">
// The shared environment of an idle lane: everyone can join it (an account of their own, SSH keys only, R1-10).
// Not joined: a Join button (with the key step inline when you have no key yet); joined: how to log in, Leave.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useDialog, useMessage } from 'naive-ui'
import { LogIn, Users } from 'lucide-vue-next'
import { api, ApiError } from '@/api/client'
import DevEnvPorts from '@/components/devenv/DevEnvPorts.vue'
import { session as me } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { devenvApi, type DevSessionX } from '../api'
import ConnectInfo from './ConnectInfo.vue'
import SshCredentials from './SshCredentials.vue'

const props = defineProps<{ session: DevSessionX; alias: string; sshHost: string; hasKeys: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!me.user?.is_admin)
const s = computed(() => props.session)
const busy = ref(false)
const needKey = ref(false)

async function join() {
  if (!s.value.lane_id) return
  busy.value = true
  try {
    await devenvApi.join(s.value.lane_id)
    needKey.value = false
    message.success(t('devenvUi.shared.joined'))
    emit('changed')
  } catch (e) {
    if (e instanceof ApiError && e.code === 'ssh_key_required') needKey.value = true
    else showError(e)
  } finally {
    busy.value = false
  }
}
function leave() {
  if (!s.value.lane_id) return
  dialog.warning({
    title: t('devenvUi.shared.leave'),
    content: t('devenvUi.shared.leaveConfirm', { name: s.value.lane_name ?? '' }),
    positiveText: t('devenvUi.shared.leave'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await devenvApi.leave(s.value.lane_id as number)
        emit('changed')
      } catch (e) {
        showError(e)
      }
    },
  })
}
async function keyAdded() {
  await join()
}
async function restart() {
  try {
    await api.devenv.restart(s.value.id)
    message.success(t('devenv.restarting'))
    emit('changed')
  } catch (e) {
    showError(e)
  }
}
const stateTone: Record<string, string> = { running: 'ok', failed: 'danger', pulling: 'warn', starting: 'warn' }
</script>

<template>
  <article class="card env" data-kind="shared" :data-lane="s.lane_id" data-test="shared-env">
    <div class="row wrap head">
      <Users :size="15" class="faint" />
      <span class="strong">{{ s.lane_name }}</span>
      <span class="badge brand">{{ t('devenv.sharedBadge') }}</span>
      <span class="badge" :class="stateTone[s.state]">{{ t(`devenv.state.${s.state}`) }}</span>
      <span v-if="s.joined" class="badge ok">{{ t('devenvUi.shared.member') }}</span>
      <span class="spacer" />
      <span class="faint small">{{ s.template_name ?? s.image }}</span>
    </div>
    <p class="muted small">{{ s.joined ? t('devenvUi.shared.hintJoined') : t('devenvUi.shared.hint') }}</p>

    <div v-if="s.state === 'pulling'" class="progress">
      <div class="bar"><span :style="{ width: `${s.progress ?? 3}%` }" /></div>
      <div class="faint small">{{ t('devenv.pulling', { pct: s.progress !== null ? Math.round(s.progress) : '…' }) }}</div>
    </div>

    <template v-if="s.joined">
      <ConnectInfo v-if="s.state === 'running' && s.ssh_command" :session="s" :alias="alias" :ssh-host="sshHost" />
      <p v-else-if="s.state === 'running'" class="faint small">{{ t('devenvUi.shared.accountSoon') }}</p>
      <DevEnvPorts v-if="s.state === 'running'" :session="s" :can-manage="false" />
    </template>
    <template v-else>
      <div class="row wrap actions">
        <NButton type="primary" size="small" :loading="busy" data-test="shared-join" @click="join">
          <template #icon><LogIn :size="14" /></template>
          {{ t('devenvUi.shared.join') }}
        </NButton>
        <span v-if="!hasKeys" class="faint small">{{ t('devenvUi.shared.keyNeeded') }}</span>
      </div>
      <SshCredentials v-if="needKey" :password-login="false" keys-only @done="keyAdded" />
      <p v-if="s.host_key_fingerprint" class="faint small">
        {{ t('devenvUi.connect.hostKey') }} <span class="mono fp">{{ s.host_key_fingerprint }}</span>
      </p>
    </template>

    <div v-if="isAdmin && s.error" class="failure"><pre class="mono small">{{ s.error }}</pre></div>
    <div class="row wrap actions">
      <NButton v-if="s.joined" size="small" quaternary data-test="shared-leave" @click="leave">{{ t('devenvUi.shared.leave') }}</NButton>
      <NButton v-if="isAdmin && (s.state === 'running' || s.state === 'failed')" size="small" quaternary @click="restart">
        {{ s.state === 'failed' ? t('devenv.retry') : t('devenv.restart') }}
      </NButton>
    </div>
  </article>
</template>

<style scoped>
.env {
  display: grid;
  gap: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
  min-width: 0;
}
.head {
  gap: 8px;
}
p {
  margin: 0;
}
.actions {
  gap: 8px;
  align-items: center;
}
.actions:empty {
  display: none;
}
.fp {
  overflow-wrap: anywhere;
}
.progress .bar {
  height: 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: hidden;
  margin-bottom: 6px;
}
.progress .bar span {
  display: block;
  height: 100%;
  background: var(--brand);
}
.failure pre {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #0f1117;
  color: #fca5a5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
