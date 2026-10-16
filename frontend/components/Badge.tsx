type Tone = "green" | "red" | "amber" | "blue" | "purple" | "gray";

const TONES: Record<string, Tone> = {
  Completed: "green",
  Cancelled: "red",
  Returned: "red",
  Refunded: "red",
  "Return pending": "amber",
  NONE: "gray",
  RETURN_STARTED: "amber",
  RETURN_COMPLETED: "red",
  RETURN_CANCELLED: "gray",
  REFUNDED: "red",
  MATCHED: "green",
  MANUAL: "blue",
  NEEDS_REVIEW: "amber",
  PENDING: "gray",
  Proven: "green",
  Explore: "purple",
  NEW: "blue",
  RETAINED: "green",
  RETURNING: "purple",
  DROPPED: "gray",
  rising: "green",
  new_activity: "blue",
  falling: "red",
  flat: "gray",
  insufficient_data: "gray",
};

const LABELS: Record<string, string> = {
  NONE: "No return",
  RETURN_STARTED: "Return started",
  RETURN_COMPLETED: "Returned",
  RETURN_CANCELLED: "Return cancelled",
  REFUNDED: "Refunded",
  NEEDS_REVIEW: "Needs review",
  insufficient_data: "too few units",
  new_activity: "new activity",
};

/**
 * Render a compact status badge using configured labels and color tones, with optional overrides.
 *
 * @param value - Raw status or badge text used for default label/tone lookup.
 * @param tone - Optional explicit badge color; unknown statuses otherwise use gray.
 * @param label - Optional display label overriding both configured labels and the raw value.
 * @returns React span containing the resolved label and tone class.
 */
export default function Badge({ value, tone, label }: { value: string; tone?: Tone; label?: string }) {
  return <span className={`badge ${tone ?? TONES[value] ?? "gray"}`}>{label ?? LABELS[value] ?? value}</span>;
}
