export type BlockKind = "user" | "assistant" | "reasoning" | "tool";

export type ToolStatus = "running" | "ok" | "error";

export interface TextSegment {
  id: string;
  type: "user" | "assistant" | "reasoning";
  content?: string;
}

export interface ToolSegment {
  id: string;
  type: "tool";
  call_id?: string;
  tool?: string;
  summary?: string;
  arguments?: Record<string, unknown> | null;
  status?: ToolStatus;
  duration?: number | null;
  exit_code?: number | null;
  output?: string | null;
  error?: string | null;
}

export type Segment = TextSegment | ToolSegment;

export interface SessionSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  busy?: boolean;
  model?: string | null;
  harness?: string | null;
  harness_locked?: boolean;
  origin?: string | null;
  target?: Record<string, unknown> | null;
  report_segment_id?: string | null;
}

export interface SessionDetail extends SessionSummary {
  segments?: Segment[];
}

export type TaskStatus = "backlog" | "todo" | "doing" | "review" | "done";

export type TaskPriority = "low" | "normal" | "high" | "urgent";

export interface BoardSessionSummary {
  id: string;
  title: string;
  busy: boolean;
  model: string | null;
  harness: string | null;
  harness_locked: boolean;
  updated_at: number;
}

export interface BoardTask {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  tags: string[];
  position: number;
  model: string | null;
  harness: string | null;
  session_id: string | null;
  queued_at: number | null;
  run_id: string | null;
  started_at: number | null;
  finished_at: number | null;
  last_error: string | null;
  resume_pending: boolean;
  prompt_started: boolean;
  created_at: number;
  updated_at: number;
  comment_count: number;
  run_count: number;
  session?: BoardSessionSummary | null;
}

export interface TaskComment {
  id: string;
  body: string;
  created_at: number;
  run_id: string | null;
}

export type TaskRunStatus = "completed" | "error" | "cancelled";

export interface TaskRun {
  run_id: string;
  started_at: number | null;
  finished_at: number;
  status: TaskRunStatus;
  response: string;
  error: string | null;
}

export interface TaskDetail extends BoardTask {
  comments: TaskComment[];
  runs: TaskRun[];
}

export interface CommentResult {
  board: BoardSnapshot;
  detail: TaskDetail;
  deferred: boolean;
}

export interface BoardExecutorState {
  paused: boolean;
  running_task_id: string | null;
}

export interface BoardSnapshot {
  version: number;
  statuses: TaskStatus[];
  updated_at: number;
  tasks: BoardTask[];
  executor?: BoardExecutorState;
}

export interface TaskInput {
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  tags: string[];
    model: string;
  harness: string;
}

export interface DeleteTaskResult {
  board: BoardSnapshot;
  deleted_session: boolean;
}

export interface EnsureSessionResult {
  board: BoardSnapshot;
  session_id: string;
  created: boolean;
}

export interface ModelOption {
  id: string;
  label: string;
  model_id: string;
}

export interface HarnessOption {
  id: string;
  /** The harness yaml's one-gene mutation note, shown as the info tooltip. */
  mutation?: string | null;
  /** Falls back to the yaml reason when a base harness has no mutation. */
  reason?: string | null;
}

export interface RuntimeConfig {
  model?: string;
  harness?: string;
  models?: ModelOption[];
  default_model?: string;
  harnesses?: HarnessOption[];
  default_harness?: string;
  stream?: boolean;
  tools?: string[];
  error?: string | null;
}

/* ------------------------------------------------ workspace (/files) */

export type WorkspaceEntryType = "dir" | "file" | "symlink";

export interface WorkspaceEntry {
  name: string;
  path: string;
  type: WorkspaceEntryType;
  size: number | null;
  mtime: number;
  ignored: boolean;
  protected: boolean;
}

export interface WorkspaceTree {
  root: string;
  repo_root: string | null;
  is_git: boolean;
  path: string;
  entries: WorkspaceEntry[];
  hidden_ignored: number;
  truncated: boolean;
}

export interface WorkspaceFile {
  path: string;
  size: number;
  mtime: number;
  binary: boolean;
  truncated: boolean;
  lines: number | null;
  encoding: string | null;
  content: string | null;
}

