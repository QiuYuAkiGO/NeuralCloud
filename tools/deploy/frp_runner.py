"""Run one SakuraFrp tunnel using credentials held outside process arguments."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher-config", type=Path, required=True)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--tunnel-id", type=int, required=True)
    args = parser.parse_args()
    if args.tunnel_id <= 0 or not args.client.is_absolute() or not args.client.is_file():
        parser.error("a positive tunnel id and absolute client path are required")
    path = args.launcher_config
    if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 131072:
        parser.error("invalid private launcher configuration")
    token = json.loads(path.read_text(encoding="utf-8"))["token"]
    if not isinstance(token, str) or not token or len(token) > 4096 or any(c.isspace() for c in token):
        parser.error("invalid private FRP token")
    env = {**os.environ, "NATFRP_TOKEN": token, "NATFRP_TARGET": str(args.tunnel_id)}
    os.execve(args.client, [str(args.client), "--no_check_update", "--disable_log_color"], env)


if __name__ == "__main__":
    main()
