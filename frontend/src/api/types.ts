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
  /** Расход по отчётам площадок, как в статистике; spent_kop может быть больше — в нём свежий расход за сегодня. */
  reported_kop: number;
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
/** Статус последней передачи данных рекламодателя в площадку; null — передачи ещё не было или кабинет не подключён. */
export type MarkingTransmissionStatus = 'marking_transmitted' | 'marking_manual_required';
export interface MarkingTransmission { direct: MarkingTransmissionStatus | null; vk: MarkingTransmissionStatus | null }
/** Маркировка: что владелец подтвердил галочкой и что площадки приняли сами. */
export interface MarkingStatus { confirmed: { direct: boolean; vk: boolean }; transmission: MarkingTransmission }
// Proposed contracts for endpoints that are not available yet.
export interface AutopilotStatus {
  /** no_business — мастер ещё не пройден, бизнеса нет. */
  status: 'running' | 'paused' | 'budget_stop' | 'draft' | 'no_business';
  last_check_at: string | null;
  auto_continue: boolean;
  needs_attention: { id: string; title: string; description: string; destination: 'clients' | 'ads' | 'settings' }[];
  /** План собран или уже запущен: до запуска экраны ведут в план, без плана — в мастер. */
  has_plan: boolean;
  /** Когда автопилот примет первые решения по цене — пока срок не прошёл; только у работающей рекламы. */
  first_decisions?: string | null;
  /** Маркировка рекламодателя; null — данные ещё не сохраняли. */
  marking?: MarkingStatus | null;
  /** Реклама работает, но «Продолжить» включил не все кампании: кнопка нужна и у работающей рекламы. */
  resume_pending?: boolean;
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
export interface CampaignDay { date: string; cost_kop: number; leads: number; is_incomplete: boolean }
/** Кампании действующего плана; daily — последние 14 дней до сегодня по дате бизнеса. */
export interface Campaign { id: number; platform: string; name: string; status: string; daily: CampaignDay[] }
export interface AdsResponse { items: Ad[]; campaigns: Campaign[] }
export interface Experiment {
  id: number;
  title: string;
  /** Что меняется между вариантами; список вариантов — в arms. */
  hypothesis: 'text' | 'image' | 'audience';
  comparing: string;
  /** Варианты А и Б; image — ссылка на картинку варианта. */
  /** audience — какой аудитории показывают вариант; только у проверки аудиторий. */
  arms: { title: string; text: string; image: string | null; audience?: string | null }[];
  remaining_impressions: number;
  /** За какие дни цифры в выводе: у завершённой — на момент вывода; null — устоявшихся дней ещё нет. */
  period: { from: string; to: string } | null;
  status: 'running' | 'concluded' | 'cancelled';
  conclusion: string | null;
}
export interface ExperimentsResponse { items: Experiment[] }
