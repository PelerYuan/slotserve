<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NModal } from 'naive-ui'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { docsUrl, setupApi, type CheckAction, type HealthCheck } from '../api'

// The buttons of one health check: what the server suggests (open a page, change a setting, retry, docs), checking
// it again and dismissing it with a reason (audited).
const props = withDefaults(defineProps<{ check: HealthCheck; name?: string; size?: 'tiny' | 'small' }>(), {
  name: '',
  size: 'small',
})
const emit = defineEmits<{ updated: [check: HealthCheck] }>()
const { t, te, locale } = useI18n()
const router = useRouter()
const { showError, message } = useErrorMessage()

const running = ref(false)
const LINKS: Record<string, string> = {
  '/models': 'models',
  '/admin?tab=lanes': 'lanes',
  '/admin?tab=runtime': 'runtime',
  '/admin?tab=storage': 'storage',
  '/admin?tab=channels': 'channels',
}

const settingName = (key: string) => (te(`admin.settings.${key}`) ? t(`admin.settings.${key}`) : key)

function label(a: CheckAction): string {
  if (a.kind === 'setting') return t('adminSetup.actions.setting', { name: settingName(a.target) })
  if (a.kind === 'retry') return t('adminSetup.actions.retry')
  if (a.kind === 'docs') return t('adminSetup.actions.docs')
  const link = LINKS[a.target]
  return link ? t(`adminSetup.actions.links.${link}`) : t('adminSetup.actions.open')
}

// "retry" reruns the check named by the target (e.g. the backup check after a failed backup)
const actions = computed(() => props.check.actions.filter((a) => !(a.kind === 'retry' && a.target === props.check.id)))

async function run(id = props.check.id) {
  running.value = true
  try {
    const c = await setupApi.runCheck(id)
    if (c.id === props.check.id) emit('updated', c)
  } catch (e) {
    showError(e)
  } finally {
    running.value = false
  }
}

function act(a: CheckAction) {
  if (a.kind === 'retry') run(a.target)
  else if (a.kind === 'docs') window.open(docsUrl(a.target, locale.value), '_blank', 'noopener')
  else if (a.kind === 'setting') router.push({ path: '/admin', query: { tab: 'settings', q: a.target } })
  else router.push(a.target)
}

const dismissForm = reactive({ open: false, reason: '', busy: false })

async function dismiss() {
  dismissForm.busy = true
  try {
    emit('updated', await setupApi.dismiss(props.check.id, dismissForm.reason.trim()))
    dismissForm.open = false
    message.success(t('adminSetup.dismiss.done'))
  } catch (e) {
    showError(e)
  } finally {
    dismissForm.busy = false
  }
}

async function undismiss() {
  try {
    emit('updated', await setupApi.undismiss(props.check.id))
    message.success(t('adminSetup.dismiss.restored'))
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <div class="check-actions">
    <NButton
      v-for="(a, i) in actions"
      :key="i"
      :size="size"
      :type="i === 0 && check.level !== 'ok' && !check.dismissed ? 'primary' : 'default'"
      :secondary="i === 0 && check.level !== 'ok' && !check.dismissed"
      :quaternary="!(i === 0 && check.level !== 'ok' && !check.dismissed)"
      :data-action="a.kind"
      @click="act(a)"
    >
      {{ label(a) }}
    </NButton>
    <slot />
    <NButton :size="size" quaternary :loading="running" data-action="run" @click="run()">
      {{ t('adminSetup.actions.runCheck') }}
    </NButton>
    <NButton
      v-if="check.dismissible && !check.dismissed && check.level !== 'ok'"
      :size="size"
      quaternary
      data-action="dismiss"
      @click="Object.assign(dismissForm, { open: true, reason: '' })"
    >
      {{ t('adminSetup.actions.dismiss') }}
    </NButton>
    <NButton v-if="check.dismissed" :size="size" quaternary data-action="undismiss" @click="undismiss">
      {{ t('adminSetup.actions.undismiss') }}
    </NButton>

    <NModal
      v-model:show="dismissForm.open"
      preset="card"
      :bordered="false"
      :title="t('adminSetup.dismiss.title', { name: name || check.title })"
      style="width: 460px; max-width: calc(100vw - 32px)"
    >
      <p class="muted intro">{{ t('adminSetup.dismiss.intro') }}</p>
      <label class="field">
        <span class="field-label">{{ t('adminSetup.dismiss.reason') }}</span>
        <NInput
          v-model:value="dismissForm.reason"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 5 }"
          :maxlength="500"
          :placeholder="t('adminSetup.dismiss.reasonPh')"
        />
      </label>
      <template #footer>
        <div class="footer">
          <NButton @click="dismissForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :disabled="!dismissForm.reason.trim()" :loading="dismissForm.busy" @click="dismiss">
            {{ t('adminSetup.dismiss.confirm') }}
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.check-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  align-items: center;
}
.intro {
  margin: 0 0 14px;
}
.field {
  display: grid;
  gap: 6px;
}
.field-label {
  font-size: 13px;
  font-weight: 500;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
