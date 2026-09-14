<script setup lang="ts">
import {
  ChatDotRound,
  Close,
  Collection,
  CopyDocument,
  Document,
  Download,
  Files,
  Grid,
  Paperclip,
  Plus,
  Promotion,
  Search,
  UserFilled,
} from '@element-plus/icons-vue'
import { ElButton, ElIcon, ElMessage, ElUpload } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import type {
  AgentJob,
  AgentJobEvent,
  AgentStep,
  AssistantConversation,
  AssistantMessage,
  EvaluationModelOption,
  FileObject,
  Project,
  ReactToolStep,
} from '../types/agent'

const router = useRouter()
const route = useRoute()
const projects = ref<Project[]>([])
const files = ref<FileObject[]>([])
const models = ref<EvaluationModelOption[]>([])
const conversations = ref<AssistantConversation[]>([])
const current = ref<AssistantConversation | null>(null)
const messages = ref<AssistantMessage[]>([])
const projectId = ref('')
const sourceFileId = ref('')
const modelId = ref('')
const input = ref('')
const conversationSearch = ref('')
const streamingConversationIds = ref<Set<string>>(new Set())
const conversationMessageBuffers = new Map<string, AssistantMessage[]>()
const conversationInputDrafts = new Map<string, string>()
const uploading = ref(false)
const starting = ref(false)
const conversationSwitching = ref(false)
const messagePane = ref<HTMLElement | null>(null)
const runJob = ref<AgentJob | null>(null)
const runSteps = ref<AgentStep[]>([])
const runEvents = ref<AgentJobEvent[]>([])
let viewActive = false
let runEventSource: EventSource | null = null
let runEventJobId = ''
let conversationSelectionVersion = 0

function runJobRenderKey(job: AgentJob | null) {
  if (!job) return ''
  return JSON.stringify([job.id, job.title, job.status, job.error])
}

function runStepsRenderKey(items: AgentStep[]) {
  return JSON.stringify(items.map((step) => [step.id, step.name, step.status, step.error]))
}

const streaming = computed(() => Boolean(
  current.value?.id && streamingConversationIds.value.has(current.value.id),
))
const selectedFile = computed(() => files.value.find((file) => file.id === sourceFileId.value))
const filteredConversations = computed(() => {
  const keyword = conversationSearch.value.trim().toLocaleLowerCase()
  if (!keyword) return conversations.value
  return conversations.value.filter((conversation) => conversation.title.toLocaleLowerCase().includes(keyword))
})
const lastAssistantIndex = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    if (messages.value[index]?.role === 'assistant') return index
  }
  return -1
})
const hasCompleteHumanReviewConfig = computed(() => {
  const draft = current.value?.draft
  return Boolean(
    draft?.task_mode === 'human_review'
    && draft.title
    && draft.source_file_id
    && draft.source_inspected
    && (
      (Array.isArray(draft.reviewer_type_codes) && draft.reviewer_type_codes.length > 0)
      || (Array.isArray(draft.reviewer_usernames) && draft.reviewer_usernames.length > 0)
    )
    && Array.isArray(draft.review_rubric)
    && draft.review_rubric.length
    && Number(draft.deadline_hours) > 0,
  )
})
const confirmationAssistantIndex = computed(() => {
  const confirmationIndex = messages.value.findIndex(
    (message) => message.role === 'user' && message.content.trim() === '确认并开启',
  )
  if (confirmationIndex < 1) return -1
  for (let index = confirmationIndex - 1; index >= 0; index -= 1) {
    if (messages.value[index]?.role === 'assistant') return index
  }
  return -1
})
const hasRunnableConfig = computed(() => {
  const draft = current.value?.draft
  if (!draft?.title || (!draft.goal && !hasCompleteHumanReviewConfig.value)) return false
  return Boolean(
    (draft.source_file_id && draft.source_inspected)
    || (draft.target_url && draft.target_body && draft.target_validated),
  )
})
const outputFormatValues = new Set(['xlsx', 'jsonl', 'markdown', 'text'])

function isOutputFormatAction(action: { type?: string, options?: unknown[] } | undefined) {
  if (action?.type === 'choose_output') return true
  if (action?.type !== 'user_input' || !Array.isArray(action.options)) return false
  const options = new Set(action.options.map((item) => String(item).trim().toLocaleLowerCase()))
  return options.size === outputFormatValues.size
    && [...outputFormatValues].every((format) => options.has(format))
}

const showOutputChoices = computed(() => {
  const action = current.value?.draft.ui_action as { type?: string, options?: unknown[] } | undefined
  return isOutputFormatAction(action) || current.value?.status === 'choose_output'
})
const canStartTask = computed(() => hasRunnableConfig.value
  && Boolean(current.value?.draft.output_format)
  && current.value?.status !== 'started'
  && !streaming.value
  && (
    current.value?.status === 'ready'
    || (current.value?.draft.ui_action as { type?: string } | undefined)?.type === 'confirm'
    || hasCompleteHumanReviewConfig.value
  )
  && messages.value.at(-1)?.role === 'assistant'
  && !messages.value.at(-1)?.streaming
  && Boolean(messages.value.at(-1)?.content.trim()))

