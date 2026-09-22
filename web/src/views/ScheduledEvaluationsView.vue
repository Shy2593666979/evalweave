<script setup lang="ts">
import { ArrowRight, Calendar, Check, Clock, Delete, Edit, Plus, Refresh, Search, VideoPlay, WarningFilled } from '@element-plus/icons-vue'
import { ElButton, ElCheckbox, ElCheckboxGroup, ElDialog, ElEmpty, ElForm, ElFormItem, ElIcon, ElInput, ElMessage, ElMessageBox, ElOption, ElRadioButton, ElRadioGroup, ElSelect, ElSwitch, ElTag, ElTimePicker, ElTooltip } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, errorMessage } from '../api/client'
import NavFeatureIcon from '../components/NavFeatureIcon.vue'
import { useAuthStore } from '../stores/auth'
import type { AgentJob, AgentJobStatus, Project, ScheduledEvaluation } from '../types/agent'
import { formatBeijingDateTime, parseServerDateTime } from '../utils/datetime'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const actionLoading = ref(false)
const projects = ref<Project[]>([])
const projectId = ref('')
const schedules = ref<ScheduledEvaluation[]>([])
const jobs = ref<AgentJob[]>([])
const selectedId = ref('')
const detailVisible = ref(false)
const searchKeyword = ref('')
const statusFilter = ref<'all' | 'enabled' | 'paused'>('all')
const createVisible = ref(false)
const editVisible = ref(false)
const initialized = ref(false)
const form = reactive({ source_job_id: '', name: '', description: '', type: 'daily' as 'daily' | 'weekly', time: '09:00', weekdays: [0] as number[] })
const weekdays = [
  { value: 0, label: '周一' }, { value: 1, label: '周二' }, { value: 2, label: '周三' },
  { value: 3, label: '周四' }, { value: 4, label: '周五' }, { value: 5, label: '周六' }, { value: 6, label: '周日' },
]
const canManage = computed(() => auth.hasPermission('experiment:run'))
const selected = computed(() => schedules.value.find((item) => item.schedule.id === selectedId.value) ?? null)
const completedJobs = computed(() => jobs.value.filter((job) => job.status === 'completed' && job.trigger_type !== 'scheduled' && Object.keys(job.eval_spec ?? {}).length > 0))
const filteredSchedules = computed(() => {
  const keyword = searchKeyword.value.trim().toLocaleLowerCase()
  return schedules.value.filter((item) => {
    if (statusFilter.value !== 'all' && item.schedule.status !== statusFilter.value) return false
    if (!keyword) return true
    return `${item.schedule.name} ${item.description ?? ''} ${item.goal}`.toLocaleLowerCase().includes(keyword)
  })
})
const rulePreview = computed(() => {
  if (form.type === 'daily') return `每天 ${form.time}`
  const names = form.weekdays.map((day) => weekdays.find((item) => item.value === day)?.label).filter(Boolean)
  return names.length ? `每周 ${names.join('、')} ${form.time}` : '请选择执行日期'
})
const enabledCount = computed(() => schedules.value.filter((item) => item.schedule.status === 'enabled').length)
const recentRuns = computed(() => selected.value?.recent_runs ?? [])
const selectedLastRun = computed(() => recentRuns.value[0] ?? null)
const lastRun = computed(() => schedules.value.flatMap((item) => item.recent_runs).sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ?? null)
const statusLabels: Record<AgentJobStatus, string> = {
  pending: '等待执行', discovering: '读取数据', planning: '生成方案', waiting_human: '等待评审',
  running: '执行中', analyzing: '整理结果', completed: '成功', failed: '失败', cancelled: '已取消',
}

