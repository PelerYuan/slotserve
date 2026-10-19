import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { i18n } from './i18n'
import { setUnauthorizedHandler } from './api/client'
import { session } from './composables/session'
import './styles/main.css'

setUnauthorizedHandler(() => {
  if (session.user) {
    session.user = null
    router.push({ name: 'login' })
  }
})

document.documentElement.lang = i18n.global.locale.value
createApp(App).use(i18n).use(router).mount('#app')