const taskStartConfirmations = new Set([
  '确认',
  '确认执行',
  '确认启动',
  '确认并开启',
  '开始执行',
  '立即执行',
])

function isTaskStartConfirmation(content: string) {
  const normalized = content.trim().replace(/[\s，。！!]+/g, '')
  return taskStartConfirmations.has(normalized)
}

function humanReviewGoal(draft: Record<string, unknown>) {
  const rubric = Array.isArray(draft.review_rubric) ? draft.review_rubric : []
  const labels = rubric
    .map((item) => (item && typeof item === 'object' && 'label' in item ? String(item.label).trim() : ''))
    .filter(Boolean)
  const dimensions = labels.join('、') || '已配置的评分'
  return `由指定评审人员按${dimensions}维度对文件中的回答进行人工评分。`
}

function shouldShowStartTaskButton(index: number) {
  if (canStartTask.value) return index === lastAssistantIndex.value
  return current.value?.status === 'started' && index === confirmationAssistantIndex.value
}
const genericOptions = computed(() => {
  // While a new reply is streaming, draft.ui_action still belongs to the
  // previous turn. Do not move those stale buttons onto the new assistant bubble.
  if (streaming.value) return []
  const action = current.value?.draft.ui_action as { type?: string, options?: unknown[] } | undefined
  if (action?.type !== 'user_input' || !Array.isArray(action.options) || isOutputFormatAction(action)) return []
  return action.options.map(String)
})
const runHeading = computed(() => {
  if (runJob.value?.status === 'completed') return 'Agent 执行完成'
  if (runJob.value?.status === 'failed') return 'Agent 执行未完成'
  if (runJob.value?.status === 'cancelled') return 'Agent 执行已取消'
  if (runJob.value?.status === 'waiting_human') return 'Agent 等待确认'
  return 'Agent 正在执行'
})
const showRunError = computed(() => Boolean(
  runJob.value?.error && !runSteps.value.some((step) => step.error === runJob.value?.error),
))
const stepLabels: Record<string, string> = {
  discover_source: '理解数据',
  generate_eval_spec: '制定评测方案',
  generate_test_cases: '生成测试用例',
  validate_target: '验证目标接口',
  update_task_draft: '修正请求配置',
  probe_http_target: '重新验证目标接口',
  request_user_input: '请求补充信息',
  request_confirmation: '确认修复结果',
  execute_eval_spec: '执行评测',
  summarize: '总结结果',
}
const modelOutputStreams = computed(() => {
  const streams = new Map<string, { phase: string, label: string, content: string, status: string }>()
  for (const event of runEvents.value) {
    const stream = streams.get(event.phase) ?? {
      phase: event.phase,
      label: String(event.payload.label ?? stepLabels[event.phase] ?? event.phase),
      content: '',
      status: 'running',
    }
    if (event.event_type === 'model_start') {
      stream.label = String(event.payload.label ?? stream.label)
      stream.status = 'running'
    } else if (event.event_type === 'model_delta') {
      stream.content += event.content
    } else if (event.event_type === 'model_complete') {
      stream.status = 'completed'
    } else if (event.event_type === 'model_error') {
      stream.status = 'failed'
      if (event.content) stream.content += `\n${event.content}`
    }
    streams.set(event.phase, stream)
  }
  return [...streams.values()]
})
const runTimelineItems = computed(() => {
  const streams = new Map(modelOutputStreams.value.map((stream) => [stream.phase, stream]))
  const dataProgramStreams = modelOutputStreams.value.filter((stream) => stream.phase.startsWith('data_program_'))
  const dataProgramStream = dataProgramStreams.length
    ? {
        phase: 'data_program',
        label: '处理数据',
        content: dataProgramStreams.map((stream) => stream.content).filter(Boolean).join('\n'),
        status: dataProgramStreams.some((stream) => stream.status === 'running')
          ? 'running'
          : dataProgramStreams.some((stream) => stream.status === 'failed') ? 'failed' : 'completed',
      }
    : undefined
  return runSteps.value.map((step) => ({
    step,
    stream: streams.get(step.name)
      ?? (step.name === 'execute_eval_spec' ? streams.get('evaluate_results') ?? dataProgramStream : undefined),
  }))
})

function modelOutputPreview(content: string) {
  const normalized = content.replace(/\s+/g, ' ').trim()
  if (normalized.length <= 260) return normalized
  return `…${normalized.slice(-260)}`
}

function renderMarkdown(content: string) {
  if (!content.trim()) return ''
  return DOMPurify.sanitize(marked.parse(content, {
    async: false,
    breaks: true,
    gfm: true,
  }) as string)
}

const finalAssistantReply = computed(() => {
  const stream = modelOutputStreams.value.find((item) => item.phase === 'summarize')
  const storedSummary = typeof runJob.value?.result?.summary === 'string'
    ? runJob.value.result.summary.trim()
    : ''
  const content = stream?.content.trim() || storedSummary
  if (!content) return null
  return { content, streaming: stream?.status === 'running' }
})

const finalReplyStoredInMessages = computed(() => Boolean(
  finalAssistantReply.value?.content
  && messages.value.some(
    (message) => message.role === 'assistant'
      && message.content.trim() === finalAssistantReply.value?.content,
  ),
))

