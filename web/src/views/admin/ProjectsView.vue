<script setup lang="ts">
import { EditPen, Plus, User } from '@element-plus/icons-vue'
import { ElButton, ElDialog, ElForm, ElFormItem, ElIcon, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { api, errorMessage } from '../../api/client'
import type { Project } from '../../types/agent'
import type { User as Account } from '../../types/auth'

const projects = ref<Project[]>([])
const users = ref<Account[]>([])
const projectMemberIds = ref<Record<string, string[]>>({})
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editingId = ref('')
const form = reactive({ name: '', description: '', agent_context: '', user_ids: [] as string[] })
const dialogTitle = computed(() => editingId.value ? '编辑项目' : '创建项目')

async function load() {
  loading.value = true
  try {
    const [projectResponse, userResponse] = await Promise.all([
      api.get<Project[]>('/projects'),
      api.get<Account[]>('/admin/users'),
    ])
    projects.value = projectResponse.data
    users.value = userResponse.data.filter((item) => item.system_role === 'user' && item.is_active)
    const memberships = await Promise.all(projects.value.map(async (project) => {
      const ids = (await api.get<string[]>(`/projects/${project.id}/members`)).data
      return [project.id, ids] as const
    }))
    projectMemberIds.value = Object.fromEntries(memberships)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function projectMembers(projectId: string) {
  const ids = new Set(projectMemberIds.value[projectId] ?? [])
  return users.value.filter((user) => ids.has(user.id))
}

function openCreate() {
  editingId.value = ''
  Object.assign(form, { name: '', description: '', agent_context: '', user_ids: [] })
  dialogVisible.value = true
}

async function openEdit(project: Project) {
  editingId.value = project.id
  Object.assign(form, {
    name: project.name,
    description: project.description ?? '',
    agent_context: project.agent_context ?? '',
    user_ids: (await api.get<string[]>(`/projects/${project.id}/members`)).data,
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim()) return ElMessage.warning('请输入项目名称')
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      agent_context: form.agent_context.trim() || null,
    }
    const project = editingId.value
      ? (await api.patch<Project>(`/projects/${editingId.value}`, payload)).data
      : (await api.post<Project>('/projects', payload)).data
    await api.put(`/projects/${project.id}/members`, { user_ids: form.user_ids })
    dialogVisible.value = false
    ElMessage.success(editingId.value ? '项目已更新' : '项目已创建')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-header">
    <div><p class="eyebrow">项目空间</p><h1>项目管理</h1><p>维护项目基础信息，并决定哪些成员可以查看和使用项目。</p></div>
    <div class="page-actions"><el-button type="primary" :icon="Plus" @click="openCreate">创建项目</el-button></div>
  </header>

  <section v-loading="loading" class="project-admin-grid">
    <article v-for="project in projects" :key="project.id" class="project-admin-card">
      <div class="project-admin-card-head">
        <span class="project-admin-mark">{{ project.name.slice(0, 1) }}</span>
        <el-button text :icon="EditPen" @click="openEdit(project)">编辑</el-button>
      </div>
      <h2>{{ project.name }}</h2>
      <p>{{ project.description || '暂无项目说明' }}</p>
      <footer>
        <div v-if="projectMembers(project.id).length" class="project-member-stack" :aria-label="`${projectMembers(project.id).length} 位成员`">
          <span v-for="member in projectMembers(project.id).slice(0, 3)" :key="member.id" class="project-member-avatar" :title="member.username">{{ member.username.slice(0, 1).toUpperCase() }}</span>
          <span v-if="projectMembers(project.id).length > 3" class="project-member-more">+{{ projectMembers(project.id).length - 3 }}</span>
        </div>
        <span v-else class="project-member-empty"><el-icon><User /></el-icon>暂无成员</span>
        <small>{{ project.agent_context ? '已配置 Agent 背景' : '未配置 Agent 背景' }}</small>
      </footer>
    </article>
    <button v-if="!loading" type="button" class="project-admin-create" @click="openCreate"><el-icon><Plus /></el-icon><strong>创建新项目</strong><small>填写项目说明并分配成员</small></button>
  </section>

  <el-dialog v-model="dialogVisible" :title="dialogTitle" width="560px" class="project-admin-dialog">
    <el-form label-position="top" hide-required-asterisk>
      <el-form-item label="项目名称"><el-input v-model="form.name" maxlength="128" placeholder="例如：智能客服评测" /></el-form-item>
      <el-form-item label="项目说明"><el-input v-model="form.description" type="textarea" :rows="2" maxlength="2000" placeholder="简单说明项目用途" /></el-form-item>
      <el-form-item label="Agent 背景"><el-input v-model="form.agent_context" type="textarea" :rows="3" maxlength="8000" placeholder="例如接口约定、业务范围或评测注意事项；会自动提供给评测助手" /></el-form-item>
      <el-form-item label="项目成员"><el-select v-model="form.user_ids" multiple filterable collapse-tags collapse-tags-tooltip :max-collapse-tags="3" class="full-button" placeholder="选择可访问该项目的用户"><el-option v-for="user in users" :key="user.id" :label="user.username" :value="user.id" /></el-select></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>
