import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { User } from '../types/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const initialized = ref(false)
  const isAdmin = computed(() => user.value?.system_role === 'admin')

  async function loadCurrentUser() {
    try {
      user.value = (await api.get<User>('/auth/me')).data
    } catch {
      user.value = null
    } finally {
      initialized.value = true
    }
  }

  async function login(username: string, password: string) {
    user.value = (await api.post<User>('/auth/login', { username, password })).data
    initialized.value = true
  }

  async function logout() {
    await api.post('/auth/logout')
    user.value = null
  }

  return { user, initialized, isAdmin, loadCurrentUser, login, logout }
})
