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

export default function Badge({ value, tone, label }: { value: string; tone?: Tone; label?: string }) {
  return <span className={`badge ${tone ?? TONES[value] ?? "gray"}`}>{label ?? LABELS[value] ?? value}</span>;
}