function isStoredResultMessage(message: AssistantMessage) {
  return Boolean(
    runJob.value?.status === 'completed'
    && finalAssistantReply.value?.content
    && message.role === 'assistant'
    && message.content.trim() === finalAssistantReply.value.content,
  )
}

const runInsertionIndex = computed(() => {
  if (!runJob.value) return -1
  const confirmationIndex = messages.value.findIndex(
    (message) => message.role === 'user' && message.content.trim() === '确认并开启',
  )
  if (confirmationIndex >= 0) return confirmationIndex
  const resultIndex = messages.value.findIndex(isStoredResultMessage)
  return resultIndex > 0 ? resultIndex - 1 : messages.value.length - 1
})

function downloadResult() {
  if (runJob.value?.result_file_id) {
    window.open(`/api/files/${runJob.value.result_file_id}/content`, '_blank')
  }
}

const outputChoices = [
  { label: '不需要文件', description: '直接在对话中查看结果', icon: Collection, tone: 'plain', prompt: '不需要文件，请直接以纯文本消息返回结果。', value: 'text' },
  { label: 'Markdown 文件', description: '适合阅读与分享的文档', icon: Document, tone: 'markdown', prompt: '请把结果整理成 Markdown 文件。', value: 'markdown' },
  { label: 'Excel 文件', description: '适合分析与二次处理', icon: Grid, tone: 'excel', prompt: '请把结果整理成 Excel 文件。', value: 'xlsx' },
  { label: 'JSONL 文件', description: '适合程序读取与批处理', icon: Files, tone: 'jsonl', prompt: '请把结果整理成 JSONL 文件。', value: 'jsonl' },
]
function isOutputChoiceMessage(content: string) {
  const normalized = content.trim().toLocaleLowerCase()
  return outputChoices.some((choice) => (
    normalized === choice.value
    || normalized === choice.label.toLocaleLowerCase()
    || normalized === choice.prompt.toLocaleLowerCase()
  ))
}

const outputSelectionMessageIndex = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]
    if (message?.role === 'user' && isOutputChoiceMessage(message.content)) return index
  }
  return -1
})

const outputChoiceMessageIndex = computed(() => {
  if (outputSelectionMessageIndex.value >= 0) {
    for (let previous = outputSelectionMessageIndex.value - 1; previous >= 0; previous -= 1) {
      if (messages.value[previous]?.role === 'assistant') return previous
    }
  }
  return showOutputChoices.value ? lastAssistantIndex.value : -1
})
const outputChoiceSubmitted = computed(() => (
  outputSelectionMessageIndex.value >= 0
  || (Boolean(current.value?.draft.output_format) && current.value?.status !== 'choose_output')
))

async function scrollToBottom() {
  await nextTick()
  if (messagePane.value) messagePane.value.scrollTop = messagePane.value.scrollHeight
}

async function copyMessage(content: string) {
  if (!content) return
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(content)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = content
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      const copied = document.execCommand('copy')
      textarea.remove()
      if (!copied) throw new Error('Copy command failed')
    }
    ElMessage.success('已复制整条消息')
  } catch {
    ElMessage.error('复制失败，请手动选择文字复制')
  }
}

async function loadFiles() {
  if (!projectId.value) return (files.value = [])
  files.value = (await api.get<FileObject[]>(`/projects/${projectId.value}/files`, {
    params: { category: 'dataset_source' },
  })).data
}

async function loadConversations() {
  conversations.value = (await api.get<AssistantConversation[]>('/assistant/conversations')).data
}

async function createConversation() {
  if (!projectId.value) return ElMessage.warning('请先选择项目')
  const conversation = (await api.post<AssistantConversation>('/assistant/conversations', {
    project_id: projectId.value,
  })).data
  conversations.value.unshift(conversation)
  await selectConversation(conversation)
}

async function selectConversation(conversation: AssistantConversation) {
  const selectionVersion = ++conversationSelectionVersion
  const conversationId = conversation.id
  conversationSwitching.value = true
  try {
  if (current.value) conversationInputDrafts.set(current.value.id, input.value)
  stopRunEventStream()
  runJob.value = null
  runSteps.value = []
  runEvents.value = []
  current.value = conversation
  input.value = conversationInputDrafts.get(conversationId) ?? ''
  const conversationProjectId = conversation.project_id ?? projectId.value
  projectId.value = conversationProjectId
  sourceFileId.value = ''

  // Keep the URL in sync before loading. A slower, older selection must never
  // navigate back after a newer conversation has already been selected.
  if (
    viewActive
    && route.name === 'assistant'
    && String(route.params.conversationId ?? '') !== conversationId
  ) {
    await router.replace({ name: 'assistant', params: { conversationId } })
    if (selectionVersion !== conversationSelectionVersion || current.value?.id !== conversationId) return
  }

  const bufferedMessages = conversationMessageBuffers.get(conversationId)
  const messageRequest = bufferedMessages
    ? Promise.resolve(bufferedMessages)
    : api.get<AssistantMessage[]>(`/assistant/conversations/${conversationId}/messages`)
      .then((response) => response.data)
  const fileRequest = conversationProjectId
    ? api.get<FileObject[]>(`/projects/${conversationProjectId}/files`, {
        params: { category: 'dataset_source' },
      }).then((response) => response.data)
    : Promise.resolve([] as FileObject[])
  const runRequest = conversation.agent_job_id
    ? Promise.all([
        api.get<AgentJob>(`/agent-jobs/${conversation.agent_job_id}`),
        api.get<AgentStep[]>(`/agent-jobs/${conversation.agent_job_id}/steps`),
      ]).then(([jobResponse, stepResponse]) => ({
        job: jobResponse.data,
        steps: stepResponse.data,
      }))
    : Promise.resolve(null)
  const [loadedFiles, loadedMessages, loadedRun] = await Promise.all([
    fileRequest,
    messageRequest,
    runRequest,
  ])
  if (selectionVersion !== conversationSelectionVersion || current.value?.id !== conversationId) return

  // Commit the complete conversation in one render. Inserting the run timeline
  // after the messages were already visible caused a second layout and a jump.
  files.value = loadedFiles
  messages.value = loadedMessages
  runJob.value = loadedRun?.job ?? null
  runSteps.value = loadedRun?.steps ?? []
  await scrollToBottom()
  if (selectionVersion !== conversationSelectionVersion || current.value?.id !== conversationId) return
  if (
    conversation.agent_job_id
    && loadedRun
    && !['completed', 'failed', 'cancelled'].includes(loadedRun.job.status)
  ) {
    startRunEventStream(conversation.agent_job_id)
  }
  } finally {
    if (selectionVersion === conversationSelectionVersion && current.value?.id === conversationId) {
      conversationSwitching.value = false
    }
  }
}

