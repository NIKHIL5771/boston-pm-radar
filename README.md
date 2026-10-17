# Boston PM Radar — one dashboard, not 706 browser tabs

An **operational starter project** for sourcing entry-level project management jobs in Greater Boston. It fetches public JSON feeds exposed by certain applicant-tracking systems, filters/ranks against your background, deduplicates entries in SQLite, and writes an offline HTML dashboard plus CSV/JSON files.

**What it really covers today:** Eight configured public employer job boards across four ATS providers. It is **not** a completed crawl of 706 job boards. It can accept a larger list of known supported employer feeds through `import_boards.py`; unsupported sites remain visibly unprocessed, not silently skipped.

## Start in two commands

Use Python 3.10 or newer. No pip packages or API keys required.

```bash
cd Boston_PM_Radar_Starter
python3 -m unittest discover -s tests -v
python3 radar.py
```

Then open **`out/index.html`** in a browser. Use the search field, evidence-category selector and **New to MY radar** checkbox to avoid repeatedly scrolling previously discovered jobs. This last filter uses the tool's first-seen date and **does not assert original posting freshness**. Also see `out/jobs.csv` for a sortable spreadsheet-style job list.

On Windows, replace `python3` with `py -3`. Internet access is needed to make the first scan. An initial run may have zero matches if none of these employers have suitable openings.

## What it filters

- Greater Boston (you can edit cities in `settings.json`), plus US remote jobs if allowed
- Entry-level or early-career PM, assistant PM, project coordinator, program coordinator, project controls, project administrator, scheduling and junior implementation openings
- Full-time only. **Unknown employment type is conservatively excluded** unless the description explicitly says full-time
- Roles explicitly requiring more than three years *when detectable* are excluded; this is heuristic, not a hiring-eligibility guarantee
- Senior titles, internships, co-ops and contract roles excluded
- Explicit "no immigration sponsorship" and citizenship requirements filtered when detected; **absence of such a statement does not imply sponsorship availability**
- Scores reflect skill overlap with MBTA transit, construction, P6, Excel, scheduling, public sector and project controls work

## Strict freshness semantics

**Do not interpret first seen today or updated today as originally posted today.** The dashboard separates:

1. `Published/released within 24h` — official feed publication timestamp in the window, **still not proof this isn't a repost** (SmartRecruiters `releasedDate`, Ashby `publishedAt` (last published))
2. `Updated within 24h` — Greenhouse `updated_at`; **not an original posting date**
3. `Unknown posting date` — Lever public postings API currently does not expose a created/published timestamp
4. `Older relevant` — dated posts older than the freshness window, in a separate section

Jobs with a verified **original** first-publication time would be eligible for a stricter category only after an independent first-posted check. This starter deliberately **does not mark any result as guaranteed originally posted in the past 24h** without that check. `first_seen` measures the first scan by *this tool*, not the date the employer originally published the job.

`out/run_log.json` reports actual source successes and failures, so a blocked feed is never counted as scanned. Repeated runs preserve first-seen history in `radar.sqlite3`.

## Add hundreds of compatible employer boards

1. Gather a list of **employer job-board URLs**, one per line, into `boards.txt`, or CSV with a `url` header.
2. Run `python3 import_boards.py boards.txt`.
3. Read any `unsupported_boards.csv`. These require separate permitted connectors; they are *not* automatically scraped.
4. Run `python3 radar.py` again.

Recognized URL formats:

```
https://jobs.lever.co/pmaconsultants
https://job-boards.greenhouse.io/klaviyojobs
https://boards.greenhouse.io/companyhandle
https://jobs.smartrecruiters.com/Socotec
https://jobs.ashbyhq.com/wheel
```

Sample import: `python3 import_boards.py sample_board_urls.txt --dry-run`.

Names imported from URL tokens can be renamed in `sources.json` for readability. Editing or appending sources is safe; invalid/unavailable boards are flagged in logs. Respect each service's published access terms and rate limits. Do not crawl authentication walls, bypass CAPTCHAs, or submit automated applications. Adding hundreds of boards may need a longer runner, stricter quotas, and provider approval.

## Schedule it each evening with GitHub Actions

The included `.github/workflows/evening-radar.yml` runs daily at **23:00 UTC**, roughly **7 PM ET in summer / 6 PM ET in winter**, because GitHub Actions uses UTC schedules. Scheduled runs can be delayed.

1. Create a **private GitHub repository** (if available to your plan), and upload the contents of this folder, including `.github/workflows/evening-radar.yml`.
2. Under the repository's **Settings → Actions → General**, allow workflow permissions for **Read and write** (if your organization/policies allow it).
3. Open the **Actions** tab and manually run **Boston PM Radar - Evening Scan** once using `Run workflow`.
4. After the job finishes, open the run's **Artifacts** and download the `boston-pm-radar-...` file, then open `index.html`. The history and reports are also committed to your repo for next-day deduplication.
5. Keep the repository **private** if you later add personal resume data or application tracking.

GitHub can throttle or delay cron jobs, and free usage minutes / artifact retention vary by plan. This workflow *does not send email automatically*. Your existing ChatGPT evening reminder/search task is separate and remains active; it does not run or operate this project automatically.

**No deployment has been performed for you.** Uploading to a repo and enabling Actions are setup steps you control.

## Project structure

- `radar.py` — public API clients, filters, SQLite state, HTML + CSV reports
- `sources.json` — editable employer board list (eight starter sources)
- `settings.json` — matching, location, sponsorship and freshness settings
- `import_boards.py` — bulk recognizer for supported board URLs
- `.github/workflows/evening-radar.yml` — scheduled GitHub Actions scan
- `tests/` — offline tests using simulated API responses
- `out/index.html` — initial dashboard shell; replaced after a real scan

## Supported public API references

- Lever: https://github.com/lever/postings-api
- Greenhouse: https://docs.greenhouse.io/job-board
- Ashby: https://developers.ashbyhq.com/docs/public-job-posting-api
- SmartRecruiters: https://developers.smartrecruiters.com/docs/endpoints

## Reality checks

- It can't replace LinkedIn/Indeed or 706 separate sites by itself; those require allowed third-party aggregators, permission or access to job data, and more adapters.
- This system does **not** automatically verify future H-1B sponsorship or STEM OPT eligibility. Employer policies must be checked directly.
- Dashboard time labels reflect upstream provider metadata, not proof of a listing's original publication on the internet.
- Job listings can close at any time. Always open the employer application link before spending time tailoring a resume.
