#!/usr/bin/env python3
"""Boston PM Radar: public ATS feeds -> evidence-aware screening -> SQLite -> dashboard.

Only GET requests to documented public posting APIs. No accounts, bypasses or auto-applications.
Python 3.10+, standard library only.
"""
from __future__ import annotations
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import directory_discovery
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
                return json.loads(response.read(40_000_000).decode('utf-8'))
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
    """The public list has updated_at; each candidate's public detail has first_published.

    Only fetch details for Boston/US-remote plausible titles to reduce unnecessary
    calls; an unpublished or missing first_published stays in the evidence queue.
    """
    base=f"https://boards-api.greenhouse.io/v1/boards/{quote(source['slug'])}"
    obj=get_json(base+'/jobs?content=true',delay)
    result=[]
    for v in obj.get('jobs',[]):
        title=v.get('title') or ''
        location=(v.get('location') or {}).get('name') or ''
        is_remote=bool(re.search(r'\bremote\b',location,re.I))
        if not likely_title(title) or not nearby(location,is_remote,SETTINGS): continue
        detail={}
        try:
            detail=get_json(base+'/jobs/'+quote(str(v['id'])),delay)
        except (URLError,HTTPError,ValueError,TimeoutError,KeyError) as e:
            print(f"  [GH detail skipped] {title}: {e}",file=sys.stderr)
        meta=v.get('metadata') or []
        emp=''
        for m in meta:
            if re.search(r'employment|job type',str(m.get('name','')),re.I):
                emp=str(m.get('value') or '')
        desc=detail.get('content') or v.get('content','')
        first=detail.get('first_published')
        ev=first or v.get('updated_at')
        kind='created' if first else 'updated_only'
        result.append(make_job(source,v.get('id'),title,location,emp,desc,v.get('absolute_url'),ev,kind,remote=is_remote))
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
        url=f"https://api.smartrecruiters.com/v1/companies/{quote(source['slug'])}/postings?"+urlencode({'offset':page*100,'limit':100,'country':'us','region':'MA'})
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

def money_range(row, low='salaryMin', high='salaryMax', currency='salaryCurrency', period='salaryPeriod'):
    lo=row.get(low); hi=row.get(high)
    if lo is None and hi is None:return ''
    symbol=row.get(currency) or 'USD'
    return f"{symbol} {lo or hi:,.0f}–{hi or lo:,.0f} / {row.get(period) or 'yearly'}"

def public_feed_job(source, item, company, title, location, employment, description, url,
                    date, salary='', external_id='', remote=True):
    # Keep the real employer for relevance and cross-feed deduplication.
    employer_source=dict(source,name=company or source['name'])
    job=make_job(employer_source,external_id,title,location,employment,description,url,
                 date,'aggregator_publication',salary,remote)
    # Attribute the data source visibly (the company is still the real employer).
    job['source_type']=source['type']
    return job

def jobicy_jobs(source, delay, max_pages):
    """Jobicy public API: listings published on Jobicy in the past 7 days.
    This is NOT evidence of when the employer originally created the posting.
    """
    items=[];cursor=None
    for _ in range(min(max_pages,8)):
        args={'count':200,'geo':'usa'}
        if cursor:args['cursor']=cursor
        obj=get_json('https://jobicy.com/api/v2/remote-jobs?'+urlencode(args),delay)
        if obj.get('success') is False:raise ValueError(str(obj.get('error','Jobicy response error')))
        arr=obj.get('jobs') or []
        for v in arr:
            if not likely_title(v.get('jobTitle') or ''):continue
            geo=v.get('jobGeo') or ''
            if not nearby(geo,True,SETTINGS):continue
            item=public_feed_job(source,v,v.get('companyName'),v.get('jobTitle'),geo,
               ', '.join(v.get('jobType') or []), v.get('jobDescription') or v.get('jobExcerpt'),
               v.get('url'),v.get('pubDate'),money_range(v),v.get('id'))
            items.append(item)
        cursor=obj.get('nextCursor')
        if not cursor or not arr:break
    return items

