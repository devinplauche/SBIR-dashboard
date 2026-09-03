#!/usr/bin/env python3
"""
Pull open SBIR/STTR topics from every source we can reach, score them for
small-software-team fit, and write docs/data/{topics,meta}.json.

Standard library only, so CI needs no install step.

Sources
  DSIP      www.dodsbirsttr.mil  JSON API. Rich: phases, TPOC emails, full text.
  SBIR.gov  www.sbir.gov/topics  HTML scrape. Only source for NSF/HHS/civilian.

Both sources are scraped rather than contracted, so both are expected to break
eventually. Each is isolated: if one fails, its previous topics are carried
forward from the existing topics.json and the failure is recorded in meta.json
for the dashboard to surface. A run never silently produces a thinner dataset.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs", "data")

DSIP_SEARCH = "https://www.dodsbirsttr.mil/topics/api/public/topics/search"
DSIP_DETAIL = "https://www.dodsbirsttr.mil/topics/api/public/topics/{}/details"
SBIRGOV = "https://www.sbir.gov/topics?status=open&page={}"

# How long SAM.gov entity validation realistically takes for a new registrant.
# Drives the "reachable" calculation. The dashboard lets a viewer set this to 0
# if they are already registered.
DEFAULT_REGISTRATION_DAYS = 28


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

def get(url, tries=3, timeout=45):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json, text/html"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:          # noqa: BLE001 - want every transport failure
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last


def iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def clean(s):
    """HTML fragment -> readable plain text."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# --------------------------------------------------------------------------
# source: DSIP (Department of War)
# --------------------------------------------------------------------------

def dsip_search_param():
    return urllib.parse.quote(json.dumps({
        "searchText": None, "components": None, "programYear": None,
        "solicitationCycleNames": ["openTopics"], "releaseNumbers": [],
        # 591 = open, 592 = pre-release. Pre-release matters most: it is the only
        # window in which topic authors may be contacted directly.
        "topicReleaseStatus": [591, 592],
        "modernizationPriorities": [], "sortBy": "finalTopicCode,asc",
        "technologyAreaIds": [], "component": None, "program": None,
    }))


def phases_of(raw):
    """phaseHierarchy is a JSON string: {"config":[{"displayValue":"I"},...]}."""
    try:
        return [p["displayValue"] for p in json.loads(raw)["config"]]
    except Exception:  # noqa: BLE001
        return []


def fetch_dsip(with_detail=True):
    topics, page = [], 0
    while page < 20:
        url = f"{DSIP_SEARCH}?searchParam={dsip_search_param()}&size=50&page={page}"
        payload = json.loads(get(url))
        batch = payload.get("data") or []
        if not batch:
            break
        for t in batch:
            phases = phases_of(t.get("phaseHierarchy"))
            topics.append({
                "source": "dsip",
                "id": str(t.get("topicId")),
                "code": t.get("topicCode") or t.get("finalTopicCode"),
                "title": (t.get("topicTitle") or "").strip(),
                "agency": "DOD",
                "component": t.get("component"),
                "program": t.get("program"),
                "status": t.get("topicStatus"),
                "phases": phases,
                "has_phase1": "I" in phases,
                "open": iso(t.get("topicStartDate")),
                "close": iso(t.get("topicEndDate")),
                "tpoc_until": iso(t.get("topicQATpocEndDate")),
                "tpocs": [m.get("email") for m in (t.get("topicManagers") or []) if m.get("email")],
                "url": f"https://www.dodsbirsttr.mil/topics-app/details/?topicCode={t.get('topicCode')}",
                "text": "",
            })
        total = payload.get("total", 0)
        page += 1
        if len(topics) >= total:
            break
        time.sleep(0.3)

    if with_detail:
        for i, t in enumerate(topics):
            try:
                d = json.loads(get(DSIP_DETAIL.format(t["id"]), tries=2, timeout=30))
                t["text"] = clean(
                    " ".join(filter(None, [d.get("objective"), d.get("description"), d.get("phase1Description")]))
                )[:6000]
                t["objective"] = clean(d.get("objective"))[:700]
            except Exception:  # noqa: BLE001 - detail is enrichment, not essential
                t["objective"] = ""
            if i % 10 == 0:
                print(f"  dsip detail {i}/{len(topics)}", flush=True)
            time.sleep(0.2)

    return topics


# --------------------------------------------------------------------------
# source: SBIR.gov (NSF, HHS, and the rest of the civilian agencies)
# --------------------------------------------------------------------------

BLOCK_RE = re.compile(
    r'<h3[^>]*><a href="/topics/(\d+)">(.*?)</a></h3>(.*?)(?=<h3[^>]*><a href="/topics/\d+"|<nav|$)',
    re.S,
)
SEAL_RE = re.compile(r'alt="Seal of the Agency: ([^"]+)"')
DESC_RE = re.compile(r'<p class="measure-6">(.*?)</p>', re.S)
TAG_RE = re.compile(r'radius-sm text-white text-no-uppercase margin-top-1">([A-Z]+)</p>')


