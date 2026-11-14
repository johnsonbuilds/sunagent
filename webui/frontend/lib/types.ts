/** Frame and transcript shapes shared by the client and the runtime. */

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
  /** Empty string means "use the service default". */
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
