<script setup lang="ts">
import { ArrowRight, Check, FolderOpened, Switch, UserFilled } from '@element-plus/icons-vue'
import { ElAside, ElContainer, ElDialog, ElDropdown, ElDropdownItem, ElDropdownMenu, ElIcon, ElMain, ElMenu, ElMenuItem, ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorMessage } from './api/client'
import NavFeatureIcon from './components/NavFeatureIcon.vue'
import { useAuthStore } from './stores/auth'
import type { Project } from './types/agent'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const publicPage = computed(() => Boolean(route.meta.public))
const projects = ref<Project[]>([])
const projectDialogVisible = ref(false)
const projectsLoading = ref(false)
const currentProjectId = ref(localStorage.getItem('evalweave-project') ?? '')
const currentProject = computed(() => projects.value.find((project) => project.id === currentProjectId.value) ?? projects.value[0] ?? null)

async function loadProjects() {
  if (!auth.user) return
  projectsLoading.value = true
  try {
    projects.value = (await api.get<Project[]>('/projects')).data
    if (!projects.value.some((project) => project.id === currentProjectId.value)) {
      currentProjectId.value = projects.value[0]?.id ?? ''
      if (currentProjectId.value) localStorage.setItem('evalweave-project', currentProjectId.value)
      else localStorage.removeItem('evalweave-project')
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    projectsLoading.value = false
  }
}

async function handleAccountCommand(command: string) {
  if (command === 'switch-project') {
    projectDialogVisible.value = true
    await loadProjects()
    return
  }
  if (command === 'logout') await logout()
}

async function switchProject(project: Project) {
  if (project.id === currentProjectId.value) {
    projectDialogVisible.value = false
    return
  }
  projectDialogVisible.value = false
  await router.replace({ name: route.name === 'assistant' ? 'assistant' : route.name === 'evaluations' ? 'evaluations' : route.name })
  localStorage.setItem('evalweave-project', project.id)
  currentProjectId.value = project.id
  ElMessage.success(`已切换到「${project.name}」`)
}

async function logout() {
  await auth.logout()
  await router.push({ name: 'login' })
}

watch(() => auth.user?.id, (userId) => {
  if (userId) void loadProjects()
  else projects.value = []
}, { immediate: true })
</script>

<template>
  <router-view v-if="publicPage" />
  <el-container v-else class="shell">
    <el-aside width="198px" class="sidebar">
      <div class="brand"><div class="brand-mark">EW</div><div><strong>EvalWeave</strong><small>智能评测平台</small></div></div>
      <el-menu :default-active="String(route.name)" router class="nav primary-nav">
        <el-menu-item index="dashboard" route="/"><el-icon><NavFeatureIcon kind="overview" /></el-icon>概览</el-menu-item>
        <el-menu-item
          v-if="auth.hasPermission('experiment:run')"
          index="assistant"
          route="/assistant"
        ><el-icon><NavFeatureIcon kind="assistant" /></el-icon>评测助手</el-menu-item>
        <el-menu-item
          v-if="auth.hasPermission('experiment:read')"
          index="evaluations"
          route="/evaluations"
        ><el-icon><NavFeatureIcon kind="evaluations" /></el-icon>评测任务</el-menu-item>
        <el-menu-item
          v-if="auth.hasPermission('experiment:read')"
          index="schedules"
          route="/schedules"
        ><el-icon><NavFeatureIcon kind="schedules" /></el-icon>定时任务</el-menu-item>
        <el-menu-item
          v-if="auth.hasPermission('evaluation:review') || auth.hasPermission('experiment:run')"
          index="human-tasks"
          route="/human-tasks"
        ><el-icon><NavFeatureIcon kind="human-tasks" /></el-icon>人工评审</el-menu-item>
      </el-menu>
      <el-menu v-if="auth.isAdmin" :default-active="String(route.name)" router class="nav admin-nav">
          <div class="nav-label">系统管理</div>
          <el-menu-item index="admin-projects" route="/admin/projects"><el-icon><NavFeatureIcon kind="admin-projects" /></el-icon>项目管理</el-menu-item>
          <el-menu-item index="admin-users" route="/admin/users"><el-icon><NavFeatureIcon kind="admin-users" /></el-icon>用户管理</el-menu-item>
          <el-menu-item index="admin-user-types" route="/admin/user-types"><el-icon><NavFeatureIcon kind="user-types" /></el-icon>用户类型</el-menu-item>
          <el-menu-item index="admin-evaluation-models" route="/admin/evaluation-models"><el-icon><NavFeatureIcon kind="evaluation-models" /></el-icon>评测模型</el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <div class="sidebar-promo" aria-hidden="true">
          <strong>让 AI 评测<br>更简单、更可靠</strong>
          <span>EvalWeave<br>驱动更好的 AI 应用</span>
          <i></i><i></i><i></i>
        </div>
        <el-dropdown trigger="click" class="account" popper-class="account-dropdown" @command="handleAccountCommand">
          <div class="account-trigger"><span class="account-avatar"><el-icon><UserFilled /></el-icon></span><div><strong>{{ auth.user?.username }}</strong><small>{{ currentProject?.name || (auth.isAdmin ? '系统管理员' : auth.user?.user_type_name) }}</small></div></div>
          <template #dropdown><el-dropdown-menu><el-dropdown-item command="switch-project"><el-icon><Switch /></el-icon>切换项目</el-dropdown-item><el-dropdown-item divided command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
        </el-dropdown>
      </div>
    </el-aside>
    <el-main class="content" :class="{ 'assistant-content': route.name === 'assistant' }"><router-view :key="currentProjectId" /></el-main>
    <el-dialog v-model="projectDialogVisible" width="420px" align-center class="project-switch-dialog" :show-close="true">
      <template #header>
        <div class="project-switch-heading">
          <span><el-icon><FolderOpened /></el-icon></span>
          <div><strong>切换项目</strong><small>选择接下来要查看和使用的项目</small></div>
        </div>
      </template>
      <div v-loading="projectsLoading" class="project-switch-list">
        <button v-for="project in projects" :key="project.id" type="button" class="project-switch-item" :class="{ active: project.id === currentProjectId }" @click="switchProject(project)">
          <span class="project-switch-icon">{{ project.name.slice(0, 1) }}</span>
          <span class="project-switch-copy"><strong>{{ project.name }}</strong><small>{{ project.description || '暂无项目说明' }}</small></span>
          <el-icon v-if="project.id === currentProjectId" class="project-switch-check"><Check /></el-icon>
          <el-icon v-else class="project-switch-arrow"><ArrowRight /></el-icon>
        </button>
        <div v-if="!projectsLoading && !projects.length" class="project-switch-empty">暂无可用项目</div>
      </div>
    </el-dialog>
  </el-container>
</template>
