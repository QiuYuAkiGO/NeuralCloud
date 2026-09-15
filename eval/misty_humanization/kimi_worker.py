"""One-run Kimi worker. Credentials stay in process memory; jobs contain no secrets."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

import httpx


def save(path: Path, value: object) -> None:
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.pop('MOONSHOT_API_KEY', '')
    if not key:
        raise SystemExit('MOONSHOT_API_KEY is required')
    root = args.run_dir
    jobs = root / 'jobs'
    results = root / 'results'
    jobs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    base = 'https://api.moonshot.cn/v1'
    with httpx.Client(base_url=base, headers={'Authorization': f'Bearer {key}'},
                      timeout=httpx.Timeout(900, connect=30), follow_redirects=False) as client:
        try:
            response = client.get('/models')
            body = response.json()
            model_ids = [m['id'] for m in body.get('data', [])]
            probe = {'checked_at': datetime.now(timezone.utc).isoformat(),
                     'endpoint': base, 'status': response.status_code,
                     'available_model_ids': model_ids,
                     'kimi_k3_listed': 'kimi-k3' in model_ids}
            if response.is_error:
                probe['error'] = str(body.get('error', {})).replace(key, '[REDACTED]')
            save(root / 'api_probe.json', probe)
            print(json.dumps(probe, ensure_ascii=False), flush=True)
            if response.is_error:
                return
        except Exception as exc:
            save(root / 'api_probe.json', {'error_type': type(exc).__name__, 'message': str(exc).replace(key, '[REDACTED]')})
            print('API_PROBE_FAILED', type(exc).__name__, flush=True)
            return
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            pending = [p for p in sorted(jobs.glob('*.json')) if not (results / p.name).exists()]
            if not pending:
                if (root / 'STOP').exists():
                    break
                time.sleep(2)
                continue
            for job in pending:
                payload = json.loads(job.read_text(encoding='utf-8'))
                if payload.get('model') != 'kimi-k3':
                    raise ValueError('This run is restricted to kimi-k3')
                started = datetime.now(timezone.utc).isoformat()
                print('START', job.name, flush=True)
                result = None
                for attempt in range(4):
                    try:
                        response = client.post('/chat/completions', json=payload)
                        body = response.json()
                        # Preserve final content and transport/usage evidence, not internal reasoning text.
                        for choice in body.get('choices', []):
                            choice.get('message', {}).pop('reasoning_content', None)
                        result = {'job': job.name, 'started_at': started,
                                  'completed_at': datetime.now(timezone.utc).isoformat(),
                                  'endpoint': base + '/chat/completions',
                                  'request_sha256': hashlib.sha256(job.read_bytes()).hexdigest(),
                                  'status': response.status_code,
                                  'request_id': response.headers.get('x-request-id'),
                                  'attempt': attempt + 1, 'response': body}
                        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                            print('RETRY', job.name, response.status_code, flush=True)
                            time.sleep(min(60, 15 * (2 ** attempt)))
                            continue
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        result = {'job': job.name, 'started_at': started,
                                  'error_type': type(exc).__name__,
                                  'message': str(exc).replace(key, '[REDACTED]')}
                        if attempt < 3:
                            time.sleep(15 * (attempt + 1))
                save(results / job.name, result)
                print('DONE', job.name, result.get('status', result.get('error_type')),
                      json.dumps(result.get('response', {}).get('usage', {})), flush=True)
                if result.get('status') in (401, 402, 403):
                    print('API_ACCESS_BLOCKED', flush=True)
                    return


if __name__ == '__main__':
    main()