export type ChangeStatus =
  | "modified"
  | "added"
  | "deleted"
  | "renamed"
  | "copied"
  | "typechange"
  | "untracked"
  | "conflicted";

export interface ChangeEntry {
  path: string;
  old_path: string | null;
  status: ChangeStatus;
  staged: boolean;
  unstaged: boolean;
  additions: number | null;
  deletions: number | null;
  binary: boolean;
  protected: boolean;
}

export interface ChangesCounts {
  total: number;
  modified: number;
  added: number;
  deleted: number;
  renamed: number;
  untracked: number;
  conflicted: number;
}

export interface WorkspaceChanges {
  root: string;
  repo_root: string | null;
  is_git: boolean;
  git_error: string | null;
  branch: string | null;
  head: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  entries: ChangeEntry[];
  counts: ChangesCounts;
  truncated: boolean;
}

export type DiffLineKind = "ctx" | "add" | "del" | "note";

export interface DiffLine {
  kind: DiffLineKind;
  old: number | null;
  new: number | null;
  text: string;
}

export interface DiffHunk {
  header: string;
  old_start: number;
  old_count: number;
  new_start: number;
  new_count: number;
  lines: DiffLine[];
}

export interface WorkspaceDiff {
  path: string;
  old_path: string | null;
  status: ChangeStatus;
  binary: boolean;
  truncated: boolean;
  hunks: DiffHunk[];
}

export interface DeltaFrame {
  type: "delta";
  id: string;
  channel: "assistant" | "reasoning";
  text?: string;
  seq?: number;
}

export interface ToolFrame {
  type: "tool.started" | "tool.completed" | "tool.failed";
  id: string;
  call_id?: string;
  tool?: string;
  summary?: string;
  arguments?: Record<string, unknown> | null;
  status?: ToolStatus;
  duration?: number | null;
  exit_code?: number | null;
  output?: string | null;
  error?: string | null;
  seq?: number;
}

export interface SegmentFrame {
  type: "segment";
  segment: Segment;
  seq?: number;
}

export interface RunStartedFrame {
  type: "run.started";
  run_id?: string;
  message?: string;
  seq?: number;
}

export interface RunCompletedFrame {
  type: "run.completed";
  answer?: string;
  iterations?: number;
  seq?: number;
}

export interface RunErrorFrame {
  type: "run.error";
  stage?: string;
  error?: string;
  seq?: number;
}

export type Frame =
  | DeltaFrame
  | ToolFrame
  | SegmentFrame
  | RunStartedFrame
  | RunCompletedFrame
  | RunErrorFrame;

/* -------------------------------------------- evaluation (/evaluation) */

export type EvalRange = "24h" | "7d" | "30d" | "90d" | "all";

export type EvalTab =
  | "overview"
  | "validity"
  | "process"
  | "efficiency"
  | "reliability"
  | "tasks"
  | "benchmarks"
  | "diagnosis";

/** ``null`` always means "no sample", never "0". */
export type Ratio = number | null;

export interface EvalTokens {
  prompt: number;
  completion: number;
  cached: number;
  summarizer: number;
  total: number;
}

export interface EvalLatency {
  llm: number;
  tool: number;
  summarizer: number;
  overhead: number;
}

export interface EvalHistogramRow {
  label: string;
  count: number;
}

export interface OverviewKpis {
  runs: number;
  tcr: Ratio;
  error_rate: Ratio;
  cancelled_rate: Ratio;
  tool_calls: number;
  tool_error_rate: Ratio;
  schema_pass_rate: Ratio;
  retry_rate: Ratio;
  summary_invalid_rate: Ratio;
  policy_blocks: number;
  tokens_total: number;
  tokens_per_run: Ratio;
  duration_p50_ms: Ratio;
  duration_p95_ms: Ratio;
  ttft_p50_ms: Ratio;
  ttft_p95_ms: Ratio;
  dropped_events: number;
  trace_bytes: number;
}

export interface EvalTrendRow {
  day: string;
  runs: number;
  tcr: Ratio;
  tokens: number;
  errors: number;
  duration_p50_ms: Ratio;
  duration_p95_ms: Ratio;
}

