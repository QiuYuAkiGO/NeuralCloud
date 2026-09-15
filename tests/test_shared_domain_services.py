"""Service imports are read-only; lifespan takes ownership before state opens."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from harness.runtime.ownership import SharedDomainAlreadyOwned, SharedDomainLease


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("NC_RUNTIME_MANIFEST_PATH", "NC_COGNITIVE_MANIFEST_PATH", "NC_EXPERIMENT_MANIFEST",
                 "NC_HOMEOSTASIS_PROFILE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "0")
    monkeypatch.setenv("NC_SERVICE_TOKEN", "a7"*32)
    monkeypatch.setenv("NC_CONTROL_TOKEN", "b8"*32)
    monkeypatch.setenv("NC_HOMEOSTASIS_STATE_DIR", str(tmp_path/"homeostasis"))
    monkeypatch.setenv("NC_RUNTIME_STATE_DIR", str(tmp_path/"runtime"))
    return tmp_path


def leases(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "SharedDomainLease", lambda *a,**kw: SharedDomainLease(*a,**kw,lease_root=tmp_path/"leases"))


def test_homeostasis_lifespan_lock_precedes_state_and_releases(clean_env, monkeypatch):
    from harness.homeostasis import service
    tmp_path=clean_env
    leases(service,monkeypatch,tmp_path)
    first,second = service.create_app(),service.create_app()
    assert not (tmp_path/"homeostasis").exists() and not (tmp_path/"leases").exists()
    with TestClient(first) as client:
        result=client.get("/v1/snapshot",headers={"Authorization":"Bearer "+"a7"*32})
        assert result.status_code==200
        assert result.json()["payload"]["profile_id"]=="qq-social-drive-v1"
        monkeypatch.setenv("NC_HOMEOSTASIS_STATE_DIR", str(tmp_path/"other"))
        with pytest.raises(SharedDomainAlreadyOwned), TestClient(second):
            pass
        assert not (tmp_path/"other").exists()
    with TestClient(second):
        assert (tmp_path/"other").exists()


def test_runtime_lifespan_lock_precedes_all_builders(clean_env, monkeypatch):
    from harness.runtime import service
    tmp_path=clean_env
    leases(service,monkeypatch,tmp_path)
    builds=[]
    def build(manifest,state_dir):
        assert manifest.control_mode=="study_runtime" and not manifest.autonomous_chat_enabled
        assert (tmp_path/"leases"/"misty.runtime.lock").exists()
        builds.append(state_dir)
        @asynccontextmanager
        async def lifespan(app):
            yield
        app=FastAPI(lifespan=lifespan)
        for name in ("supervisor","control","monitor"):
            setattr(app.state,name,object())
        @app.get("/v1/check")
        async def check():
            return {"healthy":True}
        return app
    monkeypatch.setattr(service,"_create_owned_app",build)
    first,second=service.create_app(),service.create_app()
    assert builds==[]
    with TestClient(first) as client:
        assert client.get("/v1/check").json()=={"healthy":True}
        with pytest.raises(SharedDomainAlreadyOwned), TestClient(second):
            pass
        assert len(builds)==1
    with TestClient(second):
        assert len(builds)==2


@pytest.mark.parametrize("which", ["homeostasis","runtime"])
@pytest.mark.parametrize("bad", [None,"development-service-token","short","has whitespace "+"a"*40])
def test_normal_service_never_uses_placeholder_tokens(clean_env,monkeypatch,which,bad):
    from harness.homeostasis import service as homeostasis
    from harness.runtime import service as runtime
    service=homeostasis if which=="homeostasis" else runtime
    leases(service,monkeypatch,clean_env)
    if bad is None:
        monkeypatch.delenv("NC_SERVICE_TOKEN")
    else:
        monkeypatch.setenv("NC_SERVICE_TOKEN",bad)
    with pytest.raises(ValueError), TestClient(service.create_app()):
        pass
    assert not (clean_env/"homeostasis").exists() and not (clean_env/"runtime").exists()


@pytest.mark.parametrize("isolation", ["true","yes","2","1"])
def test_normal_service_rejects_noncanonical_or_development_isolation(clean_env,monkeypatch,isolation):
    from harness.homeostasis import service
    leases(service,monkeypatch,clean_env)
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT",isolation)
    with pytest.raises((ValueError,PermissionError)), TestClient(service.create_app()):
        pass
    assert not (clean_env/"homeostasis").exists()
