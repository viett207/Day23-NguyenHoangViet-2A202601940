"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def _ready(region: str, timeout: float = 1.0) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        return response.status_code == 200, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _latest_outage(primary: str):
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    latest = None
    for line in events.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("action") == "kill" and record.get("region") == primary:
            latest = record.get("ts")
    return latest


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary va target phai la hai region khac nhau trong a/b")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    started = time.time()

    probes = {region: [] for region in (primary, target)}
    for attempt in range(3):
        for region in (primary, target):
            probes[region].append(_ready(region))
        if attempt < 2:
            time.sleep(5.0)
    primary_failed = all(not ready for ready, _ in probes[primary])
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_failed=primary_failed, probes=probes)
    if not primary_failed:
        return {"ok": False, "failed_step": "1_xac_nhan_outage",
                "reason": f"region-{primary} chua fail lien tiep"}

    outage_ts = _latest_outage(primary)
    incident = step(2, "thong_bao_incident", primary=primary,
                    t_outage=outage_ts,
                    notification_delay_s=(None if outage_ts is None
                                          else round(time.time() - outage_ts, 3)))

    if not confirm(auto, f"Xac nhan failover region-{primary} sang region-{target}?"):
        return {"ok": False, "failed_step": "operator_confirmation",
                "reason": "operator khong xac nhan", "incident_ts": incident["ts"]}

    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, failover_result=result)

    target_state = result.get("target_state", {})
    step(4, "verify_state_replica", target=target,
         ok=result.get("ok", False),
         vector_count=target_state.get("count"),
         weights=target_state.get("weights"),
         rpo_seconds=result.get("rpo_seconds"),
         docs_lost=result.get("docs_lost"),
         embed_model_version=result.get("embed_model_version"))

    cutover_ok = bool(result.get("ok") and result.get("active_region") == target)
    step(5, "dns_cutover", target=target, ok=cutover_ok,
         active_region=result.get("active_region"))
    if not cutover_ok:
        return {"ok": False, "failed_step": result.get("failed_step", "5_dns_cutover"),
                "failover_result": result}

    latencies = []
    errors = 0
    for _ in range(10):
        request_started = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0)
            if response.status_code != 200:
                errors += 1
        except httpx.HTTPError:
            errors += 1
        latencies.append((time.perf_counter() - request_started) * 1000)

    ordered = sorted(latencies)
    p95_ms = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    error_rate = errors / len(latencies)
    step(6, "verify_golden_signals", target=target, requests=len(latencies),
         errors=errors, error_rate=error_rate, p95_latency_ms=round(p95_ms, 2))

    elapsed = round(time.time() - started, 3)
    step(7, "post_incident", ok=errors == 0, elapsed_s=elapsed,
         measure_rto_command=("python tools/measure_rto.py --loadgen "
                              "reports/drill-2-withdr.jsonl --target-rto 300"))
    return {"ok": errors == 0, "target": target, "elapsed_s": elapsed,
            "error_rate": error_rate, "p95_latency_ms": round(p95_ms, 2),
            "failover_result": result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
