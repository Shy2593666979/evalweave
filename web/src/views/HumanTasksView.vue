<script setup lang="ts">
import { ElButton, ElMessage, ElMessageBox, ElTable, ElTableColumn, ElTag } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, errorMessage } from '../api/client'

type HumanTaskStatus = 'pending' | 'approved' | 'rejected' | 'cancelled'

interface HumanTask {
  id: string
  job_id: string
  title: string
  instructions: string
  status: HumanTaskStatus
  decision_reason: string | null
  resolved_by: string | null
  resolved_at: string | null
  created_at: string
}

const route = useRoute()
const loading = ref(false)
const tasks = ref<HumanTask[]>([])
const focusedTaskId = computed(() => String(route.params.taskId ?? ''))

function statusType(status: HumanTaskStatus) {
  if (status === 'approved') return 'success'
  if (status === 'rejected' || status === 'cancelled') return 'danger'
  return 'warning'
}

async function loadTasks() {
  loading.value = true
  try {
    tasks.value = (await api.get<HumanTask[]>('/human-tasks')).data
    if (focusedTaskId.value) {
      tasks.value.sort((left, right) =>
        left.id === focusedTaskId.value ? -1 : right.id === focusedTaskId.value ? 1 : 0,
      )
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

async function decide(taskId: string, decision: 'approve' | 'reject') {
  try {
    let reason: string | undefined
    if (decision === 'reject') {
      const result = await ElMessageBox.prompt('请说明拒绝原因', '拒绝任务', {
        inputType: 'textarea',
        inputValidator: (value) => Boolean(value.trim()) || '请输入原因',
      })
      reason = result.value
    } else {
      await ElMessageBox.confirm('确认让 Agent 按当前 EvalSpec 继续执行？', '批准任务')
    }
    await api.post(`/human-tasks/${taskId}/decision`, { decision, reason })
    ElMessage.success(decision === 'approve' ? '已批准，Agent 将继续执行' : '已拒绝')
    await loadTasks()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(errorMessage(error))
  }
}

onMounted(loadTasks)
</script>

<template>
  <header class="page-header">
    <div>
      <p class="eyebrow">HUMAN IN THE LOOP</p>
      <h1>人工协同任务</h1>
      <p>审查 Agent 生成的评测方案，并决定是否继续执行。</p>
    </div>
    <el-button :loading="loading" @click="loadTasks">刷新</el-button>
  </header>

  <section class="table-panel">
    <el-table v-loading="loading" :data="tasks" row-key="id">
      <el-table-column label="任务" min-width="240">
        <template #default="scope">
          <strong>{{ scope.row.title }}</strong>
          <p class="task-instructions">{{ scope.row.instructions }}</p>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="scope">
          <el-tag :type="statusType(scope.row.status)">{{ scope.row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="210" />
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="scope">
          <template v-if="scope.row.status === 'pending'">
            <el-button type="primary" link @click="decide(scope.row.id, 'approve')">批准</el-button>
            <el-button type="danger" link @click="decide(scope.row.id, 'reject')">拒绝</el-button>
          </template>
          <span v-else>{{ scope.row.decision_reason || '已处理' }}</span>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.task-instructions { margin: 6px 0 0; color: #818898; }
</style>
