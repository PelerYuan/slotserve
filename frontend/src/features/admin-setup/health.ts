import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { setupApi, type Health, type HealthCheck } from './api'

/** The health checks (A05) of the admin overview: loaded once, refreshed now and then and after a language switch
 *  (titles are rendered by the server in the viewer's language). */
export function useHealth(refreshMs = 30_000) {
  const { locale } = useI18n()
  const health = ref<Health | null>(null)
  const error = ref<unknown>(null)
  let timer = 0

  async function load() {
    try {
      health.value = await setupApi.health()
      error.value = null
    } catch (e) {
      error.value = e
    }
  }

  function replace(check: HealthCheck) {
    if (!health.value) return
    health.value = { ...health.value, checks: health.value.checks.map((c) => (c.id === check.id ? check : c)) }
  }

  onMounted(() => {
    load()
    timer = window.setInterval(load, refreshMs)
  })
  onBeforeUnmount(() => window.clearInterval(timer))
  // the server renders titles in the person's language; App.vue stores the new one, then the titles follow
  watch(locale, () => window.setTimeout(load, 400))

  return { health, error, load, replace }
}
