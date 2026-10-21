import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import radar
import directory_discovery
import import_boards

NOW=datetime(2026,10,16,22,0,tzinfo=timezone.utc)
SETTINGS={
 'freshness_hours':24,'max_experience_years':3,'exclude_explicit_no_sponsorship':True,
 'allow_us_remote':True,'show_unknown_dates':True,
 'local_cities':['Boston','Somerville','Waltham','Cambridge','Newton']
}

def job(**kw):
    s={'name':'PMA Consultants','type':'lever','slug':'pmaconsultants','board_url':'https://jobs.lever.co/pmaconsultants'}
    j=radar.make_job(s,'123','Junior Project Controls','Boston, MA','Full-Time',
      'Track construction schedule in Primavera P6 and Excel, 0 years experience required',
      'https://jobs.lever.co/pmaconsultants/123')
    j.update(kw)
    return j

class TestFilters(unittest.TestCase):
    def test_early_career_transit(self):
        okay,why,score=radar.screening(job(),SETTINGS)
        self.assertTrue(okay);self.assertGreaterEqual(score,60)
    def test_senior_rejected(self):
        self.assertFalse(radar.screening(job(title='Senior Project Manager'),SETTINGS)[0])
    def test_explicit_no_sponsorship(self):
        self.assertFalse(radar.screening(job(description='We cannot provide visa sponsorship for this role. Full-time.'),SETTINGS)[0])
    def test_vague_sponsorship_question_not_rejected(self):
        self.assertTrue(radar.screening(job(description='Will you require sponsorship now or in the future? Full time.'),SETTINGS)[0])
    def test_en_dash_10_to_15_years_rejected(self):
        j=job(description='Required: 10–15 years of experience in project controls and scheduling')
        self.assertEqual(radar.experience_years(j['description']),10)
        self.assertFalse(radar.screening(j,SETTINGS)[0])
    def test_two_to_four_years_allowed(self):
        self.assertEqual(2,radar.experience_years('Qualifications: 2–4 years of related project coordination experience'))
    def test_aggregator_week_publication(self):
        j=job(evidence_at=radar.iso(NOW-timedelta(days=5)),evidence_kind='aggregator_publication')
        self.assertEqual('recent_aggregator',radar.recent_status(j,NOW,168))
        self.assertEqual('older',radar.recent_status(j,NOW,24))
    def test_experience_cap(self):
        self.assertFalse(radar.screening(job(description='A minimum of 5 years of experience required'),SETTINGS)[0])
    def test_mixed_us_remote_eligibility(self):
        self.assertTrue(radar.nearby('USA / Canada',True,SETTINGS))
        self.assertTrue(radar.nearby('Worldwide',True,SETTINGS))
        self.assertFalse(radar.nearby('Canada only',True,SETTINGS))
    def test_multiple_job_locations(self):
        self.assertTrue(radar.nearby('Boston, MA / San Francisco, CA',False,SETTINGS))
        self.assertFalse(radar.nearby('Cambridge, Ontario / Reading, PA',False,SETTINGS))
    def test_greater_boston_locations_without_nationwide_remote(self):
        metro={**SETTINGS,
          'local_cities':['Boston','Marlborough','Framingham','Rockland','Waltham'],
          'allow_us_remote':False}
        for place in ('Boston, MA','Marlborough, MA','Framingham, Massachusetts','Rockland, MA','Waltham, MA'):
            self.assertTrue(radar.nearby(place,False,metro),place)
        for place in ('Rockland, ME','Framingham, NY','Remote - USA','Seattle, WA'):
            self.assertFalse(radar.nearby(place,True,metro),place)

    def test_saved_results_obey_updated_location_settings(self):
        metro={**SETTINGS,'local_cities':['Boston','Framingham','Rockland'],'allow_us_remote':False}
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            radar.setup_db(db)
            for name,loc in [('boston','Boston, MA'),('framingham','Framingham, MA'),
                             ('rockland','Rockland, MA'),('remote','Remote - USA'),
                             ('oldcity','Waltham, MA')]:
                candidate=job(key=name,location=loc,remote=name=='remote')
                candidate['score']=80
                radar.save(db,candidate,radar.iso(NOW))
            rows=radar.query_current(db,NOW,metro)
            self.assertEqual({j['key'] for j in rows},{'boston','framingham','rockland'})

    def test_old_listing_from_last_scan_not_shown_today(self):
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            radar.setup_db(db)
            candidate=job(key='stale',location='Boston, MA')
            candidate['score']=80
            radar.save(db,candidate,radar.iso(NOW-timedelta(days=1)))
            self.assertEqual([],radar.query_current(db,NOW,SETTINGS))
            radar.save(db,candidate,radar.iso(NOW))
            self.assertEqual(1,len(radar.query_current(db,NOW,SETTINGS)))

    def test_http_404_is_expired(self):
        from urllib.error import HTTPError
        error=HTTPError('https://jobs.example.com/expired',404,'Not found',None,None)
        with patch.object(radar,'urlopen',side_effect=error):
            result,reason=radar.check_application_link('https://jobs.example.com/expired')
        self.assertEqual(result,'expired')

    def test_http_403_is_not_assumed_expired(self):
        from urllib.error import HTTPError
        error=HTTPError('https://jobs.example.com/blocked',403,'Forbidden',None,None)
        with patch.object(radar,'urlopen',side_effect=error):
            result,reason=radar.check_application_link('https://jobs.example.com/blocked')
        self.assertEqual(result,'unverified')

    def test_expired_notice_on_http_200(self):
        from io import BytesIO
        class Response:
            status=200
            headers={'Content-Type':'text/html; charset=utf-8'}
            def __init__(self,body):
                self.body=BytesIO(body.encode())
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,n):return self.body.read(n)
        with patch.object(radar,'urlopen',return_value=Response('<html>This position is no longer accepting applications</html>')):
            status,_=radar.check_application_link('https://jobs.example.com/expired')
        self.assertEqual(status,'expired')
        with patch.object(radar,'urlopen',return_value=Response('<html>Submit your application</html>')):
            status,_=radar.check_application_link('https://jobs.example.com/current')
        self.assertEqual(status,'accessible')

    def test_expired_rows_removed_from_live_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(radar,'OUT',Path(tmp)):
                a=dict(job(key='one'),status='date_unknown')
                b=dict(job(key='two'),status='date_unknown')
                a['url']='https://jobs.example.com/expired'
                b['url']='https://jobs.example.com/blocked'
                checker=lambda url: ('expired','HTTP 410') if url.endswith('/expired') else ('unverified','HTTP 403')
                with patch.object(radar,'check_application_link',side_effect=checker):
                    stats={}
                    result=radar.verify_current_links([a,b],NOW,stats,SETTINGS)
                self.assertEqual(1,len(result))
                self.assertEqual('unverified',result[0]['application_status'])
                self.assertEqual(stats['expired_links_removed'],1)
                self.assertTrue((Path(tmp)/'expired_jobs.csv').exists())

    def test_first_published_in_seven_days(self):
        j=job(evidence_at=radar.iso(NOW-timedelta(days=3)),evidence_kind='created')
        self.assertEqual(radar.recent_status(j,NOW,168),'new_created')
        self.assertEqual(radar.recent_status(j,NOW,24),'older')
    def test_older_or_undated_not_counted_as_weekly(self):
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            radar.setup_db(db)
            old=job(evidence_at=radar.iso(NOW-timedelta(days=15)),evidence_kind='released',external_id='x')
            old['score']=70
            radar.save(db,old,radar.iso(NOW))
            report=radar.query_current(db,NOW,{**SETTINGS,'freshness_hours':168})
            self.assertEqual(report[0]['status'],'older')
    def test_remote(self):
        self.assertTrue(radar.nearby('Remote - USA',True,SETTINGS))
        self.assertFalse(radar.nearby('Seattle, WA',True,SETTINGS))
    def test_city_name_other_state_is_not_local(self):
        self.assertFalse(radar.nearby('Reading, PA',False,SETTINGS))
        self.assertFalse(radar.nearby('Cambridge, Ontario, Canada',False,SETTINGS))
    def test_unknown_employment_review_mode_flagged_not_confirmed(self):
        candidate=job(employment='',description='Coordinate construction projects and Excel reporting')
        okay,why,score=radar.screening(candidate,{**SETTINGS,'include_employment_unverified_review':True})
        self.assertTrue(okay)
        self.assertIn('Unverified',candidate['employment'])
        self.assertLess(score,100)
    def test_part_time_description_not_passed_as_unknown(self):
        candidate=job(employment='',description='Part-time project coordinator, 20 hours a week')
        self.assertFalse(radar.screening(candidate,{**SETTINGS,'include_employment_unverified_review':True})[0])
    def test_missing_employment_is_not_assumed_fulltime(self):
        self.assertFalse(radar.screening(job(employment='',description='We have many opportunities'),SETTINGS)[0])
    def test_internship_excluded(self):
        self.assertFalse(radar.screening(job(title='Project Coordinator Intern'),SETTINGS)[0])
    def test_ashby_republication_not_original(self):
        j=job(evidence_at=radar.iso(NOW-timedelta(hours=2)), evidence_kind='last_published')
        self.assertEqual('recent_publication',radar.recent_status(j,NOW,24))
    def test_greenhouse_updated_not_original(self):
        j=job(evidence_at=radar.iso(NOW-timedelta(hours=2)), evidence_kind='updated_only')
        self.assertEqual('recent_update_only',radar.recent_status(j,NOW,24))
    def test_old_and_unknown_dates(self):
        self.assertEqual('older',radar.recent_status(job(evidence_at=radar.iso(NOW-timedelta(days=5)),evidence_kind='released'),NOW,24))
        self.assertEqual('date_unknown',radar.recent_status(job(),NOW,24))
    def test_first_seen_preserved_by_sqlite_upsert(self):
        with sqlite3.connect(':memory:') as db:
            radar.setup_db(db)
            j=job();j['score']=85
            radar.save(db,j,'2026-10-14T12:00:00+00:00')
            radar.save(db,j,'2026-10-16T12:00:00+00:00')
            first_seen,last_seen=db.execute('select first_seen,last_seen from jobs').fetchone()
            self.assertEqual(first_seen,'2026-10-14T12:00:00+00:00')
            self.assertEqual(last_seen,'2026-10-16T12:00:00+00:00')

