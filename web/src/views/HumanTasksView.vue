<script setup lang="ts">
import { ArrowRight, CircleCheck, Clock, Document, Refresh, WarningFilled } from '@element-plus/icons-vue'
import DOMPurify from 'dompurify'
import { ElButton, ElDialog, ElEmpty, ElIcon, ElInput, ElInputNumber, ElMessage, ElMessageBox, ElProgress, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag } from 'element-plus'
import { marked } from 'marked'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'
import type { AgentJob, AgentStep, HumanReviewAssignment, HumanReviewCampaign, HumanTask } from '../types/agent'
import { formatBeijingDateTime } from '../utils/datetime'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const detailLoading = ref(false)
const deciding = ref(false)
const submitting = ref(false)
const detailVisible = ref(false)
const reviewVisible = ref(false)
const activeArea = ref<'approval' | 'review'>('review')
const reviewRole = ref<'assigned' | 'created'>('assigned')
const assignmentView = ref<'pending' | 'completed'>('pending')
const tasks = ref<HumanTask[]>([])
const assignments = ref<HumanReviewAssignment[]>([])
const campaigns = ref<HumanReviewCampaign[]>([])
const selectedTask = ref<HumanTask | null>(null)
const selectedJob = ref<AgentJob | null>(null)
const selectedAssignment = ref<HumanReviewAssignment | null>(null)
const steps = ref<AgentStep[]>([])
const scoreForm = reactive<{ scores: Record<string, number | undefined>; reason: string }>({ scores: {}, reason: '' })
const focusedTaskId = computed(() => String(route.params.taskId ?? ''))
const canApprove = computed(() => auth.hasPermission('evaluation:review'))
interface AssignedReviewTask {
  campaignId: string
  title: string
  instructions: string
  deadlineAt: string | null
  assignments: HumanReviewAssignment[]
  pendingCount: number
  submittedCount: number
  expiredCount: number
}
const assignedReviewTasks = computed<AssignedReviewTask[]>(() => {
  const grouped = new Map<string, HumanReviewAssignment[]>()
  for (const assignment of assignments.value) {
    const group = grouped.get(assignment.campaign_id) ?? []
    group.push(assignment)
    grouped.set(assignment.campaign_id, group)
  }
  return [...grouped.entries()].map(([campaignId, items]) => {
    items.sort((left, right) => left.source_index - right.source_index)
    const first = items[0]
    return {
      campaignId,
      title: first?.campaign_title ?? '人工评审任务',
      instructions: first?.campaign_instructions ?? '',
      deadlineAt: first?.campaign_deadline_at ?? null,
      assignments: items,
      pendingCount: items.filter((item) => item.status === 'pending').length,
      submittedCount: items.filter((item) => item.status === 'submitted').length,
      expiredCount: items.filter((item) => item.status === 'expired').length,
    }
  }).sort((left, right) => Number(right.pendingCount > 0) - Number(left.pendingCount > 0))
})
const pendingReviewTasks = computed(() => assignedReviewTasks.value.filter((item) => item.pendingCount > 0))
const completedReviewTasks = computed(() => assignedReviewTasks.value.filter((item) => item.pendingCount === 0))
const visibleAssignedReviewTasks = computed(() => (
  assignmentView.value === 'pending' ? pendingReviewTasks.value : completedReviewTasks.value
))
const selectedTaskAssignments = computed(() => {
  const campaignId = selectedAssignment.value?.campaign_id
  return assignedReviewTasks.value.find((item) => item.campaignId === campaignId)?.assignments ?? []
})
const selectedCaseIndex = computed(() => selectedTaskAssignments.value.findIndex((item) => item.id === selectedAssignment.value?.id))
const overallScore = computed(() => {
  const values = Object.values(scoreForm.scores).filter((value): value is number => typeof value === 'number')
  return values.length ? Math.round(values.reduce((sum, value) => sum + value, 0) / values.length * 100) / 100 : 0
})

function statusType(status: HumanTask['status']) {
  if (status === 'approved') return 'success'
  if (status === 'rejected' || status === 'cancelled') return 'danger'
  return 'warning'
}
function statusLabel(status: HumanTask['status']) {
  return { pending: '待审核', approved: '已批准', rejected: '已拒绝', cancelled: '已取消' }[status]
}
function campaignStatusLabel(status: HumanReviewCampaign['status']) {
  return { active: '评审中', summarizing: '总结中', completed: '已完成', cancelled: '已取消' }[status]
}
function pretty(value: unknown) { return JSON.stringify(value, null, 2) }
function renderMarkdown(value: unknown) {
  if (typeof value !== 'string' || !value.trim()) return ''
  return DOMPurify.sanitize(marked.parse(value, { async: false, breaks: true, gfm: true }) as string)
}

