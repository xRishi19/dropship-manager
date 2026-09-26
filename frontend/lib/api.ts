// Typed client for the FastAPI backend (proxied at /api by next.config.ts).

// A preset key from RANGES, or "YYYY-MM" for one whole calendar month (daily buckets).
export type RangeKey = string;

export const RANGES: { key: RangeKey; label: string }[] = [
  { key: "1d", label: "1 Day" },
  { key: "1w", label: "1 Week" },
  { key: "30d", label: "30 Days" },
  { key: "month", label: "Current Month" },
  { key: "3m", label: "3 Months" },
  { key: "12m", label: "12 Months" },
  { key: "ytd", label: "YTD" },
];

export type Cards = {
  total_orders: number;
  net_revenue_cents: number;
  amazon_cost_cents: number;
  profit_cents: number; // Includes the ordering-provider fee (excluded orders count −fee).
  ordering_fees_cents: number; // Ordering-provider fees counted in profit_cents.
  expenses_cents: number;
  profit_after_expenses_cents: number;
  prime_cashback_cents: number;
  prime_cashback_rate: number;
  avg_profit_per_order_cents: number | null;
  profit_margin: number | null;
  awaiting_cost: number;
  needs_review: number;
  revenue_estimated: number;
  return_pending: number;
  excluded: { cancelled: number; returned: number; refunded: number };
};

export type SeriesPoint = {
  bucket: string;
  label: string;
  month?: string; // "YYYY-MM", only on monthly buckets (12 Months / YTD).
  revenue_cents: number;
  cost_cents: number;
  profit_cents: number;
  orders: number;
  awaiting_cost: number;
};

export type Overview = { range: RangeKey; granularity: string; timezone: string; cards: Cards; series: SeriesPoint[] };

export type OrderSummary = {
  order_id: string;
  created_at: string;
  product_title: string;
  quantity: number;
  display_status: string;
  return_status: string;
  match_status: string;
  match_confidence: number | null;
  flags: string[];
  excluded: boolean;
  cost_known: boolean;
  revenue_estimated: boolean;
  net_revenue_cents: number | null;
  amazon_cost_cents: number | null;
  ordering_fee_cents: number; // Ordering-provider fee, charged on every order (cancelled/refunded too).
  original_profit_cents: number | null;
  effective_profit_cents: number | null;
  margin: number | null;
  roi: number | null;
  category: string | null;
  asin: string | null;
};

export type Email = {
  gmail_message_id: string;
  received_at: string;
  subject: string;
  first_name: string | null;
  city: string | null;
  state: string | null;
  quantity: number | null;
  grand_total_cents: number | null;
  parse_status: string;
  parse_error: string | null;
  attached_to: string | null;
};

export type Candidate = {
  gmail_message_id: string;
  score: number;
  minutes_after: number;
  signals: Record<string, { points: number; detail: string }>;
  email?: Email;
};

export type OrderItem = {
  line_item_id: string;
  item_id: string;
  ebay_item_url: string;
  title: string;
  quantity: number;
  sku: string | null;
  asin: string | null;
  category: string | null;
  line_total_cents: number | null;
};

export type OrderDetail = OrderSummary & {
  ebay_order_url: string;
  fulfillment_status: string | null;
  payment_status: string | null;
  items: OrderItem[];
  ship_name: string | null;
  address1: string | null;
  address2: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  country: string | null;
  gross_total_cents: number | null;
  sales_tax_cents: number | null;
  final_value_fee_cents: number | null;
  other_fees_cents: number | null;
  other_fees: Record<string, number>;
  ad_fee_cents: number | null;
  refund_cents: number | null;
  revenue_source: string | null;
  manual_cost_cents: number | null;
  ebay_return_status: string | null;
  manual_return_status: string | null;
  effective_revenue_cents: number | null;
  effective_cost_cents: number | null;
  candidates: Candidate[];
  matched_email: Email | null;
};

export type WeeklyRow = {
  day: string;
  tier: "Proven" | "Explore";
  market: string | null;
  keyword: string;
  rank: number;
  confidence: number | null;
  total_units_sold: number;
  recent_units_sold: number;
  distinct_products: number;
  trend: string;
  reason: string;
  status: string;
  stats: Record<string, unknown> & { examples?: { title: string; units: number }[] };
};

export type Week = {
  week: string | null;
  as_of?: string;
  model?: string;
  selection_method?: string;
  asins?: { Proven: number; Explore: number };
  resting?: string[];
  dropped?: string[];
  results: WeeklyRow[];
  weeks?: string[];
  current_week?: string;
};

export type KeywordCandidate = {
  keyword: string;
  tier: string;
  score: number;
  explore_score: number;
  total_units_sold: number;
  recent_units_sold: number;
  prior_units_sold: number;
  distinct_products: number;
  successful_products: number;
  distinct_listings: number;
  sell_through_lift: number;
  largest_product_share: number;
  outside_proven_share: number | null;
  trend: string;
  evidence: string;
};

export type HistoryEntry = {
  week: string;
  day?: string;
  tier?: string;
  keyword: string;
  rank?: number;
  status: string;
  total_units_sold?: number;
  trend?: string;
  cooldown?: "picked this week" | "resting" | "available" | "explored again later" | null;
  available_from?: string | null;
};

export type Outcome = {
  week: string;
  keywords: { keyword: string; tier: string | null; new_listings?: number; new_listings_sold?: number; units_sold?: number; outcome?: string }[];
};

export type Job = {
  id: string;
  status: "running" | "succeeded" | "failed";
  step: string;
  started_at: string;
  error: string | null;
  result: Record<string, unknown> | null;
};

export type SyncStatus = {
  loop: { running: boolean; interval_seconds: number; last_started: string | null; last_finished: string | null; current_step: string | null };
  steps: Record<string, { last_success_at: string | null; last_attempt_at: string | null; last_error: string | null; cursor?: string | null; details: Record<string, unknown> | null }>;
  gmail_connected: boolean;
  accounting_start: string;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function money(cents: number | null | undefined): string {
  return cents === null || cents === undefined ? "—" : usd.format(cents / 100);
}

export function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export type Expense = {
  id: number;
  name: string;
  amount_cents: number;
  start_date: string; // YYYY-MM-DD
  recurring: boolean;
  end_date: string | null;
  next_occurrence: string | null;
};

// Ordering-provider fees on every eBay order (cancelled included); informational, already in order profit.
export type OrderingFees = {
  per_order_cents: number;
  this_month_count: number;
  this_month_cents: number;
  ytd_count: number;
  ytd_cents: number;
  all_count: number;
  all_cents: number;
};

export type ExpensesResponse = {
  expenses: Expense[];
  this_month_cents: number;
  monthly_recurring_cents: number;
  ordering_fees: OrderingFees;
};

export type ExpenseInput = { name: string; amount: string; date: string; recurring: boolean; end_date: string | null };