function runStatusType(status: AgentJobStatus) {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'waiting_human') return 'warning'
  return 'primary'
}
function scheduleDescription(item: ScheduledEvaluation) {
  const recurrence = item.schedule.recurrence
  if (recurrence.type === 'daily') return `每天 ${recurrence.time}`
  const names = recurrence.weekdays.map((day) => weekdays.find((item) => item.value === day)?.label).filter(Boolean)
  return `每${names.join('、')} ${recurrence.time}`
}
function runDuration(run: ScheduledEvaluation['recent_runs'][number]) {
  const started = parseServerDateTime(run.created_at)?.getTime()
  const ended = parseServerDateTime(run.updated_at)?.getTime()
  if (!started || !ended) return '--'
  const seconds = Math.max(0, Math.floor((ended - started) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}
async function loadBase() {
  projects.value = (await api.get<Project[]>('/projects')).data
  const remembered = localStorage.getItem('evalweave-project')
  projectId.value = projects.value.some((project) => project.id === remembered) ? String(remembered) : projects.value[0]?.id ?? ''
}
async function loadProjectData() {
  if (!projectId.value) return
  loading.value = true
  try {
    const [scheduleResponse, jobResponse] = await Promise.all([
      api.get<ScheduledEvaluation[]>(`/projects/${projectId.value}/scheduled-evaluations`),
      api.get<AgentJob[]>(`/projects/${projectId.value}/agent-jobs`),
    ])
    schedules.value = scheduleResponse.data
    jobs.value = jobResponse.data
    const requestedId = String(route.params.scheduleId ?? selectedId.value)
    selectedId.value = schedules.value.some((item) => item.schedule.id === requestedId) ? requestedId : ''
    if (route.params.scheduleId && selectedId.value) detailVisible.value = true
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { loading.value = false }
}
async function selectSchedule(item: ScheduledEvaluation) {
  selectedId.value = item.schedule.id
  detailVisible.value = true
  if (String(route.params.scheduleId ?? '') !== item.schedule.id) await router.replace({ name: 'schedules', params: { scheduleId: item.schedule.id } })
}
async function closeDetail() {
  detailVisible.value = false
  if (route.params.scheduleId) await router.replace({ name: 'schedules' })
}
function resetForm(preferredJobId = '') {
  const source = completedJobs.value.find((job) => job.id === preferredJobId) ?? completedJobs.value[0]
  Object.assign(form, { source_job_id: source?.id ?? '', name: source?.title ?? '', description: '', type: 'daily', time: '09:00', weekdays: [0] })
}
function openCreate(preferredJobId = '') { resetForm(preferredJobId); createVisible.value = true }
function selectSourceJob(jobId: string) { const source = completedJobs.value.find((item) => item.id === jobId); if (source) form.name = source.title }
function recurrencePayload() { return { type: form.type, time: form.time, weekdays: form.type === 'weekly' ? form.weekdays : [] } }
function validateForm(requireSource = true) {
  if ((requireSource && !form.source_job_id) || !form.name.trim()) { ElMessage.warning('请选择来源任务并填写名称'); return false }
  if (form.type === 'weekly' && !form.weekdays.length) { ElMessage.warning('每周任务至少选择一天'); return false }
  return true
}
async function createSchedule() {
  if (!projectId.value || !validateForm()) return
  actionLoading.value = true
  try {
    const response = await api.post<ScheduledEvaluation>(`/projects/${projectId.value}/scheduled-evaluations`, {
      source_job_id: form.source_job_id, name: form.name.trim(), description: form.description.trim() || null,
      recurrence: recurrencePayload(), timezone: 'Asia/Shanghai', overlap_policy: 'skip', misfire_policy: 'latest',
    })
    createVisible.value = false; selectedId.value = response.data.schedule.id; await loadProjectData(); ElMessage.success('定时评测已启用')
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { actionLoading.value = false }
}
function openEdit(item: ScheduledEvaluation | null = selected.value) {
  if (!item) return
  selectedId.value = item.schedule.id
  const recurrence = item.schedule.recurrence
  Object.assign(form, { name: item.schedule.name, description: item.description ?? '', type: recurrence.type, time: recurrence.time, weekdays: [...recurrence.weekdays] })
  editVisible.value = true
}
async function updateSchedule() {
  if (!selected.value || !validateForm(false)) return
  actionLoading.value = true
  try {
    await api.put(`/schedules/${selected.value.schedule.id}`, { name: form.name.trim(), recurrence: recurrencePayload(), timezone: 'Asia/Shanghai', overlap_policy: selected.value.schedule.overlap_policy, misfire_policy: selected.value.schedule.misfire_policy })
    editVisible.value = false; await loadProjectData(); ElMessage.success('执行规则已更新')
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { actionLoading.value = false }
}
async function toggleSchedule(item: ScheduledEvaluation, enabled: unknown) {
  try { await api.patch(`/schedules/${item.schedule.id}/enabled`, { enabled: Boolean(enabled) }); await loadProjectData() }
  catch (error) { ElMessage.error(errorMessage(error)) }
}
async function runNow(item: ScheduledEvaluation | null = selected.value) {
  if (!item) return
  selectedId.value = item.schedule.id
  actionLoading.value = true
  try {
    const response = await api.post<AgentJob>(`/schedules/${item.schedule.id}/run-now`)
    ElMessage.success('评测任务已启动'); await router.push({ name: 'evaluations', params: { jobId: response.data.id } })
  } catch (error) { ElMessage.error(errorMessage(error)) } finally { actionLoading.value = false }
}
async function removeSchedule(item: ScheduledEvaluation | null = selected.value) {
  if (!item) return
  selectedId.value = item.schedule.id
  try {
    await ElMessageBox.confirm(`删除“${item.schedule.name}”后将停止后续自动执行，历史运行记录仍会保留。`, '删除定时评测', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
    await api.delete(`/schedules/${item.schedule.id}`); detailVisible.value = false; selectedId.value = ''; await loadProjectData(); ElMessage.success('定时评测已删除')
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error)) }
}
watch(projectId, async (value, previous) => {
  if (!initialized.value || !value || value === previous) return
  localStorage.setItem('evalweave-project', value); selectedId.value = ''; await router.replace({ name: 'schedules' }); await loadProjectData()
})
onMounted(async () => {
  try { await loadBase(); initialized.value = true; await loadProjectData(); const sourceJobId = String(route.query.sourceJobId ?? ''); if (sourceJobId && canManage.value) openCreate(sourceJobId) }
  catch (error) { ElMessage.error(errorMessage(error)) }
})
</script>

<template>
  <header class="page-header">
    <div><h1>定时任务</h1><p>让已经验证过的评测按计划自动运行，并持续跟踪每次结果。</p></div>
    <div class="page-actions">
      <el-button :icon="Refresh" :loading="loading" @click="loadProjectData">刷新</el-button>
      <el-button v-if="canManage" type="primary" :icon="Plus" :disabled="!completedJobs.length" @click="openCreate()">新建定时评测</el-button>
    </div>
  </header>
  <section class="schedule-stats">
    <div><span>定时评测</span><strong>{{ schedules.length }}</strong></div><div><span>正在运行</span><strong>{{ enabledCount }}</strong></div><div><span>最近一次</span><strong>{{ lastRun ? statusLabels[lastRun.status] : '暂无记录' }}</strong></div>
  </section>
  <section class="schedule-workbench surface" v-loading="loading">
    <div class="schedule-toolbar">
      <el-input v-model="searchKeyword" :prefix-icon="Search" clearable placeholder="搜索定时评测" class="schedule-search" />
      <el-radio-group v-model="statusFilter" class="schedule-filter">
        <el-radio-button value="all">全部 {{ schedules.length }}</el-radio-button>
        <el-radio-button value="enabled">运行中 {{ enabledCount }}</el-radio-button>
        <el-radio-button value="paused">已暂停 {{ schedules.length - enabledCount }}</el-radio-button>
      </el-radio-group>
    </div>
    <div v-if="filteredSchedules.length" class="schedule-card-grid">
      <article v-for="item in filteredSchedules" :key="item.schedule.id" class="schedule-card" @click="selectSchedule(item)">
        <div class="schedule-card-head">
          <span class="schedule-card-icon"><NavFeatureIcon kind="schedules" /></span>
          <div class="schedule-card-title"><h3>{{ item.schedule.name }}</h3><span :class="item.schedule.status">{{ item.schedule.status === 'enabled' ? '运行中' : '已暂停' }}</span></div>
          <el-switch :model-value="item.schedule.status === 'enabled'" :disabled="!canManage" @click.stop @change="toggleSchedule(item, $event)" />
        </div>
        <p>{{ item.description || item.goal }}</p>
        <div class="schedule-card-facts">
          <div><span>执行计划</span><strong>{{ scheduleDescription(item) }}</strong></div>
          <div><span>下次执行</span><strong>{{ item.schedule.status === 'enabled' ? formatBeijingDateTime(item.schedule.next_run_at) : '暂停中' }}</strong></div>
          <div><span>最近结果</span><strong>{{ item.recent_runs[0] ? statusLabels[item.recent_runs[0].status] : '暂无记录' }}</strong></div>
        </div>
        <div class="schedule-card-foot">
          <el-button text class="schedule-detail-link" @click.stop="selectSchedule(item)">查看详情<el-icon><ArrowRight /></el-icon></el-button>
          <div v-if="canManage" class="schedule-card-actions">
            <el-button size="small" type="primary" :icon="VideoPlay" @click.stop="runNow(item)">立即运行</el-button>
            <el-tooltip content="编辑" placement="top"><el-button class="schedule-icon-action" size="small" :icon="Edit" aria-label="编辑" @click.stop="openEdit(item)" /></el-tooltip>
            <el-tooltip content="删除" placement="top"><el-button class="schedule-icon-action danger" size="small" :icon="Delete" aria-label="删除" @click.stop="removeSchedule(item)" /></el-tooltip>
          </div>
        </div>
      </article>
    </div>
    <el-empty v-else-if="!loading" :description="schedules.length ? '没有符合条件的定时评测' : '还没有定时评测'" :image-size="72">
      <el-button v-if="canManage && completedJobs.length && !schedules.length" type="primary" :icon="Plus" @click="openCreate()">新建定时评测</el-button>
    </el-empty>
  </section>
  <el-dialog v-model="detailVisible" width="min(860px, calc(100vw - 32px))" align-center class="schedule-detail-dialog" @closed="closeDetail">
    <template #header><div class="detail-dialog-title"><span class="create-dialog-icon"><NavFeatureIcon kind="schedules" /></span><div><strong>定时评测详情</strong><small>查看执行计划、评测内容和最近运行记录</small></div></div></template>
    <main v-if="selected" class="schedule-detail">
      <section class="surface hero-card">
        <div class="hero-main"><div><div class="hero-status"><span :class="selected.schedule.status"></span>{{ selected.schedule.status === 'enabled' ? '自动执行已开启' : '自动执行已暂停' }}</div><h2>{{ selected.schedule.name }}</h2><p>{{ selected.description || selected.goal }}</p></div></div>
      </section>
      <section class="facts-grid">
        <div class="surface fact-card accent"><span>下次执行</span><strong>{{ selected.schedule.status === 'enabled' ? formatBeijingDateTime(selected.schedule.next_run_at) : '已暂停' }}</strong><small>{{ scheduleDescription(selected) }} · 北京时间</small></div>
        <div class="surface fact-card"><span>上次执行</span><strong>{{ selected.schedule.last_run_at ? formatBeijingDateTime(selected.schedule.last_run_at) : '尚未执行' }}</strong><small>{{ selectedLastRun ? statusLabels[selectedLastRun.status] : '等待首次运行' }}</small></div>
        <div class="surface fact-card"><span>结果格式</span><strong>{{ selected.output_format.toUpperCase() }}</strong><small>沿用已验证任务配置</small></div>
        <div class="surface fact-card switch-card"><span>自动执行</span><el-switch :model-value="selected.schedule.status === 'enabled'" :disabled="!canManage" @change="toggleSchedule(selected, $event)" /><small>暂停不会影响历史结果</small></div>
      </section>
      <section class="surface source-card"><div class="section-heading"><div><h3>评测内容</h3></div><router-link :to="`/evaluations/${selected.source_job_id}`">查看来源任务</router-link></div><p>{{ selected.goal }}</p><div class="policy-note"><el-icon><Check /></el-icon><span>执行时复用已经验证的评测配置；上一次尚未结束时，本次自动跳过。</span></div></section>
      <section class="surface history-card">
        <div class="section-heading"><div><h3>最近运行</h3></div><span>{{ recentRuns.length }} 条记录</span></div>
        <div v-if="recentRuns.length" class="run-list"><router-link v-for="run in recentRuns" :key="run.id" :to="`/evaluations/${run.id}`" class="run-row"><span class="run-status-icon" :class="run.status"><el-icon><Check v-if="run.status === 'completed'" /><WarningFilled v-else-if="run.status === 'failed'" /><Clock v-else /></el-icon></span><span><strong>{{ formatBeijingDateTime(run.scheduled_for || run.created_at) }}</strong><small>{{ run.error || (run.status === 'completed' ? '评测已完成' : statusLabels[run.status]) }}</small></span><span class="run-duration">{{ runDuration(run) }}</span><el-tag size="small" :type="runStatusType(run.status)">{{ statusLabels[run.status] }}</el-tag></router-link></div>
        <el-empty v-else description="尚无运行记录" :image-size="56" />
      </section>
    </main>
  </el-dialog>
  <el-dialog v-model="createVisible" width="min(680px, calc(100vw - 32px))" align-center class="new-schedule-dialog">
    <template #header>
      <div class="create-dialog-header">
        <span class="create-dialog-icon"><NavFeatureIcon kind="schedules" /></span>
        <div><h2>新建定时评测</h2><p>复用已经验证的评测任务，按固定计划持续执行。</p></div>
      </div>
    </template>
    <el-form label-position="top" class="create-form">
      <section class="create-section">
        <div class="create-section-heading"><span>1</span><div><strong>选择评测内容</strong><small>只展示已成功完成并生成评测方案的任务</small></div></div>
        <el-form-item>
          <el-select v-model="form.source_job_id" class="evaluation-source-select" popper-class="evaluation-source-popper" placeholder="选择一条已验证的评测任务" filterable no-data-text="暂无可用于定时评测的任务" @change="selectSourceJob">
            <el-option v-for="job in completedJobs" :key="job.id" :label="job.title" :value="job.id">
              <div class="evaluation-source-option">
                <strong>{{ job.title }}</strong>
                <time>{{ formatBeijingDateTime(job.updated_at) }}</time>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </section>
      <section class="create-section">
        <div class="create-section-heading"><span>2</span><div><strong>任务信息</strong><small>用于在定时评测列表中识别这项任务</small></div></div>
        <div class="create-fields"><el-form-item label="名称"><el-input v-model="form.name" maxlength="128" placeholder="例如：每日客服回复质量检查" /></el-form-item><el-form-item label="说明"><el-input v-model="form.description" maxlength="4000" placeholder="补充这项定时评测的用途" /></el-form-item></div>
      </section>
      <section class="create-section schedule-rule-section">
        <div class="create-section-heading"><span>3</span><div><strong>执行规则</strong><small>所有时间均使用北京时间</small></div></div>
        <div class="rule-fields"><el-form-item label="频率"><el-radio-group v-model="form.type"><el-radio-button value="daily">每天</el-radio-button><el-radio-button value="weekly">每周</el-radio-button></el-radio-group></el-form-item><el-form-item label="时间"><el-time-picker v-model="form.time" format="HH:mm" value-format="HH:mm" :clearable="false" /></el-form-item></div>
        <el-form-item v-if="form.type === 'weekly'" label="执行日期" class="weekday-field"><el-checkbox-group v-model="form.weekdays"><el-checkbox v-for="day in weekdays" :key="day.value" :value="day.value" border>{{ day.label }}</el-checkbox></el-checkbox-group></el-form-item>
        <div class="rule-preview"><span class="rule-preview-icon"><el-icon><Calendar /></el-icon></span><div><small>创建后的执行计划</small><strong>{{ rulePreview }}</strong></div><span class="enable-preview"><i></i>立即启用</span></div>
      </section>
    </el-form>
    <template #footer><div class="create-dialog-footer"><span>可随时暂停或修改执行规则</span><div><el-button @click="createVisible=false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="createSchedule">创建定时评测</el-button></div></div></template>
  </el-dialog>
  <el-dialog v-model="editVisible" title="编辑执行规则" width="520px" align-center><el-form label-position="top"><el-form-item label="名称"><el-input v-model="form.name" maxlength="128" /></el-form-item><el-form-item label="执行频率"><el-radio-group v-model="form.type"><el-radio-button value="daily">每天</el-radio-button><el-radio-button value="weekly">每周</el-radio-button></el-radio-group></el-form-item><el-form-item v-if="form.type === 'weekly'" label="执行日期"><el-checkbox-group v-model="form.weekdays"><el-checkbox v-for="day in weekdays" :key="day.value" :value="day.value">{{ day.label }}</el-checkbox></el-checkbox-group></el-form-item><el-form-item label="执行时间"><el-time-picker v-model="form.time" format="HH:mm" value-format="HH:mm" :clearable="false" /></el-form-item></el-form><template #footer><el-button @click="editVisible=false">取消</el-button><el-button type="primary" :loading="actionLoading" @click="updateSchedule">保存修改</el-button></template></el-dialog>
</template>

<style scoped>
.schedule-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:18px}.schedule-header h1{margin:5px 0 6px;color:#17243b;font-size:30px;letter-spacing:-.04em}.schedule-header p{margin:0;color:#748198}.eyebrow{color:#607be5;font-size:11px;font-weight:800;letter-spacing:.12em}.header-actions{display:flex;align-items:center;gap:10px}.project-select{width:180px}.schedule-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:16px}.schedule-stats>div{padding:14px 18px;border:1px solid #e7ebf3;border-radius:14px;background:linear-gradient(135deg,#fff,#f8faff)}.schedule-stats span,.schedule-stats strong{display:block}.schedule-stats span{color:#8490a5;font-size:12px}.schedule-stats strong{margin-top:4px;color:#22304a;font-size:21px}.schedule-layout{display:grid;grid-template-columns:310px minmax(0,1fr);gap:18px;align-items:start}.surface{border:1px solid #e7ebf3;border-radius:16px;background:#fff;box-shadow:0 8px 30px rgba(31,48,82,.055)}.schedule-list{min-height:560px;padding:10px}.list-title{display:flex;justify-content:space-between;padding:10px 9px 12px;color:#34415a}.list-title span{color:#99a2b2}.schedule-list-item{display:grid;width:100%;grid-template-columns:42px minmax(0,1fr) 10px;gap:11px;align-items:center;margin-bottom:5px;padding:13px 11px;border:0;border-radius:12px;background:transparent;text-align:left;cursor:pointer}.schedule-list-item:hover,.schedule-list-item.active{background:#f1f5ff}.schedule-list-item.active{box-shadow:inset 3px 0 #4d6ee8}.schedule-icon{display:grid;width:40px;height:40px;place-items:center;border-radius:11px;color:#526fdf;background:#eaf0ff}.schedule-copy strong,.schedule-copy small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.schedule-copy strong{color:#2b374d;font-size:14px}.schedule-copy small{margin-top:3px;color:#8490a4;font-size:11px}.status-dot,.hero-status span{width:8px;height:8px;border-radius:50%;background:#aeb8c8}.status-dot.enabled,.hero-status span.enabled{background:#39ad72;box-shadow:0 0 0 4px rgba(57,173,114,.12)}.schedule-detail{display:grid;gap:14px}.hero-card{display:flex;justify-content:space-between;gap:20px;padding:22px}.hero-main{display:flex;min-width:0;gap:14px}.hero-icon{display:grid;width:48px;height:48px;flex:0 0 auto;place-items:center;border-radius:14px;color:#fff;background:linear-gradient(145deg,#526fe2,#7a64dc);box-shadow:0 8px 18px rgba(82,111,226,.24)}.hero-status{display:flex;align-items:center;gap:8px;color:#748199;font-size:12px}.hero-main h2{margin:5px 0 7px;color:#1e2b43;font-size:24px}.hero-main p{max-width:650px;margin:0;color:#717e94;line-height:1.6}.hero-actions{display:flex;align-items:flex-start;gap:7px}.facts-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.fact-card{min-height:105px;padding:16px}.fact-card span,.fact-card strong,.fact-card small{display:block}.fact-card span{color:#8a95a7;font-size:12px}.fact-card strong{margin-top:9px;color:#29364e;font-size:16px}.fact-card small{margin-top:7px;color:#98a1b1;font-size:11px}.fact-card.accent{border-color:#dbe4ff;background:#f7f9ff}.switch-card .el-switch{margin-top:7px}.source-card,.history-card{padding:20px 22px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.section-heading h3{margin:4px 0 0;color:#253149;font-size:18px}.section-heading>a{color:#526fdf;text-decoration:none}.source-card>p{margin:16px 0;color:#56637a;line-height:1.75}.policy-note{display:flex;align-items:center;gap:8px;padding:11px 13px;border-radius:10px;color:#54705f;background:#f0f8f3;font-size:13px}.run-list{margin-top:13px}.run-row{display:grid;grid-template-columns:36px minmax(0,1fr) 90px 70px;gap:11px;align-items:center;padding:12px 8px;border-top:1px solid #edf0f5;color:inherit;text-decoration:none}.run-row:hover{background:#fafbfe}.run-status-icon{display:grid;width:30px;height:30px;place-items:center;border-radius:50%;color:#526fdf;background:#edf2ff}.run-status-icon.completed{color:#25945e;background:#eaf8f0}.run-status-icon.failed{color:#d75454;background:#fff0f0}.run-row strong,.run-row small{display:block}.run-row strong{color:#344058;font-size:13px}.run-row small{margin-top:3px;overflow:hidden;color:#8a95a7;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.run-duration{color:#7c8799;font-size:12px}.empty-detail{min-height:560px;display:grid;place-items:center}.form-help{display:block;margin-top:6px;color:#8a94a6}.hero-actions .el-button+.el-button{margin-left:0}
:global(.new-schedule-dialog){overflow:hidden;border:1px solid rgba(220,229,242,.92);border-radius:18px;box-shadow:0 26px 76px rgba(35,58,96,.19)}:global(.new-schedule-dialog .el-dialog__header){margin:0;padding:22px 24px 18px;border-bottom:1px solid var(--ew-line)}:global(.new-schedule-dialog .el-dialog__body){padding:18px 24px 6px;background:var(--ew-canvas)}:global(.new-schedule-dialog .el-dialog__footer){padding:16px 24px;border-top:1px solid var(--ew-line);background:var(--ew-panel)}.create-dialog-header{display:flex;align-items:center;gap:13px}.create-dialog-logo{width:44px;height:42px;background-size:44px 44px}.create-dialog-header h2{margin:0;color:var(--ew-ink);font-size:20px}.create-dialog-header p{margin:4px 0 0;color:#8490a4;font-size:12px}.create-form{display:grid;gap:12px}.create-section{padding:16px 17px;border:1px solid var(--ew-line);border-radius:13px;background:var(--ew-panel)}.create-section-heading{display:flex;align-items:center;gap:10px;margin-bottom:14px}.create-section-heading>span{display:grid;width:25px;height:25px;flex:0 0 auto;place-items:center;border-radius:8px;color:var(--ew-blue-dark);background:var(--ew-blue-soft);font-size:12px;font-weight:800}.create-section-heading strong,.create-section-heading small{display:block}.create-section-heading strong{color:#2c3950;font-size:14px}.create-section-heading small{margin-top:2px;color:#96a0b0;font-size:11px}.create-section .el-form-item{margin-bottom:0}.evaluation-source-select{width:100%}.evaluation-source-select :deep(.el-select__wrapper){min-height:42px}.evaluation-source-option{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:20px}.evaluation-source-option>span{min-width:0}.evaluation-source-option strong,.evaluation-source-option small{display:block}.evaluation-source-option strong{overflow:hidden;color:#2c3950;text-overflow:ellipsis;white-space:nowrap}.evaluation-source-option small{overflow:hidden;color:#8a98ac;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.evaluation-source-option>span small{max-width:390px;margin-top:2px}.evaluation-source-option>small{flex:0 0 auto}.source-preview{display:grid;grid-template-columns:32px minmax(0,1fr) auto;gap:10px;align-items:center;margin-top:10px;padding:10px 12px;border:1px solid #dcefe4;border-radius:10px;background:#f3f8f5}.source-preview>span{display:grid;width:28px;height:28px;place-items:center;border-radius:50%;color:#26945e;background:#ddf3e7}.source-preview strong,.source-preview small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.source-preview strong{color:#354359;font-size:13px}.source-preview small{margin-top:3px;color:#7f8b9d;font-size:11px}.create-fields,.rule-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.rule-fields{grid-template-columns:1fr 190px}.weekday-field{margin-top:13px!important}.weekday-field :deep(.el-checkbox-group){display:flex;flex-wrap:wrap;gap:7px}.weekday-field :deep(.el-checkbox){margin-right:0}.rule-preview{display:flex;align-items:center;gap:11px;margin-top:14px;padding:12px 13px;border:1px solid var(--el-color-primary-light-7);border-radius:11px;background:var(--ew-blue-soft)}.rule-preview-icon{display:grid;width:34px;height:34px;place-items:center;border-radius:10px;color:var(--ew-blue);background:#dfe9ff}.rule-preview div{min-width:0;flex:1}.rule-preview small,.rule-preview strong{display:block}.rule-preview small{color:#8894a8;font-size:11px}.rule-preview strong{margin-top:3px;color:#31415e;font-size:14px}.enable-preview{display:flex;align-items:center;gap:7px;color:#56806a;font-size:12px}.enable-preview i{width:7px;height:7px;border-radius:50%;background:#39ad72;box-shadow:0 0 0 4px rgba(57,173,114,.12)}.create-dialog-footer{display:flex;align-items:center;justify-content:space-between;gap:16px}.create-dialog-footer>span{color:#929cac;font-size:12px}
.create-dialog-icon{display:grid;width:42px;height:42px;flex:0 0 auto;place-items:center;border:1px solid #dce6ff;border-radius:12px;color:var(--ew-blue);background:var(--ew-blue-soft);box-shadow:0 7px 16px rgba(79,124,255,.12)}.create-dialog-icon .nav-feature-icon{width:23px;height:23px}
:global(.evaluation-source-popper .el-select-dropdown__item){height:42px;padding:0 12px;line-height:42px}.evaluation-source-option{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:16px}.evaluation-source-option strong{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.evaluation-source-option time{flex:0 0 auto;color:#8a98ac;font-size:11px;font-weight:400}
.schedule-stats>div{text-align:center}
.schedule-workbench.surface{min-height:420px;padding:18px}.schedule-workbench.surface .schedule-toolbar{padding-bottom:16px;border-bottom:1px solid var(--ew-line)}
.schedule-card-foot{justify-content:space-between}.schedule-detail-link{padding:5px 2px;color:#65738a}.schedule-detail-link .el-icon{margin-left:4px;font-size:12px;transition:transform .16s ease}.schedule-detail-link:hover .el-icon{transform:translateX(2px)}.schedule-card-actions{display:flex;align-items:center;gap:7px}.schedule-card-actions .el-button+.el-button{margin-left:0}.schedule-icon-action{width:30px;padding:0}.schedule-icon-action.danger{color:#d85d5d;border-color:#f0cccc;background:#fffafa}.schedule-icon-action.danger:hover{color:#fff;border-color:#e66767;background:#e66767}
.schedule-detail-dialog .fact-card{text-align:center}.schedule-detail-dialog .switch-card .el-switch{display:flex;width:max-content;margin:7px auto 0}
.schedule-workbench{min-height:420px}.schedule-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.schedule-search{width:280px}.schedule-filter{flex:0 0 auto}.schedule-card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}.schedule-card{display:flex;min-height:272px;padding:19px 20px 15px;flex-direction:column;border:1px solid var(--ew-line);border-radius:16px;background:var(--ew-panel);box-shadow:var(--ew-shadow);cursor:pointer;transition:border-color .18s ease,box-shadow .18s ease,transform .18s ease}.schedule-card:hover{border-color:#ccdafa;box-shadow:0 14px 36px rgba(30,64,175,.1);transform:translateY(-2px)}.schedule-card-head{display:flex;min-width:0;align-items:center;gap:11px}.schedule-card-icon{display:grid;width:38px;height:38px;flex:0 0 auto;place-items:center;border-radius:11px;color:var(--ew-blue);background:var(--ew-blue-soft)}.schedule-card-icon .nav-feature-icon{width:21px;height:21px}.schedule-card-title{display:flex;min-width:0;flex:1;align-items:center;gap:8px}.schedule-card-title h3{min-width:0;margin:0;overflow:hidden;color:var(--ew-ink);font-size:15px;text-overflow:ellipsis;white-space:nowrap}.schedule-card-title>span{padding:3px 8px;flex:0 0 auto;border-radius:999px;color:#79869a;background:#f0f2f6;font-size:10px;font-weight:650}.schedule-card-title>span.enabled{color:#218a56;background:#eaf7ef}.schedule-card>p{display:-webkit-box;min-height:42px;margin:15px 0 16px;overflow:hidden;color:#718096;font-size:13px;line-height:1.65;-webkit-box-orient:vertical;-webkit-line-clamp:2}.schedule-card-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.schedule-card-facts>div{min-width:0;padding:10px;border-radius:10px;background:#f7f9fc}.schedule-card-facts span,.schedule-card-facts strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.schedule-card-facts span{color:#98a3b4;font-size:10px}.schedule-card-facts strong{margin-top:4px;color:#3a4860;font-size:12px;font-weight:600}.schedule-card-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:auto;padding-top:13px;border-top:1px solid #edf0f5}.schedule-card-foot>span{display:flex;align-items:center;gap:6px;color:#909bad;font-size:10px}.schedule-card-foot>span i{width:6px;height:6px;border-radius:50%;background:#aeb8c8}.schedule-card-foot>span i.enabled{background:#39ad72}.schedule-card-foot>div{display:flex;align-items:center;gap:2px}.schedule-card-foot .el-button+.el-button{margin-left:0}:global(.schedule-detail-dialog){overflow:hidden;border:1px solid rgba(220,229,242,.92);border-radius:18px}:global(.schedule-detail-dialog .el-dialog__header){margin:0;padding:20px 24px;border-bottom:1px solid var(--ew-line)}:global(.schedule-detail-dialog .el-dialog__body){max-height:min(720px,calc(100vh - 150px));overflow-y:auto;padding:18px;background:var(--ew-canvas)}.detail-dialog-title{display:flex;align-items:center;gap:12px}.detail-dialog-title strong,.detail-dialog-title small{display:block}.detail-dialog-title strong{color:var(--ew-ink);font-size:18px}.detail-dialog-title small{margin-top:3px;color:#8b98ad;font-size:12px}.schedule-detail-dialog .schedule-detail{gap:12px}
@media(max-width:1100px){.facts-grid{grid-template-columns:repeat(2,1fr)}.hero-card{display:block}.hero-actions{margin-top:16px}}@media(max-width:850px){.schedule-header{align-items:flex-start;flex-direction:column}.header-actions{width:100%;flex-wrap:wrap}.schedule-layout{grid-template-columns:1fr}.schedule-list{min-height:auto}.schedule-stats{grid-template-columns:1fr}.facts-grid{grid-template-columns:1fr}.run-row{grid-template-columns:36px minmax(0,1fr) 65px}.run-duration{display:none}.create-fields,.rule-fields{grid-template-columns:1fr}.create-dialog-footer{align-items:flex-end;flex-direction:column}}
</style>
