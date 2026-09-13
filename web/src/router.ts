import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
    { path: '/register', name: 'register', component: () => import('./views/RegisterView.vue'), meta: { public: true } },
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
    {
      path: '/assistant/:conversationId?',
      name: 'assistant',
      component: () => import('./views/EvaluationAssistantView.vue'),
      meta: { permission: 'experiment:run' },
    },
    {
      path: '/evaluations/:jobId?',
      name: 'evaluations',
      component: () => import('./views/AgentJobsView.vue'),
      meta: { permission: 'experiment:read' },
    },
    {
      path: '/human-tasks/:taskId?',
      name: 'human-tasks',
      component: () => import('./views/HumanTasksView.vue'),
      meta: { anyPermission: ['evaluation:review', 'experiment:run'] },
    },
    { path: '/admin/users', name: 'admin-users', component: () => import('./views/admin/UsersView.vue'), meta: { admin: true } },
    { path: '/admin/user-types', name: 'admin-user-types', component: () => import('./views/admin/UserTypesView.vue'), meta: { admin: true } },
    { path: '/admin/evaluation-models', name: 'admin-evaluation-models', component: () => import('./views/admin/EvaluationModelsView.vue'), meta: { admin: true } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.initialized) await auth.loadCurrentUser()
  if (to.meta.public) return auth.user ? { name: 'dashboard' } : true
  if (!auth.user) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.isAdmin) return { name: 'dashboard' }
  if (to.meta.permission && !auth.hasPermission(String(to.meta.permission))) {
    return { name: 'dashboard' }
  }
  if (Array.isArray(to.meta.anyPermission) && !to.meta.anyPermission.some((item) => auth.hasPermission(String(item)))) {
    return { name: 'dashboard' }
  }
  return true
})

export default router
