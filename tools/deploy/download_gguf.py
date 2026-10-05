"""Resume a pinned GGUF download in validated HTTP ranges, then verify SHA-256."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import threading
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--transport-url", help="Optional mirror of the exact same bytes; pinned SHA-256 is still required")
    parser.add_argument("--no-proxy", action="store_true")
    args = parser.parse_args()
    target = args.output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    state_path = target.with_suffix(target.suffix + ".ranges.json")
    lock = threading.Lock()
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state["url"] != args.url or state["size"] != args.size or state["sha256"] != args.sha256:
            raise ValueError("Download metadata mismatch")
    else:
        # A pre-existing sequential curl download is a contiguous prefix.
        prefix = target.stat().st_size if target.exists() else 0
        if prefix > args.size:
            raise ValueError("Existing file exceeds expected size")
        state = {"url": args.url, "size": args.size, "sha256": args.sha256,
                 "prefix": prefix, "completed": []}
    def save():
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        for retry in range(20):
            try:
                tmp.replace(state_path)
                break
            except PermissionError:
                if retry == 19:
                    raise
                time.sleep(0.05)
    save()
    with target.open("ab") as f:
        f.truncate(args.size)
    chunk = 128 * 1024 * 1024
    ranges = [(s, min(s + chunk, args.size) - 1) for s in range(state["prefix"], args.size, chunk)]
    done = set(state["completed"])
    partial = state.setdefault("partial", {})
    started = time.monotonic()
    initial = state["prefix"] + sum(e - s + 1 for s, e in ranges if s in done)
    progress = {int(k): v for k, v in partial.items() if int(k) not in done}
    initial_partial = sum(progress.values())
    def download(bounds):
        start, end = bounds
        if start in done:
            return
        for attempt in range(8):
            try:
                count = partial.get(str(start), 0)
                request_start = start + count
                checkpoint = count
                with httpx.Client(follow_redirects=True, trust_env=not args.no_proxy,
                                  timeout=httpx.Timeout(60, connect=30)) as client:
                    with client.stream("GET", args.transport_url or args.url, headers={"Range": f"bytes={request_start}-{end}"}) as response:
                        response.raise_for_status()
                        expected = f"bytes {request_start}-{end}/{args.size}"
                        if response.status_code != 206 or response.headers.get("content-range") != expected:
                            raise ValueError("Server did not honor exact byte range")
                        with target.open("r+b", buffering=0) as f:
                            f.seek(request_start)
                            for block in response.iter_bytes(1024 * 1024):
                                if count + len(block) > end - start + 1:
                                    raise ValueError("Oversized range response")
                                f.write(block)
                                count += len(block)
                                with lock:
                                    progress[start] = count
                                    if count - checkpoint >= 1024 * 1024 and count < end - start + 1:
                                        partial[str(start)] = count
                                        checkpoint = count
                                        save()
                        if count != end - start + 1:
                            raise ValueError("Incomplete range response")
                with lock:
                    state["completed"].append(start)
                    partial.pop(str(start), None)
                    save()
                return
            except Exception as exc:
                with lock:
                    progress[start] = partial.get(str(start), 0)
                detail = str(exc) if isinstance(exc, PermissionError) else type(exc).__name__
                print(f"Retry {attempt + 1} range {start}: {detail}", flush=True)
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 30))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download, pair) for pair in ranges if pair[0] not in done]
        while any(not f.done() for f in futures):
            time.sleep(10)
            with lock:
                transferred = sum(progress.values())
            elapsed = time.monotonic() - started
            rate = max(0, transferred - initial_partial) / max(elapsed, 1)
            remaining = max(0, args.size - initial - transferred)
            print(json.dumps({"percent": round(100 * (initial + transferred) / args.size, 2),
                              "MiB_per_second": round(rate / 2**20, 2),
                              "remaining_minutes": round(remaining / rate / 60, 1) if rate > 0 else None}), flush=True)
        for f in as_completed(futures):
            f.result()
    print("Verifying SHA-256...", flush=True)
    with target.open("rb") as f:
        actual = hashlib.file_digest(f, "sha256").hexdigest()
    if actual != args.sha256:
        raise ValueError(f"SHA-256 mismatch: {actual}")
    state["verified_sha256"] = actual
    save()
    print(json.dumps({"verified": True, "path": str(target), "sha256": actual}), flush=True)


if __name__ == "__main__":
    main()
