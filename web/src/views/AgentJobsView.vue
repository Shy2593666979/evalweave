<script setup lang="ts">
import {
  CircleCheck,
  Clock,
  Document,
  Download,
  Refresh,
  WarningFilled,
} from '@element-plus/icons-vue'
import {
  ElButton,
  ElEmpty,
  ElIcon,
  ElMessage,
  ElTag,
} from 'element-plus'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'
import type { AgentJob, AgentRuntime, AgentStep, FileObject, Project } from '../types/agent'
import { formatBeijingDateTime, parseServerDateTime } from '../utils/datetime'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const detailLoading = ref(false)
const projects = ref<Project[]>([])
const selectedProjectId = ref('')
const files = ref<FileObject[]>([])
const jobs = ref<AgentJob[]>([])
const selectedJob = ref<AgentJob | null>(null)
const steps = ref<AgentStep[]>([])
const runtime = ref<AgentRuntime | null>(null)
const workerProbeFailures = ref(0)
let jobEventSource: EventSource | null = null
let jobEventJobId = ''
let viewActive = false
let runtimeRetryTimer: ReturnType<typeof setTimeout> | null = null
let elapsedTimer: ReturnType<typeof setInterval> | null = null
const currentTime = ref(Date.now())

async function loadRuntime() {
  try {
    const response = await api.get<AgentRuntime>('/agent/runtime')
    if (!viewActive) return
    runtime.value = response.data
    workerProbeFailures.value = response.data.worker_available ? 0 : workerProbeFailures.value + 1
    if (!response.data.worker_available) {
      if (runtimeRetryTimer) clearTimeout(runtimeRetryTimer)
      runtimeRetryTimer = setTimeout(() => { void loadRuntime() }, 3000)
    }
  } catch {
    if (viewActive) {
      if (runtimeRetryTimer) clearTimeout(runtimeRetryTimer)
      runtimeRetryTimer = setTimeout(() => { void loadRuntime() }, 3000)
    }
  }
}

function jobRenderKey(job: AgentJob | null) {
  if (!job) return ''
  const { updated_at: _updatedAt, ...visibleData } = job
  return JSON.stringify(visibleData)
}

function stepsRenderKey(items: AgentStep[]) {
  return JSON.stringify(items)
}

const canRun = computed(() => auth.hasPermission('experiment:run'))
const selectedFile = computed(() => files.value.find((item) => item.id === selectedJob.value?.source_file_id))
const isPythonJob = computed(() => selectedJob.value?.input_config.job_type === 'python')
const currentSteps = computed(() => {
  const latest = new Map<string, AgentStep>()
  for (const step of steps.value) {
    if (step.name.startsWith('preflight_')) continue
    latest.set(step.name, step)
  }
  return [...latest.values()]
})
const hasEvaluationPlan = computed(() => (
  currentSteps.value.some((step) => (
    step.name === 'generate_eval_spec' && step.status === 'completed'
  ))
  && Object.keys(selectedJob.value?.eval_spec ?? {}).length > 0
))
const progressSegments = computed(() => {
  const total = currentSteps.value.length
  if (!total) return { completed: 0, activeLeft: 0, activeWidth: 0, activeStatus: '' }
  const completed = currentSteps.value.filter((step) => step.status === 'completed').length
  const activeIndex = currentSteps.value.findIndex((step) => (
    step.status === 'running' || step.status === 'failed'
  ))
  return {
    completed: selectedJob.value?.status === 'completed' ? 100 : (completed / total) * 100,
    activeLeft: activeIndex < 0 ? 0 : (activeIndex / total) * 100,
    activeWidth: activeIndex < 0 ? 0 : 100 / total,
    activeStatus: activeIndex < 0 ? '' : currentSteps.value[activeIndex].status,
  }
})
const isActive = (status: AgentJob['status']) =>
  ['pending', 'discovering', 'planning', 'running', 'analyzing'].includes(status)

