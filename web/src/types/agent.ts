export type AgentJobStatus =
  | 'pending'
  | 'discovering'
  | 'planning'
  | 'waiting_human'
  | 'running'
  | 'analyzing'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface Project {
  id: string
  name: string
  description: string | null
  created_at: string
  updated_at: string
}

export interface FileObject {
  id: string
  project_id: string
  category: string
  original_name: string
  content_type: string | null
  size_bytes: number
  created_at: string
}

export interface AgentJob {
  id: string
  project_id: string
  source_file_id: string | null
  result_file_id: string | null
  title: string
  goal: string
  status: AgentJobStatus
  input_config: Record<string, unknown>
  eval_spec: Record<string, unknown>
  result: Record<string, unknown>
  error: string | null
  repair_attempts: number
  max_repair_attempts: number
  requires_approval: boolean
  created_at: string
  updated_at: string
}

export interface AgentStep {
  id: string
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  attempt: number
  input_data: Record<string, unknown>
  output_data: Record<string, unknown>
  error: string | null
  started_at: string | null
  finished_at: string | null
}

export interface AgentJobEvent {
  id: number
  job_id: string
  phase: string
  event_type: 'model_start' | 'model_delta' | 'model_complete' | 'model_error'
  content: string
  payload: Record<string, unknown>
  created_at: string
}

export interface AgentRuntime {
  enabled: boolean
  model: string | null
  require_approval: boolean
  worker_available: boolean
}

export interface EvaluationModelOption {
  id: string
  name: string
  model_name: string
  api_mode: 'responses' | 'chat_completions'
}

export interface EvaluationModel extends EvaluationModelOption {
  base_url: string
  is_active: boolean
  api_key_configured: boolean
  created_at: string
  updated_at: string
}

export interface AgentChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface AgentAssistResponse {
  reply: string
  draft: Record<string, unknown>
}

export interface AssistantConversation {
  id: string
  project_id: string | null
  title: string
  draft: Record<string, unknown>
  status: 'collecting' | 'choose_output' | 'ready' | 'started'
  agent_job_id: string | null
  created_at: string
  updated_at: string
}

export interface AssistantMessage {
  id?: string
  conversation_id?: string
  role: 'user' | 'assistant'
  content: string
  ui_action?: { type?: string, question?: string, summary?: string, options?: unknown[] } | null
  include_in_context?: boolean
  is_streaming?: boolean
  attachment_file_id?: string | null
  attachment_name?: string | null
  attachment_content_type?: string | null
  attachment_size_bytes?: number | null
  created_at?: string
  streaming?: boolean
  react_trace?: ReactToolStep[]
}

export interface ReactToolStep {
  name: string
  label: string
  status: 'running' | 'completed' | 'failed'
  summary?: unknown
}

export interface HumanTask {
  id: string
  job_id: string
  title: string
  instructions: string
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  decision_reason: string | null
  resolved_by: string | null
  resolved_at: string | null
  created_at: string
}

export interface HumanReviewRubric {
  key: string
  label: string
  min_score: number
  max_score: number
}

export interface HumanReviewAssignment {
  id: string
  campaign_id: string
  campaign_title: string
  campaign_instructions: string
  campaign_deadline_at: string | null
  item_id: string
  source_index: number
  prompt: string
  response: string
  metadata: Record<string, unknown>
  rubric: HumanReviewRubric[]
  status: 'pending' | 'submitted' | 'expired'
  dimension_scores: Array<{ key: string; label: string; score: number }>
  overall_score: number | null
  reason: string | null
  submitted_at: string | null
  created_at: string
}

export interface HumanReviewCampaign {
  id: string
  job_id: string
  created_by: string
  title: string
  instructions: string
  status: 'active' | 'summarizing' | 'completed' | 'cancelled'
  rubric: HumanReviewRubric[]
  blind_config: Record<string, unknown>
  item_count: number
  total_assignments: number
  completed_assignments: number
  deadline_at: string | null
  completion_reason: 'all_submitted' | 'deadline_reached' | 'manual' | null
  summary: {
    average_overall_score?: number | null
    dimension_average_scores?: Record<string, { label: string; score: number }>
    markdown?: string
  }
  completed_at: string | null
  created_at: string
  updated_at: string
}
