# RTO/RPO Evidence - Lab 23

Tat ca so lieu duoi day lay tu drill ngay 2026-08-25 va tro den dong log thuc te.
Muc tieu RTO/RPO la 300 giay.

## 1. Drill 1 - khong co DR

| Chi so | Gia tri | Cach do | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T14:07:09` | Chaos kill Region A | `chaos/chaos-events.jsonl:4` |
| Request fail dau tien | `+1.8s` | Dong `ok:false` dau sau outage | `reports/drill-1-nodr.jsonl:21` |
| Request thanh cong sau do | Khong co | Tu dong 21 den het deu loi | `reports/drill-1-nodr.jsonl:34` |
| RTO | `NO_RECOVERY` | Khong co request thanh cong sau loi | `reports/drill-1-nodr.jsonl:34` |

## 2. Drill 2 - co DR

| Moc | +giay tu t_outage | Cach do | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | `action:kill`, Region A | `chaos/chaos-events.jsonl:8` |
| User thay loi dau tien | 0.1s | Dong `ok:false` dau tien | `reports/drill-2-withdr.jsonl:25` |
| Health check phat hien | 15.0s | `to:UNHEALTHY`, Region A | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 15.1s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phu ready | 22.2s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 22.2s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO do duoc** | **25.6s** | Request `ok:true` dau tien do Region B phuc vu | `reports/drill-2-withdr.jsonl:36` |

| Chi so | Do duoc | Muc tieu | Verdict |
|---|---:|---:|---|
| RTO - Inference API | `25.6s` | 300s | PASS - thap hon muc tieu 274.4s |
| RPO - Vector DB | `2.0s` / `1` document | 300s | PASS - thap hon muc tieu 298.0s |

RPO va model compatibility nam tai `reports/failover-events.jsonl:2`:
`rpo_seconds=2.0`, `docs_lost=1`, model `embed-model=vi-e5-base@v3`.

## 3. Thanh phan RTO

| Thanh phan | Giay | Nguon | Cach giam |
|---|---:|---|---|
| Health-check detection | 15.04s | `interval_s=5.0 x threshold=3` tai `reports/health-events.jsonl:2` | Giam interval/threshold; doi lai tang probe va flapping risk |
| Verify + restore + scale | 0.10s | Health detect den scale; `reports/failover-events.jsonl:2` va `reports/failover-events.jsonl:3` | Snapshot nho hon, restore incremental |
| GPU pool warm-up | 7.01s | `3_scale_pool` den `4_wait_ready`, `reports/failover-events.jsonl:3` va `reports/failover-events.jsonl:4` | Duy tri warm capacity |
| DNS/LB TTL + scheduling | 3.40s | Cutover `reports/failover-events.jsonl:5` den request B `reports/drill-2-withdr.jsonl:36` | Giam TTL, active health routing |
| **Tong** | **25.6s** | Timestamp outage den request B dau tien | Dat muc tieu 300s |

Sai so cong cac thanh phan duoi 0.1 giay do lam tron. `tools/measure_rto.py` tinh
tren timestamp tho va cho RTO chinh thuc `25.6s`.