def himalayas_jobs(source,delay,max_pages):
    """Targeted US-eligible remote jobs, via public Himalayas search API."""
    items=[];seen=set()
    for term in ('project coordinator','project manager','program coordinator','project controls','implementation specialist','operations coordinator'):
        for page in range(1,min(max_pages,3)+1):
            args={'q':term,'country':'US','employment_type':'Full Time','sort':'recent','page':page}
            obj=get_json('https://himalayas.app/jobs/api/search?'+urlencode(args),delay)
            arr=obj.get('jobs') or []
            if not isinstance(arr,list):raise ValueError('Unexpected Himalayas jobs API')
            if not arr:break
            for v in arr:
                if not likely_title(v.get('title') or ''):continue
                guid=str(v.get('guid') or v.get('applicationLink') or '')
                if guid in seen:continue
                seen.add(guid)
                places=v.get('locationRestrictions') or []
                allowed=not places or any(str(x).lower() in ('us','usa','united states','united states of america') for x in places)
                if not allowed:continue
                entry=public_feed_job(source,v,v.get('companyName'),v.get('title'),'Remote - USA',
                     v.get('employmentType'),v.get('description') or v.get('excerpt'),
                     v.get('applicationLink') or 'https://himalayas.app',v.get('pubDate'),
                     money_range(v,low='minSalary',high='maxSalary',currency='currency'),guid)
                items.append(entry)
    return items

def remotive_jobs(source,delay,max_pages):
    """One polite request to Remotive; source publication dates are not employer dates."""
    obj=get_json('https://remotive.com/api/remote-jobs?search=project&limit=900',delay)
    result=[]
    for v in obj.get('jobs') or []:
        if not likely_title(v.get('title') or ''):continue
        geo=v.get('candidate_required_location') or 'Worldwide'
        if not nearby(geo,True,SETTINGS):continue
        result.append(public_feed_job(source,v,v.get('company_name'),v.get('title'),geo,
              v.get('job_type'),v.get('description'),v.get('url'),v.get('publication_date'),
              v.get('salary') or '',v.get('id')))
    return result

CONNECTORS = {'lever':lever_jobs,'greenhouse':greenhouse_jobs,'ashby':ashby_jobs,'smartrecruiters':smartrecruiters_jobs,'jobicy':jobicy_jobs,'himalayas':himalayas_jobs,'remotive':remotive_jobs}

ROLE_PHRASES = (
  'project coordinator','project administrator','project assistant','assistant project manager',
  'associate project manager','junior project manager','project management coordinator',
  'project controls','project control','scheduler','scheduling specialist','planning engineer',
  'pmo analyst','pmo coordinator','project analyst','program coordinator','program administrator',
  'operations project','implementation coordinator','implementation specialist',
  'technical project manager','project engineer','project manager','operations coordinator',
  'program assistant','program specialist','project specialist','business operations associate',
  'project support specialist','delivery coordinator','construction coordinator',
  'project scheduling','project planning','implementation analyst','implementation associate',
  'program operations associate','program operations coordinator','operations analyst',
  'program assistant','program specialist','project specialist','business operations associate',
  'project support specialist','delivery coordinator','construction coordinator',
  'project scheduling','project planning','implementation analyst','implementation associate',
  'program operations associate','program operations coordinator','operations analyst'
)
SENIOR_TITLE = re.compile(r'\b(senior|sr\.?|principal|director|head of|vice president|vp|chief|staff|lead|manager ii|manager iii|manager iv|portfolio manager|senior associate)\b',re.I)
INTERNSHIP = re.compile(r'\b(internship|intern|co[- ]?op|apprentice|temporary|seasonal|contractor|contract)\b',re.I)
NO_SPONSOR = re.compile(
 r'(?:no|not|without|cannot|can\s*not|unable|won\W?t|does\s*not|do\s*not|will\s*not|is\s*not)\b.{0,70}\b(?:visa|immigration|h-?1b)\s*sponsor|'
 r'\b(?:visa|immigration)\s*sponsorship\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+provided)|'
 r'\b(?:us|u\.s\.)\s+citizenship\s+(?:is\s+)?required|'
 r'\b(?:only|must be)\s+(?:a\s+)?(?:us|u\.s\.)\s+citizen|'
 r'\bwithout\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+)?(?:visa|immigration)\s+sponsorship|'+
 r'\b(?:sponsorship\s+(?:now\s+or\s+in\s+the\s+future)\s+is\s+not\s+available)|'+
 r'\bwithout\s+sponsorship\s+(?:now|currently|in\s+the\s+future)' ,re.I|re.S)

SETTINGS={}
def likely_title(title):
    t=title.lower()
    return any(p in t for p in ROLE_PHRASES) and not SENIOR_TITLE.search(t) and not INTERNSHIP.search(t)

