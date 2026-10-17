# Boston PM Radar | wide employer search (v2)

This project searches many **candidate** employer feeds across Greenhouse, Lever, Ashby and SmartRecruiters, plus Jobicy/Himalayas/Remotive **public remote-job sources**. It supports a configurable seven-day window (168 hours) and a target of **50** *date-evidenced* matching jobs, but does not invent jobs or publication times to meet that target.

## How to run

- GitHub Actions: **Actions → Boston PM Radar - Evening Scan → Run workflow** on `main`.
- Locally: `python3 -m unittest discover -s tests -v`, then `python3 radar.py` and open `out/index.html`.
- Reports: `out/jobs.csv`, `out/jobs.json`, `out/run_log.json`, `out/source_health.csv`, `out/employers_with_matches.csv`. Artifacts are downloadable from each Actions run.

No pip packages or keys required. The scanner uses documented public endpoints, with rate limits and failures exposed rather than bypassed. It never submits applications automatically.

## What "all employers" means

`employer_watchlist.json` contains a broader list of *potential* employers by industry, not evidence that these companies have live matching positions, provide immigration sponsorship, or have been scanned. `sources.json` contains **candidate** public recruiting feeds; some URL handles will be invalid, closed or unavailable. Only records in `out/source_health.csv` marked `ok=True` count as successfully checked. Remote aggregators discover hiring companies that may not be on the fixed list. Import additional supported ATS URLs using `python3 import_boards.py boards.txt`.

There is no complete, authoritative registry of every US employer. Workday, iCIMS, Oracle, government HR portals and private employer career sites are **not all integrated** here. Expansion is incremental and depends on permitted public APIs or specific authorized integrations. You may add more companies at any time.

## Job matching and dates

Filters target Greater Boston plus US-eligible remote, full-time, PMO/project controls/scheduling/program coordination and junior/assistant project management roles. It rejects obvious senior positions, over-3-year experience requirements and explicit restrictions on visa sponsorship where detectable. These are screening heuristics, not eligibility confirmation.

The dashboard **separates**:
- Greenhouse first-published dates (per current job post; may not prove the job was never advertised earlier)
- Ashby last-published and SmartRecruiters released dates (may include reposts)
- Jobicy/Himalayas/Remotive *aggregator* publication dates (not employer original dates)
- Greenhouse update-only dates (does not mean newly posted)
- Lever and other sources missing dates (cannot be treated as within seven days)
- Older jobs, shown separately, never counted toward the week's goal

**A weekly target is a goal, not an assurance.** If fewer than 50 qualifying date-evidenced jobs exist across reachable sources, `target_met` will be `false` and the actual verified count shown. Opening an application link before applying and confirming sponsorship/OPT eligibility remain essential.

## Coverage and API documentation

- Greenhouse: https://docs.greenhouse.io/job-board (public detail exposes `first_published`)
- Lever: https://github.com/lever/postings-api
- Ashby: https://developers.ashbyhq.com/docs/public-job-posting-api
- SmartRecruiters: https://developers.smartrecruiters.com/docs/endpoints
- Jobicy: https://jobicy.com/jobs-rss-feed
- Himalayas: https://himalayas.app/api (API attribution/terms apply)
- Remotive: https://remotive.com/remote-jobs/api

Some feeds surface application links via the provider itself; not every link is a direct employer career page. Site API terms, fair-use and public access requirements must always be observed.

## Source hygiene

Inspect `out/source_health.csv` after runs; invalid or blocked board handles must not be described as successfully scanned. We do not require every employer to be in the fixed list because aggregators may find additional hiring companies. Daily scans are configured for 23:00 UTC, which is ~7 PM ET during EDT and ~6 PM ET during EST. Scheduled runs may be delayed. GitHub's workflow commits report history to `main`, and downloads the report as an Actions artifact.