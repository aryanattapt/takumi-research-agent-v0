export type Provider = {
  id: string;
  name: string;
  provider_type: "openai_compatible" | "anthropic";
  base_url: string;
  model: string;
  secret_configured: boolean;
  timeout_seconds: number;
  max_output_tokens: number;
  capability_result: Record<string, unknown>;
  revision: number;
  active: boolean;
};

export type Blog = {
  id: string;
  name: string;
  base_url: string;
  api_spec_source: string;
  auth_header: string;
  auth_scheme: string;
  secret_configured: boolean;
  provider_id: string | null;
  adapter_mapping: Record<string, unknown>;
  capabilities: Record<string, any>;
  capability_result: Record<string, any>;
  topic_settings: Record<string, boolean>;
  editorial_settings: Record<string, unknown>;
  schedule_enabled: boolean;
  schedule_timezone: string;
  publish_time: string;
  lead_minutes: number;
  revision: number;
  destination_epoch: number;
  active: boolean;
};

export type Run = {
  id: string;
  blog_id: string;
  editorial_date: string;
  target_at: string;
  status: string;
  stage: string;
  editorial_decision: string;
  pair_completeness: string;
  deadline_result: string;
  execution_mode?: string;
  capacity_state?: "ok" | "at_risk" | "infeasible";
  start_by_at?: string | null;
  schedule_version?: number;
  created_at: string;
};

export type RunArticle = {
  locale: "id" | "en";
  title: string;
  summary: string;
  body: string;
  body_hash: string;
  cited_evidence_ids: string[];
};

export type RunSource = {
  source_id: string;
  title: string;
  url: string;
  source_type: string;
  content_hash: string | null;
  read_status: string;
  peer_reviewed: boolean | null;
  peer_review_basis: string;
  retracted: boolean;
  available: boolean;
};

export type RunEvidence = {
  evidence_id: string;
  source_id: string;
  content_hash: string;
  quote: string | null;
  locator: string | null;
  available: boolean;
};

export type RunClaim = {
  claim: string;
  evidence_ids: string[];
  importance: "critical" | "material" | "context";
  support: "supported" | "partial" | "contradicted" | "unknown";
  qualification?: string | null;
};

export type AgentTraceEvent = {
  type: "model_response" | "tool" | string;
  step?: number;
  stop_reason?: string;
  tokens_in?: number | null;
  tokens_out?: number | null;
  name?: string;
  status?: string;
  reason?: string;
};

export type RunAgentMetrics = {
  physical_requests?: number;
  research_physical_requests?: number;
  editorial_physical_requests?: number;
  tool_actions?: number;
  trace?: AgentTraceEvent[];
};

export type DiscoveryFeedCoverage = {
  name: string;
  status: "ok" | "not_modified" | "failed" | string;
  items: number;
  error?: string;
};

export type DiscoverySourceCoverage = {
  configured: number;
  successful: number;
  failed: number;
  hits?: number;
  items?: number;
  feeds?: DiscoveryFeedCoverage[];
};

export type DiscoveryCoverage = {
  window_start: string;
  baseline_start: string;
  observed_at: string;
  candidate_count: number;
  sources: Record<string, DiscoverySourceCoverage>;
};

export type RunDetail = {
  id: string;
  blog_id: string;
  status: string;
  stage: string;
  execution_mode: string;
  pipeline_outcome: string;
  editorial_decision: string;
  pair_completeness: string;
  deadline_result: string;
  capacity_state: string;
  start_by_at: string | null;
  schedule_version: number;
  discovery_coverage: DiscoveryCoverage | null;
  deliveries: Array<Record<string, any>>;
  pair: {
    id: string;
    topic: string;
    field: string;
    research_question: string;
    decision: string;
    review: Record<string, any>;
    agent: RunAgentMetrics;
    sources: RunSource[];
    evidence: RunEvidence[];
    claims: RunClaim[];
    articles: RunArticle[];
  } | null;
};

export type CapacitySlot = {
  run_id: string;
  blog_id: string;
  target_at: string;
  start_by_at: string;
  estimated_start_at: string;
  estimated_finish_at: string;
  state: "ok" | "at_risk" | "infeasible";
  reason: string;
};

export type CapacityReport = {
  now: string;
  concurrency: number;
  run_minutes: number;
  pending: number;
  counts: { ok: number; at_risk: number; infeasible: number };
  at_risk_run_ids: string[];
  infeasible_run_ids: string[];
  slots: CapacitySlot[];
};

export type ArtifactIntegrity = {
  checked: boolean;
  total: number;
  verified: number | null;
  missing: string[];
  corrupt: string[];
  truncated: boolean;
};

export type RestoreReport = {
  backup_id: string;
  created_at?: string;
  restored_at?: string;
  schema_revision?: { backup: string | null; current: string | null };
  tables?: Record<string, number>;
  artifacts?: ArtifactIntegrity;
  master_key_present?: boolean;
  blocked_runs?: string[];
  unknown_intents?: string[];
  profiles_requiring_revalidation?: string[];
  providers_requiring_recredential?: string[];
  blogs_requiring_recredential?: string[];
  schedule_cancel_or_resync_review?: string[];
  recovery_mode?: boolean;
};

export type RecoveryStatus = {
  recovery_mode: boolean;
  content_mutations_paused: boolean;
  artifacts: ArtifactIntegrity;
  last_restore: RestoreReport | null;
};