function stopRunEventStream() {
  runEventSource?.close()
  runEventSource = null
  runEventJobId = ''
}

function startRunEventStream(jobId: string) {
  if (runEventJobId === jobId && runEventSource) return
  stopRunEventStream()
  runEvents.value = []
  runEventJobId = jobId
  const source = new EventSource(`/api/agent-jobs/${jobId}/events`)
  runEventSource = source
  source.addEventListener('agent-event', (event) => {
    if (runEventJobId !== jobId) return
    try {
      const item = JSON.parse((event as MessageEvent).data) as AgentJobEvent
      if (!runEvents.value.some((existing) => existing.id === item.id)) {
        runEvents.value = [...runEvents.value, item]
        void scrollToBottom()
      }
    } catch {
      // Ignore malformed transport events; persisted events can be replayed on reconnect.
    }
  })
  source.addEventListener('job-state', (event) => {
    if (runEventJobId !== jobId) return
    try {
      const state = JSON.parse((event as MessageEvent).data) as {
        job: AgentJob
        steps: AgentStep[]
      }
      applyRunState(state.job, state.steps)
    } catch {
      // Ignore malformed snapshots; the next changed snapshot will replace it.
    }
  })
  source.addEventListener('end', () => {
    if (runEventJobId === jobId) stopRunEventStream()
  })
}

function applyRunState(job: AgentJob, steps: AgentStep[]) {
  const jobChanged = runJobRenderKey(runJob.value) !== runJobRenderKey(job)
  const stepsChanged = runStepsRenderKey(runSteps.value) !== runStepsRenderKey(steps)
  if (jobChanged) runJob.value = job
  if (stepsChanged) runSteps.value = steps
  if (jobChanged || stepsChanged) void scrollToBottom()
}

async function loadRun(jobId: string, isCurrent: () => boolean = () => true) {
  const [jobResponse, stepResponse] = await Promise.all([
    api.get<AgentJob>(`/agent-jobs/${jobId}`),
    api.get<AgentStep[]>(`/agent-jobs/${jobId}/steps`),
  ])
  if (!isCurrent()) return
  applyRunState(jobResponse.data, stepResponse.data)
  if (isCurrent() && !['completed', 'failed', 'cancelled'].includes(jobResponse.data.status)) {
    startRunEventStream(jobId)
  }
}

async function attachDataset(fileToUpload: File) {
  if (!projectId.value || uploading.value) return false
  uploading.value = true
  const form = new FormData()
  form.append('file', fileToUpload)
  form.append('category', 'dataset_source')
  try {
    const file = (await api.post<FileObject>(`/projects/${projectId.value}/files`, form)).data
    await loadFiles()
    sourceFileId.value = file.id
    ElMessage.success('数据文件已加入当前对话')
    return true
  } catch (error) {
    ElMessage.error(errorMessage(error))
    return false
  } finally {
    uploading.value = false
  }
}

async function uploadDataset(options: UploadRequestOptions) {
  if (await attachDataset(options.file)) options.onSuccess({})
}

function dropDataset(event: DragEvent) {
  if (streaming.value || uploading.value) return
  const file = event.dataTransfer?.files.item(0)
  if (file) void attachDataset(file)
}

function removeAttachedFile() {
  sourceFileId.value = ''
}

function attachmentExtension(name?: string | null) {
  const extension = name?.split('.').pop()?.trim().toUpperCase() ?? ''
  return extension && extension.length <= 5 ? extension : 'FILE'
}

function attachmentTone(name?: string | null) {
  const extension = name?.split('.').pop()?.trim().toLowerCase()
  if (extension === 'xlsx' || extension === 'xls') return 'excel'
  if (extension === 'json' || extension === 'jsonl') return 'json'
  if (extension === 'csv') return 'csv'
  if (extension === 'md' || extension === 'markdown') return 'markdown'
  return 'default'
}