def date_field(label, s):
    m = re.search(r"<b>" + label + r":</b>\s*([A-Za-z]+ \d+, \d{4})", s)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


TOTAL_RE = re.compile(r"of ([\d,]+) results")


def parse_page(h, into):
    """Parse one results page into `into`, keyed by topic id. Returns rows seen."""
    rows = 0
    for m in BLOCK_RE.finditer(h):
        tid, title, body = m.group(1), clean(m.group(2)), m.group(3)
        rows += 1
        if tid in into:
            continue
        seal = SEAL_RE.search(body)
        desc = DESC_RE.search(body)
        tags = TAG_RE.findall(body)
        programs = [t for t in tags if t in ("SBIR", "STTR", "BOTH")]
        into[tid] = {
                "source": "sbirgov",
                "id": tid,
                "code": None,
                "title": title,
                "agency": (seal.group(1) if seal else "UNKNOWN"),
                "component": None,
                "program": ("BOTH" if "BOTH" in programs else (programs[0] if programs else None)),
                "status": "Open",
                "phases": [],
                "has_phase1": True,   # SBIR.gov open listings are Phase I unless stated
                "open": date_field("Open Date", body),
                "close": date_field("Close Date", body),
                "tpoc_until": None,
                "tpocs": [],
            "url": f"https://www.sbir.gov/topics/{tid}",
            "objective": clean(desc.group(1))[:700] if desc else "",
            "text": clean(desc.group(1))[:6000] if desc else "",
        }
    return rows


def fetch_sbirgov(max_passes=6):
    """Sweep the paged listing until the unique count reaches the site's own total.

    The listing's sort has no tiebreaker, and the NSF block shares a close date
    across hundreds of entries, so rows shuffle between pages from one request
    to the next. A single sweep therefore returns duplicates on some pages and
    silently omits whatever they displaced - measured at 323 of 337 topics, with
    the missing 14 differing per run.

    Re-sweeping converges: each pass sees a fresh shuffle, so previously missed
    rows surface. Stop as soon as the site's reported total is reached, or when
    a pass adds nothing.
    """
    found = {}
    target = None

    for attempt in range(max_passes):
        before = len(found)
        page = 0
        while page < 80:
            h = get(SBIRGOV.format(page))
            if target is None:
                m = TOTAL_RE.search(h)
                if m:
                    target = int(m.group(1).replace(",", ""))
                    print(f"  sbir.gov reports {target} open topics", flush=True)
            if parse_page(h, found) == 0:
                break
            page += 1
            time.sleep(0.25)

        gained = len(found) - before
        print(f"  sbir.gov pass {attempt + 1}: {len(found)} unique (+{gained})", flush=True)
        if target and len(found) >= target:
            break
        if gained == 0 and attempt > 0:
            break

    if target and len(found) < target:
        print(f"  sbir.gov WARNING: got {len(found)} of {target} after {max_passes} passes",
              file=sys.stderr, flush=True)

    out = list(found.values())
    for t in out:
        t["_target"] = target
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
# Encodes one question: can two or three people with strong software and LLM
# skills produce the Phase I deliverable without a lab, a fab, a clearance, or
# a university partner? Weights are deliberately blunt - this ranks a shortlist
# for a human to read, it does not decide anything.

GOOD = {
    "agentic": 7, "large language model": 7, "llm": 7, "foundation model": 6,
    "benchmark": 6, "evaluation framework": 6, "test environment": 5, "testbed": 5,
    "machine learning": 4, "artificial intelligence": 4, "neural network": 3,
    "software": 4, "algorithm": 3, "simulation": 4, "digital twin": 4,
    "decision support": 5, "analytics": 3, "data fusion": 3, "knowledge graph": 5,
    "natural language": 5, "computer vision": 3, "anomaly detection": 4,
    "open source": 3, "api": 2, "dashboard": 3, "schema": 4, "workflow": 3,
}

BAD = {
    # things you cannot do from a laptop
    "wafer": -9, "semiconductor": -8, "transistor": -8, "photonic": -7,
    "fabricate": -6, "fabrication": -6, "cleanroom": -9, "lithography": -8,
    "laser": -6, "optics": -5, "antenna": -5, "propulsion": -8, "cryogenic": -8,
    "alloy": -7, "coating": -6, "additive manufacturing": -6, "materials": -4,
    "chemical synthesis": -8, "nanocrystalline": -8, "focal plane": -8,
    # wet lab / regulated
    "in vivo": -9, "clinical trial": -9, "cell culture": -9, "assay": -7,
    "animal model": -9, "toxicity": -7, "fda approval": -8, "therapeutic": -7,
    "preclinical": -8, "biomarker": -6,
    # access barriers
    "security clearance": -9, "classified": -7, "facility clearance": -9,
    "top secret": -9, "sci ": -6,
}

