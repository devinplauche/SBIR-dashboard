# SBIR Watch

An auto-refreshing dashboard over every open SBIR/STTR topic, scored for one
specific question: **could two or three people with strong software skills
actually deliver the Phase I?**

Federal R&D topic listings are large, unranked, and mostly irrelevant to a small
software team — the majority need a fab, a wet lab, a clearance, or a university
partner. This filters to what's buildable, then answers the question that
actually decides things: is the deadline still reachable from a standing start?

## What it does

- **Scores fit.** A keyword heuristic over topic text, weighted toward agentic
  AI, benchmarking, evaluation, simulation and decision-support work, and
  weighted hard against fabrication, wet-lab, and clearance requirements.
- **Computes reachability live.** SAM.gov registration takes a new entity about
  28 days, because DLA entity validation sits in the middle and cannot be
  rushed. Every deadline is measured against that lag plus writing time, in the
  browser, against today's date — so the page stays correct between refreshes.
  Toggle to **Registered** to drop the lag to zero.
- **Surfaces contact windows.** DoD pre-release is the only period in which you
  may email a topic author directly; once proposals open, contact is cut off
  entirely. Those windows are counted down separately from deadlines, with the
  author's address attached.
- **Separates the two kinds of listing.** DoD publishes specific asks you
  respond to. NSF publishes a taxonomy you propose *into*. Ranking them in one
  list would be misleading, so they get separate lanes.

## Layout

```
scripts/fetch.py       scraper + scoring. Standard library only.
docs/index.html        dashboard. Vanilla JS, no build step.
docs/report.html       the written analysis this came out of.
docs/data/*.json       generated. Do not hand-edit.
.github/workflows/     daily refresh + Pages deploy.
```

## Running it locally

```bash
python scripts/fetch.py && python -m http.server -d docs 8899
```

Then open <http://localhost:8899>. Serving it matters — opening `index.html`
over `file://` fails, because the page fetches its data as JSON.

## Sources, and how they fail

| Source | What it covers | How it's read |
| --- | --- | --- |
| [DSIP](https://www.dodsbirsttr.mil/topics-app/) | Department of War, including pre-release | JSON API |
| [SBIR.gov](https://www.sbir.gov/topics) | NSF, HHS, civilian agencies | HTML scrape |

Neither is a contract, and both will break eventually. SBIR.gov's own public API
was under maintenance when this was built, which is why the civilian side is
scraped from HTML.

Failures are isolated per source. If one breaks, its previous topics are carried
forward from the last good run, the dashboard shows that source as **STALE**
with the error text, and the run emits a CI warning. A refresh never silently
produces a thinner dataset. If every source fails and there's no cache, the
script exits non-zero rather than writing an empty file.

## Refresh schedule

Daily at 11:00 UTC, plus manual dispatch from the Actions tab. Data changes are
committed straight to `main`.

**GitHub Pages needs enabling once:** Settings → Pages → Source: *GitHub
Actions*. On a private repo, Pages also requires a paid plan — the data commit
runs either way, so the dataset stays current even if Pages is off, and you can
always serve `docs/` locally.

## The scoring is a shortlist, not a verdict

Fit scores rank topics for a human to read. They don't judge technical merit or
your odds, and a low score means "unlikely to suit a small software team," not
"bad topic." The weights are in `GOOD`, `BAD` and `HARD_FLAGS` in
`scripts/fetch.py` — they're deliberately blunt and worth tuning to your team.

Both sources mirror agency documents and are not authoritative. Confirm anything
you're about to act on against the agency's own solicitation.
