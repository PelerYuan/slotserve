<script setup lang="ts">
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  NNotificationProvider,
  darkTheme,
  dateEnUS,
  dateZhCN,
  enUS,
  zhCN,
} from 'naive-ui'
import { api } from './api/client'
import { session } from './composables/session'
import { isDark } from './composables/theme'
import { darkOverrides, themeOverrides } from './theme'

const { locale } = useI18n()
const nLocale = computed(() => (locale.value === 'zh-CN' ? zhCN : enUS))
const nDateLocale = computed(() => (locale.value === 'zh-CN' ? dateZhCN : dateEnUS))

// The server writes notifications in each person's language.
watch(
  [locale, () => session.user?.id],
  () => {
    if (session.user && session.user.locale !== locale.value) {
      api
        .preferences({ locale: locale.value })
        .then((u) => session.user && (session.user.locale = u.locale))
        .catch(() => undefined)
    }
  },
  { immediate: true },
)
</script>

<template>
  <NConfigProvider
    :theme="isDark ? darkTheme : null"
    :theme-overrides="isDark ? darkOverrides : themeOverrides"
    :locale="nLocale"
    :date-locale="nDateLocale"
  >
    <NMessageProvider placement="top">
      <NNotificationProvider>
        <NDialogProvider>
          <RouterView />
        </NDialogProvider>
      </NNotificationProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
