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
  <main class="auth-page"><section class="auth-card">
    <div class="auth-brand"><div class="brand-mark">EW</div><strong>EvalWeave</strong></div>
    <p class="eyebrow">WELCOME BACK</p><h1>登录评测平台</h1><p class="auth-subtitle">使用用户名和密码继续</p>
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名"><el-input v-model="form.username" size="large" autofocus /></el-form-item>
      <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password size="large" @keyup.enter="submit" /></el-form-item>
      <el-button type="primary" size="large" :loading="loading" class="full-button" @click="submit">登录</el-button>
    </el-form>
    <p class="auth-link">还没有账号？<router-link to="/register">立即注册</router-link></p>
  </section></main>
</template>
