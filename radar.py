#!/usr/bin/env python3
"""Boston PM Radar: public ATS feeds -> evidence-aware screening -> SQLite -> dashboard.

Only GET requests to documented public posting APIs. No accounts, bypasses or auto-applications.
Python 3.10+, standard library only.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DB = HERE / "radar.sqlite3"
OUT = HERE / "out"
NOW = lambda: datetime.now(timezone.utc)

class StripHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data): self.parts.append(data)
    def get_text(self): return ' '.join(' '.join(self.parts).split())

def strip_html(s):
    if not s: return ''
    p = StripHTML(); p.feed(str(s)); return html.unescape(p.get_text())

def parse_date(value):
    if not value: return None
    try:
        if isinstance(value,(float,int)):
            v = float(value)
            return datetime.fromtimestamp(v/1000 if v>1e11 else v, tz=timezone.utc)
        v = str(value).strip().replace('Z','+00:00')
        d = datetime.fromisoformat(v)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError): return None

def iso(d): return d.isoformat(timespec='seconds') if d else None

def text_norm(s): return re.sub(r'[^a-z0-9]+','', str(s).lower())

def fingerprint(j):
    return hashlib.sha256('|'.join(text_norm(j[x]) for x in ('company','title','location')).encode()).hexdigest()[:24]

def get_json(url, delay):
    """Polite GET with mild backoff. API endpoints only, with network timeout."""
    for attempt in range(3):
        if delay: time.sleep(delay)
        req = Request(url, headers={'User-Agent':'BostonPMRadar/1.0 (personal job search; please respect rate limits)', 'Accept':'application/json'})
        try:
            with urlopen(req, timeout=16) as response:
                return json.loads(response.read(12_000_000).decode('utf-8'))
        except HTTPError as e:
            if e.code not in (429,500,502,503,504): raise
            if attempt==2: raise
            time.sleep(2**attempt*2)
        except (URLError, TimeoutError):
            if attempt==2: raise
            time.sleep(2**attempt)
    raise RuntimeError('No response')

def make_job(source, external_id, title, location, employment, description, url,
             evidence_date=None, evidence_kind='unknown', salary='', remote=False):
    j = dict(company=source['name'], source_type=source['type'], source_slug=source['slug'],
        external_id=str(external_id), title=(title or '').strip(), location=(location or '').strip(),
        employment=(employment or '').strip(), description=strip_html(description), url=url or source['board_url'],
        salary=str(salary or ''), evidence_at=iso(parse_date(evidence_date)), evidence_kind=evidence_kind,
        remote=bool(remote))
    j['key'] = fingerprint(j)
    return j

def lever_jobs(source, delay, max_pages):
    items=[]
    for page in range(max_pages):
        url=f"https://api.lever.co/v0/postings/{quote(source['slug'])}?"+urlencode({'mode':'json','limit':100,'skip':page*100})
        arr=get_json(url,delay)
        if not isinstance(arr,list): raise ValueError('Unexpected Lever JSON')
        for v in arr:
            cat=v.get('categories') or {}
            places=cat.get('allLocations') or [cat.get('location') or '']
            loc=' / '.join(dict.fromkeys(str(x) for x in places if x))
            salary=v.get('salaryRange') or {}
            sal=''
            if isinstance(salary,dict) and salary.get('min') is not None:
                sal=f"{salary.get('currency','USD')} {salary['min']:,.0f}–{salary.get('max',salary['min']):,.0f} / {salary.get('interval','year')}"
            desc=v.get('descriptionPlain') or v.get('description') or ''
            desc+=' '+ ' '.join(strip_html(x.get('content','')) for x in (v.get('lists') or []))
            # Official Lever public postings API does not expose an original-posted timestamp.
            items.append(make_job(source,v.get('id'),v.get('text'),loc,cat.get('commitment'),desc,
               v.get('applyUrl') or v.get('hostedUrl'),None,'unknown', sal,v.get('workplaceType')=='remote'))
        if len(arr)<100: break
    return items

def greenhouse_jobs(source,delay,max_pages):
    url=f"https://boards-api.greenhouse.io/v1/boards/{quote(source['slug'])}/jobs?content=true"
    obj=get_json(url,delay)
    result=[]
    for v in obj.get('jobs',[]):
        meta=v.get('metadata') or []
        emp=''
        for m in meta:
            if re.search(r'employment|job type',str(m.get('name','')),re.I):
                emp=str(m.get('value') or '')
        result.append(make_job(source,v.get('id'),v.get('title'),(v.get('location') or {}).get('name'),
             emp,v.get('content',''),v.get('absolute_url'),v.get('updated_at'),'updated_only'))
    return result

def ashby_jobs(source,delay,max_pages):
    url=f"https://api.ashbyhq.com/posting-api/job-board/{quote(source['slug'])}?includeCompensation=true"
    obj=get_json(url,delay)
    result=[]
    for v in obj.get('jobs',[]):
        if v.get('isListed') is False: continue
        locations=[v.get('location') or ''] + [x.get('location','') for x in v.get('secondaryLocations') or []]
        loc=' / '.join(dict.fromkeys(x for x in locations if x))
        comp=v.get('compensation') or {}
        sal=comp.get('scrapeableCompensationSalarySummary','') if isinstance(comp,dict) else ''
        result.append(make_job(source,v.get('id') or v.get('jobUrl'),v.get('title'),loc,v.get('employmentType'),
          v.get('descriptionPlain') or v.get('descriptionHtml',''),v.get('applyUrl') or v.get('jobUrl'),
          v.get('publishedAt'),'last_published',sal,v.get('isRemote') or v.get('workplaceType')=='Remote'))
    return result

def smartrecruiters_jobs(source,delay,max_pages):
    result=[]
    for page in range(max_pages):
        url=f"https://api.smartrecruiters.com/v1/companies/{quote(source['slug'])}/postings?"+urlencode({'offset':page*100,'limit':100})
        obj=get_json(url,delay)
        arr=obj.get('content') or []
        for v in arr:
            loc=v.get('location') or {}
            place=', '.join(x for x in [loc.get('city'),loc.get('region')] if x)
            title=v.get('name') or ''
            # Fetch full description only for jobs with plausible PM/coordination titles and localities.
            if not likely_title(title): continue
            if not nearby(place,loc.get('remote'), SETTINGS): continue
            details={}
            try:
                details=get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(source['slug'])}/postings/{quote(str(v['id']))}",delay)
            except (URLError, HTTPError, ValueError, TimeoutError) as err:
                print(f"  [detail skipped] {title}: {err}",file=sys.stderr)
            sections=((details.get('jobAd') or {}).get('sections') or {})
            description=' '.join(str(s.get('text','')) for s in sections.values() if isinstance(s,dict))
            job_url=details.get('applyUrl') or details.get('postingUrl') or f"https://jobs.smartrecruiters.com/{quote(source['slug'])}/{v['id']}"
            employment=(v.get('typeOfEmployment') or details.get('typeOfEmployment') or {}).get('label','')
            result.append(make_job(source,v.get('id'),title,place,employment,description,job_url,
               v.get('releasedDate') or details.get('releasedDate'),'released',remote=loc.get('remote',False)))
        if len(arr)<100:break
    return result

CONNECTORS = {'lever':lever_jobs,'greenhouse':greenhouse_jobs,'ashby':ashby_jobs,'smartrecruiters':smartrecruiters_jobs}

ROLE_PHRASES = (
  'project coordinator','project administrator','project assistant','assistant project manager',
  'associate project manager','junior project manager','project management coordinator',
  'project controls','project control','scheduler','scheduling specialist','planning engineer',
  'pmo analyst','pmo coordinator','project analyst','program coordinator','program administrator',
  'operations project','implementation coordinator','implementation specialist',
  'technical project manager','project engineer','project manager','operations coordinator'
)
SENIOR_TITLE = re.compile(r'\b(senior|sr\.?|principal|director|head of|vice president|vp|chief|staff|lead|manager ii|manager iii|manager iv)\b',re.I)
INTERNSHIP = re.compile(r'\b(internship|intern|co[- ]?op|apprentice|temporary|seasonal|contractor|contract)\b',re.I)
NO_SPONSOR = re.compile(
 r'(?:no|not|without|cannot|can\s*not|unable|won\W?t|does\s*not|do\s*not|will\s*not|is\s*not)\b.{0,70}\b(?:visa|immigration|h-?1b)\s*sponsor|'
 r'\b(?:visa|immigration)\s*sponsorship\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+provided)|'
 r'\b(?:us|u\.s\.)\s+citizenship\s+(?:is\s+)?required|'
 r'\b(?:only|must be)\s+(?:a\s+)?(?:us|u\.s\.)\s+citizen|'
 r'\bwithout\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+)?(?:visa|immigration)\s+sponsorship',re.I|re.S)

SETTINGS={}
def likely_title(title):
    t=title.lower()
    return any(p in t for p in ROLE_PHRASES) and not SENIOR_TITLE.search(t) and not INTERNSHIP.search(t)

def nearby(location,remote,settings):
    t=(location or '').lower()
    if any(re.search(r'\b'+re.escape(city.lower())+r'\b',t) for city in settings['local_cities']):
        # Do not confuse e.g. Cambridge, Ontario or Reading, PA with Greater Boston.
        if re.search(r'\b(united kingdom|england|canada|ontario|uk|germany|australia)\b',t):return False
        explicit_states=re.findall(r',\s*([a-z]{2})\b',t)
        if explicit_states and 'ma' not in explicit_states:return False
        return True
    if remote and settings.get('allow_us_remote',False):
        return (not t or bool(re.search(r'\b(united states|usa|u\.?s\.?|remote|nationwide)\b',t)))
    return False

def fulltime_type(t,title,description):
    s=' '.join([t or '',title or '']).lower().replace('-','').replace(' ','')
    if any(x in s for x in ['parttime','intern','coop','temporary','contract','freelance']):return False
    if 'fulltime' in s or 'permanent' in s: return True
    if t.strip():return False # Explicitly another type: don't assume full-time
    d=(description or '').lower()
    return bool(re.search(r'\bfull[- ]?time\b|\b40 hours per week\b',d))

def experience_years(description):
    d=(description or '').lower()
    patterns=[r'(?:minimum\s+of|at\s+least|minimum|requires?|must have|seeking|prefer(?:red)?)\s+(?:a\s+)?(\d{1,2})\s*\+?\s*years?',
              r'(\d{1,2})\s*\+\s*years?\s+of\s+(?:relevant|related|professional|project|construction|work|experience)',
              r'(\d{1,2})\s*(?:-|to)\s*\d{1,2}\s+years?\s+of\s+(?:relevant|related|professional|project|construction|work|experience)']
    found=[]
    for p in patterns:
        found += [int(m.group(1)) for m in re.finditer(p,d) if int(m.group(1))<=30]
    return min(found) if found else None

def screening(job,settings):
    if not likely_title(job['title']):return False,'unrelated/senior title',0
    if not nearby(job['location'],job['remote'],settings):return False,'outside metro',0
    if not fulltime_type(job['employment'],job['title'],job['description']):return False,'full-time not verified',0
    if settings.get('exclude_explicit_no_sponsorship') and NO_SPONSOR.search(job['description']):
        return False,'explicit visa restriction',0
    yrs=experience_years(job['description'])
    if yrs is not None and yrs>settings.get('max_experience_years',3):return False,f'requires {yrs}+ years',0
    t=job['title'].lower(); d=job['description'].lower()
    score=40
    for p,delta in [('junior',15),('project coordinator',18),('assistant project manager',14),('project controls',17),('scheduler',12),('pmo',12),('project administrator',13),('program coordinator',10),('implementation specialist',9)]:
        if p in t:score+=delta
    for p,delta in [('transit',6),('rail',7),('transportation',6),('primavera',9),('p6',9),('construction',5),('excel',4),('agile',3),('public sector',5),('schedule',4),('stakeholder',3)]:
        if p in d:score+=delta
    if yrs is not None:score+=max(-10,10-yrs*5)
    return True,'',min(100,score)

def recent_status(job,now,hours):
    ev=parse_date(job.get('evidence_at'))
    if ev and ev>now+timedelta(minutes=5):return 'date_problem'
    if ev and ev < now-timedelta(hours=hours):return 'older'
    if ev:
        if job['evidence_kind']=='created':return 'new_created' # currently no public adapter provides first-created date
        if job['evidence_kind'] in ('last_published','released'):return 'recent_publication'
        return 'recent_update_only'
    return 'date_unknown'

def setup_db(db):
    db.execute('''CREATE TABLE IF NOT EXISTS jobs(
      key TEXT PRIMARY KEY, company TEXT, source_type TEXT, title TEXT, location TEXT, employment TEXT,
      description TEXT, url TEXT, salary TEXT, evidence_at TEXT, evidence_kind TEXT, remote INTEGER,
      score INTEGER, first_seen TEXT, last_seen TEXT, match_reason TEXT, status TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS runs(
      run_at TEXT, source_count INTEGER, succeeded INTEGER, failed INTEGER, raw_count INTEGER,
      eligible_count INTEGER, summary TEXT)''')
    db.commit()

def save(db,j,run_at):
    data=(j['key'],j['company'],j['source_type'],j['title'],j['location'],j['employment'],j['description'],
      j['url'],j['salary'],j.get('evidence_at'),j['evidence_kind'],int(j['remote']),j['score'],run_at,
      run_at,'','')
    db.execute('''INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
     ON CONFLICT(key) DO UPDATE SET
       description=excluded.description, url=excluded.url, salary=excluded.salary,
       last_seen=excluded.last_seen, score=excluded.score,
       employment=excluded.employment, evidence_at=excluded.evidence_at,
       evidence_kind=excluded.evidence_kind''',data)

def query_current(db,now,settings):
    # Fresh listings should be shown, plus older/undated still-active listings as a separately labelled queue.
    cutoff=iso(now-timedelta(days=3))
    rows=[dict(r) for r in db.execute('SELECT * FROM jobs WHERE last_seen>=? ORDER BY score DESC',(cutoff,))]
    for j in rows:
        j['status']=recent_status(j,now,settings['freshness_hours'])
    return rows

def escape(s):return html.escape(str(s or ''),quote=True)

def generate_html(jobs,stats,now, settings):
    # No native network requests or JS external assets: works offline.
    data=[]
    for j in jobs:
        data.append({k:j.get(k) for k in ('company','title','location','salary','url','score','status','evidence_at','evidence_kind','first_seen','description')})
    groups=[('recent_publication','Published in last 24h (not necessarily original)'),
            ('recent_update_only','Updated in last 24h (original date unknown)'),
            ('date_unknown','Date unknown — review separately'),
            ('older','Older but relevant')]
    counters={g:sum(j['status']==g for j in jobs) for g,_ in groups}
    render=[]
    for status,label in groups:
        subset=sorted((j for j in jobs if j['status']==status),key=lambda j:-j['score'])
        if status=='date_unknown' and not settings['show_unknown_dates']:continue
        parts=[]
        for j in subset:
            ev = j['evidence_at'][:16].replace('T',' ')+' UTC' if j['evidence_at'] else 'No original date supplied by source'
            snippet=(j.get('description') or '')[:300].strip()
            fresh_discovery = parse_date(j.get('first_seen')) and parse_date(j['first_seen']) >= now-timedelta(hours=settings['freshness_hours'])
            parts.append(f'''<article class="job" data-new="{'yes' if fresh_discovery else 'no'}" data-text="{escape(' '.join([j['company'],j['title'],j['location']]).lower())}">
            <div class="head"><div><div class="company">{escape(j['company'])} · {escape(j['location'])}</div>
            <h3>{escape(j['title'])}</h3></div><span class="score">{int(j['score'])}/100 fit</span></div>
            <div class="meta"><span>{escape(j['salary'] or 'Salary not listed')}</span><span>{escape(ev)}</span>
            <span>Evidence: {escape(j['evidence_kind'].replace('_',' '))}</span></div>
            <p class="summary">{escape(snippet)}</p><a class="apply" href="{escape(j['url'])}" target="_blank" rel="noopener noreferrer">Open employer application ↗</a></article>''')
        render.append(f'''<section class="section" data-section="{status}"><h2>{escape(label)} <span class="count">{len(subset)}</span></h2>{''.join(parts) if parts else '<p class="empty">None from the latest run</p>'}</section>''')
    generated=now.strftime('%Y-%m-%d %H:%M UTC')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Boston PM Radar</title><style>
:root{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#e7edf7;background:#0b1020}
*{box-sizing:border-box}body{max-width:1100px;margin:auto;padding:26px 18px 80px}header{padding:30px 0 20px}h1{margin:0;font-size:clamp(32px,5vw,54px);letter-spacing:-.05em}
.sub{color:#a7b6d2;line-height:1.55}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:13px;margin:26px 0}
.stat{border:1px solid #26334b;background:#131d32;border-radius:16px;padding:20px}.stat strong{font-size:32px;display:block}.stat span{color:#aec0dc;font-size:13px}
.controls{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0 8px}input,select{padding:13px 15px;border:1px solid #2e3a52;border-radius:12px;background:#172239;color:#f5f7fb;min-width:240px;font:inherit}
.section{margin:32px 0}.section h2{font-size:21px;letter-spacing:-.025em}.count{font-size:13px;padding:4px 10px;border-radius:40px;color:#aac6ed;background:#202f4b}
.job{background:#131d32;border:1px solid #26334b;padding:21px 23px;border-radius:16px;margin:11px 0}.head{display:flex;justify-content:space-between;align-items:start;gap:12px}
h3{font-size:19px;margin:8px 0 10px}.company{font-size:13px;color:#97bdf4}.score{white-space:nowrap;color:#adf4b8;font-size:13px;font-weight:650}
.meta{display:flex;gap:14px;flex-wrap:wrap;color:#b2bfd4;font-size:12px}.summary{font-size:13px;color:#aebdd1;line-height:1.55;max-height:6em;overflow:hidden}
.apply{display:inline-block;margin-top:8px;color:#a5cbff;font-weight:600;text-decoration:none}.apply:hover{text-decoration:underline}
.note{border:1px solid #46516a;padding:15px;border-radius:13px;color:#c5cede;background:#121d31;line-height:1.5}.empty{color:#9ba8bc;font-size:14px}.footer{color:#8394b2;margin-top:36px;font-size:12px}
</style></head><body><header><div class="company">PERSONAL JOB SEARCH OPERATIONS</div><h1>Boston PM Radar</h1>
<p class="sub">Entry-level project management, project controls, PMO and coordination jobs, collected from public employer recruiting feeds. Updated '''+escape(generated)+'''.</p></header>
<div class="stats"><div class="stat"><strong>'''+str(stats['sources_ok'])+'''</strong><span>Sources checked successfully</span></div>
<div class="stat"><strong>'''+str(stats['eligible'])+'''</strong><span>Relevant full-time jobs</span></div><div class="stat"><strong>'''+str(counters['recent_publication'])+'''</strong><span>Recently published / released</span></div>
<div class="stat"><strong>'''+str(counters['date_unknown'])+'''</strong><span>Original date not supplied</span></div></div>
<p class="note"><strong>Freshness rule:</strong> Published / released does not prove original first-posted time; reposts remain possible. Greenhouse timestamps describe updates only; Lever's public API supplies no posting date. No job is claimed to be originally posted in the last 24 hours without independent evidence. <strong>Source errors:</strong> '''+str(stats['sources_failed'])+''' board(s) failed and were not silently counted as checked.</p>
<div class="controls"><input id="search" placeholder="Filter company, role or city..." aria-label="Filter jobs">
<select id="group" aria-label="Show job group"><option value="all">All date-evidence groups</option><option value="recent_publication">Recently published</option><option value="recent_update_only">Recent updates</option><option value="date_unknown">Unknown date</option><option value="older">Older relevant</option></select><label style="display:flex;align-items:center;gap:8px;color:#c9d7ee"><input type="checkbox" id="newonly" style="min-width:unset">New to MY radar only (not necessarily new online)</label></div>
'''+''.join(render)+'''<footer class="footer">Prepared by Boston PM Radar. Open each employer application to confirm the job is still live and verify work authorization requirements. The system never applies on your behalf.</footer>
<script>function filter(){let s=document.getElementById('search').value.toLowerCase(),v=document.getElementById('group').value,n=document.getElementById('newonly').checked;document.querySelectorAll('.section').forEach(x=>{let show=v==='all'||x.dataset.section===v;x.style.display=show?'':'none';x.querySelectorAll('.job').forEach(j=>j.style.display=(j.dataset.text.includes(s)&&(!n||j.dataset.new==='yes'))?'':'none')})}document.getElementById('search').addEventListener('input',filter);document.getElementById('group').addEventListener('change',filter);document.getElementById('newonly').addEventListener('change',filter);</script></body></html>'''

def write_reports(db,now,stats,settings):
    OUT.mkdir(parents=True,exist_ok=True)
    jobs=query_current(db,now,settings)
    (OUT/'index.html').write_text(generate_html(jobs,stats,now,settings),encoding='utf-8')
    cols=['company','title','location','salary','score','status','evidence_at','evidence_kind','first_seen','url']
    with (OUT/'jobs.csv').open('w',newline='',encoding='utf-8-sig') as file:
        w=csv.DictWriter(file,fieldnames=cols);w.writeheader();w.writerows({c:j.get(c,'') for c in cols} for j in jobs)
    (OUT/'jobs.json').write_text(json.dumps(jobs,indent=2,ensure_ascii=False),encoding='utf-8')
    (OUT/'run_log.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    return jobs

def run(args):
    global SETTINGS
    SETTINGS=json.loads((HERE/'settings.json').read_text(encoding='utf-8'))
    sources=json.loads((HERE/'sources.json').read_text(encoding='utf-8'))
    now=NOW(); stats={'run_utc':iso(now),'sources_total':len(sources),'sources_ok':0,
                     'sources_failed':0,'raw_jobs':0,'eligible':0,'errors':[]}
    conn=sqlite3.connect(DB);conn.row_factory=sqlite3.Row;setup_db(conn)
    for source in sources:
        print(f"Checking {source['name']} [{source['type']}]...",flush=True)
        try:
            if source['type'] not in CONNECTORS:raise ValueError('Unsupported adapter')
            items=CONNECTORS[source['type']](source, SETTINGS['request_delay_seconds'],SETTINGS['max_pages_per_board'])
            stats['raw_jobs']+=len(items)
            accepted=0
            for j in items:
                ok,why,score=screening(j,SETTINGS)
                if not ok:continue
                j['score']=score
                save(conn,j,iso(now))
                stats['eligible']+=1;accepted+=1
            conn.commit();stats['sources_ok']+=1
            print(f"  {len(items)} fetched, {accepted} matching")
        except (HTTPError, URLError, ValueError, TimeoutError, KeyError, RuntimeError) as e:
            stats['sources_failed']+=1;stats['errors'].append({'name':source['name'],'error':str(e)})
            print(f"  ERROR: {e}",file=sys.stderr)
    jobs=write_reports(conn,now,stats,SETTINGS)
    conn.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
       (iso(now),len(sources),stats['sources_ok'],stats['sources_failed'],stats['raw_jobs'],stats['eligible'],json.dumps(stats)))
    conn.commit();conn.close()
    print(f"\nDone. {len(jobs)} current matched jobs. Dashboard: {OUT/'index.html'}")
    if stats['sources_failed'] and args.fail_on_partial:sys.exit(2)

def cli():
    p=argparse.ArgumentParser(description='Boston PM Radar — public job feeds, no login or API key required')
    p.add_argument('--fail-on-partial',action='store_true',help='Exit nonzero when any configured board fails')
    args=p.parse_args()
    run(args)

if __name__=='__main__':cli()