class TestAdapters(unittest.TestCase):
    def test_lever_job_is_undated_and_fulltime(self):
        obj=[{'id':'p1','text':'Junior Project Controls','categories':{'location':'Boston, MA','commitment':'Full-Time'},
              'descriptionPlain':'Excel P6','applyUrl':'https://jobs.lever.co/example/p1',
              'salaryRange':{'min':71038,'max':83470,'currency':'USD','interval':'year'}}]
        with patch.object(radar,'get_json',return_value=obj):
            r=radar.lever_jobs({'name':'PMA','type':'lever','slug':'pmaconsultants','board_url':'https://jobs.lever.co/pmaconsultants'},0,1)
        self.assertEqual(r[0]['evidence_kind'],'unknown')
        self.assertIn('71,038',r[0]['salary'])
    def test_greenhouse_first_published_detail(self):
        source={'name':'Example','type':'greenhouse','slug':'example','board_url':'https://job-boards.greenhouse.io/example'}
        listing={'jobs':[{'id':7,'title':'Project Coordinator','location':{'name':'Boston, MA'},'updated_at':'2026-10-16T19:00:00Z','content':'Full-time project coordination'}]}
        detail={'first_published':'2026-10-13T10:00:00Z','content':'Full-time project coordination'}
        with patch.object(radar,'get_json',side_effect=[listing,detail]):
            with patch.object(radar,'SETTINGS',SETTINGS):
                out=radar.greenhouse_jobs(source,0,1)
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['evidence_kind'],'created')
        self.assertIn('2026-10-13',out[0]['evidence_at'])
    def test_smartrecruiters_details(self):
        src={'name':'SOCOTEC','type':'smartrecruiters','slug':'Socotec','board_url':'https://jobs.smartrecruiters.com/Socotec'}
        items={'content':[{'id':'42','name':'Junior Scheduler','releasedDate':'2026-10-16T19:00:00Z',
             'location':{'city':'Boston','region':'MA','country':'us','remote':False},
             'typeOfEmployment':{'label':'Full-time'}}]}
        detail={'applyUrl':'https://jobs.smartrecruiters.com/Socotec/42','jobAd':{'sections':{'qualifications':{'text':'0+ years of experience'}}}}
        with patch.object(radar,'get_json',side_effect=[items,detail]):
            with patch.object(radar,'SETTINGS',SETTINGS):
                out=radar.smartrecruiters_jobs(src,0,1)
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['evidence_kind'],'released')
        self.assertIn('0+ years',out[0]['description'])
    def test_report_contains_no_guaranteed_original_fresh_claim(self):
        j=job();j['score']=85
        out=radar.generate_html([dict(j,status='date_unknown',first_seen='2026-10-16')],{'sources_ok':1,'eligible':1,'sources_failed':0},NOW,SETTINGS)
        self.assertIn('does not prove original',out)
        self.assertIn('PMA Consultants',out)

