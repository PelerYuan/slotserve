<script setup lang="ts">
withDefaults(defineProps<{ size?: number; withName?: boolean }>(), { size: 28, withName: true })
</script>

<template>
  <span class="brand">
    <svg :width="size" :height="size" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="#4f46e5" />
      <rect x="7" y="8" width="18" height="4" rx="2" fill="#fff" opacity=".5" />
      <rect x="7" y="14" width="12" height="4" rx="2" fill="#fff" />
      <rect x="7" y="20" width="18" height="4" rx="2" fill="#fff" opacity=".5" />
    </svg>
    <span v-if="withName" class="name">SlotServe</span>
  </span>
</template>

<style scoped>
.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
.name {
  font-weight: 650;
  font-size: 15px;
  letter-spacing: -0.02em;
  color: var(--text);
}
</style>
