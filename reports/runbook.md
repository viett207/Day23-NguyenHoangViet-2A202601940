# Runbook — Region chính down

Phạm vi: bare mode trên máy lab, primary `a`, target `b`, snapshot backend `fs`.
Chạy các lệnh từ thư mục gốc repository trong Git Bash. Không sửa trực tiếp
`edge/active_region`; mọi cutover phải đi qua `dr/runbook.py`/`dr/failover.py`.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` (chạy 3 lần, cách nhau 5 giây) | Cả 3 lần đều cho Region A `alive=false` hoặc `ready=false`; Region B vẫn `alive=true`. Không tiếp tục nếu cả hai region đều down. | Primary on-call |
| 2 | Mở incident và bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` rồi nhập `y` khi được hỏi | `reports/runbook-run.jsonl` có dòng `thong_bao_incident`, gồm `t_outage` và `notification_delay_s`. | Incident Commander |
| 3 | Restore state ở Region B | `curl.exe -s localhost:8002/v1/state` | `reports/failover-events.jsonl` có bước `2_restore_snapshot` với `ok=true`, `rpo_seconds`, `docs_lost`, `embed_model_version`; API state cho `weights=true` và `count>0`. | DR operator |
| 4 | Scale pool `warm` → `full` | `curl.exe -i localhost:8002/readyz` | HTTP `200`, `ready=true`, `pool_state=full`; failover log có `3_scale_pool` rồi `4_wait_ready` theo đúng thứ tự. | Compute on-call |
| 5 | DNS/LB cutover | `curl.exe -s localhost:8080/edge/state` | `active_region` là `b`, và failover log có `5_dns_cutover` sau `4_wait_ready`. | Incident Commander |
| 6 | Verify golden signals | `curl.exe -s localhost:8080/v1/infer` | Response có `edge_region=b`, `upstream_status=200`; dòng `verify_golden_signals` trong `reports/runbook-run.jsonl` ghi 10 requests, error rate `0.0` và p95 dưới `1000 ms`. | Service owner |
| 7 | Đo RTO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Kết quả có `valid=true`, `rto_verdict=PASS`, `recovered_by_region=b`; chép các timestamp thật vào `reports/rto-evidence.md` và gap analysis vào `reports/postmortem.md`. | Incident Commander + SRE |

## Rollback / failback về Region A

Chỉ rollback khi Region B có error rate lớn hơn 1%, p95 lớn hơn 1000 ms trong hai
cửa sổ kiểm tra liên tiếp, hoặc `/readyz` của B thất bại; đồng thời Region A phải
trả HTTP 200 từ `/readyz` ba lần liên tiếp. Nếu A chưa chạy, khởi động lại bare stack
và xác minh readiness trước. Không rollback chỉ vì một probe lỗi đơn lẻ.

Incident Commander là người duy nhất có quyền phê duyệt failback. Sau khi được phê
duyệt, DR operator chạy:

```bash
python dr/failover.py --target a --backend fs
curl.exe -s localhost:8080/edge/state
curl.exe -s localhost:8080/v1/infer
```

Thành công khi edge báo `active_region=a`, inference được Region A phục vụ và golden
signals trở lại trong ngưỡng. Nếu Region A không ready, `failover.py` phải abort trước
bước `5_dns_cutover`; giữ traffic ở B và tiếp tục incident.