HARD_FLAGS = [
    ("needs_clearance", ("security clearance", "facility clearance", "top secret", "classified")),
    ("wet_lab", ("in vivo", "cell culture", "animal model", "preclinical", "clinical trial")),
    ("fabrication", ("wafer", "cleanroom", "lithography", "fabricate", "semiconductor")),
]


def score(topic):
    title = (topic.get("title") or "").lower()
    body = (topic.get("text") or "").lower()

    # NSF publishes a taxonomy you propose *into*, not an ask you answer, and
    # the entries carry little or no body text. Scoring those on body text just
    # produces a wall of zeros, so score the title - which is the whole signal -
    # and weight it up to sit on a comparable scale.
    if topic["kind"] == "category":
        pts, hits = 0, []
        for term, w in GOOD.items():
            if term in title:
                pts += w * 3
                hits.append(term)
            elif term in body:
                pts += w
                hits.append(term)
        for term, w in BAD.items():
            if term in title:
                pts += w
        topic["fit"] = max(0, min(100, int(pts * 2.4)))
        topic["fit_hits"] = sorted(set(hits))[:8]
        topic["flags"] = []
        topic["blocked"] = False
        return topic

    blob = f"{title} {body}"
    pts, hits = 0, []

    for term, w in GOOD.items():
        if term in blob:
            pts += w
            hits.append(term)
    for term, w in BAD.items():
        if term in blob:
            pts += w

    # A first-time applicant cannot bid Direct-to-Phase-II: it requires
    # documented Phase I-equivalent work already completed.
    if topic.get("phases") and not topic["has_phase1"]:
        pts -= 30
    # STTR obliges a research-institution partner at 30% of the work.
    if topic.get("program") == "STTR":
        pts -= 8

    flags = [name for name, terms in HARD_FLAGS if any(t in blob for t in terms)]

    topic["fit"] = max(0, min(100, int(pts * 2.2)))
    topic["fit_hits"] = sorted(set(hits))[:8]
    topic["flags"] = flags
    topic["blocked"] = bool(flags) or (bool(topic.get("phases")) and not topic["has_phase1"])
    return topic


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def dedupe(topics):
    """SBIR.gov mirrors DoD topics that DSIP already carries, in thinner form.

    Keep the DSIP copy - it has phases, TPOC emails and full text, none of
    which the mirror has. Matching is on a normalised title because the two
    systems do not share an identifier.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())[:60]
    dsip_titles = {norm(t["title"]) for t in topics if t["source"] == "dsip"}
    kept, dropped = [], 0
    for t in topics:
        if t["source"] == "sbirgov" and t["agency"] == "DOD" and norm(t["title"]) in dsip_titles:
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


def load_previous():
    path = os.path.join(OUT_DIR, "topics.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    previous = load_previous()
    sources, topics = {}, []

    for name, fn in (("dsip", fetch_dsip), ("sbirgov", fetch_sbirgov)):
        print(f"[{name}] fetching...", flush=True)
        try:
            got = fn()
            if not got:
                raise RuntimeError("source returned zero topics")
            expected = next((t.pop("_target", None) for t in got), None)
            for t in got:
                t.pop("_target", None)
            sources[name] = {
                "ok": True, "count": len(got), "error": None,
                "expected": expected,
                "complete": (expected is None or len(got) >= expected),
            }
            topics += got
            short = "" if sources[name]["complete"] else f" (SHORT of {expected})"
            print(f"[{name}] ok, {len(got)} topics{short}", flush=True)
        except Exception as e:  # noqa: BLE001
            carried = [t for t in previous if t.get("source") == name]
            topics += carried
            sources[name] = {
                "ok": False, "count": len(carried),
                "error": f"{type(e).__name__}: {e}"[:300],
                "carried_forward": True,
            }
            print(f"[{name}] FAILED ({e}); carried {len(carried)} stale topics", file=sys.stderr, flush=True)

    if not topics:
        print("no topics from any source and no cache - refusing to write", file=sys.stderr)
        return 1

    topics, dropped = dedupe(topics)
    sources["_dedupe"] = {"ok": True, "dropped_duplicates": dropped, "error": None}
    print(f"[dedupe] dropped {dropped} SBIR.gov mirrors of DSIP topics", flush=True)

    for t in topics:
        t["kind"] = "category" if t["agency"] == "NSF" else "solicitation"
        score(t)
    topics.sort(key=lambda t: (-t["fit"], t.get("close") or "9999"))

    agencies, kinds = {}, {}
    for t in topics:
        agencies[t["agency"]] = agencies.get(t["agency"], 0) + 1
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1

    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(topics),
        "sources": sources,
        "agencies": dict(sorted(agencies.items(), key=lambda kv: -kv[1])),
        "kinds": kinds,
        "registration_days": DEFAULT_REGISTRATION_DAYS,
        "degraded": any(not s["ok"] for s in sources.values()),
    }

    write_atomic(os.path.join(OUT_DIR, "topics.json"), topics)
    write_atomic(os.path.join(OUT_DIR, "meta.json"), meta)
    print(f"\nwrote {len(topics)} topics; degraded={meta['degraded']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