class TestDirectoryDiscovery(unittest.TestCase):
    def test_dated_directory_priority_and_rotation(self):
        raw='ats_vendor,company_name,board_slug,last_crawled\nashby,PMA Consultants,pma,2026-07-15\nlever,Other Company,other,2026-07-16\ngreenhouse,Zero,zero,2026-08-01\n'
        rows=directory_discovery.parse_rows(raw)
        self.assertEqual(len(rows),3)
        selected,stats=directory_discovery.choose_sources(rows,[],[{'name':'PMA Consultants'}],NOW,1)
        self.assertEqual(len(selected),2)
        self.assertEqual(selected[0]['name'],'PMA Consultants')
        self.assertEqual(stats['catalog_total'],3)
    def test_duplicate_existing_directory_board_ignored(self):
        rows=[{'type':'ashby','name':'PMA Consultants','slug':'pma','last_crawled':'2026-07-15'}]
        selected,stats=directory_discovery.choose_sources(rows,[{'type':'ashby','slug':'pma'}],[{'name':'PMA Consultants'}],NOW,10)
        self.assertFalse(selected)
class TestImport(unittest.TestCase):
    def test_detect_supported(self):
        self.assertEqual('lever',import_boards.detect('https://jobs.lever.co/pmaconsultants/foo')['type'])
        self.assertEqual('greenhouse',import_boards.detect('https://job-boards.greenhouse.io/klaviyojobs/jobs/12')['type'])
        self.assertEqual('smartrecruiters',import_boards.detect('https://jobs.smartrecruiters.com/Socotec/744')['type'])
        self.assertEqual('ashby',import_boards.detect('https://jobs.ashbyhq.com/wheel/jobs')['type'])
        self.assertIsNone(import_boards.detect('https://indeed.com/jobs?q=project'))

if __name__=='__main__':unittest.main()