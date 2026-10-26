// Dates and timestamps are ISO strings; all monetary values are integer kopecks.
export interface Period { from: string; to: string }
export type StatsLevel = 'platform' | 'campaign' | 'group' | 'ad';
export interface Source { platform: string; fetched_at: string }
export interface Metrics {
  cost_kop: number;
  impressions: number;
  clicks: number;
  leads: number;
  ctr: number | null;
  cost_per_click_kop: number | null;
  cost_per_lead_kop: number | null;
  clients: number | null;
  cost_per_client_kop: number | null;
  is_incomplete: boolean;
  incomplete_days: string[];
  sources: Source[];
}
export interface StatsPeriod extends Period {
  business_id: number;
  incomplete_days: string[];
}
export interface ClientsWindow extends Period {
  clients: number | null;
  cost_kop: number;
  cost_per_client_kop: number | null;
  answered_weeks: number;
  unknown_weeks: number;
}
export interface Budget {
  month: string;
  budget_kop: number;
  spent_kop: number;
  remaining_kop: number;
  days_remaining: number;
  platforms: { platform: string; spent_kop: number; source: 'budget_ledger' | 'stats_daily'; spent_at: string | null }[];
}
export interface Summary extends StatsPeriod, Metrics { clients_window: ClientsWindow; budget: Budget }
export interface Breakdown extends StatsPeriod {
  level: StatsLevel;
  is_incomplete: boolean;
  items: (Metrics & { id: number | string | null })[];
}
export interface Daily extends StatsPeriod {
  is_incomplete: boolean;
  items: (Metrics & { date: string })[];
}
// Proposed contracts for endpoints that are not available yet.
export interface AutopilotStatus {
  status: 'running' | 'paused' | 'budget_stop' | 'draft';
  last_check_at: string | null;
  auto_continue: boolean;
  needs_attention: { id: string; title: string; description: string; destination: 'clients' | 'ads' | 'settings' }[];
}
export type AdStatus = 'draft' | 'moderation' | 'active' | 'paused_auto' | 'paused_owner' | 'rejected' | 'archived';
export interface Ad {
  id: number;
  platform: string;
  campaign_id: number;
  campaign_name: string;
  group_id: number;
  group_name: string;
  title: string;
  text: string;
  image_url: string | null;
  status: AdStatus;
  reason: string | null;
  metrics: Metrics;
}
export interface AdsResponse { items: Ad[] }
export interface Experiment {
  id: number;
  title: string;
  comparing: string;
  remaining_impressions: number;
  status: 'running' | 'concluded' | 'cancelled';
  conclusion: string | null;
}
export interface ExperimentsResponse { items: Experiment[] }
export interface HistoryItem {
  id: number;
  created_at: string;
  actor: string;
  reason_text: string;
  status: string;
}
export interface HistoryResponse { items: HistoryItem[]; total: number; page: number; page_size: number }