function attachmentTypeLabel(name?: string | null) {
  const extension = name?.split('.').pop()?.trim().toLowerCase()
  return {
    xlsx: 'Excel 工作簿',
    xls: 'Excel 工作簿',
    csv: 'CSV 数据文件',
    json: 'JSON 数据文件',
    jsonl: 'JSONL 数据文件',
    md: 'Markdown 文档',
    markdown: 'Markdown 文档',
  }[extension ?? ''] ?? '数据文件'
}

function formatFileSize(size?: number | null) {
  if (size == null || !Number.isFinite(size)) return '附件'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function downloadAttachment(fileId?: string | null) {
  if (fileId) window.open(`/api/files/${fileId}/content`, '_blank', 'noopener,noreferrer')
}

function chooseOutput(value: string, prompt: string) {
  if (!current.value || streaming.value || outputChoiceSubmitted.value) return
  current.value.draft = { ...current.value.draft, output_format: value }
  void sendMessage(prompt)
}

function setConversationStreaming(conversationId: string, value: boolean) {
  const next = new Set(streamingConversationIds.value)
  if (value) next.add(conversationId)
  else next.delete(conversationId)
  streamingConversationIds.value = next
}

function isConversationStreaming(conversationId: string) {
  return streamingConversationIds.value.has(conversationId)
}

function parseStreamEvent(
  line: string,
  assistant: AssistantMessage,
  conversation: AssistantConversation,
) {
  if (!line.trim()) return
  const event = JSON.parse(line) as {
    type: 'start' | 'delta' | 'tool_start' | 'tool_result' | 'done' | 'error'
    content?: string
    message?: string
    draft?: Record<string, unknown>
    stage?: AssistantConversation['status']
    tool?: ReactToolStep
    attachment_file_id?: string | null
    attachment_name?: string | null
    attachment_content_type?: string | null
    attachment_size_bytes?: number | null
  }
  if (event.type === 'delta') assistant.content += event.content ?? ''
  if (event.type === 'tool_start' && event.tool) {
    assistant.react_trace = [...(assistant.react_trace ?? []), event.tool]
  }
  if (event.type === 'tool_result' && event.tool) {
    const trace = assistant.react_trace ?? []
    let index = -1
    for (let candidate = trace.length - 1; candidate >= 0; candidate -= 1) {
      if (trace[candidate]?.name === event.tool.name && trace[candidate]?.status === 'running') {
        index = candidate
        break
      }
    }
    if (index >= 0) trace[index] = event.tool
    else trace.push(event.tool)
    assistant.react_trace = [...trace]
  }
  if (event.type === 'error') throw new Error(event.message || '助手回复失败')
  if (event.type === 'done') {
    if (typeof event.content === 'string') {
      // New servers return the complete accumulated stream. With an older
      // server, do not let its final-turn-only payload erase earlier deltas.
      if (!assistant.content || event.content.startsWith(assistant.content)) {
        assistant.content = event.content
      }
    }
    const draft = event.draft ?? {}
    const status = event.stage ?? 'collecting'
    conversation.draft = draft
    conversation.status = status
    const listedConversation = conversations.value.find((item) => item.id === conversation.id)
    if (listedConversation && listedConversation !== conversation) {
      listedConversation.draft = draft
      listedConversation.status = status
    }
    if (current.value?.id === conversation.id && current.value !== conversation) {
      current.value.draft = draft
      current.value.status = status
    }
    assistant.react_trace = (conversation.draft.react_trace as ReactToolStep[] | undefined) ?? assistant.react_trace
    assistant.attachment_file_id = event.attachment_file_id ?? null
    assistant.attachment_name = event.attachment_name ?? null
    assistant.attachment_content_type = event.attachment_content_type ?? null
    assistant.attachment_size_bytes = event.attachment_size_bytes ?? null
    assistant.streaming = false
  }
}

async function sendMessage(text = input.value) {
  const conversation = current.value
  const attachedFile = selectedFile.value
  const content = text.trim() || (attachedFile ? '请分析这个文件。' : '')
  if (!content || !conversation || isConversationStreaming(conversation.id)) return
  if (canStartTask.value && isTaskStartConfirmation(content)) {
    input.value = ''
    conversationInputDrafts.set(conversation.id, '')
    await startTask()
    return
  }
  const conversationId = conversation.id
  const conversationMessages = messages.value
  const attachedSourceFileId = sourceFileId.value
  const assistant = reactive<AssistantMessage>({ role: 'assistant', content: '', streaming: true })
  conversationMessages.push({
    role: 'user',
    content,
    attachment_file_id: attachedFile?.id ?? null,
    attachment_name: attachedFile?.original_name ?? null,
    attachment_content_type: attachedFile?.content_type ?? null,
    attachment_size_bytes: attachedFile?.size_bytes ?? null,
  })
  conversationMessages.push(assistant)
  conversationMessageBuffers.set(conversationId, conversationMessages)
  input.value = ''
  conversationInputDrafts.set(conversationId, '')
  setConversationStreaming(conversationId, true)
  await scrollToBottom()
  try {
    const response = await fetch(
      `/api/assistant/conversations/${conversationId}/messages/stream`,
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content,
          source_file_id: attachedSourceFileId || null,
          evaluation_model_id: modelId.value || null,
          output_format: conversation.draft.output_format || null,
        }),
      },
    )
    if (!response.ok || !response.body) throw new Error(`助手请求失败（${response.status}）`)
    if (sourceFileId.value === attachedSourceFileId) sourceFileId.value = ''
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) parseStreamEvent(line, assistant, conversation)
      if (current.value?.id === conversationId) await scrollToBottom()
      if (done) break
    }
    if (buffer.trim()) parseStreamEvent(buffer, assistant, conversation)
    await loadConversations()
  } catch (error) {
    const last = conversationMessages[conversationMessages.length - 1]
    if (last === assistant && !last.content) conversationMessages.pop()
    ElMessage.error(errorMessage(error))
  } finally {
    setConversationStreaming(conversationId, false)
    assistant.streaming = false
    conversationMessageBuffers.delete(conversationId)
  }
}

