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
