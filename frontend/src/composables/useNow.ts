import { onBeforeUnmount, onMounted, ref } from 'vue'

export function useNow(intervalMs = 1000) {
  const now = ref(Date.now())
  let timer: number | undefined
  onMounted(() => (timer = window.setInterval(() => (now.value = Date.now()), intervalMs)))
  onBeforeUnmount(() => window.clearInterval(timer))
  return now
}
