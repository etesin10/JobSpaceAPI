import os

import pytest

os.environ["JOBFINDER_IPV6"] = "1"  # skip the network probe in tests


@pytest.fixture(autouse=True)
def _no_local_llm(monkeypatch):
    from jobfinder.llm import router

    monkeypatch.setattr(router, "_local_models", lambda base: None)


@pytest.fixture
def ws(tmp_path):
    from jobfinder.settings import Workspace

    return Workspace(tmp_path / "ws").ensure()


@pytest.fixture
def app(ws):
    from jobfinder.core import App

    a = App(ws)
    yield a
    a.close()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    from jobfinder.api import create_app

    c = TestClient(create_app(app, watch_config=False))
    c.headers.update({"X-JobFinder-Token": app.ws.api_token()})
    return c


@pytest.fixture
def profile(app):
    from jobfinder.models import Profile

    return app.store.save_profile(Profile(name="Ana Test"))


@pytest.fixture
def mx_criteria(app, profile):
    from jobfinder.models import Criteria

    cr = Criteria.model_validate({
        "name": "Data MX", "profile_id": profile.id,
        "constraints": {"locations": [{"country": "MX", "region": "MX-NLE", "city": "Monterrey"}],
                        "salary": {"min": 600000, "currency": "MXN"}, "posted_within_days": 60},
        "targets": {"titles": ["data engineer", "ingeniero de datos"]},
        "rules": {"should": [{"field": "skills", "op": "any", "value": ["Python", "SQL"], "weight": 2}],
                  "must_not": [{"field": "title", "op": "contains", "value": ["intern", "becario"]}]},
    })
    return app.store.save_criteria(cr)
