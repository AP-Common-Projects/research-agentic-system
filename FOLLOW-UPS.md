# Follow-ups (deferred until after the 2026-09-01 delivery)

## 1. Validate LLM enum answers against the allowed set

`populate_taxonomy_dimensions` states an allowed set for every dimension in
its prompt, then writes whatever comes back. Nothing checks the answer
against the enum, so invented values reach the workbook: a Crime channel
shipped with `primary_topic = "Crypto"`, and four more with `"Crime"` —
circular as a *crime* family. Caught by eye, not by code.

Fix: reject an out-of-set value and retry that channel once; on a second
miss, write `"Other"` and record the rejected value so the gap is visible
rather than silent. Apply to `primary_topic`, `geography_focus`,
`target_audience`, `content_approach` — every dimension with a stated set.

Related: the Crime `primary_topic` set originally held 8 families and
omitted the largest category in the data, so 29.6% of the deliverable fell
into "Other". Extending it to 13 fixed 63 channels. A validator would not
have caught that one — worth a periodic check of how much lands in "Other",
since a fat "Other" means the taxonomy is wrong, not the classifier.

## 2. Cost/record ledger for direct-client discovery scripts

`broad_discovery.py` calls BrightDataClient without writing a `harness_runs`
row, so per-run spend cannot be read back from the database — only record
counts from the logs. Five discovery passes on 2026-08-31/09-01 pulled
39,208 records with no cost attribution.

## 3. Per-channel LLM nodes are latency-bound and need parallel slices

classify_channel, populate_taxonomy_dimensions, populate_shared_fields and
populate_crime_metadata each make one call per channel/batch and each had
to be parallelised ad hoc during the run. Worth a shared driver rather than
four scripts.

Note: slices must run as separate PROCESSES. `src.db.connection` pools per
thread, so a worker thread can return a connection to a pool that did not
issue it (ValueError, worker dies on round 1, and its disjoint slice is
left for nobody).
