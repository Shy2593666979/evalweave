import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
    { path: '/register', name: 'register', component: () => import('./views/RegisterView.vue'), meta: { public: true } },
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
    { path: '/admin/users', name: 'admin-users', component: () => import('./views/admin/UsersView.vue'), meta: { admin: true } },
    { path: '/admin/user-types', name: 'admin-user-types', component: () => import('./views/admin/UserTypesView.vue'), meta: { admin: true } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.initialized) await auth.loadCurrentUser()
  if (to.meta.public) return auth.user ? { name: 'dashboard' } : true
  if (!auth.user) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.isAdmin) return { name: 'dashboard' }
  return true
})

export default router
