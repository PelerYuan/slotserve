<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  NNotificationProvider,
  dateEnUS,
  dateZhCN,
  enUS,
  zhCN,
} from 'naive-ui'
import { themeOverrides } from './theme'

const { locale } = useI18n()
const nLocale = computed(() => (locale.value === 'zh-CN' ? zhCN : enUS))
const nDateLocale = computed(() => (locale.value === 'zh-CN' ? dateZhCN : dateEnUS))
</script>

<template>
  <NConfigProvider :theme-overrides="themeOverrides" :locale="nLocale" :date-locale="nDateLocale">
    <NMessageProvider placement="top">
      <NNotificationProvider>
        <NDialogProvider>
          <RouterView />
        </NDialogProvider>
      </NNotificationProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