def nearby(location,remote,settings):
    """Accept an MA location among multiple locations, reject homonymous non-MA towns.

    Reject non-US-only remote jobs, but permit multiple eligible countries when
    the US is expressly one of them.
    """
    t=(location or '').lower()
    parts=re.split(r'\s*(?:/|;|\||\n)\s*',t)
    non_ma_state=re.compile(r',\s*([a-z]{2})\b')
    for part in parts:
        if not any(re.search(r'\b'+re.escape(city.lower())+r'\b',part) for city in settings['local_cities']):continue
        if re.search(r'\b(united kingdom|england|canada|ontario|uk|germany|australia)\b',part):continue
        states=non_ma_state.findall(part)
        if states and 'ma' not in states:continue
        return True
    if remote and settings.get('allow_us_remote',False):
        us=bool(re.search(r'\b(united states|usa|u\.?s\.?|nationwide|north america)\b',t))
        foreign_only=bool(re.search(r'\b(uk|england|canada|germany|australia|europe only|india only)\b',t))
        if foreign_only and not us:return False
        return not t or us or 'worldwide' in t or ('remote' in t and not foreign_only)
    return False

def fulltime_type(t,title,description):
    s=' '.join([t or '',title or '']).lower().replace('-','').replace(' ','')
    if any(x in s for x in ['parttime','intern','coop','temporary','contract','freelance']):return False
    if 'fulltime' in s or 'permanent' in s: return True
    if t.strip():return False # Explicitly another type: don't assume full-time
    d=(description or '').lower()
    return bool(re.search(r'\bfull[- ]?time\b|\b40 hours per week\b',d))

def experience_years(description):
    """Return minimum requested years, including 10–15/5+ variants.

    Only count years connected to experience; never treat company age as the
    applicant requirement. More than one candidate requirement means take
    the highest, not the lowest, to avoid 0+ in a benefit blurb masking 10+.
    """
    d=' '.join((description or '').lower().split())
    year_re = re.compile(r"(?<![\w])(?P<lo>\d{1,2})\s*(?:\+|[-–—]\s*\d{1,2}|\s+to\s+\d{1,2})?\s*(?:years?|yrs?)(?![\w])",re.I)
    found=[]
    for m in year_re.finditer(d):
        v=int(m.group('lo'))
        if v>25: continue
        before=d[max(0,m.start()-110):m.start()]
        after=d[m.end():m.end()+95]
        # A requirement must describe employment or subject-matter experience,
        # not employer age, product history, or warranty terms.
        context=before[-65:]+' '+after[:65]
        relevant=(re.search(r'\b(experience|background|working|project|construction|management|coordination|industry|professional|relevant|related|hands.on|scheduling|leadership|delivery)\b',context) or
                  re.search(r'\b(?:of|in)\s+(?:[a-z-]+\s+){0,4}experience\b',after))
        if relevant and not re.search(r'\b(company|founded|serving customers|history|established)\s+\w*\s*$',before[-35:]):
            found.append(v)
    return max(found) if found else None

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
        if job['evidence_kind']=='created':return 'new_created'
        if job['evidence_kind'] in ('last_published','released'):return 'recent_publication'
        if job['evidence_kind']=='aggregator_publication':return 'recent_aggregator'
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
    cutoff=iso(now-timedelta(days=settings.get('recent_seen_days',7)))
    rows=[dict(r) for r in db.execute('SELECT * FROM jobs WHERE last_seen>=? ORDER BY score DESC',(cutoff,))]
    for j in rows:
        j['status']=recent_status(j,now,settings['freshness_hours'])
    return rows

def escape(s):return html.escape(str(s or ''),quote=True)

