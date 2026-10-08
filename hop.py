#!/usr/bin/env python3
"""Parent hop: the first hop for jurisdictions the ownership agent has not configured.

Input: a UK company (Companies House number or LEI). Output: the GLEIF chain above it and a
pre-filled document request for the register of the first parent outside the configured
jurisdictions. GLEIF only, stdlib only. Every value on the request card carries the GLEIF URL
and the JSON path it was read from, so a reviewer can check each one.

    python3 hop.py 06649982            # print the request for one company (recorded data)
    python3 hop.py 06649982 --live     # same, straight from api.gleif.org
    python3 hop.py record              # re-record the fixtures for the five demo cases
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.gleif.org/api/v1/"
SEARCH_UI = "https://search.gleif.org/#/record/"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEI_RE = re.compile(r"[A-Z0-9]{18}[0-9]{2}")

# Registers the agent already has tools for (the UBO-Bench scope). Every other country needs this hop.
CONFIGURED = ("GB", "DE")
EU = set("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())

# Five real UK companies whose GLEIF parent sits outside GB and DE. Checked live on 2026-10-08.
CASES = [
    {"q": "06649982", "label": "Mars Wrigley Confectionery UK", "note": "US, withheld direct parent, natural persons"},
    {"q": "02280000", "label": "Tata Steel UK", "note": "India, direct parent has no LEI"},
    {"q": "03026333", "label": "IKEA Properties Investments", "note": "Netherlands, non-consolidating owner"},
    {"q": "02937724", "label": "AXA UK", "note": "France, no known controller"},
    {"q": "05598549", "label": "Hitachi Rail", "note": "Japan, legal name in Japanese"},
]

# GLEIF reporting exception reasons, in plain words.
REASONS = {
    # Wording follows GLEIF / LEI ROC policy (leiroc.org/publications/gls/roc_20220107.pdf), checked 2026-10-08.
    "NATURAL_PERSONS": "Controlled by one or more natural persons, with no consolidating company in between.",
    "NON_CONSOLIDATING": "Controlled by a legal entity not subject to consolidation, so no group accounts link the two (often a foundation, trust or fund).",
    "NO_KNOWN_PERSON": "No known person or entity controls it, for example a widely held listed company.",
    "NON_PUBLIC": "The relationship information is non-public, which creates obstacles to releasing it, so GLEIF does not show the parent.",
    "NO_LEI": "The parent does not consent to obtain an LEI, or to let this company obtain one on its behalf, so GLEIF holds no record of it.",
}
ROLE_LABEL = {
    "subject": "UK company",
    "direct": "Direct parent",
    "ultimate": "Ultimate parent",
    "direct+ultimate": "Direct and ultimate parent",
    "above": "Above the ultimate parent",
}


def fixture_file(url):
    return FIXTURES / (re.sub(r"[^A-Za-z0-9.]+", "_", url.removeprefix(API)).strip("_") + ".json")


class Gleif:
    """GET JSON from GLEIF. mode: 'fixtures' (no network), 'live', or 'record' (live, then save).

    `trail` lists every record read, in reading order: it becomes the evidence trail."""

    def __init__(self, mode="fixtures"):
        self.mode, self.cache, self.trail, self.warm = mode, {}, [], {}

    def fetch(self, url):
        if url in self.warm:
            return self.warm.pop(url)
        if self.mode == "fixtures":
            f = fixture_file(url)
            if not f.exists():
                raise LookupError(f"No recorded GLEIF response for {url}. Start the app with --live to look it up.")
            return json.loads(f.read_text())
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.api+json", "User-Agent": "parent-hop/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LookupError(f"GLEIF has no record at {url}.") from None
            raise
        if self.mode == "record":
            fixture_file(url).write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")
        return body

    def prefetch(self, urls):
        """Live mode: fetch the next round in parallel (GLEIF takes ~0.7 s per call). get() still logs the trail."""
        todo = [u for u in dict.fromkeys(urls) if u not in self.cache and u not in self.warm]
        if self.mode == "fixtures" or len(todo) < 2:
            return
        with ThreadPoolExecutor(len(todo)) as ex:
            futures = {u: ex.submit(self.fetch, u) for u in todo}
        for u, fut in futures.items():
            if not fut.exception():  # failures surface again, properly, when get() asks
                self.warm[u] = fut.result()

    def get(self, path, why):
        url = path if path.startswith(API) else API + path
        if url not in self.cache:
            self.cache[url] = self.fetch(url)
            self.trail.append({"url": url, "why": why})
        return url, self.cache[url]


def dig(body, path):
    for k in path.split("."):
        body = body[int(k)] if isinstance(body, list) else body[k]
    return body


def src(url, body, path):
    """A value plus the GLEIF record and JSON path it came from."""
    return {"value": dig(body, path), "src": url, "path": path}


def entity_node(role, url, body):
    a = body["data"]["attributes"]
    e = a["entity"]
    p = "data.attributes.entity"
    alt = None
    if any(ord(ch) > 0x24F for ch in e["legalName"]["name"]):  # non-Latin script: add the English or ASCII name
        alt = next((f"{p}.otherNames.{i}.name" for i, n in enumerate(e["otherNames"])
                    if n.get("type") == "ALTERNATIVE_LANGUAGE_LEGAL_NAME" and n.get("language") == "en"), None)
        if not alt and e["transliteratedOtherNames"]:
            alt = f"{p}.transliteratedOtherNames.0.name"
    jur = e["jurisdiction"] or e["legalAddress"]["country"]
    return {
        "role": role, "kind": "entity",
        "name": src(url, body, f"{p}.legalName.name"),
        "alt_name": src(url, body, alt) if alt else None,
        "lei": src(url, body, "data.attributes.lei"),
        "jurisdiction": src(url, body, f"{p}.jurisdiction" if e["jurisdiction"] else f"{p}.legalAddress.country"),
        "country": jur.split("-")[0],
        "registered_as": src(url, body, f"{p}.registeredAs"),
        "ra": src(url, body, f"{p}.registeredAt.id"),
        "ra_other": src(url, body, f"{p}.registeredAt.other"),
        "city": src(url, body, f"{p}.legalAddress.city"),
        "lei_status": src(url, body, "data.attributes.registration.status"),
        "url": url, "ui": SEARCH_UI + a["lei"],
    }


def rel_info(url, body):
    b = "data.attributes"
    periods = dig(body, f"{b}.relationship.periods")
    i = next((i for i, x in enumerate(periods) if x["type"] == "ACCOUNTING_PERIOD" and x.get("endDate")), None)
    return {
        "type": src(url, body, f"{b}.relationship.type"),
        "corroboration": src(url, body, f"{b}.registration.corroborationLevel"),
        "documents": src(url, body, f"{b}.registration.corroborationDocuments"),
        "reference": src(url, body, f"{b}.registration.corroborationReference"),
        "period_end": src(url, body, f"{b}.relationship.periods.{i}.endDate") if i is not None else None,
        "url": url,
    }


def parent_side(g, body, which, role, label):
    """Follow the record's own relationship links, so no request ever needs to 404."""
    links = body["data"]["relationships"].get(f"{which}-parent", {}).get("links", {})
    if "lei-record" in links:
        purl, pbody = g.get(links["lei-record"], f"{label}: LEI record")
        rurl, rbody = g.get(links["relationship-record"], f"{label}: relationship record")
        node = entity_node(role, purl, pbody)
        node["rel"] = rel_info(rurl, rbody)
        return node
    if "reporting-exception" in links:
        xurl, xbody = g.get(links["reporting-exception"], f"{label}: reporting exception")
        reason = dig(xbody, "data.attributes.reason")
        return {
            "role": role, "kind": "exception", "url": xurl,
            "reason": src(xurl, xbody, "data.attributes.reason"),
            "category": src(xurl, xbody, "data.attributes.category"),
            "reference": src(xurl, xbody, "data.attributes.reference"),
            "explain": REASONS.get(reason, "GLEIF reports no parent for this reason code."),
        }
    return {"role": role, "kind": "missing", "explain": "No parent information reported to GLEIF."}


