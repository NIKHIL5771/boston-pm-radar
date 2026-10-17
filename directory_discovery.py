"""Optional broad discovery from LastRound AI's CC BY 4.0 public ATS directory.

Last crawled July/August 2026, not a current-open-job or sponsorship database.
Attribution: LastRound AI, https://github.com/fyrosofttech/lastroundai-hiring-data
License: https://creativecommons.org/licenses/by/4.0/
"""
from __future__ import annotations
import csv
from datetime import datetime
import io
from urllib.request import Request,urlopen

DIRECTORY_URL = ('https://raw.githubusercontent.com/fyrosofttech/'
                 'lastroundai-hiring-data/main/ats-directory/'
                 'lastroundai-ats-company-directory-2026-08.csv')
ALLOWED={'greenhouse','lever','ashby'}

def normalize(value):
    return ''.join(c for c in str(value).casefold() if c.isalnum())

def parse_rows(raw_text):
    rows=[]
    for r in csv.DictReader(io.StringIO(raw_text)):
        t=(r.get('ats_vendor') or '').lower().strip()
        name=(r.get('company_name') or '').strip()
        slug=(r.get('board_slug') or '').strip()
        if t in ALLOWED and slug and name:
            rows.append({'type':t,'name':name,'slug':slug,'last_crawled':r.get('last_crawled','')})
    return rows

def fetch_directory():
    req=Request(DIRECTORY_URL,headers={'User-Agent':'BostonPMRadar/2.0 (private job-search research; public CC-BY dataset)','Accept':'text/csv'})
    with urlopen(req,timeout=20) as r:
        return parse_rows(r.read(8_000_000).decode('utf-8-sig'))

def source_obj(r):
    kind=r['type']; slug=r['slug']
    prefixes={'greenhouse':'https://job-boards.greenhouse.io/','lever':'https://jobs.lever.co/','ashby':'https://jobs.ashbyhq.com/'}
    return {'name':r['name'],'type':kind,'slug':slug,'board_url':prefixes[kind]+slug,
            'catalog_last_checked':r.get('last_crawled','')}

def choose_sources(rows,existing,watchlist,now,daily_extra=250):
    """Prioritize the personal employer watchlist and rotate broader discovery.

    No provider endpoint is called by this function. The live scanner confirms
    source reachability and logs failures separately.
    """
    known={(s['type'],s['slug'].casefold()) for s in existing}
    wanted={normalize(x['name']) for x in watchlist}
    priority=[];other=[]
    for r in rows:
        k=(r['type'],r['slug'].casefold())
        if k in known:continue
        known.add(k)
        (priority if normalize(r['name']) in wanted else other).append(source_obj(r))
    # Rotate by page (not job count), so each day explores new ATS boards.
    other.sort(key=lambda x:(x['type'],x['slug'].casefold()))
    daily_extra=max(0,int(daily_extra))
    if other and daily_extra:
        start=((now.date().toordinal()*daily_extra) % len(other))
        extra=(other+other)[start:start+min(daily_extra,len(other))]
    else:extra=[]
    return priority+extra,{'catalog_total':len(rows),'catalog_priority_selected':len(priority),
                            'catalog_rotation_selected':len(extra),'catalog_extra_pool':len(other)}

if __name__=='__main__':
    print('This module is imported by radar.py; run that scanner to build reports.')