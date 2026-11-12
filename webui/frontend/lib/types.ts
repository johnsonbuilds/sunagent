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
}

export interface SessionDetail extends SessionSummary {
  segments?: Segment[];
}

export interface RuntimeConfig {
  model?: string;
  harness?: string;
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
