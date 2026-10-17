#!/usr/bin/env python3
"""Import employer job-board URLs. Unrecognized sites are reported, NOT scraped."""
import argparse, csv, json
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent

def detect(url):
    url=url.strip()
    if not url or url.startswith('#'):return None
    if not url.startswith(('https://','http://')):url='https://'+url
    p=urlparse(url)
    host=(p.hostname or '').lower()
    path=[x for x in p.path.split('/') if x]
    if not path:return None
    if host in ('jobs.lever.co','jobs.eu.lever.co'):
        kind='lever';slug=path[0]
    elif host in ('job-boards.greenhouse.io','boards.greenhouse.io','boards-api.greenhouse.io'):
        if host=='boards-api.greenhouse.io' and path[:3]!=['v1','boards',path[2] if len(path)>2 else '']:return None
        kind='greenhouse';slug=path[2] if host=='boards-api.greenhouse.io' and len(path)>2 else path[0]
    elif host in ('jobs.ashbyhq.com','api.ashbyhq.com'):
        kind='ashby';slug=path[0] if host=='jobs.ashbyhq.com' else (path[-1] if 'job-board' in path else '')
    elif host in ('jobs.smartrecruiters.com','careers.smartrecruiters.com'):
        kind='smartrecruiters';slug=path[0]
    else:return None
    if not slug:return None
    return {'name':slug.replace('-',' ').title(),'type':kind,'slug':slug,'board_url':url}

def main():
    p=argparse.ArgumentParser(description='Add supported employer job board URLs from a text file (one per line)')
    p.add_argument('file',type=Path,help='TXT or CSV containing URLs (CSV header named url or board_url)')
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    if a.file.suffix.lower()=='.csv':
        with a.file.open(encoding='utf-8-sig',newline='') as f:
            rd=csv.DictReader(f)
            if rd.fieldnames and ('url' in rd.fieldnames or 'board_url' in rd.fieldnames):
                urls=[r.get('url') or r.get('board_url') or '' for r in rd]
            else:raise SystemExit('CSV needs a url or board_url header')
    else: urls=a.file.read_text(encoding='utf-8').splitlines()
    source_file=ROOT/'sources.json'
    existing=json.loads(source_file.read_text(encoding='utf-8'))
    keys={(x['type'],x['slug'].lower()) for x in existing}
    rejected=[];add=[]
    for url in urls:
        if not url.strip() or url.lstrip().startswith('#'):continue
        s=detect(url)
        if s is None:rejected.append((url,'unsupported site; manual adapter or permitted API needed'));continue
        key=(s['type'],s['slug'].lower())
        if key not in keys:keys.add(key); add.append(s)
    print(f'Sources already configured: {len(existing)} | newly recognized: {len(add)} | unsupported: {len(rejected)}')
    if not a.dry_run:
        source_file.write_text(json.dumps(existing+add,indent=2)+'\n',encoding='utf-8')
        if rejected:
            with (ROOT/'unsupported_boards.csv').open('w',newline='',encoding='utf-8') as f:
                w=csv.writer(f);w.writerow(['url','reason']);w.writerows(rejected)
    if add:print('New source handles: '+', '.join(f"{x['type']}:{x['slug']}" for x in add[:20]))
    if rejected:print('Unsupported sample: '+', '.join(x[0] for x in rejected[:5]))
if __name__=='__main__':main()