async function loadAll() {
  loading.value = true
  try {
    const requests: Array<Promise<unknown>> = [
      api.get<HumanReviewAssignment[]>('/human-reviews/assignments/mine').then((response) => { assignments.value = response.data }),
      api.get<HumanReviewCampaign[]>('/human-reviews/campaigns/mine').then((response) => { campaigns.value = response.data }),
    ]
    if (canApprove.value) requests.push(api.get<HumanTask[]>('/human-tasks').then((response) => { tasks.value = response.data }))
    await Promise.all(requests)
    if (focusedTaskId.value && canApprove.value) {
      activeArea.value = 'approval'
      const focused = tasks.value.find((item) => item.id === focusedTaskId.value)
      if (focused) await openDetail(focused)
    }
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { loading.value = false }
}

async function openDetail(task: HumanTask) {
  selectedTask.value = task
  detailVisible.value = true
  detailLoading.value = true
  if (focusedTaskId.value !== task.id) await router.replace({ name: 'human-tasks', params: { taskId: task.id } })
  try {
    const [jobResponse, stepResponse] = await Promise.all([
      api.get<AgentJob>(`/agent-jobs/${task.job_id}`), api.get<AgentStep[]>(`/agent-jobs/${task.job_id}/steps`),
    ])
    selectedJob.value = jobResponse.data
    steps.value = stepResponse.data
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { detailLoading.value = false }
}
function openTaskRow(row: unknown) { void openDetail(row as HumanTask) }
async function closeDetail() {
  selectedTask.value = null
  selectedJob.value = null
  steps.value = []
  if (focusedTaskId.value) await router.replace({ name: 'human-tasks' })
}
async function decide(task: HumanTask, decision: 'approve' | 'reject') {
  try {
    let reason: string | undefined
    if (decision === 'reject') {
      const result = await ElMessageBox.prompt('请说明需要修改的内容或拒绝原因。', '拒绝评测方案', { inputType: 'textarea', inputValidator: (value) => Boolean(value.trim()) || '请输入原因', confirmButtonText: '确认拒绝', cancelButtonText: '取消' })
      reason = result.value
    } else await ElMessageBox.confirm('批准后，Agent 将按当前方案继续执行评测。', '批准评测方案', { confirmButtonText: '批准并继续', cancelButtonText: '取消' })
    deciding.value = true
    await api.post(`/human-tasks/${task.id}/decision`, { decision, reason })
    ElMessage.success(decision === 'approve' ? '方案已批准，任务将继续执行' : '方案已拒绝')
    detailVisible.value = false
    await closeDetail()
    await loadAll()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  } finally { deciding.value = false }
}

function selectReviewCase(assignment: HumanReviewAssignment) {
  selectedAssignment.value = assignment
  scoreForm.scores = Object.fromEntries(assignment.rubric.map((dimension) => [dimension.key, assignment.dimension_scores.find((item) => item.key === dimension.key)?.score]))
  scoreForm.reason = assignment.reason ?? ''
}
function openReviewTask(task: AssignedReviewTask) {
  selectReviewCase(task.assignments.find((item) => item.status === 'pending') ?? task.assignments[0])
  reviewVisible.value = true
}
function moveReviewCase(offset: number) {
  const target = selectedTaskAssignments.value[selectedCaseIndex.value + offset]
  if (target) selectReviewCase(target)
}
async function submitReview() {
  if (!selectedAssignment.value || selectedAssignment.value.status === 'submitted') return
  if (selectedAssignment.value.rubric.some((item) => typeof scoreForm.scores[item.key] !== 'number')) {
    return ElMessage.warning('请完成所有评分维度后再提交')
  }
  submitting.value = true
  try {
    await api.post(`/human-reviews/assignments/${selectedAssignment.value.id}/submit`, {
      dimension_scores: selectedAssignment.value.rubric.map((item) => ({ key: item.key, score: scoreForm.scores[item.key] })), overall_score: overallScore.value, reason: scoreForm.reason || null,
    })
    ElMessage.success('评分已提交')
    const campaignId = selectedAssignment.value.campaign_id
    const submittedIndex = selectedCaseIndex.value
    await loadAll()
    const refreshed = assignedReviewTasks.value.find((item) => item.campaignId === campaignId)
    const next = refreshed?.assignments.find((item, index) => item.status === 'pending' && index > submittedIndex)
      ?? refreshed?.assignments.find((item) => item.status === 'pending')
      ?? refreshed?.assignments[Math.min(submittedIndex, Math.max((refreshed?.assignments.length ?? 1) - 1, 0))]
    if (next) selectReviewCase(next)
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { submitting.value = false }
}
onMounted(loadAll)
</script>

<template>
  <header class="page-header">
    <div><p class="eyebrow">人工协作</p><h1>人工评审</h1><p>审核 Agent 执行方案，或对分配给你的模型回复进行独立评分。</p></div>
    <div class="page-actions"><el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button></div>
  </header>

  <el-tabs v-model="activeArea" class="human-workspace-tabs">
    <el-tab-pane label="方案审核" name="approval">
      <template v-if="canApprove">
        <section class="review-summary">
          <div><span>待处理</span><strong>{{ tasks.filter((item) => item.status === 'pending').length }}</strong></div>
          <div><span>已完成</span><strong>{{ tasks.filter((item) => item.status !== 'pending').length }}</strong></div>
          <p>检查 Agent 生成的执行方案，批准后任务会自动回到执行队列。</p>
        </section>
        <section class="table-panel">
          <div class="table-toolbar"><strong>方案审核记录</strong><span>共 {{ tasks.length }} 项</span></div>
          <el-table v-loading="loading" :data="tasks" row-key="id" empty-text="当前没有方案审核任务" @row-click="openTaskRow">
            <el-table-column label="任务" min-width="280"><template #default="scope"><strong>{{ scope.row.title }}</strong><p class="task-instructions">{{ scope.row.instructions }}</p></template></el-table-column>
            <el-table-column label="状态" width="110"><template #default="scope"><el-tag :type="statusType(scope.row.status)">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间（北京时间）" width="205"><template #default="scope">{{ formatBeijingDateTime(scope.row.created_at, true) }}</template></el-table-column>
            <el-table-column label="操作" width="120" fixed="right"><template #default="scope"><el-button type="primary" link @click.stop="openTaskRow(scope.row)">{{ scope.row.status === 'pending' ? '查看并审核' : '查看详情' }}</el-button></template></el-table-column>
          </el-table>
        </section>
      </template>
      <el-empty v-else description="当前账号没有方案审核权限" />
    </el-tab-pane>

    <el-tab-pane label="人工评审" name="review">
      <section class="human-review-overview">
        <div><span>我的待评</span><strong>{{ pendingReviewTasks.length }}</strong><small>按人工评审任务展示</small></div>
        <div><span>我的已评</span><strong>{{ completedReviewTasks.length }}</strong><small>评分仅你自己可见</small></div>
        <div><span>我发起的</span><strong>{{ campaigns.length }}</strong><small>查看整体进度与汇总</small></div>
      </section>
      <el-tabs v-model="reviewRole" class="review-role-tabs">
        <el-tab-pane label="我的待评" name="assigned">
          <section class="review-queue">
            <header class="review-queue-head">
              <div><h2>{{ assignmentView === 'pending' ? '待完成任务' : '已完成任务' }}</h2><p>{{ assignmentView === 'pending' ? '优先处理临近截止时间的评审任务' : '查看你已经提交的评分记录' }}</p></div>
              <div class="review-queue-filter" role="group" aria-label="评审任务状态">
                <button type="button" :class="{ active: assignmentView === 'pending' }" @click="assignmentView = 'pending'">待完成 <b>{{ pendingReviewTasks.length }}</b></button>
                <button type="button" :class="{ active: assignmentView === 'completed' }" @click="assignmentView = 'completed'">已完成 <b>{{ completedReviewTasks.length }}</b></button>
              </div>
            </header>
            <div v-if="visibleAssignedReviewTasks.length" class="review-assignment-list">
              <article v-for="task in visibleAssignedReviewTasks" :key="task.campaignId" class="review-assignment-card" :class="{ submitted: task.pendingCount === 0 }">
                <div class="review-task-main">
                  <div class="review-task-kicker">
                    <span class="review-task-status"><i></i>{{ task.pendingCount ? '进行中' : '已完成' }}</span>
                    <span v-if="task.deadlineAt" class="review-task-deadline"><el-icon><Clock /></el-icon>{{ formatBeijingDateTime(task.deadlineAt, true) }} 截止</span>
                  </div>
                  <h3>{{ task.title }}</h3>
                  <p>{{ task.instructions || '暂无补充说明' }}</p>
                  <div class="review-task-rubric">
                    <span v-for="dimension in (task.assignments[0]?.rubric ?? [])" :key="dimension.key">{{ dimension.label }} · {{ dimension.min_score }}–{{ dimension.max_score }} 分</span>
                  </div>
                </div>
                <div class="review-task-progress">
                  <div><span>完成进度</span><strong>{{ task.submittedCount }}<small>/{{ task.assignments.length }}</small></strong></div>
                  <el-progress :percentage="task.assignments.length ? Math.round(task.submittedCount / task.assignments.length * 100) : 0" :stroke-width="7" :show-text="false" />
                  <span v-if="task.pendingCount">剩余 {{ task.pendingCount }} 条待评</span><span v-else>全部评分已提交</span>
                </div>
                <button type="button" class="review-task-action" @click="openReviewTask(task)"><span>{{ task.pendingCount ? '继续评审' : '查看结果' }}</span><el-icon><ArrowRight /></el-icon></button>
              </article>
            </div>
            <el-empty v-else :description="assignmentView === 'pending' ? '当前没有待完成的评审任务' : '当前没有已完成的评审任务'" />
          </section>
        </el-tab-pane>
        <el-tab-pane label="我发起的" name="created">
          <div v-if="campaigns.length" class="review-campaign-list">
            <article v-for="campaign in campaigns" :key="campaign.id" class="review-campaign-card">
              <div class="review-card-head"><div><h3>{{ campaign.title }}</h3><p>{{ campaign.instructions || '暂无补充说明' }}</p></div><el-tag :type="campaign.status === 'completed' ? 'success' : 'primary'">{{ campaignStatusLabel(campaign.status) }}</el-tag></div>
              <el-progress :percentage="campaign.total_assignments ? Math.round(campaign.completed_assignments / campaign.total_assignments * 100) : 0" />
              <div class="campaign-facts"><span>{{ campaign.item_count }} 条样本</span><span>{{ campaign.completed_assignments }}/{{ campaign.total_assignments }} 份评分</span><span v-if="campaign.deadline_at">截止 {{ formatBeijingDateTime(campaign.deadline_at, true) }}</span><span v-if="campaign.summary.average_overall_score != null">综合 {{ campaign.summary.average_overall_score }}/10</span></div>
              <p v-for="warning in (campaign.blind_config.evidence_warnings as string[] || [])" :key="warning" class="task-instructions">{{ warning }}</p>
              <div v-if="campaign.summary.markdown" class="campaign-summary markdown-body" v-html="renderMarkdown(campaign.summary.markdown)"></div>
            </article>
          </div>
          <el-empty v-else description="你还没有发起人工评审任务" />
        </el-tab-pane>
      </el-tabs>
    </el-tab-pane>
  </el-tabs>

  <el-dialog v-model="detailVisible" width="min(820px, 94vw)" class="review-dialog" destroy-on-close @closed="closeDetail">
    <template #header><div class="review-dialog-head"><span>评测方案</span><el-tag v-if="selectedTask" :type="statusType(selectedTask.status)">{{ statusLabel(selectedTask.status) }}</el-tag></div></template>
    <div v-loading="detailLoading" class="review-detail"><template v-if="selectedTask && selectedJob">
      <div class="review-title"><span>任务 {{ selectedJob.id.slice(0, 8) }}</span><h2>{{ selectedJob.title }}</h2><p>{{ selectedJob.goal }}</p></div>
      <div class="review-checks">
        <div><el-icon><Document /></el-icon><span>数据结构</span><strong>{{ (selectedJob.eval_spec.source as Record<string, unknown>)?.format || '待识别' }}</strong></div>
        <div><el-icon><CircleCheck /></el-icon><span>执行步骤</span><strong>{{ Array.isArray(selectedJob.eval_spec.operations) ? selectedJob.eval_spec.operations.length : 0 }} 项</strong></div>
        <div><el-icon><WarningFilled /></el-icon><span>待配置项</span><strong>{{ Array.isArray(selectedJob.eval_spec.needs_configuration) ? selectedJob.eval_spec.needs_configuration.length : 0 }} 项</strong></div>
      </div>
      <div class="job-section"><div class="section-heading"><h2>Agent 生成的方案</h2><span>请重点检查目标、字段映射和评测项</span></div><pre class="json-panel review-json">{{ pretty(selectedJob.eval_spec) }}</pre></div>
      <div v-if="selectedTask.decision_reason" class="decision-reason"><strong>处理说明</strong><p>{{ selectedTask.decision_reason }}</p><span>{{ formatBeijingDateTime(selectedTask.resolved_at, true) }}</span></div>
    </template></div>
    <template #footer><template v-if="selectedTask?.status === 'pending'"><el-button :disabled="deciding" @click="decide(selectedTask, 'reject')">拒绝并说明</el-button><el-button type="primary" :loading="deciding" @click="decide(selectedTask, 'approve')">批准并继续</el-button></template><el-button v-else @click="detailVisible = false">关闭</el-button></template>
  </el-dialog>

  <el-dialog v-model="reviewVisible" width="min(1040px, 96vw)" class="review-score-dialog" align-center destroy-on-close>
    <template #header><div class="review-dialog-head"><span>{{ selectedAssignment?.campaign_title }}</span><el-tag v-if="selectedAssignment" :type="selectedAssignment.status === 'submitted' ? 'success' : 'warning'">{{ selectedAssignment.status === 'submitted' ? '已提交' : '匿名评审' }}</el-tag></div></template>
    <template v-if="selectedAssignment">
      <div class="review-case-navigator"><strong>Case {{ selectedCaseIndex + 1 }} / {{ selectedTaskAssignments.length }}</strong><span>已完成 {{ selectedTaskAssignments.filter((item) => item.status === 'submitted').length }} 条</span></div>
      <el-progress :percentage="selectedTaskAssignments.length ? Math.round(selectedTaskAssignments.filter((item) => item.status === 'submitted').length / selectedTaskAssignments.length * 100) : 0" />
      <div class="review-workbench">
        <section class="review-reading-pane">
          <div class="blind-review-content">
            <div><span>测试问题</span><div class="review-content-text">{{ selectedAssignment.prompt || '未提供' }}</div></div>
            <div><span>候选回复</span><div class="review-content-text response">{{ selectedAssignment.response }}</div></div>
            <div v-if="Object.keys(selectedAssignment.metadata).length" class="review-visible-meta"><span v-for="(value, key) in selectedAssignment.metadata" :key="key"><b>{{ key }}</b>{{ value }}</span></div>
          </div>
        </section>
        <section class="review-score-form">
          <div class="score-form-heading"><div><strong>本条评分</strong><span>各维度独立评分</span></div><em>{{ overallScore || '-' }}</em></div>
          <div v-for="dimension in selectedAssignment.rubric" :key="dimension.key" class="score-dimension">
            <div><label>{{ dimension.label }}</label><small>{{ dimension.min_score }}–{{ dimension.max_score }} 分</small></div>
            <el-input-number v-model="scoreForm.scores[dimension.key]" :min="dimension.min_score" :max="dimension.max_score" :precision="1" :step="0.5" :disabled="selectedAssignment.status !== 'pending'" />
          </div>
          <div class="computed-overall"><span>综合评分</span><strong>{{ overallScore }}/10</strong></div>
          <div class="review-reason-field"><label>评分依据 <small>选填</small></label><el-input v-model="scoreForm.reason" type="textarea" :rows="5" maxlength="10000" show-word-limit placeholder="记录判断依据，便于后续复盘" :disabled="selectedAssignment.status !== 'pending'" /></div>
        </section>
      </div>
    </template>
    <template #footer><el-button :disabled="selectedCaseIndex <= 0" @click="moveReviewCase(-1)">上一条</el-button><el-button :disabled="selectedCaseIndex >= selectedTaskAssignments.length - 1" @click="moveReviewCase(1)">下一条</el-button><el-button @click="reviewVisible = false">关闭</el-button><el-button v-if="selectedAssignment?.status === 'pending'" type="primary" :loading="submitting" @click="submitReview">提交并继续</el-button></template>
  </el-dialog>
</template>
