"""Private FastAPI entrypoint for the Homeostasis domain."""

from __future__ import annotations

import os
from pathlib import Path

from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.profiles import load_profile
from harness.runtime.rpc import create_domain_service_app

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("NC_HOMEOSTASIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_HOMEOSTASIS_PORT", "8772"))


def create_app():
    profile_path = Path(
        os.environ.get(
            "NC_HOMEOSTASIS_PROFILE_PATH",
            str(ROOT / "presets" / "homeostasis" / "functional-proxy-v1.json"),
        )
    )
    state_dir = Path(os.environ.get("NC_HOMEOSTASIS_STATE_DIR", str(ROOT / "data" / "homeostasis")))
    token = os.environ.get("NC_SERVICE_TOKEN", "development-service-token")
    engine = HomeostasisEngine(load_profile(profile_path), state_dir=state_dir)
    app = create_domain_service_app(engine, service_token=token, service_name="homeostasis")
    app.state.engine = engine
    app.router.add_event_handler("shutdown", engine.close)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
