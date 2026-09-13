<script setup lang="ts">
import { ElButton, ElCheckbox, ElCheckboxGroup, ElDialog, ElForm, ElFormItem, ElInput, ElMessage, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { api, errorMessage } from '../../api/client'
import type { PermissionOption, UserType } from '../../types/auth'

const userTypes = ref<UserType[]>([])
const permissions = ref<PermissionOption[]>([])
const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const form = reactive({ code: '', name: '', description: '', permissions: [] as string[], selectable_on_registration: true })

async function load() {
  const [typeResponse, permissionResponse] = await Promise.all([
    api.get<UserType[]>('/admin/user-types'), api.get<PermissionOption[]>('/admin/permissions'),
  ])
  userTypes.value = typeResponse.data
  permissions.value = permissionResponse.data
}

function openCreate() {
  editingId.value = null
  Object.assign(form, { code: '', name: '', description: '', permissions: [], selectable_on_registration: true })
  dialogVisible.value = true
}

function openEdit(row: unknown) {
  const item = row as UserType
  editingId.value = item.id
  Object.assign(form, { code: item.code, name: item.name, description: item.description || '', permissions: [...item.permissions], selectable_on_registration: item.selectable_on_registration })
  dialogVisible.value = true
}

async function save() {
  try {
    if (editingId.value) {
      await api.patch(`/admin/user-types/${editingId.value}`, { name: form.name, description: form.description, permissions: form.permissions, selectable_on_registration: form.selectable_on_registration })
    } else {
      await api.post('/admin/user-types', form)
    }
    dialogVisible.value = false
    ElMessage.success('用户类型已保存')
    await load()
  } catch (error) { ElMessage.error(errorMessage(error)) }
}

async function updateFlags(row: unknown) {
  const item = row as UserType
  try {
    await api.patch(`/admin/user-types/${item.id}`, { is_active: item.is_active, selectable_on_registration: item.selectable_on_registration })
  } catch (error) { ElMessage.error(errorMessage(error)); await load() }
}

onMounted(load)
</script>

<template>
  <header class="page-header"><div><p class="eyebrow">权限配置</p><h1>用户类型与权限</h1><p>用清晰的团队身份定义功能边界，并控制注册时可选择的角色。</p></div><div class="page-actions"><el-button type="primary" @click="openCreate">新建用户类型</el-button></div></header>
  <section class="table-panel">
    <div class="table-toolbar"><strong>权限角色</strong><span>共 {{ userTypes.length }} 种类型</span></div>
    <el-table :data="userTypes" empty-text="还没有用户类型">
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column prop="code" label="编码" min-width="130" />
      <el-table-column label="权限" min-width="300"><template #default="scope"><el-tag v-for="key in scope.row.permissions" :key="key" class="permission-tag" type="info">{{ permissions.find((item) => item.key === key)?.label || key }}</el-tag></template></el-table-column>
      <el-table-column label="注册可选" width="110"><template #default="scope"><el-switch v-model="scope.row.selectable_on_registration" @change="updateFlags(scope.row)" /></template></el-table-column>
      <el-table-column label="启用" width="90"><template #default="scope"><el-switch v-model="scope.row.is_active" @change="updateFlags(scope.row)" /></template></el-table-column>
      <el-table-column label="操作" width="90"><template #default="scope"><el-button link type="primary" @click="openEdit(scope.row)">编辑</el-button></template></el-table-column>
    </el-table>
  </section>

  <el-dialog v-model="dialogVisible" :title="editingId ? '编辑用户类型' : '新建用户类型'" width="600px">
    <el-form label-position="top">
      <el-form-item label="名称"><el-input v-model="form.name" placeholder="例如：运营同学" /></el-form-item>
      <el-form-item label="编码"><el-input v-model="form.code" :disabled="Boolean(editingId)" placeholder="例如：operations" /></el-form-item>
      <el-form-item label="说明"><el-input v-model="form.description" type="textarea" /></el-form-item>
      <el-form-item label="权限"><el-checkbox-group v-model="form.permissions" class="permission-grid"><el-checkbox v-for="item in permissions" :key="item.key" :value="item.key">{{ item.label }}</el-checkbox></el-checkbox-group></el-form-item>
      <el-form-item label="注册页面可选"><el-switch v-model="form.selectable_on_registration" /></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" @click="save">保存</el-button></template>
  </el-dialog>
</template>
