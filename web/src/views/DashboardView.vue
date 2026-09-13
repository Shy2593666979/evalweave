<script setup lang="ts">
import { ArrowRight } from '@element-plus/icons-vue'
import { ElIcon } from 'element-plus'
import NavFeatureIcon from '../components/NavFeatureIcon.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
</script>

<template>
  <header class="page-header">
    <div>
      <p class="eyebrow">工作台</p>
      <h1>{{ auth.user?.username }}，你好</h1>
      <p>选择一项工作继续。</p>
    </div>
  </header>

  <section class="workspace-panel">
    <div class="section-heading">
      <h2>可用功能</h2>
      <span>根据当前账号权限显示</span>
    </div>

    <div class="action-grid">
      <router-link
        v-if="auth.hasPermission('experiment:run')"
        to="/assistant"
        class="action-card"
      >
        <span class="action-icon"><NavFeatureIcon kind="assistant" /></span>
        <div><strong>评测助手</strong><p>通过自然语言配置数据、接口和完整评测任务。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <router-link
        v-if="auth.hasPermission('experiment:read')"
        to="/evaluations"
        class="action-card"
      >
        <span class="action-icon"><NavFeatureIcon kind="evaluations" /></span>
        <div><strong>评测任务</strong><p>准备数据、启动智能评测并跟踪运行结果。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <router-link
        v-if="auth.hasPermission('evaluation:review') || auth.hasPermission('experiment:run')"
        to="/human-tasks"
        class="action-card"
      >
        <span class="action-icon"><NavFeatureIcon kind="human-tasks" /></span>
        <div><strong>人工评审</strong><p>审核执行方案，完成分配给你的匿名评分任务。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <router-link v-if="auth.isAdmin" to="/admin/users" class="action-card">
        <span class="action-icon"><NavFeatureIcon kind="admin-users" /></span>
        <div><strong>用户管理</strong><p>创建账号、分配用户类型并管理账号状态。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <router-link v-if="auth.isAdmin" to="/admin/user-types" class="action-card">
        <span class="action-icon"><NavFeatureIcon kind="user-types" /></span>
        <div><strong>用户类型</strong><p>维护用户类型以及每类用户可使用的功能权限。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <router-link v-if="auth.isAdmin" to="/admin/evaluation-models" class="action-card">
        <span class="action-icon"><NavFeatureIcon kind="evaluation-models" /></span>
        <div><strong>评测模型</strong><p>配置评测 Agent 使用的模型服务和连接信息。</p></div>
        <el-icon class="action-arrow"><ArrowRight /></el-icon>
      </router-link>

      <div
        v-if="!auth.isAdmin && !auth.hasPermission('experiment:run') && !auth.hasPermission('evaluation:review') && !auth.hasPermission('experiment:read')"
        class="workspace-empty"
      >
        当前账号暂无可用功能，请联系管理员分配权限。
      </div>
    </div>
  </section>
</template>