async function startTask() {
  if (!current.value || !projectId.value || starting.value) return
  const conversation = current.value
  const draft = conversation.draft
  const title = String(draft.title ?? conversation.title).trim()
  const goal = String(draft.goal ?? '').trim()
    || (draft.task_mode === 'human_review' ? humanReviewGoal(draft) : '')
  const outputFormat = String(draft.output_format ?? 'text')
  const taskSourceFileId = sourceFileId.value || String(draft.source_file_id ?? '')
  if (!goal) return ElMessage.warning('还需要补充评测目标')
  if (!taskSourceFileId && !draft.target_url && !Array.isArray(draft.targets)) {
    return ElMessage.warning('请上传数据文件，或在对话中提供目标接口')
  }
  starting.value = true
  try {
    const inputConfig: Record<string, unknown> = { max_cases: Number(draft.max_cases ?? 100) }
    if (Array.isArray(draft.targets) && draft.targets.length) {
      inputConfig.targets = draft.targets
    }
    if (draft.target_url) {
      let targetBody = draft.target_body ?? '{{row}}'
      if (typeof targetBody === 'string') {
        try {
          const parsed = JSON.parse(targetBody)
          if (parsed && typeof parsed === 'object') targetBody = parsed
        } catch {
          // Non-JSON templates remain strings and are validated by the backend Agent.
        }
      }
      inputConfig.target = {
        url: String(draft.target_url),
        body: targetBody,
        response_path: String(draft.response_path ?? ''),
        expected_streaming: draft.expected_streaming ?? null,
        ...(draft.target_credentials ? { credentials: String(draft.target_credentials) } : {}),
      }
    }
    const job = (await api.post<AgentJob>(`/projects/${projectId.value}/agent-jobs`, {
      title,
      goal,
      source_file_id: taskSourceFileId || null,
      requires_approval: false,
      output_format: outputFormat,
      evaluation_model_id: modelId.value || null,
      input_config: inputConfig,
    })).data
    const isHumanReview = draft.task_mode === 'human_review'
    if (isHumanReview) {
      await api.post('/human-reviews/campaigns/from-file', {
        job_id: job.id,
        source_file_id: taskSourceFileId,
        title,
        instructions: goal,
        reviewer_type_codes: Array.isArray(draft.reviewer_type_codes) ? draft.reviewer_type_codes : [],
        reviewer_usernames: Array.isArray(draft.reviewer_usernames) ? draft.reviewer_usernames : [],
        query_column: String(draft.query_column ?? 'query'),
        answer_column: String(draft.answer_column ?? 'answer'),
        latency_column: draft.latency_column ? String(draft.latency_column) : null,
        deadline_hours: Number(draft.deadline_hours ?? 24),
        rubric: Array.isArray(draft.review_rubric) ? draft.review_rubric : [],
        max_items: Number(draft.max_cases ?? 10000),
      })
    } else {
      await api.post(`/agent-jobs/${job.id}/start`)
    }
    await api.post(`/assistant/conversations/${conversation.id}/started`, {
      agent_job_id: job.id,
    })
    conversation.agent_job_id = job.id
    conversation.status = 'started'
    if (current.value?.id === conversation.id) {
      messages.value.push({ role: 'user', content: '确认并开启' })
      await scrollToBottom()
    }
    ElMessage.success(isHumanReview ? '人工评审已发布' : '评测任务已开始')
    if (!isHumanReview) await loadRun(job.id)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    starting.value = false
  }
}

onMounted(async () => {
  viewActive = true
  try {
    const [projectResponse, modelResponse, conversationResponse] = await Promise.all([
      api.get<Project[]>('/projects'),
      api.get<EvaluationModelOption[]>('/evaluation-models'),
      api.get<AssistantConversation[]>('/assistant/conversations'),
    ])
    if (!viewActive) return
    projects.value = projectResponse.data
    models.value = modelResponse.data
    conversations.value = conversationResponse.data
    const rememberedProject = localStorage.getItem('evalweave-project')
    projectId.value = projects.value.some((project) => project.id === rememberedProject)
      ? String(rememberedProject)
      : projects.value[0]?.id || ''
    modelId.value = models.value[0]?.id || ''
    if (!viewActive) return
    const requested = conversations.value.find(
      (item) => item.id === String(route.params.conversationId ?? ''),
    )
    if (requested || conversations.value.length) {
      await selectConversation(requested ?? conversations.value[0])
    }
    else if (projectId.value) await createConversation()
  } catch (error) {
    if (viewActive) ElMessage.error(errorMessage(error))
  }
})

