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
    def test_experience_cap(self):
        self.assertFalse(radar.screening(job(description='A minimum of 5 years of experience required'),SETTINGS)[0])
    def test_remote(self):
        self.assertTrue(radar.nearby('Remote - USA',True,SETTINGS))
        self.assertFalse(radar.nearby('Seattle, WA',True,SETTINGS))
    def test_city_name_other_state_is_not_local(self):
        self.assertFalse(radar.nearby('Reading, PA',False,SETTINGS))
        self.assertFalse(radar.nearby('Cambridge, Ontario, Canada',False,SETTINGS))
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

class TestImport(unittest.TestCase):
    def test_detect_supported(self):
        self.assertEqual('lever',import_boards.detect('https://jobs.lever.co/pmaconsultants/foo')['type'])
        self.assertEqual('greenhouse',import_boards.detect('https://job-boards.greenhouse.io/klaviyojobs/jobs/12')['type'])
        self.assertEqual('smartrecruiters',import_boards.detect('https://jobs.smartrecruiters.com/Socotec/744')['type'])
        self.assertEqual('ashby',import_boards.detect('https://jobs.ashbyhq.com/wheel/jobs')['type'])
        self.assertIsNone(import_boards.detect('https://indeed.com/jobs?q=project'))

if __name__=='__main__':unittest.main()
