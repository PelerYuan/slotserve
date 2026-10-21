<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput } from 'naive-ui'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { AdminChannelStatus, AdminChannels } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { relativeFromNow } from '@/utils/format'

const emit = defineEmits<{ navigate: [tab: 'settings'] }>()
const { t } = useI18n()
const { showError, message } = useErrorMessage()

const data = ref<AdminChannels | null>(null)
const testing = ref<string | null>(null)
const announcement = ref('')
const sending = ref(false)
let timer: number | undefined

async function load() {
  try {
    data.value = await api.admin.channels()
  } catch (e) {
    showError(e)
  }
}
onMounted(() => {
  load()
  timer = window.setInterval(load, 10000)
})
onBeforeUnmount(() => window.clearInterval(timer))

function state(c: AdminChannelStatus): { key: string; cls: string } {
  if (!c.switched_on) return { key: 'off', cls: '' }
  if (!c.configured) return { key: 'incomplete', cls: 'warn' }
  if (c.connected) return { key: 'connected', cls: 'ok' }
  if (c.error) return { key: 'error', cls: 'danger' }
  return { key: 'connecting', cls: 'warn' }
}

const announceOn = computed(() => !!data.value?.group_events.includes('announcements'))
const anyWebhook = computed(() => !!data.value?.webhooks.some((w) => w.configured))

async function testWebhook(id: string) {
  testing.value = id
  try {
    const res = await api.admin.webhookTest(id)
    if (res.error) message.error(t('admin.channels.testFailed', { error: res.error }))
    else message.success(t('admin.channels.testSent'))
    await load()
  } catch (e) {
    showError(e)
  } finally {
    testing.value = null
  }
}

async function announce() {
  sending.value = true
  try {
    const res = await api.admin.announce(announcement.value)
    message.success(t('admin.channels.announced', { n: res.targets.length }))
    announcement.value = ''
  } catch (e) {
    showError(e)
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <p class="muted intro">
    {{ t('admin.channels.intro') }}
    <a href="#" @click.prevent="emit('navigate', 'settings')">{{ t('admin.channels.toSettings') }}</a>
  </p>

  <section class="card group">
    <div class="group-title">{{ t('admin.channels.bots') }}</div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.channels.platform') }}</th>
            <th>{{ t('admin.channels.state') }}</th>
            <th>{{ t('admin.channels.receive') }}</th>
            <th>{{ t('admin.channels.bot') }}</th>
            <th>{{ t('admin.channels.linkedPeople') }}</th>
            <th>{{ t('admin.channels.lastEvent') }}</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="c in data?.channels ?? []" :key="c.id">
            <tr :data-channel="c.id">
              <td class="strong">{{ c.label }}</td>
              <td>
                <span class="badge" :class="state(c).cls">{{ t(`admin.channels.states.${state(c).key}`) }}</span>
              </td>
              <td class="muted">{{ t(`admin.channels.modes.${c.receive_mode}`) }}</td>
              <td>{{ c.bot_name || '—' }}</td>
              <td>{{ c.linked_count }}</td>
              <td class="muted">{{ c.last_event_at ? relativeFromNow(c.last_event_at) : '—' }}</td>
            </tr>
            <tr v-if="c.enabled && (c.error || c.callback_url)" class="detail">
              <td colspan="6">
                <div v-if="c.error" class="err">{{ c.error }}</div>
                <div v-if="c.callback_url" class="callback">
                  <span class="muted small">
                    {{ c.receives ? t('admin.channels.callback') : t('admin.channels.callbackMissing') }}
                  </span>
                  <CodeBlock :code="c.callback_url" inline />
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card group">
    <div class="group-title">{{ t('admin.channels.webhooks') }}</div>
    <div class="card-pad">
      <p class="muted small no-margin">
        {{ t('admin.channels.groupEvents') }}:
        <strong v-if="data?.group_events.length">
          {{ data.group_events.map((k) => t(`admin.channels.kinds.${k}`)).join(', ') }}
        </strong>
        <span v-else>{{ t('admin.channels.noneSelected') }}</span>
      </p>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.channels.platform') }}</th>
            <th>{{ t('admin.channels.state') }}</th>
            <th>{{ t('admin.channels.lastOk') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="w in data?.webhooks ?? []" :key="w.id" :data-webhook="w.id">
            <td class="strong">{{ w.label }}</td>
            <td>
              <span v-if="!w.configured" class="badge">{{ t('admin.channels.states.off') }}</span>
              <span v-else-if="w.error" class="badge danger" :title="w.error">{{ t('admin.channels.states.error') }}</span>
              <span v-else class="badge ok">{{ t('admin.channels.states.ready') }}</span>
              <span v-if="w.signed" class="faint small"> · {{ t('admin.channels.signed') }}</span>
              <div v-if="w.error" class="err small">{{ w.error }}</div>
            </td>
            <td class="muted">{{ w.last_ok_at ? relativeFromNow(w.last_ok_at) : '—' }}</td>
            <td class="right">
              <NButton
                v-if="w.configured"
                size="small"
                secondary
                :loading="testing === w.id"
                @click="testWebhook(w.id)"
              >
                {{ t('admin.channels.test') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card-pad announce">
      <div class="strong">{{ t('admin.channels.announce') }}</div>
      <p v-if="!anyWebhook" class="faint small no-margin">{{ t('admin.channels.announceNoHooks') }}</p>
      <p v-else-if="!announceOn" class="faint small no-margin">{{ t('admin.channels.announceOff') }}</p>
      <NInput
        v-model:value="announcement"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 6 }"
        :maxlength="2000"
        :disabled="!anyWebhook || !announceOn"
        :placeholder="t('admin.channels.announcePlaceholder')"
      />
      <div>
        <NButton
          type="primary"
          :disabled="!announcement.trim() || !anyWebhook || !announceOn"
          :loading="sending"
          @click="announce"
        >
          {{ t('admin.channels.send') }}
        </NButton>
      </div>
    </div>
  </section>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
}
.group {
  margin-bottom: 16px;
}
.group-title {
  padding: 12px 20px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
  background: var(--surface-muted);
  border-bottom: 1px solid var(--border);
}
.strong {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.no-margin {
  margin: 0;
}
.right {
  text-align: right;
}
.detail td {
  padding-top: 0;
}
.err {
  color: var(--danger, #d03050);
  font-size: 12.5px;
  word-break: break-word;
}
.callback {
  display: grid;
  gap: 4px;
  margin-top: 4px;
}
.announce {
  display: grid;
  gap: 8px;
  border-top: 1px solid var(--border);
}
</style>