onUnmounted(() => {
  viewActive = false
  conversationSelectionVersion += 1
  stopRunEventStream()
})

watch(
  () => String(route.params.conversationId ?? ''),
  async (conversationId) => {
    if (!conversationId || current.value?.id === conversationId) return
    try {
      let conversation = conversations.value.find((item) => item.id === conversationId)
      if (!conversation) {
        await loadConversations()
        if (String(route.params.conversationId ?? '') !== conversationId) return
        conversation = conversations.value.find((item) => item.id === conversationId)
      }
      if (conversation) await selectConversation(conversation)
    } catch (error) {
      ElMessage.error(errorMessage(error))
    }
  },
)
</script>

<template>
  <div class="assistant-page">
    <section class="assistant-shell surface">
      <aside class="conversation-list">
        <div class="conversation-list-title">
          <div><strong>对话记录</strong><small>{{ conversations.length }} 个会话</small></div>
          <button type="button" title="新建对话" aria-label="新建对话" @click="createConversation"><el-icon><Plus /></el-icon></button>
        </div>
        <label class="conversation-search">
          <el-icon><Search /></el-icon>
          <input v-model="conversationSearch" type="search" placeholder="搜索对话" aria-label="搜索对话">
        </label>
        <div class="conversation-list-items">
          <button
            v-for="conversation in filteredConversations"
            :key="conversation.id"
            type="button"
            class="conversation-item"
            :class="{ active: current?.id === conversation.id, streaming: isConversationStreaming(conversation.id) }"
            @click="selectConversation(conversation)"
          >
            <span class="conversation-item-icon"><el-icon><ChatDotRound /></el-icon></span>
            <span class="conversation-item-copy"><strong>{{ conversation.title }}</strong></span>
          </button>
          <div v-if="!filteredConversations.length" class="conversation-empty"><el-icon><ChatDotRound /></el-icon><span>{{ conversations.length ? '没有匹配的对话' : '还没有对话' }}</span></div>
        </div>
      </aside>
      <div class="assistant-main-column">
        <main class="assistant-conversation">
        <div ref="messagePane" class="assistant-message-pane" :class="{ switching: conversationSwitching }">
          <div class="assistant-message-inner">
            <template v-for="(message, index) in messages" :key="message.id ?? index">
            <div class="assistant-message-row" :class="message.role">
              <span v-if="message.role === 'assistant'" class="message-avatar"><el-icon><ChatDotRound /></el-icon></span>
              <div class="message-stack">
                <div class="message-bubble">
                <button
                  v-if="message.content"
                  type="button"
                  class="message-copy-button"
                  title="复制全部内容"
                  aria-label="复制全部内容"
                  @click.stop="copyMessage(message.content)"
                ><el-icon><CopyDocument /></el-icon></button>
                <button
                  v-if="message.role === 'user' && message.attachment_file_id"
                  type="button"
                  class="message-file-attachment"
                  :title="`下载 ${message.attachment_name || '附件'}`"
                  @click.stop="downloadAttachment(message.attachment_file_id)"
                >
                  <span class="message-file-icon"><i>{{ attachmentExtension(message.attachment_name) }}</i></span>
                  <span class="message-file-copy"><strong>{{ message.attachment_name || '附件' }}</strong><small>{{ formatFileSize(message.attachment_size_bytes) }}</small></span>
                </button>
                <span v-if="message.streaming && !message.content" class="message-thinking" aria-hidden="true">
                  <i></i><i></i><i></i>
                </span>
                <template v-else>
                  <div class="message-content markdown-body chat-markdown" v-html="renderMarkdown(message.content)"></div>
                  <span v-if="message.streaming" class="message-thinking message-thinking-continuation" aria-hidden="true">
                    <i></i><i></i><i></i>
                  </span>
                </template>
                <div v-if="index === outputChoiceMessageIndex" class="bubble-choice-block">
                  <div class="choice-heading"><strong>结果交付方式</strong><span>选择最适合你的结果格式</span></div>
                  <div class="output-card-grid">
                    <button
                      v-for="choice in outputChoices"
                      :key="choice.value"
                      type="button"
                      class="output-card"
                      :class="{ selected: current?.draft.output_format === choice.value, muted: current?.draft.output_format && current?.draft.output_format !== choice.value }"
                      :disabled="outputChoiceSubmitted"
                      @click="chooseOutput(choice.value, choice.prompt)"
                    >
                      <span class="output-card-icon" :class="choice.tone"><component :is="choice.icon" /></span>
                      <span class="output-card-copy"><strong>{{ choice.label }}</strong><small>{{ choice.description }}</small></span>
                      <span class="output-card-check">✓</span>
                    </button>
                  </div>
                </div>
                <div v-if="shouldShowStartTaskButton(index)" class="bubble-task-ready">
                  <el-button type="primary" :loading="starting" :disabled="starting || current?.status === 'started'" @click="startTask">确认并开启</el-button>
                </div>
                <div v-if="index === lastAssistantIndex && genericOptions.length" class="bubble-actions react-input-actions">
                  <button v-for="option in genericOptions" :key="option" @click="sendMessage(option)">{{ option }}</button>
                </div>
                <button
                  v-if="message.role === 'assistant' && message.attachment_file_id && !isStoredResultMessage(message)"
                  type="button"
                  class="result-download-link"
                  @click.stop="downloadAttachment(message.attachment_file_id)"
                >
                  <el-icon><Download /></el-icon><span>下载评测结果</span>
                </button>
                <button v-if="isStoredResultMessage(message) && runJob?.result_file_id" type="button" class="result-download-link" @click="downloadResult">
                  <el-icon><Download /></el-icon><span>下载评测结果</span>
                </button>
                </div>
              </div>
              <span v-if="message.role === 'user'" class="message-avatar user-message-avatar" aria-label="用户头像"><el-icon><UserFilled /></el-icon></span>
            </div>

            <div v-if="runJob && index === runInsertionIndex" class="assistant-message-row assistant react-run-row">
              <span class="message-avatar"><el-icon><ChatDotRound /></el-icon></span>
              <div class="message-bubble react-run-bubble">
                <div class="react-run-head"><div><strong>{{ runHeading }}</strong><span>{{ runJob.title }}</span></div><em :class="runJob.status">{{ runJob.status }}</em></div>
                <div class="react-timeline">
                  <div v-for="item in runTimelineItems" :key="item.step.id" class="react-node" :class="item.step.status">
                    <i></i>
                    <div class="react-node-body">
                      <strong>{{ stepLabels[item.step.name] ?? item.step.name }}</strong>
                      <span>{{ item.step.status === 'running' ? '执行中' : item.step.status === 'completed' ? '已完成' : item.step.status === 'failed' ? '失败' : '等待' }}</span>
                      <p v-if="item.stream?.content" class="react-node-output-text">
                        {{ modelOutputPreview(item.stream.content) }}
                        <span v-if="item.stream.status === 'running'" class="message-thinking message-thinking-continuation" aria-hidden="true">
                          <i></i><i></i><i></i>
                        </span>
                      </p>
                      <p v-if="item.step.error">{{ item.step.error }}</p>
                    </div>
                  </div>
                  <div v-if="!runSteps.length" class="react-node running"><i></i><div><strong>理解任务</strong><span>执行中</span></div></div>
                </div>
                <div v-if="runJob.status === 'waiting_human'" class="react-review-link"><router-link to="/human-tasks">前往审核方案</router-link></div>
                <div v-if="showRunError" class="react-error">{{ runJob.error }}</div>
              </div>
            </div>
            </template>

            <div v-if="finalAssistantReply && !finalReplyStoredInMessages" class="assistant-message-row assistant final-result-row">
              <span class="message-avatar"><el-icon><ChatDotRound /></el-icon></span>
              <div class="message-stack">
                <div class="message-bubble">
                  <button type="button" class="message-copy-button" title="复制全部内容" aria-label="复制全部内容" @click.stop="copyMessage(finalAssistantReply.content)"><el-icon><CopyDocument /></el-icon></button>
                  <div class="message-content markdown-body chat-markdown" v-html="renderMarkdown(finalAssistantReply.content)"></div>
                  <span v-if="finalAssistantReply.streaming" class="message-thinking message-thinking-continuation" aria-hidden="true">
                    <i></i><i></i><i></i>
                  </span>
                  <button v-if="runJob?.result_file_id" type="button" class="result-download-link" @click="downloadResult">
                    <el-icon><Download /></el-icon><span>下载评测结果</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="assistant-composer" @dragenter.prevent @dragover.prevent @drop.prevent="dropDataset">
          <div v-if="selectedFile" class="composer-attachment">
            <span class="composer-file-icon" :class="attachmentTone(selectedFile.original_name)" aria-hidden="true"><i>{{ attachmentExtension(selectedFile.original_name) }}</i></span>
            <span class="composer-file-details">
              <strong :title="selectedFile.original_name">{{ selectedFile.original_name }}</strong>
              <small><i></i>{{ attachmentTypeLabel(selectedFile.original_name) }} · {{ formatFileSize(selectedFile.size_bytes) }} · 已添加</small>
            </span>
            <button type="button" title="移除附件" aria-label="移除附件" @click="removeAttachedFile"><el-icon><Close /></el-icon></button>
          </div>
          <textarea v-model="input" :disabled="streaming" rows="1" placeholder="告诉评测助手你的需求，Shift + Enter 换行" @keydown.enter.exact.prevent="sendMessage()"></textarea>
          <div class="composer-actions">
            <div><el-upload :show-file-list="false" :http-request="uploadDataset" accept=".json,.jsonl,.csv,.xlsx"><el-button text :icon="Paperclip" :loading="uploading" title="上传数据文件">上传文件</el-button></el-upload><span>支持 JSON、JSONL、CSV、XLSX</span></div>
            <el-button class="composer-send" type="primary" circle :icon="Promotion" :loading="streaming" :disabled="!input.trim() && !selectedFile" title="发送消息" aria-label="发送消息" @click="sendMessage()" />
          </div>
        </div>
        </main>
      </div>
    </section>
  </div>
</template>