export interface EvalSliceRow {
  key: string;
  runs: number;
  tcr: Ratio;
  tokens: number;
  duration_p95_ms: Ratio;
}

export interface EvalOverview {
  range: string;
  origins: string[] | null;
  since: number | null;
  kpis: OverviewKpis;
  tokens: EvalTokens;
  latency_ms: EvalLatency;
  retries: { runs: number; total: number };
  summary: { attempts: number; invalid: number; compactions: number };
  errors: { stage: string; code: string; count: number }[];
  error_kinds: { kind: string; count: number }[];
  trends: EvalTrendRow[];
  histograms: {
    iterations: EvalHistogramRow[];
    duration_ms: EvalHistogramRow[];
  };
  slices: {
    origin: EvalSliceRow[];
    harness: EvalSliceRow[];
    model: EvalSliceRow[];
  };
  excluded: { runs_by_origin: number };
  generated_at: number;
}

export interface BoardFunnel {
  created: number;
  executed: number;
  run_completed: number;
  review: number;
  done: number;
}

export interface BoardKpis {
  tsr: Ratio;
  first_pass_rate: Ratio;
  rework_rate: Ratio;
  queue_wait_p50_ms: Ratio;
  queue_wait_p95_ms: Ratio;
  queue_wait_mean_ms: Ratio;
  tokens_per_task: Ratio;
  runs_per_task: Ratio;
  vcr: Ratio;
  hallucinated_rate: Ratio;
}

export interface HallucinatedTask {
  task_id: string;
  run_id: string | null;
  status: string;
  runs: number;
  rework_count: number;
}

export interface ArtifactPayload {
  judged_tasks: number;
  claiming_tasks: number;
  unmeasured_claiming_tasks: number;
  vcr_runs: number;
  vcr_passed: number;
  vcr: Ratio;
  hallucinated_count: number;
  hallucinated_rate: Ratio;
  hallucinated_tasks: HallucinatedTask[];
}

export interface OffenderRow {
  task_id: string;
  status: string;
  runs: number;
  tokens: number;
  wall_ms: number;
  rework: number;
}

export interface BoardOverview {
  range: string;
  since: number | null;
  funnel: BoardFunnel;
  kpis: BoardKpis;
  counts: {
    tasks: number;
    executed: number;
    accepted: number;
    reworked: number;
    first_pass: number;
    rework_events: number;
    runs: number;
    completed_runs: number;
  };
  rework_reasons: { reason: string; count: number }[];
  artifact: ArtifactPayload;
  tsr_trend: { day: string; tasks: number; executed: number; done: number;
               tsr: Ratio }[];
  top_offenders: {
    tokens: OffenderRow[];
    wall_ms: OffenderRow[];
    rework: OffenderRow[];
  };
  generated_at: number;
}

export interface EvalTaskRow {
  task_id: string;
  title: string;
  status: TaskStatus;
  harness: string | null;
  model: string | null;
  runs: number;
  completed_runs: number;
  tokens: number;
  wall_ms: number;
  rework: number;
  expects_artifact: boolean | null;
  queue_wait_ms: number | null;
  last_error: string | null;
  updated_at: number;
  created_at: number;
}

export interface EvalTaskPage {
  total: number;
  page: number;
  page_size: number;
  rows: EvalTaskRow[];
  generated_at: number;
}

export interface EvalRunMetrics {
  run_id: string;
  origin: string;
  harness_id: string;
  genes_hash: string;
  model: string;
  session_id: string | null;
  task_id: string | null;
  run_index: number | null;
  status: string;
  has_answer: boolean;
  started_at: number;
  finished_at: number;
  duration_ms: number;
  iterations: number;
  tool_calls: number;
  tool_errors: number;
  error_kinds: Record<string, number>;
  edited_files: string[];
  tokens: EvalTokens;
  latency_ms: EvalLatency;
  ttft_ms: number | null;
  retries: number;
  summary_invalid: number;
  summary_attempts: number;
  compactions: number;
  policy_blocks: number;
  error: Record<string, string> | null;
  trace_bytes: number;
  dropped_events: number;
}

export type EvalEvidence = "missing" | "present" | "none";

export interface EvalTaskRun {
  run_id: string | null;
  status: string | null;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  response_chars: number;
  response: string;
  metrics: EvalRunMetrics | null;
  evidence: EvalEvidence;
  edited_files: string[];
  trace_available: boolean;
}

