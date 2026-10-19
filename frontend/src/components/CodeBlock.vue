<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Copy } from 'lucide-vue-next'

const props = defineProps<{ code: string; inline?: boolean }>()
const { t } = useI18n()
const copied = ref(false)

async function copy() {
  try {
    await navigator.clipboard.writeText(props.code)
  } catch {
    // Fallback for non-secure (http) LAN origins.
    const ta = document.createElement('textarea')
    ta.value = props.code
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
  copied.value = true
  window.setTimeout(() => (copied.value = false), 1500)
}
</script>

<template>
  <div class="code" :class="{ inline }">
    <pre><code>{{ code }}</code></pre>
    <button class="copy" :title="t('common.copy')" @click="copy">
      <Check v-if="copied" :size="14" />
      <Copy v-else :size="14" />
      <span>{{ copied ? t('common.copied') : t('common.copy') }}</span>
    </button>
  </div>
</template>

<style scoped>
.code {
  position: relative;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
}
pre {
  margin: 0;
  padding: 14px 16px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--code-text);
}
.inline pre {
  padding: 9px 96px 9px 12px;
  white-space: nowrap;
}
.copy {
  position: absolute;
  top: 8px;
  right: 8px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: 7px;
  padding: 3px 9px;
  font-size: 12px;
  color: var(--text-2);
  cursor: pointer;
  font-family: var(--font);
}
.inline .copy {
  top: 50%;
  transform: translateY(-50%);
}
.copy:hover {
  color: var(--text);
  border-color: var(--border-strong);
}
</style>
