"""Runs entirely on the recorded GLEIF responses in fixtures/. Any network call fails the test."""
import http.client
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
import hop  # noqa: E402

# (role, kind, LEI or reason code, jurisdiction) per node, read off the live GLEIF API on 2026-10-08.
EXPECTED = {
    "06649982": [("subject", "entity", "549300WCJO7C280G3J19", "GB"), ("direct", "exception", "NON_PUBLIC", None),
                 ("ultimate", "entity", "54930073HKEO6GZBSS03", "US-DE"), ("above", "exception", "NATURAL_PERSONS", None)],
    "02280000": [("subject", "entity", "213800TR86P337IE8605", "GB"), ("direct", "exception", "NO_LEI", None),
                 ("ultimate", "entity", "335800E6C75YGSGD5T66", "IN"), ("above", "exception", "NO_KNOWN_PERSON", None)],
    "03026333": [("subject", "entity", "549300Q6ZNO3WMDNH353", "GB"), ("direct+ultimate", "entity", "5493008E81TB1MN1VT78", "NL"),
                 ("above", "exception", "NON_CONSOLIDATING", None)],
    "02937724": [("subject", "entity", "213800H9XLWV42LYIT41", "GB"), ("direct+ultimate", "entity", "F5WCUMTUM4RKZ1MAIE39", "FR"),
                 ("above", "exception", "NO_KNOWN_PERSON", None)],
    "05598549": [("subject", "entity", "213800LPXQQUVDXSN309", "GB"), ("direct+ultimate", "entity", "549300T6IPOCDWLKC615", "JP"),
                 ("above", "exception", "NO_KNOWN_PERSON", None)],
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call during a fixture test")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(scope="module")
def runs():
    return {q: hop.run(q) for q in EXPECTED}


def shape(n):
    if n["kind"] == "entity":
        return (n["role"], "entity", n["lei"]["value"], n["jurisdiction"]["value"])
    return (n["role"], n["kind"], n["reason"]["value"], None)


def traced_values(obj):
    """Every {value, src, path} triple anywhere in a result."""
    if isinstance(obj, dict):
        if {"value", "src", "path"} <= obj.keys():
            yield obj
        for v in obj.values():
            yield from traced_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from traced_values(v)


def test_cases_match_the_demo_list():
    assert [c["q"] for c in hop.CASES] == list(EXPECTED)


@pytest.mark.parametrize("q", EXPECTED)
def test_chain_resolution(runs, q):
    r = runs[q]
    assert [shape(n) for n in r["nodes"]] == EXPECTED[q]
    assert r["status"] == "foreign"
    target = r["nodes"][r["target"]]
    assert target["kind"] == "entity" and target["country"] not in hop.CONFIGURED
    # the target is the first parent entity outside the configured jurisdictions
    assert all(n["kind"] != "entity" for n in r["nodes"][1:r["target"]])


def test_lei_and_companies_house_number_give_the_same_chain(runs):
    assert [shape(n) for n in hop.run("549300WCJO7C280G3J19")["nodes"]] == [shape(n) for n in runs["06649982"]["nodes"]]


def test_reporting_exceptions_turn_into_asks(runs):
    mars = runs["06649982"]["card"]
    texts = " ".join(a["text"] for a in mars["asks"])
    assert "immediate shareholder of MARS WRIGLEY CONFECTIONERY UK LIMITED" in texts and "NON_PUBLIC" in texts
    assert "natural persons who control MARS, INCORPORATED" in texts and "NATURAL_PERSONS" in texts
    assert "31 CFR 1010.230" in texts and "UBO" not in texts  # US: certification, no ownership register
    assert any("entity supplied only" in f for f in mars["flags"])

    tata = " ".join(a["text"] for a in runs["02280000"]["card"]["asks"])
    assert "NO_LEI" in tata and "Any holder of more than 25% of TATA STEEL LIMITED" in tata

    ikea = " ".join(a["text"] for a in runs["03026333"]["card"]["asks"])
    assert "beneficial ownership (UBO) register (Netherlands)" in ikea and "NON_CONSOLIDATING" in ikea

    hitachi = runs["05598549"]["card"]
    assert hitachi["fields"]["parent_alt"]["value"] == "Hitachi,Ltd."
    assert not any("shareholder register" in a["text"] for a in hitachi["asks"])  # widely held: major-holder ask instead


def test_request_names_the_register_from_gleif(runs):
    want = {"06649982": ("register_org", "Division of Corporations, Department of State"),
            "02280000": ("register_org", "Ministry of Corporate Affairs"),
            "03026333": ("register_org", "The Netherlands Chamber of Commerce"),
            "02937724": ("register_name", "Register of Companies (Sirene)"),
            "05598549": ("register_org", "Legal Affairs Bureau")}
    for q, (key, value) in want.items():
        assert runs[q]["card"]["fields"][key]["value"] == value
        assert runs[q]["card"]["fields"][key]["src"].endswith("/registration-authorities/" + runs[q]["card"]["fields"]["parent_ra"]["value"])


@pytest.mark.parametrize("q", EXPECTED)
def test_every_value_traces_to_a_gleif_record(runs, q):
    r = runs[q]
    trail = {e["url"] for e in r["evidence"]}
    values = list(traced_values(r))
    assert len(values) > 20
    for t in values:
        assert t["src"].startswith(hop.API), t
        assert t["src"] in trail, f"{t['src']} missing from the evidence trail"
        recorded = json.loads(hop.fixture_file(t["src"]).read_text())
        assert hop.dig(recorded, t["path"]) == t["value"], t


@pytest.mark.parametrize("q", EXPECTED)
def test_request_card_only_uses_traced_fields(runs, q):
    card = runs[q]["card"]
    for key, f in card["fields"].items():
        assert {"label", "value", "src", "path"} <= f.keys(), key
    for a in card["asks"]:
        assert a["uses"], a["text"]
        for key in a["uses"]:
            assert str(card["fields"][key]["value"]) in a["text"], (key, a["text"])
    for key in ("parent_name", "parent_lei", "parent_reg", "subject_lei"):
        assert card["fields"][key]["value"] in card["draft"]
    for e in runs[q]["evidence"]:
        if e["url"].startswith(hop.API):
            assert e["url"] in card["draft"]


def test_configured_parent_needs_no_hop(monkeypatch):
    monkeypatch.setattr(hop, "CONFIGURED", ("GB", "DE", "NL"))
    r = hop.run("03026333")
    assert r["status"] == "configured" and r["target"] is None and r["card"] is None


def test_unknown_company_says_what_to_do():
    with pytest.raises(LookupError, match="--live"):
        hop.run("99999999")


def test_server_serves_screen_hop_and_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APPROVALS", tmp_path / "approvals.jsonl")
    srv = app.serve("fixtures", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body else None, headers={"Content-Type": "application/json"})
        res = c.getresponse()
        return res.status, res.read().decode()

    try:
        status, html = call("GET", "/")
        assert status == 200 and "Parent hop" in html
        status, body = call("GET", "/api/cases")
        assert status == 200 and len(json.loads(body)["cases"]) == 5
        status, body = call("GET", "/api/hop?q=03026333")
        hopped = json.loads(body)
        assert status == 200 and hopped["card"]["fields"]["parent_name"]["value"] == "INGKA HOLDING B.V."
        assert call("GET", "/api/hop?q=99999999")[0] == 404
        status, body = call("POST", "/api/approve", {"lei": "549300Q6ZNO3WMDNH353", "draft": hopped["card"]["draft"]})
        assert status == 200 and json.loads(body)["ok"]
        saved = json.loads((tmp_path / "approvals.jsonl").read_text())
        assert saved["lei"] == "549300Q6ZNO3WMDNH353" and saved["edited"] is False
        assert call("POST", "/api/approve", {"nope": 1})[0] == 400
    finally:
        srv.shutdown()


def test_no_em_or_en_dashes_in_copy():
    for f in ["hop.py", "app.py", "static/index.html", "README.md", "Makefile", "tests/test_hop.py"]:
        text = (ROOT / f).read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, f  # em dash, en dash


def test_approve_refuses_other_sites_and_non_json(tmp_path, monkeypatch):
    """Regression for a CSRF finding: only the screen's own page may approve."""
    monkeypatch.setattr(app, "APPROVALS", tmp_path / "approvals.jsonl")
    srv = app.serve("fixtures", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(headers):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/approve", body=json.dumps({"lei": "X", "draft": "d"}), headers=headers)
        return c.getresponse().status

    try:
        assert post({"Content-Type": "application/json", "Origin": "https://evil.example"}) == 403
        assert post({"Content-Type": "text/plain", "Origin": f"http://127.0.0.1:{port}"}) == 415
        assert post({"Content-Type": "application/json", "Host": "attacker.example"}) == 403
        assert not (tmp_path / "approvals.jsonl").exists()
        assert post({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}) == 200
    finally:
        srv.shutdown()


def test_every_dynamic_link_goes_through_the_https_check():
    """Regression for an XSS finding: GLEIF-sourced hrefs must pass safeUrl, never raw."""
    html = (ROOT / "static" / "index.html").read_text()
    assert "const safeUrl" in html
    for chunk in html.split('href="${')[1:]:
        assert chunk.startswith("esc(safeUrl("), chunk[:60]
