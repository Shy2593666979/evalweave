<script setup lang="ts">
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElMessage, ElOption, ElSelect, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { api, errorMessage } from '../../api/client'
import type { SystemRole, User, UserType } from '../../types/auth'
import { formatBeijingDateTime } from '../../utils/datetime'

const users = ref<User[]>([])
const userTypes = ref<UserType[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ username: '', email: '', password: '', system_role: 'user' as SystemRole, user_type_id: '' })
const formRules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱地址', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确，请检查后重新输入', trigger: ['blur', 'change'] },
  ],
}

async function load() {
  loading.value = true
  try {
    const [userResponse, typeResponse] = await Promise.all([
      api.get<User[]>('/admin/users'),
      api.get<UserType[]>('/admin/user-types'),
    ])
    users.value = userResponse.data
    userTypes.value = typeResponse.data.filter((item) => item.is_active)
  } finally { loading.value = false }
}

async function createUser() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return ElMessage.warning('请输入正确的邮箱地址')
  try {
    await api.post('/admin/users', {
      username: form.username,
      email: form.email,
      password: form.password,
      system_role: form.system_role,
      user_type_id: form.system_role === 'user' ? form.user_type_id : null,
      is_active: true,
    })
    dialogVisible.value = false
    Object.assign(form, { username: '', email: '', password: '', system_role: 'user', user_type_id: '' })
    ElMessage.success('用户已创建')
    await load()
  } catch (error) { ElMessage.error(errorMessage(error)) }
}

async function setActive(row: unknown, active: boolean) {
  const user = row as User
  try {
    await api.patch(`/admin/users/${user.id}`, { is_active: active })
    ElMessage.success(active ? '用户已启用' : '用户已停用')
  } catch (error) {
    user.is_active = !active
    ElMessage.error(errorMessage(error))
  }
}

async function setUserType(row: unknown, userTypeId: string) {
  const user = row as User
  try {
    await api.patch(`/admin/users/${user.id}`, { user_type_id: userTypeId })
    user.user_type_name = userTypes.value.find((item) => item.id === userTypeId)?.name || null
    ElMessage.success('用户类型已更新')
  } catch (error) {
    ElMessage.error(errorMessage(error))
    await load()
  }
}

async function setEmail(row: unknown, email: string) {
  const user = row as User
  try {
    const response = await api.patch<User>(`/admin/users/${user.id}`, { email })
    user.email = response.data.email
    ElMessage.success('邮箱已更新')
  } catch (error) {
    ElMessage.error(errorMessage(error))
    await load()
  }
}

onMounted(load)
</script>

<template>
  <header class="page-header"><div><p class="eyebrow">访问控制</p><h1>用户管理</h1><p>管理工作区成员、系统角色和账号可用状态。</p></div><div class="page-actions"><el-button type="primary" @click="dialogVisible = true">添加用户</el-button></div></header>
  <section class="table-panel">
    <div class="table-toolbar"><strong>工作区成员</strong><span>共 {{ users.length }} 个账号</span></div>
    <el-table :data="users" v-loading="loading" empty-text="还没有用户">
      <el-table-column prop="username" label="用户名" min-width="180" />
      <el-table-column label="邮箱" min-width="240"><template #default="scope"><el-input v-model="scope.row.email" size="small" placeholder="填写通知邮箱" @change="setEmail(scope.row, String($event))" /></template></el-table-column>
      <el-table-column label="系统角色" width="130"><template #default="scope"><el-tag :type="scope.row.system_role === 'admin' ? 'primary' : 'info'">{{ scope.row.system_role === 'admin' ? '管理员' : '普通用户' }}</el-tag></template></el-table-column>
      <el-table-column prop="user_type_name" label="用户类型" min-width="180"><template #default="scope"><el-select v-if="scope.row.system_role === 'user'" v-model="scope.row.user_type_id" size="small" @change="setUserType(scope.row, String($event))"><el-option v-for="item in userTypes" :key="item.id" :label="item.name" :value="item.id" /></el-select><span v-else>—</span></template></el-table-column>
      <el-table-column label="权限数量" width="110"><template #default="scope">{{ scope.row.permissions.length }}</template></el-table-column>
      <el-table-column label="创建时间（北京时间）" min-width="210"><template #default="scope">{{ formatBeijingDateTime(scope.row.created_at, true) }}</template></el-table-column>
      <el-table-column label="启用" width="100"><template #default="scope"><el-switch v-model="scope.row.is_active" @change="setActive(scope.row, Boolean($event))" /></template></el-table-column>
    </el-table>
  </section>

  <el-dialog v-model="dialogVisible" title="添加用户" width="480px">
    <el-form ref="formRef" :model="form" :rules="formRules" label-position="top" hide-required-asterisk>
      <el-form-item label="用户名"><el-input v-model="form.username" /></el-form-item>
      <el-form-item label="邮箱" prop="email"><el-input v-model="form.email" type="email" placeholder="设置邮箱" /></el-form-item>
      <el-form-item label="初始密码"><el-input v-model="form.password" type="password" show-password /></el-form-item>
      <el-form-item label="系统角色"><el-select v-model="form.system_role" class="full-button"><el-option label="普通用户" value="user" /><el-option label="管理员" value="admin" /></el-select></el-form-item>
      <el-form-item v-if="form.system_role === 'user'" label="用户类型"><el-select v-model="form.user_type_id" class="full-button"><el-option v-for="item in userTypes" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" @click="createUser">创建</el-button></template>
  </el-dialog>
</template>
