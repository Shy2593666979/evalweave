<script setup lang="ts">
import { Bell, DataAnalysis, Files, Histogram, Setting, User, UserFilled } from '@element-plus/icons-vue'
import { ElAside, ElContainer, ElDropdown, ElDropdownItem, ElDropdownMenu, ElIcon, ElMain, ElMenu, ElMenuItem } from 'element-plus'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
    <el-aside width="232px" class="sidebar">
      <div class="brand"><div class="brand-mark">EW</div><div><strong>EvalWeave</strong><small>AI EVALUATION</small></div></div>
      <el-menu :default-active="String(route.name)" router class="nav">
        <el-menu-item index="dashboard" route="/"><el-icon><DataAnalysis /></el-icon>概览</el-menu-item>
        <el-menu-item index="datasets" disabled><el-icon><Files /></el-icon>数据集</el-menu-item>
        <el-menu-item index="experiments" disabled><el-icon><Histogram /></el-icon>实验</el-menu-item>
        <el-menu-item
          v-if="auth.hasPermission('evaluation:review')"
          index="human-tasks"
          route="/human-tasks"
        ><el-icon><Bell /></el-icon>人工任务</el-menu-item>
        <template v-if="auth.isAdmin">
          <div class="nav-label">系统管理</div>
          <el-menu-item index="admin-users" route="/admin/users"><el-icon><User /></el-icon>用户管理</el-menu-item>
          <el-menu-item index="admin-user-types" route="/admin/user-types"><el-icon><Setting /></el-icon>用户类型</el-menu-item>
        </template>
      </el-menu>
      <el-dropdown trigger="click" class="account" @command="logout">
        <div class="account-trigger"><el-icon><UserFilled /></el-icon><div><strong>{{ auth.user?.username }}</strong><small>{{ auth.isAdmin ? 'Admin' : auth.user?.user_type_name }}</small></div></div>
        <template #dropdown><el-dropdown-menu><el-dropdown-item command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
      </el-dropdown>
    </el-aside>
    <el-main class="content"><router-view /></el-main>
  </el-container>
</template>