def generate_html(jobs,stats,now, settings):
    """Offline report with actual jobs and a separately labelled employer prospecting directory."""
    # No native network requests or JS external assets: works offline.
    data=[]
    for j in jobs:
        data.append({k:j.get(k) for k in ('company','title','location','salary','url','score','status','evidence_at','evidence_kind','first_seen','last_seen','description')})
    days=settings['freshness_hours']//24
    groups=[('new_created',f'Employer first-published in last {days} days (Greenhouse)'),
            ('recent_publication',f'Employer published / released in last {days} days'),
            ('recent_aggregator',f'Aggregators published in last {days} days (not employer-original)'),
            ('recent_update_only',f'Employer updated in last {days} days (original date unknown)'),
            ('date_unknown','Posting date unknown — review separately'),
            ('older','Older than the last 7 days (archive)')]
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
            <span>Evidence: {escape(j['evidence_kind'].replace('_',' '))}</span><span>Source: {escape(j['source_type'])}</span><span>Last checked by radar: {escape(j.get('last_seen','')[:16].replace('T',' '))} UTC</span></div>
            <p class="summary">{escape(snippet)}</p><a class="apply" href="{escape(j['url'])}" target="_blank" rel="noopener noreferrer">View listing / application ↗</a></article>''')
        render.append(f'''<section class="section" data-section="{status}"><h2>{escape(label)} <span class="count">{len(subset)}</span></h2>{''.join(parts) if parts else '<p class="empty">None from the latest run</p>'}</section>''')
    # A watchlist is NOT evidence of a live opening or employer sponsorship.
    watch=[]
    watch_path=HERE/'employer_watchlist.json'
    if watch_path.exists():
        try:watch=json.loads(watch_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):watch=[]
    feeds={x['employer'].casefold():x for x in stats.get('source_results',[]) if x.get('provider') in ('lever','greenhouse','ashby','smartrecruiters')}
    from urllib.parse import quote_plus
    entries=[]
    for w in watch:
        name=w['name']; sector=w['sector']; feed=feeds.get(name.casefold())
        state=('Public feed checked' if feed and feed.get('ok') else
               'Candidate feed unavailable' if feed else 'No direct feed configured')
        link='https://www.google.com/search?q='+quote_plus(name+' careers Boston project coordinator')
        entries.append(f'<div class="employer" data-emp="{escape((name+" "+sector).lower())}"><span>{escape(name)}</span><small>{escape(sector)} · {escape(state)}</small><a href="{escape(link)}" target="_blank" rel="noopener noreferrer">Find careers ↗</a></div>')
    employer_markup=f"""<details class='employers' id='employer-directory'><summary>Employer universe: {len(watch)} potential companies (not verified hiring)</summary>
    <p class='sub'>These are prospective employers across major industries. A company on this list is NOT proof of an open job, permission to work, or sponsorship eligibility. A company catalogued here has not necessarily been scanned today. Only reachable job feeds and supported public aggregators are live-scanned. An additional public ATS directory expands coverage, but does not establish hiring or visa eligibility.</p>
    <input type='search' placeholder='Find employer or industry...' id='employer-search' aria-label='Search employer universe'>
    <div class='employer-grid'>{''.join(entries)}</div></details>"""
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
.employers{margin-top:38px;border:1px solid #364563;border-radius:16px;padding:22px}.employers summary{font-size:19px;font-weight:700;cursor:pointer}.employer-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:7px;margin-top:14px;max-height:620px;overflow-y:auto}.employer{border:1px solid #26334b;background:#131d32;padding:10px 12px;border-radius:10px;display:flex;gap:5px;flex-direction:column}.employer small{color:#aebdd1}.employer a{color:#97bdf4;font-size:12px}.note{border:1px solid #46516a;padding:15px;border-radius:13px;color:#c5cede;background:#121d31;line-height:1.5}.empty{color:#9ba8bc;font-size:14px}.footer{color:#8394b2;margin-top:36px;font-size:12px}
</style></head><body><header><div class="company">PERSONAL JOB SEARCH OPERATIONS</div><h1>Boston PM Radar</h1>
<p class="sub">Entry-level project management, project controls, PMO and coordination jobs, collected from public employer recruiting feeds. Updated '''+escape(generated)+'''.</p></header>
<div class="stats"><div class="stat"><strong>'''+str(stats['sources_ok'])+'''</strong><span>Sources checked successfully (of '''+str(stats.get('sources_total', stats['sources_ok']))+''')</span></div>
<div class="stat"><strong>'''+str(stats['eligible'])+'''</strong><span>Relevant full-time jobs across ALL dates</span></div><div class="stat"><strong>'''+str(counters.get('new_created',0)+counters['recent_publication'])+'''</strong><span>ATS first-published / released last 7d</span></div>
<div class="stat"><strong>'''+str(counters['recent_aggregator'])+'''</strong><span>Posted on aggregators in 7d</span></div><div class="stat"><strong>'''+str(counters['date_unknown'])+'''</strong><span>Original date not supplied</span></div></div>
<p class="note"><strong>External ATS company catalog: </strong>'''+str(stats.get('external_catalog_rows',0))+''' mapped employer boards (LastRound AI directory, July–August 2026, CC BY 4.0). Only sources counted as successfully checked were visited today. <a href='https://github.com/fyrosofttech/lastroundai-hiring-data' target='_blank' rel='noopener noreferrer'>Source and attribution ↗</a>. <strong>Weekly target: </strong>'''+str(counters.get('new_created',0)+counters['recent_publication']+counters['recent_aggregator'])+''' / '''+str(settings.get('target_weekly_jobs',50))+''' jobs with a recent publication signal (employer releases and aggregator dates separately). This is a target, not a guaranteed result. <strong>Freshness rule:</strong> 7-day window (168 hours). Published / released does not prove original creation: ATS timestamps record publication or release, not necessarily the first appearance. Aggregator timestamps refer to publication on Jobicy/Himalayas/Remotive, not the employer's original date. Greenhouse job details supply first_published when available; otherwise Greenhouse supplies update times only. Lever supplies no publication time. We never inflate the weekly count with older or unknown-date listings. <strong>Source errors:</strong> '''+str(stats['sources_failed'])+''' board(s) failed and were not silently counted as checked.</p>
<div class="controls"><input id="search" placeholder="Filter company, role or city..." aria-label="Filter jobs">
<select id="group" aria-label="Show job group"><option value="week" selected>Published this week (employer + aggregator)</option><option value="all">All date-evidence groups</option><option value="recent_publication">Recently published</option><option value="new_created">First-published this week</option><option value="recent_aggregator">Aggregator published this week</option><option value="recent_update_only">Recent updates</option><option value="date_unknown">Unknown date</option><option value="older">Older archive</option></select><label style="display:flex;align-items:center;gap:8px;color:#c9d7ee"><input type="checkbox" id="newonly" style="min-width:unset">New to MY radar only (not necessarily new online)</label></div>
'''+''.join(render)+employer_markup+'''<footer class="footer">Prepared by Boston PM Radar. Some roles may have been observed up to seven days earlier through rotating company feeds. Always open the listing to confirm it is still active and check OPT/sponsorship eligibility. The system never applies on your behalf.</footer>
<script>function filter(){let s=document.getElementById('search').value.toLowerCase(),v=document.getElementById('group').value,n=document.getElementById('newonly').checked;document.querySelectorAll('.section').forEach(x=>{let show=v==='all'||x.dataset.section===v||(v==='week'&&['new_created','recent_publication','recent_aggregator'].includes(x.dataset.section));x.style.display=show?'':'none';x.querySelectorAll('.job').forEach(j=>j.style.display=(j.dataset.text.includes(s)&&(!n||j.dataset.new==='yes'))?'':'none')})}document.getElementById('search').addEventListener('input',filter);document.getElementById('group').addEventListener('change',filter);document.getElementById('newonly').addEventListener('change',filter);const es=document.getElementById('employer-search');if(es){es.addEventListener('input',()=>{let q=es.value.toLowerCase();document.querySelectorAll('.employer').forEach(x=>x.style.display=x.dataset.emp.includes(q)?'':'none')})}filter();</script></body></html>'''