def find_subject(g, query):
    q = re.sub(r"\s+", "", query).upper()
    if not q:
        raise LookupError("Enter a Companies House number or an LEI.")
    if LEI_RE.fullmatch(q):
        return g.get(f"lei-records/{q}", "UK company: LEI record")
    _, found = g.get(f"lei-records?filter[entity.registeredAs]={urllib.parse.quote(q)}&filter[entity.legalAddress.country]=GB",
                     f"UK company: LEI found by Companies House no. {q}")
    if not found["data"]:
        raise LookupError(f"No LEI in GLEIF for Companies House number {q}. This hop needs the UK company or its parent to hold an LEI.")
    rec = min(found["data"], key=lambda r: r["attributes"]["registration"]["status"] != "ISSUED")
    return g.get(rec["links"]["self"], "UK company: LEI record")


def resolve(g, query):
    surl, sbody = find_subject(g, query)
    subject = entity_node("subject", surl, sbody)
    rel = sbody["data"]["relationships"]
    g.prefetch([u for k in ("direct-parent", "ultimate-parent") for u in rel.get(k, {}).get("links", {}).values()])
    direct = parent_side(g, sbody, "direct", "direct", "Direct parent")
    ultimate = parent_side(g, sbody, "ultimate", "ultimate", "Ultimate parent")

    same_entity = direct["kind"] == ultimate["kind"] == "entity" and direct["lei"]["value"] == ultimate["lei"]["value"]
    same_gap = direct["kind"] == ultimate["kind"] != "entity" and (
        direct["kind"] == "missing" or direct["reason"]["value"] == ultimate["reason"]["value"])
    if same_entity or same_gap:
        direct["role"] = "direct+ultimate"
        nodes = [subject, direct]
    else:
        nodes = [subject, direct, ultimate]

    # Where GLEIF itself stops: the reported parent (or exception) of the highest entity in the chain.
    if nodes[-1]["kind"] == "entity":
        top = nodes[-1]
        up = g.cache[top["url"]]["data"]["relationships"].get("ultimate-parent", {}).get("links", {})
        g.prefetch(list(up.values()) + [f"{API}registration-authorities/{n['ra']['value']}" for n in nodes[1:]
                   if n["kind"] == "entity" and n["country"] not in CONFIGURED and n["ra"]["value"] not in ("RA999999", "RA888888")])
        above = parent_side(g, g.cache[top["url"]], "ultimate", "above", f"Parent of {top['name']['value']}")
        if above["kind"] != "missing":
            nodes.append(above)

    parents = [n for n in nodes[1:] if n["kind"] == "entity"]
    target = next((i for i, n in enumerate(nodes) if i and n["kind"] == "entity" and n["country"] not in CONFIGURED), None)
    status = "foreign" if target is not None else ("configured" if parents else "no_parent")
    return {"nodes": nodes, "target": target, "status": status,
            "golden_copy": sbody.get("meta", {}).get("goldenCopy", {}).get("publishDate")}


