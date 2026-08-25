# Postmortem - DR Drill Lab 23

Blameless postmortem cho drill ngay 2026-08-25. Root cause va action item tap
trung vao he thong/process, khong quy trach nhiem ca nhan.

## 1. Timeline

| ISO time | Su kien | Evidence |
|---|---|---|
| 2026-08-25T14:50:58 | Outage Region A bat dau | `chaos/chaos-events.jsonl:8` |
| 2026-08-25T14:50:58 | User dau tien bi anh huong, +0.1s | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T14:51:13 | Health checker danh dau A `UNHEALTHY`, +15.0s | `reports/health-events.jsonl:2` |
| 2026-08-25T14:51:13 | Runbook confirm incident, notification delay 14.816s | `reports/runbook-run.jsonl:2` |
| 2026-08-25T14:51:20 | Target ready va DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-08-25T14:51:24 | Resolved: request dau tien OK tu B, +25.6s | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO va gap analysis

- RTO target: 300s; measured: `25.6s`; gap/headroom: `274.4s` under target.
- RPO target: 300s; measured: `2.0s`, `1` document lost; gap/headroom:
  `298.0s` under target. Evidence: `reports/failover-events.jsonl:2`.
- Longest step: health-check detection `15.04s`, about `58.8%` of RTO, caused
  by interval 5 seconds and threshold 3.
- GPU warm-up used `7.01s` (`27.4%`); DNS TTL/scheduling used `3.40s`;
  verify/restore/scale used about `0.10s`.

## 3. Root cause - 5 whys

1. Users saw errors because edge still routed to Region A after A stopped.
2. Edge did not switch immediately because cutover waits for confirmed outage and
   target readiness, preventing flapping and double outage.
3. Confirmation took about 15 seconds because policy requires three consecutive
   failed probes at a five-second interval.
4. Region B was not ready because active-passive mode keeps B warm without runtime
   weights/vector state; state is restored during the incident.
5. In a real outage, snapshot restore is the most fragile step: a missing/stale
   snapshot or incompatible model version prevents `/readyz` from reaching 200.
   The readiness guard prevents an unsafe cutover but recovery becomes NO_RECOVERY.

System root cause: the interruption duration comes from the active-passive design,
the 15-second anti-flap window, and required pool warm-up, not an individual action.

## 4. Action items

| # | Action item | Owner | Deadline | Expected impact |
|---|---|---|---|---|
| 1 | Test interval 2s / threshold 3; add jitter and circuit breaker | SRE on-call | 2026-09-01 | Detection floor 15s to 6s; reduce RTO about 9s |
| 2 | Keep one B worker pre-warmed; validate model version after replication | Compute/ML Platform | 2026-09-08 | Warm-up 7.01s to near 0s; reduce compatibility risk |
| 3 | Alert when snapshot lag exceeds 30s or projected docs_lost exceeds SLO | Data Platform | 2026-09-08 | Keep RPO below 30s; no direct RTO reduction |

## 5. Required questions

1. `interval x threshold = 5s x 3 = 15s`. Actual detection was `15.04s`, or
   about `58.8%` of the measured `25.6s` RTO.
2. With interval 1s and threshold 3, floor falls from 15s to 3s. Other things
   equal, RTO may fall about 12s to 13.6s. Cost: five times more probes and more
   sensitivity to transient failure; jitter, hysteresis, and circuit breaker are needed.
3. For a six-hour outage with permanent primary loss, `docs_lost` means customer
   writes acknowledged by primary but absent from the restored snapshot. The drill
   lost 1 document; production must reconcile/replay those writes and report scope.
