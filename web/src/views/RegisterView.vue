<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import type { UserType } from '../types/auth'

const router = useRouter()
const loading = ref(false)
const userTypes = ref<UserType[]>([])
const form = reactive({ username: '', password: '', confirmPassword: '', user_type_id: '' })

onMounted(async () => {
  userTypes.value = (await api.get<UserType[]>('/auth/registration-options')).data
})

async function submit() {
  if (form.password !== form.confirmPassword) return ElMessage.warning('两次输入的密码不一致')
  if (!form.user_type_id) return ElMessage.warning('请选择用户类型')
  loading.value = true
  try {
    await api.post('/auth/register', {
      username: form.username,
      password: form.password,
      user_type_id: form.user_type_id,
    })
    ElMessage.success('注册成功，请登录')
    await router.push('/login')
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
    <p class="eyebrow">CREATE ACCOUNT</p><h1>注册账号</h1><p class="auth-subtitle">选择你的团队身份</p>
    <el-form label-position="top">
      <el-form-item label="用户名"><el-input v-model="form.username" size="large" /></el-form-item>
      <el-form-item label="用户类型"><el-select v-model="form.user_type_id" size="large" class="full-button" placeholder="请选择"><el-option v-for="item in userTypes" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
      <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password size="large" /></el-form-item>
      <el-form-item label="确认密码"><el-input v-model="form.confirmPassword" type="password" show-password size="large" @keyup.enter="submit" /></el-form-item>
      <el-button type="primary" size="large" :loading="loading" class="full-button" @click="submit">创建账号</el-button>
    </el-form>
    <p class="auth-link">已有账号？<router-link to="/login">返回登录</router-link></p>
  </section></main>
</template>
