<script setup lang="ts">
import { UserFilled } from '@element-plus/icons-vue'
import { ElAside, ElContainer, ElDropdown, ElDropdownItem, ElDropdownMenu, ElIcon, ElMain, ElMenu, ElMenuItem } from 'element-plus'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import NavFeatureIcon from './components/NavFeatureIcon.vue'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const publicPage = computed(() => Boolean(route.meta.public))

async function logout() {
  await auth.logout()
  await router.push({ name: 'login' })
}
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
          v-if="auth.hasPermission('evaluation:review') || auth.hasPermission('experiment:run')"
          index="human-tasks"
          route="/human-tasks"
        ><el-icon><NavFeatureIcon kind="human-tasks" /></el-icon>人工评审</el-menu-item>
      </el-menu>
      <el-menu v-if="auth.isAdmin" :default-active="String(route.name)" router class="nav admin-nav">
          <div class="nav-label">系统管理</div>
          <el-menu-item index="admin-users" route="/admin/users"><el-icon><NavFeatureIcon kind="admin-users" /></el-icon>用户管理</el-menu-item>
          <el-menu-item index="admin-user-types" route="/admin/user-types"><el-icon><NavFeatureIcon kind="user-types" /></el-icon>用户类型</el-menu-item>
          <el-menu-item index="admin-evaluation-models" route="/admin/evaluation-models"><el-icon><NavFeatureIcon kind="evaluation-models" /></el-icon>评测模型</el-menu-item>
      </el-menu>
      <div class="sidebar-promo" aria-hidden="true">
        <strong>让 AI 评测<br>更简单、更可靠</strong>
        <span>EvalWeave<br>驱动更好的 AI 应用</span>
        <i></i><i></i><i></i>
      </div>
      <el-dropdown trigger="click" class="account" @command="logout">
        <div class="account-trigger"><span class="account-avatar"><el-icon><UserFilled /></el-icon></span><div><strong>{{ auth.user?.username }}</strong><small>{{ auth.isAdmin ? '系统管理员' : auth.user?.user_type_name }}</small></div></div>
        <template #dropdown><el-dropdown-menu><el-dropdown-item command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
      </el-dropdown>
    </el-aside>
    <el-main class="content" :class="{ 'assistant-content': route.name === 'assistant' }"><router-view /></el-main>
  </el-container>
</template>
