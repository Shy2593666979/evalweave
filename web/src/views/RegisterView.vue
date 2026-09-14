<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import type { UserType } from '../types/auth'

const router = useRouter()
const loading = ref(false)
const userTypes = ref<UserType[]>([])
const formRef = ref<FormInstance>()
const form = reactive({ username: '', email: '', password: '', confirmPassword: '', user_type_id: '' })
const formRules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱地址', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确，请检查后重新输入', trigger: ['blur', 'change'] },
  ],
}

onMounted(async () => {
  userTypes.value = (await api.get<UserType[]>('/auth/registration-options')).data
})

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return ElMessage.warning('请输入正确的邮箱地址')
  if (form.password !== form.confirmPassword) return ElMessage.warning('两次输入的密码不一致')
  if (!form.user_type_id) return ElMessage.warning('请选择用户类型')
  loading.value = true
  try {
    await api.post('/auth/register', {
      username: form.username,
      email: form.email,
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
  <main class="auth-page">
    <aside class="auth-visual">
      <div class="auth-logo"><div class="brand-mark">EW</div><strong>EvalWeave</strong></div>
      <div class="auth-statement">
        <span class="signal">加入评测工作区</span>
        <h2>把评测变成<br><em>团队共同语言</em></h2>
        <p>选择你的团队身份，获得对应的工作权限，在同一条评测链路中协作。</p>
      </div>
      <div class="auth-proof"><span>统一数据</span><span>协同实验</span><span>可信决策</span></div>
    </aside>
    <section class="auth-form-wrap"><div class="auth-card">
      <div class="auth-logo auth-mobile-logo"><div class="brand-mark">EW</div><strong>EvalWeave</strong></div>
      <p class="eyebrow">创建账号</p><h1>加入工作区</h1><p class="auth-subtitle">创建账号，并选择与你工作职责匹配的用户类型。</p>
      <el-form ref="formRef" :model="form" :rules="formRules" label-position="top" hide-required-asterisk @submit.prevent="submit">
        <el-form-item label="用户名"><el-input v-model="form.username" size="large" placeholder="设置用户名" /></el-form-item>
        <el-form-item label="邮箱" prop="email"><el-input v-model="form.email" type="email" size="large" placeholder="设置邮箱" /></el-form-item>
        <el-form-item label="用户类型"><el-select v-model="form.user_type_id" size="large" class="full-button" placeholder="请选择团队身份"><el-option v-for="item in userTypes" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password size="large" placeholder="设置登录密码" /></el-form-item>
        <el-form-item label="确认密码"><el-input v-model="form.confirmPassword" type="password" show-password size="large" placeholder="再次输入密码" @keyup.enter="submit" /></el-form-item>
        <el-button type="primary" size="large" :loading="loading" class="full-button" @click="submit">创建账号</el-button>
      </el-form>
      <p class="auth-link">已有账号？ <router-link to="/login">返回登录</router-link></p>
    </div></section>
  </main>
</template>
