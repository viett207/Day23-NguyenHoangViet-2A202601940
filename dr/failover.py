"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    with LOG.open("a") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record))
    return record


def state_of(region: str) -> dict:
    """Đọc trạng thái hiện tại của một region trước khi restore/cutover."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL:
        raise ValueError(f"target khong hop le: {target}")
    if wait < 0:
        raise ValueError("wait phai >= 0")

    primary = "b" if target == "a" else "a"

    try:
        target_state = state_of(target)
        emit(step="1_verify_target", target=target, ok=True, state=target_state)
    except httpx.HTTPError as exc:
        emit(step="1_verify_target", target=target, ok=False,
             error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "target": target, "failed_step": "1_verify_target"}

    try:
        restored = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        emit(
            step="2_restore_snapshot",
            target=target,
            ok=True,
            backend=backend,
            rpo_seconds=rpo["rpo_seconds"],
            docs_lost=rpo["docs_lost"],
            embed_model_version=restored.get("embed_model_version"),
            snapshot_at=restored.get("snapshot_at"),
        )
    except (OSError, ValueError, SystemExit) as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             backend=backend, error=str(exc))
        return {"ok": False, "target": target, "failed_step": "2_restore_snapshot"}

    pool_state = pathlib.Path(f"state/region-{target}/pool_state")
    pool_state.parent.mkdir(parents=True, exist_ok=True)
    pool_state.write_text("full")
    emit(step="3_scale_pool", target=target, ok=True, pool_state="full")

    deadline = time.monotonic() + wait
    last_reason = "wait timeout"
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if response.status_code == 200:
                emit(step="4_wait_ready", target=target, ok=True)
                break
            last_reason = f"readyz_{response.status_code}: {response.text}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        if time.monotonic() >= deadline:
            emit(step="4_wait_ready", target=target, ok=False, reason=last_reason)
            return {
                "ok": False,
                "target": target,
                "failed_step": "4_wait_ready",
                "reason": last_reason,
            }
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    active_region = pathlib.Path("edge/active_region")
    active_region.parent.mkdir(parents=True, exist_ok=True)
    active_region.write_text(target)
    emit(step="5_dns_cutover", target=target, ok=True, active_region=target)
    target_state = state_of(target)
    return {
        "ok": True,
        "target": target,
        "active_region": target,
        "target_state": target_state,
        "rpo_seconds": rpo["rpo_seconds"],
        "docs_lost": rpo["docs_lost"],
        "embed_model_version": restored.get("embed_model_version"),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
