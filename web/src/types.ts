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
  created_at: string;
};