const executionElapsed = computed(() => {
  const job = selectedJob.value
  if (!job) return '0 秒'
  const executionStep = currentSteps.value.find((step) => step.name === 'execute_eval_spec')
  const started = parseServerDateTime(executionStep?.started_at ?? null)?.getTime()
  if (!started) return '0 秒'
  const running = executionStep?.status === 'running'
  const ended = running
    ? currentTime.value
    : parseServerDateTime(executionStep?.finished_at ?? null)?.getTime() ?? currentTime.value
  const totalSeconds = Math.max(0, Math.floor((ended - started) / 1000))
  if (totalSeconds < 60) return `${totalSeconds} 秒`
  const seconds = totalSeconds % 60
  const totalMinutes = Math.floor(totalSeconds / 60)
  if (totalMinutes < 60) return `${totalMinutes} 分钟 ${seconds} 秒`
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return `${hours} 小时 ${minutes} 分钟 ${seconds} 秒`
})

const statusLabels: Record<AgentJob['status'], string> = {
  pending: '等待开始',
  discovering: '读取数据',
  planning: '生成方案',
  waiting_human: '人工评审中',
  running: '执行评测',
  analyzing: '整理结果',
  completed: '已完成',
  failed: '运行失败',
  cancelled: '已取消',
}

const stepLabels: Record<string, string> = {
  discover_source: '解析评测数据',
  generate_eval_spec: '生成评测方案',
  prepare_data: '准备评测数据',
  generate_test_cases: '生成测试用例',
  validate_target: '预检目标接口',
  execute_eval_spec: '执行评测任务',
  summarize: '整理评测结果',
}

function stepLabel(step: AgentStep) {
  const dynamicLabel = step.output_data.label
  return typeof dynamicLabel === 'string' && dynamicLabel.trim()
    ? dynamicLabel
    : (stepLabels[step.name] ?? step.name)
}

function stepMeta(step: AgentStep) {
  if (step.status === 'pending') return '等待执行'
  const prefix = step.output_data.phase === 'preparation' ? '方案准备' : `第 ${step.attempt} 次`
  return `${prefix} · ${formatBeijingDateTime(step.started_at)}`
}

const outputLabels = { xlsx: 'Excel', jsonl: 'JSONL', markdown: 'Markdown', text: '纯文本' }
const operationLabels: Record<string, string> = {
  normalize: '整理与标准化数据',
  data_analysis: '分析数据特征',
  data_transform: '转换和补充字段',
  format_convert: '生成交付文件',
  ranking: '计算排名',
  aggregate: '汇总统计指标',
  target_call: '调用目标接口',
  multi_target_call: '对比多个接口',
  conversation_eval: '评估对话质量',
  tool_eval: '评估工具调用',
  latency_eval: '评估响应速度',
  safety_eval: '检查安全风险',
  human_review: '提交人工审核',
  summarize: '总结评测结果',
  convert: '转换文件格式',
  prepare: '准备评测数据',
  evaluate: '执行评测任务',
  model_map: '使用模型逐行处理',
  http_map: '逐行调用接口',
}

function readableValue(value: unknown) {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(String).join('、')
  if (!value || typeof value !== 'object') return ''
  const record = value as Record<string, unknown>
  const format = record.format ? `${String(record.format).toUpperCase()} 文件` : ''
  const fields = Array.isArray(record.fields) && record.fields.length
    ? `包含字段：${record.fields.map(String).join('、')}`
    : ''
  return [format, fields].filter(Boolean).join('，') || JSON.stringify(value, null, 2)
}

function renderMarkdown(value: unknown) {
  if (typeof value !== 'string' || !value.trim()) return ''
  return DOMPurify.sanitize(marked.parse(value, {
    async: false,
    breaks: true,
    gfm: true,
  }) as string)
}

