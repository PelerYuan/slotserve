<script setup lang="ts">
// The bell (F01): everyone's inbox. The badge counts unread messages; red when one of them is an admin to-do.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bell } from 'lucide-vue-next'
import { openInbox, useInboxPoller } from '../useInbox'

const { t } = useI18n()
const inbox = useInboxPoller()
const count = computed(() => inbox.unread.total)
const label = computed(() => (count.value ? t('inbox.bellUnread', { n: count.value }) : t('inbox.title')))
</script>

<template>
  <button class="bell" :aria-label="label" :title="label" data-action="inbox-bell" @click="openInbox()">
    <Bell :size="18" :stroke-width="1.8" />
    <span v-if="count" class="count" :class="{ admin: inbox.unread.admin > 0 }" data-field="inbox-count">
      {{ count > 99 ? '99+' : count }}
    </span>
  </button>
</template>

<style scoped>
.bell {
  position: relative;
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: 9px;
  background: none;
  color: var(--text-2);
  cursor: pointer;
  flex: none;
}
.bell:hover {
  background: var(--hover);
  color: var(--text);
}
.count {
  position: absolute;
  top: 2px;
  right: 1px;
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--brand);
  color: #fff;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 17px;
  text-align: center;
  box-shadow: 0 0 0 2px var(--surface);
}
.count.admin {
  background: var(--danger);
}
</style>