def build_card(g, chain):
    nodes, t = chain["nodes"], chain["nodes"][chain["target"]]
    s = nodes[0]
    fields = {}

    def put(key, label, v):
        if v and v["value"] not in (None, ""):
            fields[key] = {"label": label, **v}

    put("subject_name", "UK company", s["name"])
    put("subject_reg", "Companies House no.", s["registered_as"])
    put("subject_lei", "UK company LEI", s["lei"])
    put("parent_name", "Legal name", t["name"])
    put("parent_alt", "Also known as", t["alt_name"])
    put("parent_lei", "LEI", t["lei"])
    put("parent_reg", "File no." if t["country"] == "US" else "Registration no.", t["registered_as"])
    put("parent_jur", "Jurisdiction", t["jurisdiction"])
    put("parent_city", "Registered city", t["city"])
    put("parent_lei_status", "LEI status", t["lei_status"])
    put("parent_ra", "GLEIF register code", t["ra"])

    ra = t["ra"]["value"]
    if ra in ("RA999999", "RA888888"):  # GLEIF codes for "not available" and "other"
        put("register_name", "Register", t["ra_other"])
    else:
        rurl, rbody = g.get(f"registration-authorities/{ra}", f"Register that holds {t['name']['value']} ({ra})")
        a = "data.attributes"
        put("register_org", "Register operator", src(rurl, rbody, f"{a}.internationalOrganizationName"))
        name_path = f"{a}.internationalName" if dig(rbody, f"{a}.internationalName") else f"{a}.localName"
        put("register_name", "Register", src(rurl, rbody, name_path))
        if dig(rbody, f"{a}.localOrganizationName") != dig(rbody, f"{a}.internationalOrganizationName"):
            put("register_local", "Local name", src(rurl, rbody, f"{a}.localOrganizationName"))
        put("register_site", "Website", src(rurl, rbody, f"{a}.website"))
        jurs = dig(rbody, f"{a}.jurisdictions")
        j = next((i for i, x in enumerate(jurs) if x["countryCode"] == t["country"]), 0)
        put("register_country", "Country", src(rurl, rbody, f"{a}.jurisdictions.{j}.country"))
        if jurs[j]["jurisdiction"] != jurs[j]["country"]:
            put("register_area", "Covers", src(rurl, rbody, f"{a}.jurisdictions.{j}.jurisdiction"))

    gaps = [n for n in nodes[1:] if n["kind"] == "exception"]
    for n in gaps:
        fields[f"exc_{n['role'].replace('+', '_')}"] = {"label": f"{ROLE_LABEL[n['role']]}: GLEIF exception", **n["reason"]}
        if n["role"] == "above":
            top = nodes[nodes.index(n) - 1]
            put("top_name", "Highest entity in GLEIF", top["name"])
            put("top_alt", "Also known as", top["alt_name"])
    v = {k: f["value"] for k, f in fields.items()}

    where_key = "register_name" if "register_name" in v else "register_org" if "register_org" in v else None
    where = v[where_key] if where_key else "the register named by GLEIF"
    loc_keys = [k for k in ("register_area", "register_country") if k in v]
    loc = ", ".join(v[k] for k in loc_keys) or t["country"]
    P = f"{v['parent_name']} ({v['parent_alt']})" if "parent_alt" in v else v["parent_name"]
    p_keys = ["parent_name", "parent_alt"]
    reg_word = "file no." if t["country"] == "US" else "registration no."
    nxt = nodes[chain["target"] + 1] if chain["target"] + 1 < len(nodes) else None
    widely_held = bool(nxt and nxt["role"] == "above" and nxt["kind"] == "exception" and nxt["reason"]["value"] == "NO_KNOWN_PERSON")
    asks = []

    def ask(text, *keys):
        asks.append({"text": re.sub(r"\.\.$", ".", text), "uses": [k for k in keys if k in v]})

    if t["country"] == "US":
        ask(f"A certificate of good standing from the {where} ({loc}) for {P}, {reg_word} {v['parent_reg']}.",
            where_key, *loc_keys, *p_keys, "parent_reg")
        ask(f"A beneficial ownership certification from the customer covering {P}. "
            "The US has no public ownership register (31 CFR 1010.230).", *p_keys)
    else:
        ask(f"A current extract from the {where} ({loc}) for {P}, {reg_word} {v['parent_reg']}.",
            where_key, *loc_keys, *p_keys, "parent_reg")
        if t["country"] in EU:
            ask(f"An extract for {P} from the beneficial ownership (UBO) register ({loc}).", *p_keys, *loc_keys)
        elif not widely_held:  # widely held: the major-holder ask below covers it
            ask(f"The shareholder register of {P}, certified by a director, if the extract does not list shareholders.", *p_keys)

    for n in gaps:
        key, code = f"exc_{n['role'].replace('+', '_')}", n["reason"]["value"]
        if n["role"] == "above":
            T = f"{v['top_name']} ({v['top_alt']})" if "top_alt" in v else v["top_name"]
            text = {
                "NATURAL_PERSONS": f"Names, dates of birth and holdings of the natural persons who control {T}, with the document that shows it (GLEIF: {code}).",
                "NON_CONSOLIDATING": f"The name and register details of the entity that controls {T}, and who stands behind it (GLEIF: {code}).",
                "NO_KNOWN_PERSON": f"Any holder of more than 25% of {T}, or written confirmation that none exists (GLEIF: {code}).",
            }.get(code, f"Who controls {T} (GLEIF: {code}).")
            ask(text, "top_name", "top_alt", key)
        else:
            ask(f"The name and registration of the immediate shareholder of {v['subject_name']}, and how it links to "
                f"{P} (GLEIF: {code}).", "subject_name", *p_keys, key)

    flags = []
    if s["lei_status"]["value"] != "ISSUED":
        flags.append(f"The GLEIF record for {v['subject_name']} is {s['lei_status']['value']}: its parent data may be out of date.")
    if v.get("parent_lei_status") != "ISSUED":
        flags.append(f"The GLEIF record for {v['parent_name']} is {v.get('parent_lei_status')}: check every detail against the register.")
    rel = t.get("rel")
    if rel and rel["corroboration"]["value"] != "FULLY_CORROBORATED":
        level = rel["corroboration"]["value"].lower().replace("_", " ")
        flags.append(f"GLEIF marks the link to {v['parent_name']} as {level}, not fully corroborated. Treat it as a lead.")

    lines = [
        f"To: {where}{', ' + v['register_org'] if 'register_org' in v and v['register_org'] != where else ''} ({loc})",
        f"Re: {P}, {reg_word} {v['parent_reg']}, LEI {v['parent_lei']}",
        f"For: ownership check on {v['subject_name']} (Companies House {v.get('subject_reg', 'n/a')}, LEI {v['subject_lei']})",
        "",
        "Please provide:",
        *[f"{i}. {a['text']}" for i, a in enumerate(asks, 1)],
        "",
        "GLEIF evidence:",
        *[f"- {e['url']}" for e in g.trail],
    ]
    return {"fields": fields, "asks": asks, "flags": flags, "draft": "\n".join(lines)}