const readablePlan = computed(() => {
  const spec = selectedJob.value?.eval_spec ?? {}
  const operations = Array.isArray(spec.operations)
    ? spec.operations.map(String)
    : []
  const program = spec.data_program && typeof spec.data_program === 'object'
    ? spec.data_program as Record<string, unknown>
    : {}
  const programSteps = Array.isArray(program.steps)
    ? program.steps.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    : []
  const steps = programSteps.length
    ? programSteps.map((step, index) => {
      const type = String(step.type ?? step.action ?? step.tool ?? '')
      const columns = Array.isArray(step.output_columns)
        ? step.output_columns
          .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
          .map((item) => String(item.name ?? ''))
          .filter(Boolean)
        : []
      return {
        key: `${type}-${index}`,
        title: String(step.title ?? operationLabels[type] ?? `执行步骤 ${index + 1}`),
        description: String(step.instruction ?? step.note ?? '按照任务配置完成本步骤。'),
        detail: columns.length ? `输出字段：${columns.join('、')}` : '',
      }
    })
    : operations.map((operation, index) => ({
      key: `${operation}-${index}`,
      title: operationLabels[operation] ?? operation,
      description: '按照评测目标执行并记录可审计结果。',
      detail: '',
    }))
  return {
    source: readableValue(spec.source || spec.discovery),
    output: readableValue(spec.output_spec),
    operations: operations.map((operation) => operationLabels[operation] ?? operation),
    steps,
  }
})

function statusType(status: AgentJob['status']) {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'waiting_human') return 'warning'
  return 'primary'
}

function pretty(value: unknown) {
  return JSON.stringify(value, null, 2)
}

async function loadProjects() {
  projects.value = (await api.get<Project[]>('/projects')).data
  const remembered = localStorage.getItem('evalweave-project')
  if (!selectedProjectId.value) {
    selectedProjectId.value = projects.value.some((item) => item.id === remembered)
      ? String(remembered)
      : projects.value[0]?.id ?? ''
  }
}