def write_reports(db,now,stats,settings):
    OUT.mkdir(parents=True,exist_ok=True)
    jobs=query_current(db,now,settings)
    statuses={'new_created','recent_publication','recent_aggregator','recent_update_only'}
    stats['last_week_posted_or_updated']=sum(j['status'] in statuses for j in jobs)
    stats['first_published_week_ats']=sum(j['status']=='new_created' for j in jobs)
    stats['published_week_ats']=sum(j['status']=='recent_publication' for j in jobs)
    stats['published_week_aggregator']=sum(j['status']=='recent_aggregator' for j in jobs)
    stats['updated_week_only']=sum(j['status']=='recent_update_only' for j in jobs)
    stats['target_weekly_jobs']=settings.get('target_weekly_jobs',50)
    stats['weekly_dated_posts']=stats['first_published_week_ats']+stats['published_week_ats']+stats['published_week_aggregator']
    stats['target_met']=stats['weekly_dated_posts']>=stats['target_weekly_jobs']
    (OUT/'index.html').write_text(generate_html(jobs,stats,now,settings),encoding='utf-8')
    cols=['company','title','location','salary','score','status','evidence_at','evidence_kind','first_seen','url']
    with (OUT/'jobs.csv').open('w',newline='',encoding='utf-8-sig') as file:
        w=csv.DictWriter(file,fieldnames=cols);w.writeheader();w.writerows({c:j.get(c,'') for c in cols} for j in jobs)
    (OUT/'jobs.json').write_text(json.dumps(jobs,indent=2,ensure_ascii=False),encoding='utf-8')
    known={}
    for j in jobs:
        name=j['company'];known[name]=known.get(name,0)+1
    with (OUT/'employers_with_matches.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(['employer','matching_roles']);w.writerows(sorted(known.items(),key=lambda kv:(-kv[1],kv[0])))
    (OUT/'run_log.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    with (OUT/'source_health.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['employer','provider','ok','raw','matching','error'],extrasaction='ignore');w.writeheader();w.writerows(stats.get('source_results',[]))
    return jobs

def run(args):
    global SETTINGS
    SETTINGS=json.loads((HERE/'settings.json').read_text(encoding='utf-8'))
    sources=json.loads((HERE/'sources.json').read_text(encoding='utf-8'))
    registry_file=HERE/'employer_watchlist.json'
    registry=json.loads(registry_file.read_text(encoding='utf-8')) if registry_file.exists() else []
    now=NOW(); stats={'run_utc':iso(now),'sources_total':len(sources),'sources_ok':0,
                     'sources_failed':0,'raw_jobs':0,'eligible':0,'errors':[],'source_results':[], 'employer_watchlist_size':len(registry),'external_catalog_rows':0, 'external_catalog_error':None}
    # Expand via a dated public company-to-ATS mapping, under CC BY 4.0.
    # Select only a bounded batch daily so the scan respects public API limits.
    catalog=[]
    try:
        catalog=directory_discovery.fetch_directory()
        more, discovery_stats=directory_discovery.choose_sources(catalog,sources,registry,now,
            SETTINGS.get('directory_daily_extra_boards',250))
        sources+=more
        stats.update(discovery_stats)
        stats['external_catalog_rows']=len(catalog)
        print(f"External ATS catalog: {len(catalog)} candidates; added {len(more)} to today's rotation",flush=True)
    except (HTTPError,URLError,TimeoutError,ValueError,UnicodeError,OSError) as e:
        stats['external_catalog_error']=str(e)
        print(f"External company-directory lookup failed, continuing configured feeds: {e}",file=sys.stderr)
    stats['sources_total']=len(sources)
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'ats_company_catalog.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['company_name','ats_vendor','board_slug','last_crawled'])
        w.writeheader()
        for v in catalog:
            w.writerow({'company_name':v['name'],'ats_vendor':v['type'],'board_slug':v['slug'],
                        'last_crawled':v.get('last_crawled','')})
    conn=sqlite3.connect(DB);conn.row_factory=sqlite3.Row;setup_db(conn)
    def fetch_one(source):
        if source['type'] not in CONNECTORS:raise ValueError('Unsupported adapter')
        return CONNECTORS[source['type']](source,SETTINGS['request_delay_seconds'],SETTINGS['max_pages_per_board'])
    # A modest pool distributes slow network reads, with a per-request delay.
    # All SQLite writes and screening stay on the main thread.
    workers=max(1,min(4,int(SETTINGS.get('max_concurrent_sources',4))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures={executor.submit(fetch_one,source):source for source in sources}
        for future in as_completed(futures):
            source=futures[future]
            try:
                items=future.result()
                stats['raw_jobs']+=len(items)
                accepted=0
                for j in items:
                    ok,why,score=screening(j,SETTINGS)
                    if not ok:continue
                    j['score']=score
                    save(conn,j,iso(now))
                    stats['eligible']+=1;accepted+=1
                conn.commit();stats['sources_ok']+=1
                stats['source_results'].append({'employer':source['name'],'provider':source['type'],'ok':True,'raw':len(items),'matching':accepted})
                print(f"  {source['name']} [{source['type']}]: {len(items)} fetched, {accepted} matching",flush=True)
            except (HTTPError,URLError,ValueError,TimeoutError,KeyError,RuntimeError) as e:
                stats['sources_failed']+=1;stats['errors'].append({'name':source['name'],'error':str(e)})
                stats['source_results'].append({'employer':source['name'],'provider':source['type'],'ok':False,'raw':0,'matching':0,'error':str(e)})
                print(f"  {source['name']} [{source['type']}]: ERROR {e}",file=sys.stderr)
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