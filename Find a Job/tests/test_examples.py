import json
from pathlib import Path

import pytest

from jobfinder.models import SCHEMA_EXPORTS, WorkplanSubmission
from jobfinder.services import workplan as wp
from jobfinder.services.profiles import export_bundle, import_bundle

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["data-engineer-mx.profile.json", "pharmacy-assistant-on.profile.json"])
def test_example_bundles_import(app, name):
    bundle = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
    res = import_bundle(app, bundle)
    assert res["criteria"]
    again = import_bundle(app, bundle)  # importing twice never overwrites
    assert again["profile"] != res["profile"]
    out = export_bundle(app, res["profile"])
    assert len(out["criteria"]) == len(bundle["criteria"]) and out["skills"]["skills"]


def test_example_submission_merges(app):
    bundle = json.loads((ROOT / "examples" / "data-engineer-mx.profile.json").read_text(encoding="utf-8"))
    pid = import_bundle(app, bundle)["profile"]
    sub = json.loads((ROOT / "examples" / "workplan_submission.example.json").read_text(encoding="utf-8"))
    sub["criteria_id"] = app.store.list_criteria(pid)[0].id
    sub["strategy_updates"] = {sub["criteria_id"]: sub["strategy_updates"].popitem()[1]}
    rep = wp.merge(app, pid, WorkplanSubmission.model_validate(sub), verify=False)
    assert rep.companies_added == 2 and rep.roles_added == 1 and rep.contacts_added == 1
    stats = app.store.stats(pid)
    assert stats["companies"] == 1 and stats["companies_excluded"] == 1


def test_committed_schemas_are_current():
    for name, model in SCHEMA_EXPORTS.items():
        f = ROOT / "schemas" / f"{name}.schema.json"
        assert json.loads(f.read_text(encoding="utf-8")) == model.model_json_schema(by_alias=True), \
            f"run `jobfinder schemas` to refresh {f.name}"
