# API failure corrections, 2026-09-28

The actual user route is `/api/detection/submit`, running
`api/services/detection_reports_worker.py`. Fixing `api/worker.py` alone did
not fix this route.

## Causes and changes

- Review accepted HIGH conclusions with coverage gaps, but PDF publication
  rejected the same gaps. Both stages now use `review_validation.validate`,
  including the renderer's consistency and structured explanation checks.
  HIGH and LOW conclusions both enter coverage repair when incomplete.
- The report shim synthesized acknowledgements and waived unrelated findings
  on a generic pooled-data explanation. Those bypasses were removed. The
  project owns `modules/review_coverage.py`; it recognizes Figure/Fig aliases
  and the channel/series detector types without altering review prose.
- PDF existence alone could change a failed task to completed at startup.
  That reconciliation was removed. A current task must generate its report,
  verify its public PDF URL, write its own database row, and verify the write.
- Concurrent submissions of the same upload created competing workers.
  Submission reservation now runs under the capacity lock; repeated submits
  return the existing active task. Recovery suppresses older duplicate tasks.
- Initial writes now lock their logical database key and reject older owners;
  final writes and readback are fenced by task_id. Same-task initial replay
  preserves an already-written final result.
- Transient PDF failures get one rendering-only retry in the shared report
  service. Semantic failures are not retried with identical content. Batch
  user submissions render each paper independently.
- Recovery reuses completed detection and same-input reviewer checkpoints.
  It revalidates cached results and never borrows a final verdict by DOI alone.
  An interrupted, unsaved LLM round must still run again. A new explicit
  submission is intentionally a fresh task.
- Extraction runs outside the event loop. Waiting tasks are marked queued.
  Explicit None clears stale errors/results. English report failures are not
  reported as complete.

## Deployment and verification

Deployed with one controlled API reload at 08:32–08:33 CST. New PID: 414610.
Runtime dependency checks passed. Real duplicate submission returned the
existing task 20260928-1c9b30e8. Three active initial-report URLs returned PDF
content successfully. Logs confirm completed detection was reused for
20260928-4c8b40b1.

The old process failed 20260928-f1513700 just before reload. It was explicitly
resubmitted as 20260928-9e9cac18. Older duplicate 20260928-cce5b7d9 is superseded
by 20260928-1c9b30e8. Historical failed task IDs remain failed rather than
being rewritten as completed.

Targeted regression suite: test_api_recovery_regressions,
test_detection_reports_state, test_report_coverage_policy,
test_review_multidimension, test_report_generation, test_runtime_tools,
test_review_prompt_templates, test_report_explainability, test_english_reports.
It covers the actual user worker, stale-writer fencing, replay, transient
retry, invalid scientific coverage, and consistent review/publication gates.

At verification time, seven current independent submissions are still in
progress/queued. Full successful PDF + database completion of this round has
not yet been verified. These changes do not guarantee external model/network
availability or that incomplete evidence can always be resolved automatically.
