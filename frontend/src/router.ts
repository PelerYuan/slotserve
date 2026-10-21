import { createRouter, createWebHistory } from 'vue-router'
import { loadSession, session } from '@/composables/session'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        { path: '', name: 'overview', component: () => import('@/views/OverviewView.vue') },
        { path: 'schedule', name: 'schedule', component: () => import('@/views/ScheduleView.vue') },
        { path: 'models', name: 'models', component: () => import('@/views/ModelsView.vue') },
        { path: 'keys', name: 'keys', component: () => import('@/views/KeysView.vue') },
        { path: 'devenv', name: 'devenv', component: () => import('@/views/DevEnvView.vue') },
        { path: 'usage', name: 'usage', component: () => import('@/views/UsageView.vue') },
        { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
        { path: 'admin', name: 'admin', component: () => import('@/views/AdminView.vue'), meta: { admin: true } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  if (!session.checked) await loadSession()
  if (!to.meta.public && !session.user) return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  if (to.name === 'login' && session.user) return { name: 'overview' }
  if (to.meta.admin && !session.user?.is_admin) return { name: 'overview' }
})
