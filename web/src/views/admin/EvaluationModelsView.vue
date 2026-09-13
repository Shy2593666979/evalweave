<script setup lang="ts">
import { Cpu, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import { ElButton, ElDialog, ElForm, ElFormItem, ElIcon, ElInput, ElMessage, ElOption, ElSelect, ElSwitch, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { api, errorMessage } from '../../api/client'
import type { EvaluationModel } from '../../types/agent'
import { formatBeijingDateTime } from '../../utils/datetime'

const models = ref<EvaluationModel[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editingId = ref('')
const form = reactive({
  name: '',
  base_url: '',
  model_name: '',
  api_mode: 'responses' as 'responses' | 'chat_completions',
  api_key: '',
  is_active: true,
})

async function load() {
  loading.value = true
  try {
    models.value = (await api.get<EvaluationModel[]>('/admin/evaluation-models')).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = ''
  Object.assign(form, { name: '', base_url: '', model_name: '', api_mode: 'responses', api_key: '', is_active: true })
  dialogVisible.value = true
}

function openEdit(model: EvaluationModel) {
  editingId.value = model.id
  Object.assign(form, {
    name: model.name,
    base_url: model.base_url,
    model_name: model.model_name,
    api_mode: model.api_mode,
    api_key: '',
    is_active: model.is_active,
  })
  dialogVisible.value = true
}

function openEditById(modelId: string) {
  const model = models.value.find((item) => item.id === modelId)
  if (model) openEdit(model)
}

async function save() {
  if (!form.name.trim() || !form.base_url.trim() || !form.model_name.trim()) {
    return ElMessage.warning('请填写模型名称、接口地址和模型标识')
  }
  if (!editingId.value && !form.api_key.trim()) return ElMessage.warning('请填写 API Key')
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      model_name: form.model_name.trim(),
      api_mode: form.api_mode,
      is_active: form.is_active,
    }
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()
    if (editingId.value) await api.patch(`/admin/evaluation-models/${editingId.value}`, payload)
    else await api.post('/admin/evaluation-models', payload)
    dialogVisible.value = false
    ElMessage.success(editingId.value ? '评测模型已更新' : '评测模型已添加')
    await load()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function toggle(model: EvaluationModel, active: boolean) {
  try {
    await api.patch(`/admin/evaluation-models/${model.id}`, { is_active: active })
    ElMessage.success(active ? '模型已启用' : '模型已停用')
  } catch (error) {
    model.is_active = !active
    ElMessage.error(errorMessage(error))
  }
}

function toggleById(modelId: string, active: boolean) {
  const model = models.value.find((item) => item.id === modelId)
  if (model) void toggle(model, active)
}

onMounted(load)
</script>

<template>
  <header class="page-header">
    <div><p class="eyebrow">模型配置</p><h1>评测模型</h1><p>管理智能体规划和结果总结所使用的模型。</p></div>
    <div class="page-actions"><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button><el-button type="primary" :icon="Plus" @click="openCreate">添加模型</el-button></div>
  </header>

  <section class="model-overview surface">
    <span class="model-overview-icon"><el-icon><Cpu /></el-icon></span>
    <div><strong>{{ models.filter((item) => item.is_active).length }} 个可用模型</strong><p>用户创建评测时可从已启用的模型中选择。密钥经过加密保存，不会在页面和接口中回显。</p></div>
  </section>

  <section class="table-panel">
    <div class="table-toolbar"><strong>模型列表</strong><span>共 {{ models.length }} 项</span></div>
    <el-table :data="models" v-loading="loading" empty-text="还没有评测模型">
      <el-table-column label="显示名称" min-width="160"><template #default="scope"><strong>{{ scope.row.name }}</strong></template></el-table-column>
      <el-table-column prop="model_name" label="模型标识" min-width="180" />
      <el-table-column prop="base_url" label="接口地址" min-width="260" show-overflow-tooltip />
      <el-table-column label="接口模式" width="150"><template #default="scope"><el-tag type="info">{{ scope.row.api_mode === 'responses' ? 'Responses' : 'Chat Completions' }}</el-tag></template></el-table-column>
      <el-table-column label="更新时间（北京时间）" width="205"><template #default="scope">{{ formatBeijingDateTime(scope.row.updated_at, true) }}</template></el-table-column>
      <el-table-column label="启用" width="90"><template #default="scope"><el-switch v-model="scope.row.is_active" @change="toggleById(scope.row.id, Boolean($event))" /></template></el-table-column>
      <el-table-column label="操作" width="90" fixed="right"><template #default="scope"><el-button link type="primary" :icon="Edit" @click="openEditById(scope.row.id)">编辑</el-button></template></el-table-column>
    </el-table>
  </section>

  <el-dialog v-model="dialogVisible" :title="editingId ? '编辑评测模型' : '添加评测模型'" width="min(620px, 92vw)">
    <el-form label-position="top">
      <div class="form-grid">
        <el-form-item label="显示名称"><el-input v-model="form.name" placeholder="例如：DeepSeek 快速模型" /></el-form-item>
        <el-form-item label="模型标识"><el-input v-model="form.model_name" placeholder="例如：deepseek-v4-flash" /></el-form-item>
      </div>
      <el-form-item label="接口地址"><el-input v-model="form.base_url" placeholder="例如：https://api.deepseek.com" /></el-form-item>
      <div class="form-grid">
        <el-form-item label="接口模式"><el-select v-model="form.api_mode"><el-option label="Responses API" value="responses" /><el-option label="Chat Completions" value="chat_completions" /></el-select></el-form-item>
        <el-form-item label="状态"><el-switch v-model="form.is_active" inline-prompt active-text="启用" inactive-text="停用" /></el-form-item>
      </div>
      <el-form-item :label="editingId ? '更新 API Key' : 'API Key'"><el-input v-model="form.api_key" type="password" show-password autocomplete="new-password" :placeholder="editingId ? '留空则保持原密钥' : '仅用于服务端调用，不会回显'" /></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>
