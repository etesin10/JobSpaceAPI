import pytest
from pydantic import ValidationError

from jobfinder.config import ConfigManager, expand_env, parse_connections
from jobfinder.models import Criteria, Profile
from jobfinder.services import identity
from jobfinder.services.normalize import detect_seniority, detect_work_mode, parse_location


def test_criteria_requires_iso_codes():
    with pytest.raises(ValidationError):
        Criteria.model_validate({"name": "x", "targets": {"titles": ["a"]}, "constraints": {"locations": [{"country": "Mexico"}]}})
    with pytest.raises(ValidationError, match="not inside country"):
        Criteria.model_validate({"name": "x", "targets": {"titles": ["a"]},
                                 "constraints": {"locations": [{"country": "MX", "region": "CA-ON"}]}})
    with pytest.raises(ValidationError, match="at least one"):
        Criteria.model_validate({"name": "x"})
    c = Criteria.model_validate({"name": "x", "targets": {"titles": ["a"]},
                                 "constraints": {"locations": [{"country": "ca", "region": "ca-on"}],
                                                 "salary": {"min": 50000, "currency": "cad"}}})
    assert c.constraints.locations[0].region == "CA-ON" and c.constraints.salary.currency == "CAD"


def test_profile_id_pattern():
    with pytest.raises(ValidationError):
        Profile(id="Bad Id", name="x")


def test_env_expansion():
    raw = {"a": "${K1}", "b": "${MISSING:-dflt}", "c": "${MISSING}", "d": ["x-${K1}"]}
    out = expand_env(raw, {"K1": "v"})
    assert out == {"a": "v", "b": "dflt", "c": None, "d": ["x-v"]}


def test_config_keeps_last_good(tmp_path):
    f, env = tmp_path / "c.yaml", tmp_path / ".env"
    f.write_text('llm:\n  models:\n    m: {provider: openai, model: gpt, api_key: "${KEY}"}\n', encoding="utf-8")
    env.write_text("KEY=abc12345678\n", encoding="utf-8")
    cm = ConfigManager(f, env)
    assert cm.current.llm.models["m"].api_key == "abc12345678"
    assert cm.redacted()["llm"]["models"]["m"]["api_key"].startswith("abc1")
    f.write_text("llm: [broken", encoding="utf-8")
    assert cm.reload() is False and cm.last_error
    assert "m" in cm.current.llm.models
    cm.set_env_values({"KEY": "new value"})
    assert '"new value"' in env.read_text(encoding="utf-8")


def test_default_template_parses():
    from jobfinder.config import PRESETS, default_connections_text

    for p in PRESETS:
        conn = parse_connections(default_connections_text(p), {})
        assert conn.llm.routes["default"]
        assert conn.source("greenhouse") is not None
        assert conn.source("adzuna") is not None


def test_identity_keys():
    assert identity.normalize_domain("https://www.Acme.com/careers") == "acme.com"
    assert identity.normalize_domain("https://careers.acme.co.uk/x") == "acme.co.uk"
    assert identity.normalize_domain("https://boards.greenhouse.io/acme") is None
    assert identity.normalize_domain("linkedin.com") is None
    assert identity.company_name_key("ACME, Inc.") == identity.company_name_key("Acme") == "acme"
    assert identity.company_name_key("Grupo Bimbo S.A. de C.V.") == "grupo-bimbo"
    assert identity.canonical_url("https://X.com/job/1/?utm_source=a&gh_src=b&id=3#top") == "https://x.com/job/1?id=3"
    assert identity.role_id("u", "greenhouse", "acme:1") == "r:greenhouse:acme-1"
    assert identity.contact_id("Jane", None, ["https://www.linkedin.com/in/Jane-Doe/?x=1"], None) == "ct:li:jane-doe"


@pytest.mark.parametrize("raw,exp", [
    ("Monterrey, N.L., Mexico", ("MX", "MX-NLE", "Monterrey")),
    ("Toronto, ON", ("CA", "CA-ON", "Toronto")),
    ("New York, NY", ("US", "US-NY", "New York")),
    ("Remote - Canada", ("CA", None, None)),
    ("Guadalajara, Jalisco, México", ("MX", "MX-JAL", "Guadalajara")),
    ("Ontario, Canada", ("CA", "CA-ON", None)),
    ("China, Shenzhen", ("CN", None, "Shenzhen")),
    ("Berlin, Germany", ("DE", "DE-BE", "Berlin")),
    ("Atlanta, Georgia", ("US", "US-GA", "Atlanta")),
    ("Austin, TX", ("US", "US-TX", "Austin")),
    ("Quebec", ("CA", "CA-QC", None)),
    ("2 Locations", (None, None, None)),
])
def test_parse_location(raw, exp):
    p = parse_location(raw)
    assert (p.country, p.region, p.city) == exp


def test_invalid_urls_are_rejected_or_skipped():
    from pydantic import ValidationError

    from jobfinder.models import Role, SocialProfile
    from jobfinder.sources.base import make_role

    with pytest.raises(ValidationError):
        Role(title="x", url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        SocialProfile(url="data:text/html,hi")
    assert Role(title="x", url="acme.com/jobs/1").url == "https://acme.com/jobs/1"
    assert make_role(title="x", url="not a url") is None


def test_detectors():
    assert detect_work_mode("Ingeniero de datos (Híbrido)") == "hybrid"
    assert detect_work_mode("100% remoto") == "remote"
    assert detect_seniority("Sr. Data Engineer") == "senior"
    assert detect_seniority("Becario de datos") == "intern"