async function loadProjectData(quiet = false) {
  if (!selectedProjectId.value) {
    stopJobEventStream()
    files.value = []
    jobs.value = []
    selectedJob.value = null
    return
  }
  if (!quiet) loading.value = true
  try {
    const [fileResponse, jobResponse] = await Promise.all([
      api.get<FileObject[]>(`/projects/${selectedProjectId.value}/files`, { params: { category: 'dataset_source' } }),
      api.get<AgentJob[]>(`/projects/${selectedProjectId.value}/agent-jobs`),
    ])
    files.value = fileResponse.data
    jobs.value = jobResponse.data
    const routeJobId = String(route.params.jobId ?? '')
    const currentId = routeJobId || selectedJob.value?.id
    const next = jobs.value.find((item) => item.id === currentId) ?? jobs.value[0] ?? null
    if (next) await selectJob(next, true)
    else {
      stopJobEventStream()
      selectedJob.value = null
      steps.value = []
    }
  } catch (error) {
    if (!quiet) ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function applyJobDetail(job: AgentJob, nextSteps: AgentStep[]) {
  const jobChanged = jobRenderKey(selectedJob.value) !== jobRenderKey(job)
  const stepsChanged = stepsRenderKey(steps.value) !== stepsRenderKey(nextSteps)
  if (jobChanged) selectedJob.value = job
  if (stepsChanged) steps.value = nextSteps
  const index = jobs.value.findIndex((item) => item.id === job.id)
  if (index >= 0 && jobRenderKey(jobs.value[index] ?? null) !== jobRenderKey(job)) {
    jobs.value[index] = job
  }
}

function stopJobEventStream() {
  jobEventSource?.close()
  jobEventSource = null
  jobEventJobId = ''
}

function startJobEventStream(jobId: string) {
  if (jobEventJobId === jobId && jobEventSource) return
  stopJobEventStream()
  jobEventJobId = jobId
  const source = new EventSource(`/api/agent-jobs/${jobId}/events`)
  jobEventSource = source
  source.addEventListener('job-state', (event) => {
    try {
      const state = JSON.parse((event as MessageEvent).data) as {
        job: AgentJob
        steps: AgentStep[]
      }
      if (selectedJob.value?.id === state.job.id) applyJobDetail(state.job, state.steps)
    } catch {
      // Ignore malformed snapshots; the next changed snapshot will replace it.
    }
  })
  source.addEventListener('end', () => stopJobEventStream())
}

async function loadJobDetail(jobId: string, quiet = false) {
  if (!quiet) detailLoading.value = true
  try {
    const [jobResponse, stepResponse] = await Promise.all([
      api.get<AgentJob>(`/agent-jobs/${jobId}`),
      api.get<AgentStep[]>(`/agent-jobs/${jobId}/steps`),
    ])
    applyJobDetail(jobResponse.data, stepResponse.data)
    if (isActive(jobResponse.data.status)) startJobEventStream(jobId)
    else stopJobEventStream()
  } catch (error) {
    if (!quiet) ElMessage.error(errorMessage(error))
  } finally {
    detailLoading.value = false
  }
}

async function selectJob(job: AgentJob, quiet = false) {
  selectedJob.value = job
  if (
    viewActive
    && route.name === 'evaluations'
    && String(route.params.jobId ?? '') !== job.id
  ) {
    await router.replace({ name: 'evaluations', params: { jobId: job.id } })
  }
  await loadJobDetail(job.id, quiet)
}

async function restartJob() {
  if (!selectedJob.value) return
  try {
    await api.post(`/agent-jobs/${selectedJob.value.id}/start`)
    ElMessage.success('任务已重新启动')
    await loadJobDetail(selectedJob.value.id)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function cancelJob() {
  if (!selectedJob.value) return
  try {
    const response = await api.post<AgentJob>(`/agent-jobs/${selectedJob.value.id}/cancel`)
    ElMessage.success('任务已停止')
    await loadJobDetail(response.data.id)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function createScheduledTask() {
  if (!selectedJob.value) return
  await router.push({
    name: 'schedules',
    query: { sourceJobId: selectedJob.value.id },
  })
}

async function refresh() {
  await Promise.all([loadProjectData(), loadRuntime()])
}

function downloadResult() {
  if (selectedJob.value?.result_file_id) {
    window.open(`/api/files/${selectedJob.value.result_file_id}/content`, '_blank')
  }
}

watch(selectedProjectId, (value) => {
  if (value) localStorage.setItem('evalweave-project', value)
})

onMounted(async () => {
  viewActive = true
  elapsedTimer = setInterval(() => { currentTime.value = Date.now() }, 1000)
  void loadRuntime()
  try {
    await loadProjects()
    if (!viewActive) return
    await loadProjectData()
  } catch (error) {
    if (viewActive) ElMessage.error(errorMessage(error))
  }
})

onBeforeUnmount(() => {
  viewActive = false
  if (runtimeRetryTimer) clearTimeout(runtimeRetryTimer)
  if (elapsedTimer) clearInterval(elapsedTimer)
  stopJobEventStream()
})
</script>

<template>
  <header class="page-header agent-page-header">
    <div>
      <h1>评测任务</h1>
      <p>从数据准备到结果检查，完整跟踪每一次智能评测。</p>
    </div>
    <div class="page-actions">
      <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
    </div>
  </header>

  <div v-if="runtime && !runtime.enabled" class="agent-notice">
    <el-icon><WarningFilled /></el-icon>
    <div><strong>当前使用基础规划模式</strong><span>模型规划尚未启用，任务仍可运行，但只会生成默认评测方案。</span></div>
  </div>
  <div v-if="runtime && !runtime.worker_available && workerProbeFailures >= 2" class="agent-notice worker-notice">
    <el-icon><WarningFilled /></el-icon>
    <div><strong>任务执行进程未连接</strong><span>请启动 Celery Worker；否则新任务会停留在等待开始状态。</span></div>
  </div>

  <section class="agent-workspace" :class="{ 'is-empty': !jobs.length }" v-loading="loading">
    <aside class="job-list surface">
      <div class="job-list-head"><strong>任务记录</strong><span>{{ jobs.length }}</span></div>
      <button
        v-for="job in jobs"
        :key="job.id"
        class="job-item"
        :class="{ active: selectedJob?.id === job.id }"
        type="button"
        @click="selectJob(job)"
      >
        <span class="job-item-title">{{ job.title }}</span>
        <span class="job-item-meta"><el-tag size="small" :type="statusType(job.status)">{{ statusLabels[job.status] }}</el-tag>{{ formatBeijingDateTime(job.updated_at) }}</span>
      </button>
      <el-empty v-if="!jobs.length" :image-size="58" description="还没有评测任务" />
    </aside>

    <main class="job-detail surface" v-loading="detailLoading">
      <template v-if="selectedJob">
        <div class="job-detail-head">
          <div><span class="job-id">任务 {{ selectedJob.id.slice(0, 8) }}</span><h2>{{ selectedJob.title }}</h2><p>{{ selectedJob.goal }}</p></div>
          <div class="job-head-actions"><el-button v-if="canRun && selectedJob.status === 'completed' && Object.keys(selectedJob.eval_spec).length" size="small" type="primary" plain @click="createScheduledTask">创建定时任务</el-button><el-button v-if="canRun && ['pending', 'discovering', 'planning', 'waiting_human', 'running', 'analyzing'].includes(selectedJob.status)" size="small" type="danger" plain @click="cancelJob">停止任务</el-button><el-button v-if="canRun && ['completed', 'cancelled'].includes(selectedJob.status)" size="small" @click="restartJob">重新运行</el-button><el-tag size="large" :type="statusType(selectedJob.status)">{{ statusLabels[selectedJob.status] }}</el-tag></div>
        </div>

        <div class="job-progress">
          <div
            class="job-progress-line"
          >
            <span class="completed-fill" :style="{ width: `${progressSegments.completed}%` }"></span>
            <span
              v-if="progressSegments.activeWidth"
              class="active-fill"
              :class="progressSegments.activeStatus"
              :style="{ left: `${progressSegments.activeLeft}%`, width: `${progressSegments.activeWidth}%` }"
            ></span>
          </div>
          <div v-if="currentSteps.length" class="job-progress-labels" :style="{ gridTemplateColumns: `repeat(${currentSteps.length}, minmax(0, 1fr))` }">
            <span v-for="step in currentSteps" :key="`progress-label-${step.id}`" :class="step.status" :title="stepLabel(step)">{{ stepLabel(step) }}</span>
          </div>
          <div v-else class="job-progress-empty">等待任务生成运行步骤</div>
        </div>

        <div v-if="selectedJob.status === 'waiting_human'" class="decision-callout">
          <div><strong>人工评审进行中</strong><p>评审人员完成评分后，系统会自动汇总结果。</p></div>
          <router-link :to="`/human-tasks`"><el-button type="primary">查看人工评审</el-button></router-link>
        </div>
        <div v-if="selectedJob.error" class="error-callout"><el-icon><WarningFilled /></el-icon><div><strong>任务执行失败</strong><p>{{ selectedJob.error }}</p></div><el-button v-if="canRun" @click="restartJob">重新运行</el-button></div>

        <div class="job-facts">
          <div><span>创建时间（北京时间）</span><strong>{{ formatBeijingDateTime(selectedJob.created_at) }}</strong></div>
          <div v-if="isPythonJob"><span>执行时间</span><strong>{{ executionElapsed }}</strong></div>
          <div v-else><span>数据文件</span><strong>{{ selectedFile?.original_name ?? '未选择' }}</strong></div>
          <div><span>结果格式</span><strong>{{ outputLabels[String(selectedJob.input_config.output_format ?? 'xlsx') as keyof typeof outputLabels] ?? '文件' }}</strong></div>
        </div>

        <div class="job-section">
          <div class="section-heading"><h2>运行步骤</h2><span>自动刷新</span></div>
          <div v-if="currentSteps.length" class="step-list">
            <div v-for="step in currentSteps" :key="step.id" class="step-row">
              <span class="step-icon" :class="step.status"><el-icon><CircleCheck v-if="step.status === 'completed'" /><WarningFilled v-else-if="['failed', 'cancelled'].includes(step.status)" /><Clock v-else /></el-icon></span>
              <div><strong>{{ stepLabel(step) }}</strong><span>{{ stepMeta(step) }}</span><p v-if="step.error">{{ step.error }}</p></div>
              <el-tag size="small" :type="step.status === 'completed' ? 'success' : ['failed', 'cancelled'].includes(step.status) ? 'danger' : step.status === 'running' ? 'primary' : 'info'">{{ step.status === 'completed' ? '完成' : step.status === 'failed' ? '失败' : step.status === 'cancelled' ? '已取消' : step.status === 'running' ? '进行中' : '未执行' }}</el-tag>
            </div>
          </div>
          <p v-else class="section-empty">任务启动后，这里会显示每一步的执行状态。</p>
        </div>

        <div v-if="hasEvaluationPlan" class="job-section">
          <div class="section-heading"><h2>评测方案</h2><span>Agent 生成</span></div>
          <div class="plan-readable">
            <div class="plan-intro"><strong>方案目标</strong><div class="markdown-body" v-html="renderMarkdown(selectedJob.goal)"></div></div>
            <div v-if="readablePlan.source || readablePlan.output" class="plan-context-grid">
              <div v-if="readablePlan.source"><span>数据来源</span><div class="markdown-body" v-html="renderMarkdown(readablePlan.source)"></div></div>
              <div v-if="readablePlan.output"><span>交付方式</span><div class="markdown-body" v-html="renderMarkdown(readablePlan.output)"></div></div>
            </div>
            <div v-if="readablePlan.operations.length" class="plan-operation-tags">
              <span v-for="operation in readablePlan.operations" :key="operation">{{ operation }}</span>
            </div>
            <ol v-if="readablePlan.steps.length" class="plan-step-list">
              <li v-for="(step, index) in readablePlan.steps" :key="step.key">
                <i>{{ index + 1 }}</i>
                <div><strong>{{ step.title }}</strong><div class="markdown-body" v-html="renderMarkdown(step.description)"></div><div v-if="step.detail" class="markdown-body plan-step-detail" v-html="renderMarkdown(step.detail)"></div></div>
              </li>
            </ol>
          </div>
          <details class="raw-data-details"><summary>查看原始方案数据</summary><pre class="json-panel">{{ pretty(selectedJob.eval_spec) }}</pre></details>
        </div>

        <div v-if="Object.keys(selectedJob.result).length" class="job-section result-section">
          <div class="section-heading"><h2>评测结果</h2><el-button v-if="selectedJob.result_file_id" :icon="Download" @click="downloadResult">下载逐条结果</el-button></div>
          <div v-if="typeof selectedJob.result.summary === 'string'" class="result-summary markdown-body" v-html="renderMarkdown(selectedJob.result.summary)"></div>
          <details v-if="!isPythonJob" class="raw-data-details"><summary>查看原始结果数据</summary><pre class="json-panel">{{ pretty(selectedJob.result) }}</pre></details>
        </div>
      </template>
      <div v-else class="detail-empty"><el-icon><Document /></el-icon><h2>选择一个评测任务</h2><p>任务的运行步骤、方案和结果会显示在这里。</p></div>
    </main>

  </section>
</template>
