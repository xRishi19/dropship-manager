"use client";

import { useCallback, useEffect, useState } from "react";
import CandidateExplorer from "@/components/sourcing/CandidateExplorer";
import History from "@/components/sourcing/History";
import WeeklyPicks from "@/components/sourcing/WeeklyPicks";
import { api, HistoryEntry, Job, KeywordCandidate, Outcome, Week } from "@/lib/api";

type Tab = "picks" | "candidates" | "history";
type HistoryResponse = { entries: HistoryEntry[]; outcomes: Outcome[]; resting_now: string[]; cooldown_weeks: number };

export default function SourcingPage() {
  const [tab, setTab] = useState<Tab>("picks");
  const [week, setWeek] = useState<Week | null>(null);
  const [candidates, setCandidates] = useState<KeywordCandidate[] | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [refreshListings, setRefreshListings] = useState(true);
  const [job, setJob] = useState<Job | null>(null);

  const fail = (e: Error) => setError(e.message);
  const loadWeek = useCallback(() => api<Week>("/sourcing/week").then(setWeek).catch(fail), []);
  useEffect(() => { loadWeek(); }, [loadWeek]);
  useEffect(() => {
    // Resume showing a weekly run started before navigating away.
    api<Job | null>("/jobs/latest?kind=weekly").then((latest) => { if (latest?.status === "running") setJob(latest); }).catch(() => {});
  }, []);
  useEffect(() => {
    if (tab === "candidates" && !candidates) {
      api<{ candidates: KeywordCandidate[] }>("/sourcing/candidates").then((r) => setCandidates(r.candidates)).catch(fail);
    }
    if (tab === "history" && !history) api<HistoryResponse>("/sourcing/history").then(setHistory).catch(fail);
  }, [tab, candidates, history]);

  useEffect(() => {
    if (!job || job.status !== "running") return;
    const timer = setInterval(() => {
      api<Job>(`/jobs/${job.id}`).then((next) => {
        setJob(next);
        if (next.status === "succeeded") {
          loadWeek();
          setCandidates(null);
          setHistory(null);
        }
      }).catch((e: Error) => {
        // e.g. the backend restarted and forgot the job: stop polling instead of spinning forever.
        setJob({ ...job, status: "failed", error: `Lost track of the run (${e.message}); check the Weekly Picks tab.` });
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [job, loadWeek]);

  const run = () => {
    setConfirming(false);
    setError(null);
    api<Job>("/sourcing/weekly/run", { method: "POST", body: JSON.stringify({ confirm: true, refresh_listings: refreshListings }) })
      .then(setJob).catch(fail);
  };

  const running = job?.status === "running";
  return (
    <>
      <div className="page-head">
        <h1>Sourcing</h1>
        <span style={{ flex: 1 }} />
        {job && (
          <span className={job.status === "failed" ? "neg small" : "muted small"}>
            {running ? `Running: ${job.step}…` : job.status === "succeeded" ? "Weekly analysis finished." : `Failed: ${job.error}`}
          </span>
        )}
        <button className="btn primary" disabled={running} onClick={() => setConfirming(true)}>Run Weekly Analysis</button>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="tabs">
        {(["picks", "candidates", "history"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {{ picks: "Weekly Picks", candidates: "Candidate Explorer", history: "History" }[t]}
          </button>
        ))}
      </div>

      {tab === "picks" && (week ? <WeeklyPicks week={week} /> : <div className="empty">Loading…</div>)}
      {tab === "candidates" && (candidates ? <CandidateExplorer candidates={candidates} /> :
        <div className="empty">Analyzing the last 90 days of sales… (no OpenAI calls)</div>)}
      {tab === "history" && (history ? (
        <History entries={history.entries} outcomes={history.outcomes} restingNow={history.resting_now}
          cooldown={history.cooldown_weeks} />
      ) : <div className="empty">Loading…</div>)}

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Run weekly analysis?</h2>
            <p className="muted">
              This calls the <b>OpenAI API</b> (paid) to choose this week&apos;s 7 Proven + 7 Explore keywords from the
              Python-generated candidates, and replaces this week&apos;s saved picks.
            </p>
            <label className="muted small" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={refreshListings} onChange={(e) => setRefreshListings(e.target.checked)} />
              Refresh eBay listings first (a couple of minutes; recommended if not refreshed today)
            </label>
            <div className="row-actions" style={{ justifyContent: "flex-end" }}>
              <button className="btn" onClick={() => setConfirming(false)}>Cancel</button>
              <button className="btn primary" onClick={run}>Run analysis</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
