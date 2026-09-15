"""Private FastAPI entrypoint for the Homeostasis domain."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.profiles import load_profile
from harness.runtime.rpc import create_domain_service_app
from harness.runtime.ownership import SharedDomainLease, resolve_control_mode, require_service_token

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("NC_HOMEOSTASIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_HOMEOSTASIS_PORT", "8772"))


def create_app():
    # Importing the module is read-only. Ownership precedes every constructor
    # which may migrate the command ledger or repair snapshot projections.
    @asynccontextmanager
    async def lifespan(app):
        mode = resolve_control_mode()
        token = require_service_token(os.environ.get("NC_SERVICE_TOKEN"), name="NC_SERVICE_TOKEN", control_mode=mode)
        default_profile = "functional-proxy-v1.json" if mode == "development_debug" else "qq-social-drive-v1.json"
        profile_path = Path(os.environ.get("NC_HOMEOSTASIS_PROFILE_PATH", str(ROOT / "presets" / "homeostasis" / default_profile)))
        state_dir = Path(os.environ.get("NC_HOMEOSTASIS_STATE_DIR", str(ROOT / "data" / "homeostasis")))
        isolation = os.environ.get("NC_ISOLATED_DEVELOPMENT", "0")
        if isolation not in {"0", "1"}:
            raise ValueError("NC_ISOLATED_DEVELOPMENT must be 0 or 1")
        lease = SharedDomainLease("homeostasis", state_dir, control_mode=mode, isolated=isolation == "1")
        with lease:
            engine = HomeostasisEngine(load_profile(profile_path), state_dir=state_dir)
            inner = create_domain_service_app(engine,
                service_token=token, service_name="homeostasis")
            app.state.engine, app.state.owner_lease = engine, lease
            app.mount("/", inner)
            mount = app.routes[-1]
            try:
                yield
            finally:
                app.routes.remove(mount)
                await engine.close()
    return FastAPI(title="NeuralCloud homeostasis service", lifespan=lifespan)


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
