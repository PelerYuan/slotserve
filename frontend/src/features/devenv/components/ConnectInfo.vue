<script setup lang="ts">
// How to log in over SSH: the full command (works as is in PowerShell too), this environment's own ~/.ssh/config
// entry (a Host alias of its own, F10) and its own host key fingerprint (R1-07).
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import CodeBlock from '@/components/CodeBlock.vue'
import type { DevSessionX } from '../api'
import { sshConfig } from '../naming'

const props = defineProps<{ session: DevSessionX; alias: string; sshHost: string }>()
const { t } = useI18n()

const config = computed(() => sshConfig(props.alias, props.sshHost, props.session.ssh_port, props.session.ssh_user))
</script>

<template>
  <div v-if="session.ssh_command" class="connect" data-test="devenv-connect">
    <CodeBlock :code="session.ssh_command" inline />
    <p class="faint small">{{ t('devenvUi.connect.windows') }}</p>
    <details class="cfg">
      <summary class="small">{{ t('devenvUi.connect.config', { alias }) }}</summary>
      <p class="faint small">{{ t('devenvUi.connect.configHint', { alias }) }}</p>
      <CodeBlock :code="config" />
    </details>
    <p v-if="session.host_key_fingerprint" class="faint small fp" data-test="host-key">
      {{ t('devenvUi.connect.hostKey') }} <span class="mono">{{ session.host_key_fingerprint }}</span>
    </p>
  </div>
</template>

<style scoped>
.connect {
  display: grid;
  gap: 6px;
  min-width: 0;
}
p {
  margin: 0;
}
.cfg summary {
  cursor: pointer;
  color: var(--text-2);
}
.cfg p {
  margin: 6px 0;
}
.fp .mono {
  overflow-wrap: anywhere;
}
</style>