export interface EvalTimelineEvent {
  type: string;
  at: number;
  run_id: string | null;
  from: string | null;
  to: string | null;
  status: string | null;
  queue_wait_ms: number | null;
}

export interface EvalDiagnosisReportCitation {
  kind: "event" | "run" | "iteration";
  value: string;
  verified: boolean;
}

export interface EvalDiagnosisVerification {
  citations: EvalDiagnosisReportCitation[];
  unverified: string[];
  missing_sections: string[];
  order_ok: boolean;
  complete: boolean;
  suggestion_count: number;
}

export interface EvalDiagnosisSuggestion {
  index: number;
  text: string;
}

export interface EvalDiagnosisReport {
  available: boolean;
  markdown: string;
  verification?: EvalDiagnosisVerification;
  suggestions?: EvalDiagnosisSuggestion[];
}

export interface EvalDiagnosisRow {
  session_id: string;
  title: string;
  origin: string;
  target: Record<string, unknown> | null;
  target_type: string | null;
  target_id: string | null;
  report_segment_id: string | null;
  bundle_hash: string | null;
  diagnosis_id: string | null;
  created_at: number;
  updated_at: number;
  report?: EvalDiagnosisReport;
}

export type EvalDiagnosisTargetType = "task" | "run" | "cohort";

export interface EvalDiagnosisTarget {
  type: EvalDiagnosisTargetType;
  id?: string;
  filters?: Record<string, string>;
}

export interface EvalDiagnosisStart {
  duplicate: boolean;
  session_id?: string;
  existing_session_id?: string;
  bundle_hash: string;
  diagnosis_id?: string | null;
  run_id?: string;
  bundle_path?: string | null;
}

export interface EvalTaskDetail {
  task_id: string;
  title: string;
  status: TaskStatus;
  harness: string | null;
  model: string | null;
  description: string;
  expects_artifact: boolean | null;
  expects_artifact_source: "explicit" | "harness";
  executed: boolean;
  done: boolean;
  reached_review: boolean;
  rework_count: number;
  rework_reasons: Record<string, number>;
  queue_waits_ms: number[];
  last_error: string | null;
  created_at: number;
  updated_at: number;
  runs: EvalTaskRun[];
  timeline: EvalTimelineEvent[];
  diagnoses: EvalDiagnosisRow[];
  generated_at: number;
}

export interface EvalDigestTool {
  tool: string;
  calls: number;
  errors: number;
  exec_ms: number;
}

export interface EvalDigest {
  available: boolean;
  reason?: string;
  bytes?: number;
  truncated?: boolean;
  events?: number;
  event_counts?: Record<string, number>;
  iterations?: number;
  llm?: {
    requests: number;
    retries: number;
    chunks: number;
    finish_reasons: Record<string, number>;
  };
  context?: { compactions: number; summary_invalid: number };
  tools?: EvalDigestTool[];
  tool_calls?: number;
  tool_errors?: number;
  error_samples?: {
    tool: string;
    error_kind: string | null;
    iteration: number | null;
    edits: number;
  }[];
  config?: {
    model?: string | null;
    harness_id?: string | null;
    tools?: number | null;
    model_params?: Record<string, unknown> | null;
    git?: Record<string, unknown> | null;
  };
  metrics?: { status?: string | null; dropped_events?: number | null };
}

export interface EvalRunDetail {
  run_id: string;
  metrics: EvalRunMetrics;
  board: {
    task_id: string | null;
    title: string | null;
    status: string | null;
    run: Record<string, unknown>;
  } | null;
  digest: EvalDigest;
  trace_path: string | null;
  generated_at: number;
}

export interface EvalBenchmarkRow {
  record_id: string | null;
  created_at: string | null;
  harness_id: string | null;
  genes_hash: string | null;
  model: string | null;
  benchmark: string | null;
  job_dir: string | null;
  accuracy: number | null;
  score: Record<string, unknown>;
  tasks: number;
}

export interface EvalBenchmarks {
  records: EvalBenchmarkRow[];
  latest_accuracy: Record<string, number>;
  generated_at: number;
}