def run(query, mode="fixtures"):
    g = Gleif(mode)
    t0 = time.perf_counter()
    chain = resolve(g, query)
    chain["card"] = build_card(g, chain) if chain["status"] == "foreign" else None
    chain["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    chain["mode"] = mode
    chain["configured"] = list(CONFIGURED)
    links = []
    for n in chain["nodes"]:
        rel = n.get("rel")
        if rel and rel["reference"]["value"]:
            links.append({"url": rel["reference"]["value"], "why": f"Filing GLEIF cites for the link to {n['name']['value']}"})
    chain["evidence"] = g.trail + links
    chain["lookups"] = len(g.trail)
    return chain


def main(argv):
    mode = "live" if "--live" in argv else "fixtures"
    args = [a for a in argv if not a.startswith("--")]
    if args == ["record"]:
        FIXTURES.mkdir(exist_ok=True)
        for c in CASES:
            r = run(c["q"], "record")
            print(f"recorded {c['q']} {c['label']}: {r['lookups']} GLEIF responses")
        return
    if not args:
        sys.exit(__doc__)
    r = run(args[0], mode)
    for n in r["nodes"]:
        what = n["name"]["value"] + f" [{n['jurisdiction']['value']}]" if n["kind"] == "entity" else n.get("reason", {}).get("value", "not reported")
        print(f"{ROLE_LABEL[n['role']]:>28}: {what}")
    print()
    print(r["card"]["draft"] if r["card"] else f"No request needed: status {r['status']}.")


if __name__ == "__main__":
    main(sys.argv[1:])
