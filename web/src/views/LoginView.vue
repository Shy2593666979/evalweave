<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function submit() {
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    await router.push(String(route.query.redirect || '/'))
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <aside class="auth-visual">
      <div class="auth-logo"><div class="brand-mark">EW</div><strong>EvalWeave</strong></div>
      <div class="auth-statement">
        <span class="signal">智能评测基础设施</span>
        <h2>让模型表现<br><em>清晰可度量</em></h2>
        <p>统一管理数据、实验、评测证据与人工决策，建立团队可信赖的 AI 质量基线。</p>
      </div>
      <div class="auth-proof"><span>过程可追踪</span><span>结果可复现</span><span>决策可审计</span></div>
    </aside>
    <section class="auth-form-wrap"><div class="auth-card">
      <div class="auth-logo auth-mobile-logo"><div class="brand-mark">EW</div><strong>EvalWeave</strong></div>
      <p class="eyebrow">欢迎回来</p><h1>登录工作区</h1><p class="auth-subtitle">输入账号信息，继续你的评测工作。</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名"><el-input v-model="form.username" size="large" placeholder="请输入用户名" autofocus /></el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password size="large" placeholder="请输入密码" @keyup.enter="submit" /></el-form-item>
        <el-button type="primary" size="large" :loading="loading" class="full-button" @click="submit">进入工作区</el-button>
      </el-form>
      <p class="auth-link">还没有账号？ <router-link to="/register">创建账号</router-link></p>
    </div></section>
  </main>
</template>
