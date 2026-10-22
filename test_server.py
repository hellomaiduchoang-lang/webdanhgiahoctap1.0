"""Automated regression tests for ClassTrack's core student/year/security flows."""
import http.cookiejar
import base64
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from urllib.error import HTTPError

os.environ['CLASSTRACK_DB'] = str(Path(tempfile.gettempdir()) / 'classtrack-test-import.sqlite3')
import server


class ClassTrackFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.AppServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.STORE = server.Store(Path(self.tmp.name) / 'test.sqlite3')
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''
        self.admin = server.STORE.bootstrap_super_admin('admin01','admin@example.test','Quản trị','A-secure-test-pass')
        code, data = self.call('/api/login', {'username':'admin01','password':'A-secure-test-pass'}, mutate=False)
        self.assertEqual(code, 200, data)
        self.csrf = data['csrfToken']
        self.y1 = server.STORE.create_year('2026–2027', '2026-09-01', '2027-05-31', 'ACTIVE', self.admin)
        self.c1 = server.STORE.create_class(self.y1, '10A1', self.admin)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, path, data=None, method='POST', mutate=True):
        headers = {'Content-Type':'application/json'}
        if mutate and self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        req = Request(self.base + path, data=json.dumps(data or {}).encode() if data is not None else None, headers=headers, method=method)
        try:
            with self.opener.open(req) as res:
                return res.status, json.loads(res.read() or b'{}')
        except HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def other_client(self, username, password):
        opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        req=Request(self.base+'/api/login',data=json.dumps({'username':username,'password':password}).encode(),headers={'Content-Type':'application/json'},method='POST')
        with opener.open(req) as res: result=json.loads(res.read())
        return opener,result['csrfToken']

    def other_call(self, opener, csrf, path, data=None, method='POST'):
        headers={'Content-Type':'application/json','X-CSRF-Token':csrf}
        req=Request(self.base+path,data=json.dumps(data or {}).encode() if data is not None else None,headers=headers,method=method)
        try:
            with opener.open(req) as res:return res.status,json.loads(res.read() or b'{}')
        except HTTPError as e:return e.code,json.loads(e.read() or b'{}')

    def test_html_serves_vietnamese_text_with_utf8_and_system_fonts(self):
        with urlopen(self.base+'/') as response:
            self.assertEqual(response.headers.get_content_type(),'text/html')
            self.assertEqual(response.headers.get_content_charset(),'utf-8')
            html=response.read().decode('utf-8')
        self.assertIn('<html lang="vi">',html)
        self.assertIn('<meta charset="utf-8">',html)
        self.assertIn('font-family:"Segoe UI",Tahoma,Arial,sans-serif',html)
        self.assertIn('Đăng nhập',html)

    def student(self, code='HS001', name='Nguyễn Văn A', group='Nhóm 1', class_id=None):
        return server.STORE.create_student(class_id or self.c1, code, name, None, group, self.admin)

    def test_01_teacher_add_creates_one_account_profile_and_membership(self):
        profile, username, _ = self.student()
        with server.STORE.connect() as con:
            row = con.execute('SELECT sp.account_id,cm.id membership_id,a.username FROM student_profiles sp JOIN accounts a ON a.id=sp.account_id JOIN class_memberships cm ON cm.student_profile_id=sp.id WHERE sp.id=?',(profile,)).fetchone()
            count=con.execute('SELECT COUNT(*) FROM student_profiles WHERE account_id=?',(row['account_id'],)).fetchone()[0]
        self.assertTrue(row['account_id']); self.assertEqual(row['username'], username); self.assertTrue(row['membership_id'])
        self.assertEqual(count,1)

    def test_02_bulk_import_30_students_creates_all_records(self):
        rows=[{'studentCode':f'HS{i:03}','fullName':f'Học sinh {i}','group':'Nhóm 1'} for i in range(30)]
        _, preview = self.call(f'/api/classes/{self.c1}/students/import/preview', {'rows':rows})
        self.assertEqual(preview['validCount'],30)
        _, created = self.call(f"/api/imports/{preview['previewId']}/confirm", {})
        self.assertEqual(created['createdCount'],30)
        with server.STORE.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM student_profiles').fetchone()[0],30)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM accounts WHERE role="STUDENT"').fetchone()[0],30)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM class_memberships').fetchone()[0],30)

    def test_pasted_names_generate_unique_student_codes_and_usernames(self):
        # Existing codes, including retained historical profiles, are never reused.
        self.student('HS0001', 'Học sinh cũ')
        rows=[{'fullName':'Nguyễn Văn An'},{'fullName':'Trần Thị Bình'},{'fullName':'Lê Văn Cường'},{'studentCode':'HS0002','fullName':'Mã nhập thủ công'}]
        status,preview=self.call(f'/api/classes/{self.c1}/students/import/preview',{'rows':rows})
        self.assertEqual(status,200,preview)
        self.assertEqual(preview['validCount'],4)
        self.assertTrue(all(row['autoStudentCode'] for row in preview['preview'][:3]))
        self.assertNotIn('HS0001',[row['studentCode'] for row in preview['preview']])
        self.assertNotEqual(preview['preview'][0]['studentCode'],'HS0002')
        status,created=self.call(f"/api/imports/{preview['previewId']}/confirm",{})
        self.assertEqual(status,201,created)
        roster=created['activationCodes']
        self.assertEqual([row['fullName'] for row in roster],[row['fullName'] for row in rows])
        codes=[row['studentCode'].casefold() for row in roster]
        usernames=[row['username'].casefold() for row in roster]
        self.assertEqual(len(codes),len(set(codes)))
        self.assertEqual(len(usernames),len(set(usernames)))
        self.assertTrue(all(row['username']==row['studentCode'].lower() for row in roster))
        self.assertTrue(all(row.get('activationCode') for row in roster))

    def test_import_preview_flags_missing_duplicate_blank_and_invalid_date_rows(self):
        self.student('EXISTING', 'Đã có')
        rows = [
            {'studentCode':'NEW001','fullName':'Học sinh hợp lệ'},
            {'studentCode':'BADDATE','fullName':'Sai ngày','dateOfBirth':'31/02/2010'},
            {'studentCode':'','fullName':'Tự tạo mã'},
            {'studentCode':'DUP001','fullName':'Dòng đầu'},
            {'studentCode':'DUP001','fullName':'Dòng trùng'},
            {},
            {'studentCode':'EXISTING','fullName':'Mã đã có'},
            {'studentCode':'NO_NAME','fullName':''},
        ]
        status, preview = self.call(f'/api/classes/{self.c1}/students/import/preview', {'rows':rows})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['validCount'], 3)
        self.assertEqual(preview['errorCount'], 5)
        self.assertEqual([x['studentCode'] for x in preview['preview'][:1]], ['NEW001'])
        self.assertTrue(preview['preview'][1]['autoStudentCode'])
        self.assertEqual(preview['preview'][2]['studentCode'], 'DUP001')
        self.assertIn('Invalid birth date; use YYYY-MM-DD', preview['errors'][0]['errors'])
        _, created = self.call(f"/api/imports/{preview['previewId']}/confirm", {})
        self.assertEqual(created['createdCount'], 3)
        with server.STORE.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM student_profiles').fetchone()[0], 4)

    def test_manual_add_rejects_invalid_birth_date(self):
        status, error = self.call(f'/api/classes/{self.c1}/students', {
            'studentCode':'BAD-DATE','fullName':'Ngày sinh sai','dateOfBirth':'2010-02-30'
        })
        self.assertEqual(status, 400)
        self.assertIn('Invalid birth date', error.get('error',''))

    def test_conduct_rules_are_visible_only_to_class_teacher_admin_and_own_students(self):
        _, student_username, activation = self.student('HS-RULES', 'Hoc sinh quy tac')
        status, class_rule = self.call('/api/conduct-rules', {'classId':self.c1,'title':'Di hoc dung gio','points':2})
        self.assertEqual(status,201,class_rule)

        teacher_id=server.uid('acc');ts=server.now()
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','TEACHER',1,?,?,?,?)",(teacher_id,'otherteacher',server.password_hash('Other-teacher-test-pass'),ts,ts,'Other Teacher','otherteacher@example.test'))
        other_class=server.STORE.create_class(self.y1,'10B1',teacher_id)
        teacher_client,teacher_csrf=self.other_client('otherteacher','Other-teacher-test-pass')
        status,other_rule=self.other_call(teacher_client,teacher_csrf,'/api/conduct-rules',{'classId':other_class,'title':'Hoan thanh bai doc','points':4})
        self.assertEqual(status,201,other_rule)
        status,_=self.other_call(teacher_client,teacher_csrf,f'/api/conduct-rules?classId={self.c1}',None,method='GET')
        self.assertEqual(status,403)
        status,visible=self.other_call(teacher_client,teacher_csrf,f'/api/conduct-rules?classId={other_class}',None,method='GET')
        self.assertEqual(status,200);self.assertEqual([x['id'] for x in visible],[other_rule['id']])

        status,_=self.call('/api/activation/complete',{'activationCode':activation,'studentCode':'HS-RULES','password':'Student-rules-pass'},mutate=False)
        self.assertEqual(status,200)
        status,visible=self.call(f'/api/conduct-rules?classId={self.c1}',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertEqual([x['id'] for x in visible],[class_rule['id']])
        status,_=self.call(f'/api/conduct-rules?classId={other_class}',None,method='GET',mutate=False)
        self.assertEqual(status,403)

        status,login=self.call('/api/login',{'username':'admin01','password':'A-secure-test-pass'},mutate=False)
        self.assertEqual(status,200);self.csrf=login['csrfToken']
        status,visible=self.call(f'/api/conduct-rules?classId={other_class}',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertEqual([x['id'] for x in visible],[other_rule['id']])

    def test_group_leader_uses_teacher_rules_and_only_scores_own_group(self):
        leader_id, leader_name, leader_code = self.student('HS-LEAD', 'Tổ trưởng', 'Tổ 1')
        member_id, member_username, member_code = self.student('HS-MEMBER', 'Bạn cùng tổ', 'Tổ 1')
        outsider_id, _, outsider_code = self.student('HS-OTHER', 'Bạn tổ khác', 'Tổ 2')
        self.student('HS-UNGROUPED','Học sinh chưa xếp tổ','')
        with server.STORE.connect() as con:
            membership=con.execute('SELECT id FROM class_memberships WHERE student_profile_id=? AND class_id=?',(leader_id,self.c1)).fetchone()
        for code,activation,password in [
            ('HS-LEAD',leader_code,'Leader-test-password'),
            ('HS-MEMBER',member_code,'Member-test-password'),
            ('HS-OTHER',outsider_code,'Other-test-password'),
        ]:
            status,_=self.call('/api/activation/complete',{'activationCode':activation,'studentCode':code,'password':password},mutate=False)
            self.assertEqual(status,200)
        _,admin_login=self.call('/api/login',{'username':'admin01','password':'A-secure-test-pass'},mutate=False)
        self.csrf=admin_login['csrfToken']
        leader_client,leader_csrf=self.other_client(leader_name,'Leader-test-password')
        member_client,member_csrf=self.other_client(member_username,'Member-test-password')
        status,_=self.other_call(leader_client,leader_csrf,f'/api/group/conduct?classId={self.c1}',None,method='GET')
        self.assertEqual(status,403)
        status,_=self.other_call(member_client,member_csrf,'/api/group/conduct/publish',{'classId':self.c1,'week':'2026-W43'})
        self.assertEqual(status,403)
        status,role_result=self.call(f"/api/memberships/{membership['id']}/role",{'role':'GROUP_LEADER'})
        self.assertEqual(status,200,role_result)
        status,current_user=self.other_call(leader_client,leader_csrf,'/api/me',None,method='GET')
        self.assertEqual(status,200)
        self.assertTrue(any(m['classId']==self.c1 and m['role']=='GROUP_LEADER' for m in current_user['memberships']))
        status,rule=self.call('/api/conduct-rules',{'classId':self.c1,'title':'Chuẩn bị bài','points':3})
        self.assertEqual(status,201,rule)
        status,deduction_rule=self.call('/api/conduct-rules',{'classId':self.c1,'title':'Đi muộn','points':-2})
        self.assertEqual(status,201,deduction_rule)
        other_class=server.STORE.create_class(self.y1,'10A2',self.admin)
        status,other_class_rule=self.call('/api/conduct-rules',{'classId':other_class,'title':'Quy tắc riêng lớp 10A2','points':7})
        self.assertEqual(status,201,other_class_rule)
        status,leader_board=self.other_call(leader_client,leader_csrf,f'/api/group/conduct?classId={self.c1}',None,method='GET')
        self.assertEqual(status,200,leader_board)
        self.assertEqual({item['id']:item['points'] for item in leader_board['rules']},{rule['id']:3,deduction_rule['id']:-2})
        status,other_rules=self.call(f'/api/conduct-rules?classId={other_class}',None,method='GET',mutate=False)
        self.assertEqual(status,200,other_rules)
        self.assertEqual([item['id'] for item in other_rules],[other_class_rule['id']])
        status,_=self.other_call(leader_client,leader_csrf,'/api/group/conduct-records',{'classId':self.c1,'studentProfileId':member_id,'points':3,'reason':'Hoàn thành nhiệm vụ'})
        self.assertEqual(status,201)
        status,_=self.other_call(leader_client,leader_csrf,'/api/group/conduct-records',{'classId':self.c1,'studentProfileId':member_id,'points':-2,'reason':'Đi muộn'})
        self.assertEqual(status,201)
        status,invalid=self.other_call(leader_client,leader_csrf,'/api/group/conduct-records',{'classId':self.c1,'studentProfileId':member_id,'points':4,'reason':'Mức chưa cấu hình'})
        self.assertEqual(status,400);self.assertIn('chưa được giáo viên thiết lập',invalid['error'])
        status,_=self.other_call(leader_client,leader_csrf,'/api/group/conduct-records',{'classId':self.c1,'studentProfileId':outsider_id,'points':3,'reason':'Ghi ngoài tổ'})
        self.assertEqual(status,403)
        status,_=self.other_call(leader_client,leader_csrf,'/api/conduct-rules',{'classId':self.c1,'title':'Tự cấp điểm','points':100})
        self.assertEqual(status,403)
        with server.STORE.connect() as con:
            records=con.execute("SELECT id,status,points FROM conduct_records WHERE student_profile_id=? ORDER BY points DESC",(member_id,)).fetchall()
        self.assertEqual([(record['status'],record['points']) for record in records],[('PENDING',3),('PENDING',-2)])
        status,pending_board=self.call(f'/api/conduct/leaderboard?classId={self.c1}',None,method='GET',mutate=False)
        self.assertEqual(status,200)
        pending_scores={x['studentProfileId']:x['score'] for x in pending_board['members']}
        self.assertEqual(pending_scores[member_id],0)
        current_week=server.datetime.now(server.timezone.utc).date().isocalendar()
        week=f"{current_week.year}-W{current_week.week:02d}"
        status,pending_post=self.other_call(leader_client,leader_csrf,'/api/group/conduct/publish',{'classId':self.c1,'week':week})
        self.assertEqual(status,400)
        self.assertIn('Chưa có điểm đã được giáo viên xác minh',pending_post['error'])
        for record in records:
            self.call(f"/api/conduct-records/{record['id']}/confirm",{})
        status,group_post=self.other_call(leader_client,leader_csrf,'/api/group/conduct/publish',{'classId':self.c1,'week':week})
        self.assertEqual(status,201,group_post)
        status,announcements=self.other_call(leader_client,leader_csrf,f'/api/forum?classId={self.c1}&type=ANNOUNCEMENT',None,method='GET')
        self.assertEqual(status,200)
        published=next(post for post in announcements if post['id']==group_post['id'])
        self.assertIn('Bạn cùng tổ',published['body'])
        self.assertIn('1 điểm',published['body'])
        self.assertNotIn('Bạn tổ khác',published['body'])
        status,non_leader_post=self.other_call(member_client,member_csrf,'/api/group/conduct/publish',{'classId':self.c1,'week':week})
        self.assertEqual(status,403)
        status,board=self.call(f'/api/conduct/leaderboard?classId={self.c1}',None,method='GET',mutate=False)
        self.assertEqual(status,200)
        self.assertIn('Chưa xếp tổ',board['groups'])
        scores={x['studentProfileId']:x['score'] for x in board['members']}
        self.assertEqual(scores[member_id],1);self.assertEqual(scores[outsider_id],0)
        status,published=self.call('/api/conduct/leaderboard/publish',{'classId':self.c1})
        self.assertEqual(status,201,published)
        status,feed=self.call(f'/api/forum?classId={self.c1}&type=ANNOUNCEMENT',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertIn('Bạn cùng tổ',feed[0]['body'])

    def test_teacher_can_switch_class_between_group_and_team_leaders(self):
        leader_id,leader_username,leader_activation=self.student('HS-TEAM-LEAD','Nhóm trưởng thử','Nhóm 1')
        member_id,_,member_activation=self.student('HS-TEAM-MEMBER','Thành viên nhóm','Nhóm 1')
        with server.STORE.connect() as con:
            membership=con.execute('SELECT id FROM class_memberships WHERE student_profile_id=? AND class_id=?',(leader_id,self.c1)).fetchone()
        for code,activation in [('HS-TEAM-LEAD',leader_activation),('HS-TEAM-MEMBER',member_activation)]:
            self.assertEqual(self.call('/api/activation/complete',{'activationCode':activation,'studentCode':code,'password':'Team-test-password'},mutate=False)[0],200)
        leader_client,leader_csrf=self.other_client(leader_username,'Team-test-password')
        _,admin_login=self.call('/api/login',{'username':'admin01','password':'A-secure-test-pass'},mutate=False)
        self.csrf=admin_login['csrfToken']
        status,wrong_role=self.call(f"/api/memberships/{membership['id']}/role",{'role':'TEAM_LEADER'})
        self.assertEqual(status,400,wrong_role)
        status,result=self.call(f'/api/classes/{self.c1}/unit-type',{'unitType':'NHOM'})
        self.assertEqual(status,200,result)
        status,classes=self.call('/api/classes',None,method='GET',mutate=False)
        self.assertEqual(status,200)
        self.assertEqual(next(item['unitType'] for item in classes if item['id']==self.c1),'NHOM')
        status,role_result=self.call(f"/api/memberships/{membership['id']}/role",{'role':'TEAM_LEADER'})
        self.assertEqual(status,200,role_result)
        status,current_user=self.other_call(leader_client,leader_csrf,'/api/me',None,method='GET')
        self.assertEqual(status,200)
        self.assertTrue(any(m['classId']==self.c1 and m['role']=='TEAM_LEADER' and m['unitType']=='NHOM' for m in current_user['memberships']))
        status,board=self.other_call(leader_client,leader_csrf,f'/api/group/conduct?classId={self.c1}',None,method='GET')
        self.assertEqual(status,200,board)
        self.assertEqual(board['unitType'],'NHOM')
        status,rule=self.call('/api/conduct-rules',{'classId':self.c1,'title':'Hoàn thành nhiệm vụ','points':2})
        self.assertEqual(status,201,rule)
        status,record=self.other_call(leader_client,leader_csrf,'/api/group/conduct-records',{'classId':self.c1,'studentProfileId':member_id,'ruleId':rule['id']})
        self.assertEqual(status,201,record)
        with server.STORE.connect() as con:
            stored_role=con.execute('SELECT role FROM class_memberships WHERE id=?',(membership['id'],)).fetchone()[0]
            self.assertEqual(stored_role,'GROUP_LEADER')
        status,result=self.call(f'/api/classes/{self.c1}/unit-type',{'unitType':'TO'})
        self.assertEqual(status,200,result)
        status,current_user=self.other_call(leader_client,leader_csrf,'/api/me',None,method='GET')
        self.assertEqual(status,200)
        self.assertTrue(any(m['classId']==self.c1 and m['role']=='GROUP_LEADER' and m['unitType']=='TO' for m in current_user['memberships']))

    def test_assignment_is_a_reminder_and_submission_is_disabled(self):
        status,created=self.call('/api/assignments',{'classId':self.c1,'title':'Ôn tập chương 1','subject':'Toán','content':'Làm bài trong sách','dueAt':'2026-10-20T23:00'})
        self.assertEqual(status,201,created)
        status,items=self.call(f'/api/assignments?classId={self.c1}',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertEqual(items[0]['title'],'Ôn tập chương 1')
        status,error=self.call(f"/api/assignments/{created['id']}/submissions",{'answer':'not a submission'})
        self.assertEqual(status,410);self.assertIn('nhắc việc',error.get('error',''))

    def test_teacher_removes_student_membership_without_deleting_history_or_account(self):
        profile, _, activation = self.student('HS-LEAVE', 'Hoc sinh chuyen truong')
        teacher_id=server.uid('acc');ts=server.now()
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','TEACHER',1,?,?,?,?)",(teacher_id,'homeroom',server.password_hash('Homeroom-test-pass'),ts,ts,'Giao vien chu nhiem','homeroom@example.test'))
            con.execute('UPDATE classes SET teacher_account_id=? WHERE id=?',(teacher_id,self.c1))
            member=con.execute('SELECT id FROM class_memberships WHERE student_profile_id=? AND class_id=?',(profile,self.c1)).fetchone()
        status,student_login=self.call('/api/activation/complete',{'activationCode':activation,'studentCode':'HS-LEAVE','password':'Student-leave-test-pass'},mutate=False)
        self.assertEqual(status,200);self.csrf=student_login['csrfToken']
        status,_=self.call(f"/api/memberships/{member['id']}",{'confirmation':'Hoc sinh chuyen truong'},method='DELETE')
        self.assertEqual(status,403)

        teacher_client,teacher_csrf=self.other_client('homeroom','Homeroom-test-pass')
        status,_=self.other_call(teacher_client,teacher_csrf,f"/api/memberships/{member['id']}",{'confirmation':'Sai ten'},method='DELETE')
        self.assertEqual(status,400)
        status,result=self.other_call(teacher_client,teacher_csrf,f"/api/memberships/{member['id']}",{'confirmation':'Hoc sinh chuyen truong'},method='DELETE')
        self.assertEqual(status,200,result);self.assertTrue(result['historyPreserved'])
        status,rows=self.other_call(teacher_client,teacher_csrf,f'/api/classes/{self.c1}/students',None,method='GET')
        self.assertEqual(status,200);self.assertEqual(rows,[])
        with server.STORE.connect() as con:
            stored=con.execute('SELECT cm.status,a.status account_status,sp.id profile_id FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id JOIN accounts a ON a.id=sp.account_id WHERE cm.id=?',(member['id'],)).fetchone()
        self.assertEqual(stored['status'],'LEFT');self.assertEqual(stored['profile_id'],profile);self.assertEqual(stored['account_status'],'ACTIVE')

    def test_year_and_class_creation_validates_and_returns_clear_errors(self):
        status,error=self.call('/api/years',{'name':'  ','status':'UPCOMING'})
        self.assertEqual(status,400);self.assertIn('1 đến 40 ký tự',error['error'])
        status,error=self.call('/api/years',{'name':'Ngày sai','startDate':'2026-02-30','status':'UPCOMING'})
        self.assertEqual(status,400);self.assertIn('Ngày bắt đầu',error['error'])
        status,error=self.call('/api/years',{'name':'Ngày ngược','startDate':'2027-05-01','endDate':'2026-09-01','status':'UPCOMING'})
        self.assertEqual(status,400);self.assertIn('Ngày kết thúc',error['error'])
        status,error=self.call('/api/years',{'name':'2026–2027','status':'UPCOMING'})
        self.assertEqual(status,400);self.assertIn('đã tồn tại',error['error'])
        status,year=self.call('/api/years',{'name':'2027–2028','status':'ACTIVE'})
        self.assertEqual(status,201,year)
        with server.STORE.connect() as con:
            old_status=con.execute('SELECT status FROM academic_years WHERE id=?',(self.y1,)).fetchone()['status']
            new_status=con.execute('SELECT status FROM academic_years WHERE id=?',(year['id'],)).fetchone()['status']
        self.assertEqual(old_status,'ARCHIVED')
        self.assertEqual(new_status,'ACTIVE')
        status,created=self.call('/api/classes',{'academicYearId':year['id'],'name':' 11A1 '})
        self.assertEqual(status,201,created)
        with server.STORE.connect() as con:
            row=con.execute('SELECT name FROM classes WHERE id=?',(created['id'],)).fetchone()
        self.assertEqual(row['name'],'11A1')
        status,error=self.call('/api/classes',{'academicYearId':year['id'],'name':'11A1'})
        self.assertEqual(status,400);self.assertIn('lớp cùng tên chưa được đóng',error['error'])
        status,error=self.call('/api/classes',{'academicYearId':year['id'],'name':'X'*81})
        self.assertEqual(status,400);self.assertIn('1 đến 80 ký tự',error['error'])

    def test_admin_permanent_class_and_year_deletion_backups_data_and_keeps_profiles(self):
        class_profile,_,_=self.student('HS-HARD-CLASS','Học sinh xóa lớp')
        with server.STORE.connect() as con:
            con.execute("INSERT INTO conduct_records(id,student_profile_id,class_id,academic_year_id,reason,points,recorder_id,status,created_at) VALUES('hard-class-record',?,?,?,'Điểm cũ',3,?,'VERIFIED',?)",(class_profile,self.c1,self.y1,self.admin,server.now()))
        status,error=self.call(f'/api/admin/classes/{self.c1}',{'confirmation':'Sai tên'},method='DELETE')
        self.assertEqual(status,400);self.assertIn('không khớp',error['error'])
        status,result=self.call(f'/api/admin/classes/{self.c1}',{'confirmation':'10A1'},method='DELETE')
        self.assertEqual(status,200,result);self.assertTrue(result['permanent'])
        self.assertTrue((Path(server.STORE.path).parent/result['backupFile']).exists())
        with server.STORE.connect() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM classes WHERE id=?',(self.c1,)).fetchone())
            self.assertIsNone(con.execute("SELECT 1 FROM conduct_records WHERE id='hard-class-record'").fetchone())
            self.assertIsNotNone(con.execute('SELECT 1 FROM student_profiles WHERE id=?',(class_profile,)).fetchone())
            self.assertIsNotNone(con.execute('SELECT 1 FROM accounts WHERE id=(SELECT account_id FROM student_profiles WHERE id=?)',(class_profile,)).fetchone())

        year=server.STORE.create_year('2030–2031',None,None,'UPCOMING',self.admin)
        c2=server.STORE.create_class(year,'10A1',self.admin)
        c3=server.STORE.create_class(year,'10A2',self.admin)
        profile,_,_=self.student('HS-HARD-YEAR','Học sinh xóa năm',class_id=c2)
        status,error=self.call(f'/api/years/{year}/',{'confirmation':'2030'},method='DELETE')
        self.assertEqual(status,400);self.assertIn('không khớp',error['error'])
        status,result=self.call(f'/api/years/{year}',{'confirmation':'2030–2031'},method='DELETE')
        self.assertEqual(status,200,result);self.assertEqual(result['deletedCounts']['classes'],2)
        self.assertTrue((Path(server.STORE.path).parent/result['backupFile']).exists())
        with server.STORE.connect() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM academic_years WHERE id=?',(year,)).fetchone())
            self.assertIsNone(con.execute('SELECT 1 FROM classes WHERE id IN (?,?)',(c2,c3)).fetchone())
            self.assertEqual(con.execute('SELECT COUNT(*) FROM class_memberships WHERE student_profile_id=?',(profile,)).fetchone()[0],0)
            self.assertIsNotNone(con.execute('SELECT 1 FROM student_profiles WHERE id=?',(profile,)).fetchone())
            self.assertIsNotNone(con.execute("SELECT 1 FROM audit_logs WHERE action=? AND entity_id=?",('PERMANENT_DELETE_ACADEMIC_YEAR',year)).fetchone())

    def test_admin_with_class_manage_can_load_and_create_school_structure_without_class_read(self):
        account=server.uid('acc');ts=server.now()
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','ADMIN',1,?,?,?,?)",(account,'classmanager',server.password_hash('Class-manager-test-pass'),ts,ts,'Class Manager','classmanager@example.test'))
            con.execute("INSERT INTO admin_permissions(account_id,permission,granted_by,created_at) VALUES(?,?,?,?)",(account,'classes.manage',self.admin,ts))
        client,csrf=self.other_client('classmanager','Class-manager-test-pass')
        status,years=self.other_call(client,csrf,'/api/years',None,method='GET');self.assertEqual(status,200)
        status,classes=self.other_call(client,csrf,'/api/classes',None,method='GET');self.assertEqual(status,200)
        status,year=self.other_call(client,csrf,'/api/years',{'name':'2028–2029','status':'UPCOMING'})
        self.assertEqual(status,201,year)
        status,created=self.other_call(client,csrf,'/api/classes',{'academicYearId':year['id'],'name':'9C1'})
        self.assertEqual(status,201,created)

    def test_class_delete_hides_class_but_preserves_student_and_conduct_history(self):
        profile,_,_=self.student('HS-KEEP','Học sinh giữ lịch sử','Nhóm 1')
        with server.STORE.connect() as con:
            con.execute("INSERT INTO conduct_records(id,student_profile_id,class_id,academic_year_id,reason,points,recorder_id,status,created_at) VALUES('keep-record',?,?,?,'Ghi nhận cũ',2,?,'VERIFIED',?)",(profile,self.c1,self.y1,self.admin,server.now()))
        status,_=self.call(f'/api/classes/{self.c1}',{'confirmation':'Sai tên'},method='DELETE')
        self.assertEqual(status,400)
        status,result=self.call(f'/api/classes/{self.c1}',{'confirmation':'10A1'},method='DELETE')
        self.assertEqual(status,200,result);self.assertTrue(result['historyPreserved'])
        with server.STORE.connect() as con:
            con.execute("UPDATE classes SET name='10A1' WHERE id=?",(self.c1,))
        status,recreated=self.call('/api/classes',{'academicYearId':self.y1,'name':'10A1'})
        self.assertEqual(status,201,recreated)
        self.assertNotEqual(recreated['id'],self.c1)
        status,duplicate=self.call('/api/classes',{'academicYearId':self.y1,'name':'10A1'})
        self.assertEqual(status,400);self.assertIn('lớp cùng tên chưa được đóng',duplicate['error'])
        status,classes=self.call('/api/classes',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertEqual([row['name'] for row in classes],['10A1'])
        with server.STORE.connect() as con:
            row=con.execute('SELECT is_deleted,name,deleted_name FROM classes WHERE id=?',(self.c1,)).fetchone()
            membership=con.execute('SELECT status FROM class_memberships WHERE student_profile_id=?',(profile,)).fetchone()
            record=con.execute("SELECT reason FROM conduct_records WHERE id='keep-record'").fetchone()
            profile_count=con.execute('SELECT COUNT(*) FROM student_profiles WHERE id=?',(profile,)).fetchone()[0]
        self.assertEqual(row['is_deleted'],1);self.assertNotIn('10A1',row['name']);self.assertEqual(row['deleted_name'],'10A1');self.assertEqual(membership['status'],'LEFT')
        self.assertEqual(record['reason'],'Ghi nhận cũ');self.assertEqual(profile_count,1)

    def test_03_activation_changes_account_to_active(self):
        _, _, code = self.student()
        _, result = self.call('/api/activation/complete', {'activationCode':code,'studentCode':'HS001','password':'Long-unique-pass'}, mutate=False)
        self.assertIn('csrfToken',result)
        with server.STORE.connect() as con:
            a=con.execute('SELECT status,is_activated,password_hash FROM accounts WHERE username="hs001"').fetchone()
        self.assertEqual(a['status'],'ACTIVE'); self.assertEqual(a['is_activated'],1); self.assertTrue(a['password_hash'])

    def test_student_can_login_after_activation(self):
        _, username, code = self.student('HS-LOGIN', 'Student Login')
        status, activated = self.call('/api/activation/complete', {
            'activationCode':code,'studentCode':'HS-LOGIN','password':'Student-login-pass'
        }, mutate=False)
        self.assertEqual(status,200,activated)
        status, login = self.call('/api/login', {
            'username':username.upper(),'password':'Student-login-pass'
        }, mutate=False)
        self.assertEqual(status,200,login)
        self.assertIn('csrfToken',login)
        status, profile = self.call('/api/me',None,method='GET',mutate=False)
        self.assertEqual(status,200,profile)
        self.assertEqual(profile['role'],'STUDENT')
        self.assertEqual(profile['profile']['studentCode'],'HS-LOGIN')

    def test_04_activation_code_is_one_time_use(self):
        _, _, code = self.student()
        self.call('/api/activation/complete', {'activationCode':code,'studentCode':'HS001','password':'Long-unique-pass'}, mutate=False)
        status, _ = self.call('/api/activation/complete', {'activationCode':code,'studentCode':'HS001','password':'Another-unique-pass'}, mutate=False)
        self.assertEqual(status,400)

    def test_05_student_cannot_read_another_class_student_data(self):
        self.student('HS001','A'); self.student('HS002','B')
        y2=server.STORE.create_year('2027–2028',None,None,'UPCOMING',self.admin)
        c2=server.STORE.create_class(y2,'11B1',self.admin)
        profile,_,code=server.STORE.create_student(c2,'HS003','C',None,'',self.admin)
        self.call('/api/activation/complete',{'activationCode':code,'studentCode':'HS003','password':'Long-unique-pass'},mutate=False)
        self.call('/api/login',{'username':'hs003','password':'Long-unique-pass'},mutate=False)
        # Replace session with a student authenticated only in 11B1, then request 10A1.
        status,data=self.call(f'/api/classes/{self.c1}/students',None,method='GET',mutate=False)
        self.assertEqual(status,403,data)

    def test_06_student_cannot_confirm_conduct_or_change_role(self):
        p,_,code=self.student()
        self.call('/api/activation/complete',{'activationCode':code,'studentCode':'HS001','password':'Long-unique-pass'},mutate=False)
        status,_=self.call('/api/conduct-records/fake/confirm',{})
        self.assertEqual(status,403)
        status,_=self.call('/api/memberships/fake/role',{'role':'CLASS_LEADER'})
        self.assertEqual(status,403)

    def test_07_promotion_preserves_account_and_profile_adds_membership(self):
        profile,_,_=self.student()
        y2=server.STORE.create_year('2027–2028',None,None,'UPCOMING',self.admin)
        c2=server.STORE.create_class(y2,'11A1',self.admin)
        with server.STORE.connect() as con: before=con.execute('SELECT account_id FROM student_profiles WHERE id=?',(profile,)).fetchone()[0]
        self.assertEqual(server.STORE.promote(self.y1,y2,[{'oldClassId':self.c1,'newClassId':c2,'studentProfileIds':[profile]}],self.admin),1)
        with server.STORE.connect() as con:
            after=con.execute('SELECT account_id FROM student_profiles WHERE id=?',(profile,)).fetchone()[0]
            memberships=con.execute('SELECT id,academic_year_id FROM class_memberships WHERE student_profile_id=? ORDER BY academic_year_id',(profile,)).fetchall()
        self.assertEqual(before,after); self.assertEqual(len(memberships),2); self.assertNotEqual(memberships[0]['id'],memberships[1]['id'])

    def test_08_previous_membership_is_kept_when_student_changes_class(self):
        profile,_,_=self.student()
        y2=server.STORE.create_year('2027–2028',None,None,'UPCOMING',self.admin); c2=server.STORE.create_class(y2,'11A1',self.admin)
        server.STORE.promote(self.y1,y2,[{'oldClassId':self.c1,'newClassId':c2,'studentProfileIds':[profile]}],self.admin)
        with server.STORE.connect() as con: rows=con.execute('SELECT class_id FROM class_memberships WHERE student_profile_id=?',(profile,)).fetchall()
        self.assertEqual({r['class_id'] for r in rows},{self.c1,c2})

    def test_09_role_change_is_scoped_to_membership_and_preserves_old_role(self):
        profile,_,_=self.student()
        y2=server.STORE.create_year('2027–2028',None,None,'UPCOMING',self.admin); c2=server.STORE.create_class(y2,'11A1',self.admin)
        server.STORE.promote(self.y1,y2,[{'oldClassId':self.c1,'newClassId':c2,'studentProfileIds':[profile]}],self.admin)
        with server.STORE.connect() as con:
            con.execute("UPDATE class_memberships SET role='GROUP_LEADER' WHERE student_profile_id=? AND academic_year_id=?",(profile,y2))
            roles=con.execute('SELECT academic_year_id,role FROM class_memberships WHERE student_profile_id=?',(profile,)).fetchall()
        by_year={r['academic_year_id']:r['role'] for r in roles}
        self.assertEqual(by_year[self.y1],'STUDENT'); self.assertEqual(by_year[y2],'GROUP_LEADER')

    def test_10_old_year_conduct_does_not_appear_in_new_year_dashboard(self):
        profile,_,code=self.student()
        with server.STORE.connect() as con:
            con.execute("INSERT INTO conduct_records(id,student_profile_id,class_id,academic_year_id,reason,points,recorder_id,status,created_at) VALUES('old-record',?,?,?,'Thành tích',5,?,'VERIFIED',?)",(profile,self.c1,self.y1,self.admin,server.now()))
        with server.STORE.connect() as con: con.execute("UPDATE academic_years SET status='ARCHIVED' WHERE id=?",(self.y1,))
        y2=server.STORE.create_year('2027–2028',None,None,'ACTIVE',self.admin); c2=server.STORE.create_class(y2,'11A1',self.admin)
        server.STORE.promote(self.y1,y2,[{'oldClassId':self.c1,'newClassId':c2,'studentProfileIds':[profile]}],self.admin)
        self.call('/api/activation/complete',{'activationCode':code,'studentCode':'HS001','password':'Long-unique-pass'},mutate=False)
        status,data=self.call('/api/student/dashboard',None,method='GET',mutate=False)
        self.assertEqual(status,200,data); self.assertEqual(data['conduct'],[])

    def test_11_account_owner_can_save_avatar_and_display_name(self):
        raw=b'\x89PNG\r\n\x1a\n'+b'test-image'
        avatar='data:image/png;base64,'+base64.b64encode(raw).decode()
        status,result=self.call('/api/me/profile',{'displayName':'Admin đổi tên','avatarData':avatar})
        self.assertEqual(status,200,result); self.assertEqual(result['avatarData'],avatar)
        status,profile=self.call('/api/me',None,method='GET',mutate=False)
        self.assertEqual(status,200); self.assertEqual(profile['displayName'],'Admin đổi tên'); self.assertEqual(profile['avatarData'],avatar)

    def test_12_avatar_upload_rejects_non_image_data(self):
        status,result=self.call('/api/me/profile',{'displayName':'Quản trị','avatarData':'data:text/html;base64,PHNjcmlwdD4='})
        self.assertEqual(status,400); self.assertIn('PNG',result['error'])

    def test_13_dashboard_metrics_use_scoped_database_counts(self):
        self.student('HS001','A','Nhóm 1')
        self.student('HS002','B','Nhóm 2')
        status,result=self.call('/api/dashboard/stats',None,method='GET',mutate=False)
        self.assertEqual(status,200,result)
        self.assertEqual(result['classCount'],1); self.assertEqual(result['studentCount'],2); self.assertEqual(result['groupCount'],2)

    def test_dashboard_and_class_list_filter_by_selected_academic_year(self):
        self.student('HS-CURRENT', 'Current student')
        next_year = server.STORE.create_year('2027–2028', '2027-09-01', '2028-05-31', 'UPCOMING', self.admin)
        next_class = server.STORE.create_class(next_year, '11A1', self.admin)
        server.STORE.create_student(next_class, 'HS-NEXT', 'Next year student', None, '', self.admin)

        status, current_stats = self.call(f'/api/dashboard/stats?yearId={self.y1}', None, method='GET', mutate=False)
        self.assertEqual(status, 200, current_stats)
        self.assertEqual(current_stats['classCount'], 1)
        self.assertEqual(current_stats['studentCount'], 1)
        status, next_stats = self.call(f'/api/dashboard/stats?yearId={next_year}', None, method='GET', mutate=False)
        self.assertEqual(status, 200, next_stats)
        self.assertEqual(next_stats['classCount'], 1)
        self.assertEqual(next_stats['studentCount'], 1)
        status, next_classes = self.call(f'/api/classes?yearId={next_year}', None, method='GET', mutate=False)
        self.assertEqual(status, 200, next_classes)
        self.assertEqual([row['id'] for row in next_classes], [next_class])

    def test_14_public_setup_is_disabled_and_bootstrap_is_one_time(self):
        status,_=self.call('/api/setup',{'username':'attacker','password':'Long-password-1'},mutate=False)
        self.assertEqual(status,410)
        status,admins=self.call('/api/admin/administrators',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertEqual(admins['administrators'],[])
        status,logs=self.call('/api/admin/audit-logs',None,method='GET',mutate=False)
        self.assertEqual(status,200);self.assertIn('BOOTSTRAP_SUPER_ADMIN',[x['action'] for x in logs])
        with self.assertRaises(ValueError):server.STORE.bootstrap_super_admin('other','other@example.test','Other','Another-long-password')
        with server.STORE.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM accounts WHERE is_super_admin=1').fetchone()[0],1)

    def test_15_admin_invitation_hashes_token_and_accepts_only_once(self):
        status,invite=self.call('/api/admin/invitations',{'email':'ops@example.test','permissions':['dashboard.read','users.read']})
        self.assertEqual(status,201,invite); token=invite['invitationLink'].split('token=',1)[1]
        with server.STORE.connect() as con:
            row=con.execute('SELECT token_hash,status FROM admin_invitations WHERE id=?',(invite['id'],)).fetchone()
            self.assertNotEqual(row['token_hash'],token);self.assertEqual(row['status'],'PENDING')
        status,accepted=self.call('/api/admin/invitations/accept',{'token':token,'email':'ops@example.test','username':'ops_admin','displayName':'Ops','password':'A-long-admin-pass'},mutate=False)
        self.assertEqual(status,200,accepted)
        status,_=self.call('/api/admin/invitations/accept',{'token':token,'email':'ops@example.test','username':'ops_admin','displayName':'Ops','password':'A-long-admin-pass'},mutate=False)
        self.assertEqual(status,400)
        with server.STORE.connect() as con:
            account=con.execute("SELECT role,status,is_super_admin FROM accounts WHERE username='ops_admin'").fetchone()
            self.assertEqual(tuple(account),('ADMIN','ACTIVE',0))
            self.assertEqual(con.execute('SELECT COUNT(*) FROM admin_permissions WHERE account_id=(SELECT id FROM accounts WHERE username=?)',('ops_admin',)).fetchone()[0],2)

    def test_16_regular_admin_cannot_manage_admins_or_exceed_permissions(self):
        ts=server.now();account='admin_limited'
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','ADMIN',1,?,?,?,?)",(account,'limited',server.password_hash('Long-password-1'),ts,ts,'Limited','limited@example.test'))
            con.execute("INSERT INTO admin_permissions(account_id,permission,granted_by,created_at) VALUES(?,?,?,?)",(account,'dashboard.read',self.admin,ts))
        opener,csrf=self.other_client('limited','Long-password-1')
        status,_=self.other_call(opener,csrf,'/api/admin/administrators',method='GET')
        self.assertEqual(status,403)
        status,_=self.other_call(opener,csrf,'/api/admin/invitations',{'email':'intruder@example.test','permissions':['users.manage']})
        self.assertEqual(status,403)
        status,_=self.other_call(opener,csrf,'/api/conduct-records',{'studentProfileId':'x','classId':self.c1,'reason':'x','points':1})
        self.assertEqual(status,403)

    def test_17_teacher_cannot_create_or_elevate_administrator(self):
        ts=server.now();teacher='teacher_one'
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name) VALUES(?,?,?,'ACTIVE','TEACHER',1,?,?,?)",(teacher,'teacher_one',server.password_hash('Long-password-1'),ts,ts,'Teacher'))
        opener,csrf=self.other_client('teacher_one','Long-password-1')
        status,_=self.other_call(opener,csrf,'/api/admin/invitations',{'email':'new@example.test','permissions':['users.manage']})
        self.assertEqual(status,403)

    def test_18_locking_admin_revokes_existing_sessions(self):
        ts=server.now();account='admin_lock'
        with server.STORE.connect() as con:
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','ADMIN',1,?,?,?,?)",(account,'lockadmin',server.password_hash('Long-password-1'),ts,ts,'Lock admin','lock@example.test'))
        opener,csrf=self.other_client('lockadmin','Long-password-1')
        self.assertEqual(self.other_call(opener,csrf,'/api/me',method='GET')[0],200)
        status,_=self.call(f'/api/admin/administrators/{account}',{'action':'LOCK'},method='PATCH')
        self.assertEqual(status,200)
        status,_=self.other_call(opener,csrf,'/api/me',method='GET')
        self.assertEqual(status,401)

    def test_19_revoked_and_expired_invitations_cannot_be_used(self):
        status,revoked=self.call('/api/admin/invitations',{'email':'revoked@example.test','permissions':[]})
        self.assertEqual(status,201)
        self.assertEqual(self.call(f"/api/admin/invitations/{revoked['id']}/revoke",{})[0],200)
        token=revoked['invitationLink'].split('token=',1)[1]
        body={'token':token,'email':'revoked@example.test','username':'revoked_admin','displayName':'Revoked','password':'Long-password-1'}
        self.assertEqual(self.call('/api/admin/invitations/accept',body,mutate=False)[0],400)
        status,expired=self.call('/api/admin/invitations',{'email':'expired@example.test','permissions':[]})
        self.assertEqual(status,201);token=expired['invitationLink'].split('token=',1)[1]
        with server.STORE.connect() as con:con.execute("UPDATE admin_invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(expired['id'],))
        body={'token':token,'email':'expired@example.test','username':'expired_admin','displayName':'Expired','password':'Long-password-1'}
        self.assertEqual(self.call('/api/admin/invitations/accept',body,mutate=False)[0],400)

    def test_20_legacy_schema_migration_keeps_backup_and_existing_account(self):
        legacy=Path(self.tmp.name)/'legacy.sqlite3'
        con=server.sqlite3.connect(legacy)
        con.executescript("""
            CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,password_hash TEXT,status TEXT NOT NULL,role TEXT NOT NULL,is_activated INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_login_at TEXT,display_name TEXT NOT NULL DEFAULT '');
            CREATE TABLE audit_logs(id TEXT PRIMARY KEY,actor_id TEXT,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT,metadata TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
            INSERT INTO accounts(id,username,status,role,is_activated,created_at,updated_at,display_name) VALUES('keep-id','legacy','ACTIVE','ADMIN',1,'2026-01-01','2026-01-01','Legacy Admin');
        """)
        con.commit();con.close()
        migrated=server.Store(legacy)
        backups=list(Path(self.tmp.name).glob('legacy.sqlite3.pre-admin-migration-*.bak'))
        self.assertEqual(len(backups),1)
        con=server.sqlite3.connect(backups[0])
        try:
            self.assertEqual(con.execute('SELECT id,username FROM accounts').fetchone(),('keep-id','legacy'))
            self.assertNotIn('is_super_admin',[r[1] for r in con.execute('PRAGMA table_info(accounts)')])
        finally:con.close()
        with migrated.connect() as con:
            row=con.execute('SELECT id,username,is_super_admin FROM accounts').fetchone()
            self.assertEqual(tuple(row),('keep-id','legacy',0))
            self.assertIn('email',[r['name'] for r in con.execute('PRAGMA table_info(accounts)')])

    def test_legacy_classes_get_default_unit_type_during_migration(self):
        legacy=Path(self.tmp.name)/'legacy-classes.sqlite3'
        old_schema=server.SCHEMA.replace(" unit_type TEXT NOT NULL DEFAULT 'TO' CHECK(unit_type IN ('TO','NHOM')),\n","")
        con=server.sqlite3.connect(legacy)
        try:
            con.executescript(old_schema)
            con.execute("INSERT INTO accounts(id,username,status,role,is_activated,created_at,updated_at) VALUES('teacher','teacher','ACTIVE','TEACHER',1,'2026-01-01','2026-01-01')")
            con.execute("INSERT INTO academic_years(id,name,status,created_at) VALUES('year','2026-2027','ACTIVE','2026-01-01')")
            con.execute("INSERT INTO classes(id,academic_year_id,name,teacher_account_id,created_at) VALUES('class','year','10A1','teacher','2026-01-01')")
            con.commit()
        finally:con.close()
        migrated=server.Store(legacy)
        with migrated.connect() as con:
            unit=con.execute('SELECT unit_type FROM classes WHERE id=?',('class',)).fetchone()[0]
            self.assertEqual(unit,'TO')
            self.assertNotIn('leader_type',{row['name'] for row in con.execute('PRAGMA table_info(class_memberships)')})

    def test_21_super_admin_can_edit_class_and_student_records(self):
        profile,_,_=self.student()
        status,result=self.call(f'/api/classes/{self.c1}',{'name':'10A1-Edited'},method='PATCH')
        self.assertEqual(status,200,result)
        status,result=self.call(f'/api/students/{profile}',{'classId':self.c1,'fullName':'Edited Student','dateOfBirth':'2010-01-02','group':'Group 4'},method='PATCH')
        self.assertEqual(status,200,result)
        with server.STORE.connect() as con:
            self.assertEqual(con.execute('SELECT name FROM classes WHERE id=?',(self.c1,)).fetchone()[0],'10A1-Edited')
            self.assertEqual(con.execute('SELECT full_name FROM student_profiles WHERE id=?',(profile,)).fetchone()[0],'Edited Student')
            self.assertEqual(con.execute('SELECT group_name FROM class_memberships WHERE student_profile_id=?',(profile,)).fetchone()[0],'Group 4')
            self.assertIn('UPDATE_CLASS',[r[0] for r in con.execute('SELECT action FROM audit_logs')])
            self.assertIn('UPDATE_STUDENT_PROFILE',[r[0] for r in con.execute('SELECT action FROM audit_logs')])

    def test_22_forum_announcement_board_and_import_cancel(self):
        status,post=self.call('/api/forum/posts',{'classId':self.c1,'body':'Class notice','postType':'ANNOUNCEMENT'})
        self.assertEqual(status,201,post)
        status,feed=self.call(f'/api/forum?classId={self.c1}&type=ANNOUNCEMENT',None,method='GET',mutate=False)
        self.assertEqual(status,200,feed)
        self.assertEqual(feed[0]['postType'],'ANNOUNCEMENT')
        status,preview=self.call(f'/api/classes/{self.c1}/students/import/preview',{'rows':[{'studentCode':'HS-CANCEL','fullName':'Preview Student'}]})
        self.assertEqual(status,200,preview)
        status,cancelled=self.call(f"/api/imports/{preview['previewId']}/cancel",{})
        self.assertEqual(status,200,cancelled)
        status,_=self.call(f"/api/imports/{preview['previewId']}/confirm",{})
        self.assertEqual(status,400)
        with server.STORE.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM student_profiles WHERE student_code='HS-CANCEL'").fetchone()[0],0)

    def test_database_backup_and_restore_keep_a_recovery_copy(self):
        backup = Path(server.STORE.backup_database())
        self.assertTrue(backup.is_file())
        server.STORE.create_student(self.c1, 'HS-RESTORE', 'Restore test', None, '', self.admin)

        safety_backup = Path(server.STORE.restore_database(backup))

        self.assertTrue(safety_backup.is_file())
        with server.STORE.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM student_profiles WHERE student_code='HS-RESTORE'").fetchone()[0], 0)
        con = server.sqlite3.connect(safety_backup)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM student_profiles WHERE student_code='HS-RESTORE'").fetchone()[0], 1)
        finally:
            con.close()

    def test_restore_rejects_invalid_database_without_changing_live_data(self):
        invalid_backup = Path(self.tmp.name) / 'invalid.sqlite3'
        invalid_backup.write_text('not a sqlite database', encoding='utf-8')

        with self.assertRaises(ValueError):
            server.STORE.restore_database(invalid_backup)
        with server.STORE.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM accounts WHERE username='admin01'").fetchone()[0], 1)

    def test_login_cookie_and_responses_include_security_headers(self):
        request = Request(self.base + '/api/login', data=json.dumps({
            'username': 'admin01',
            'password': 'A-secure-test-pass',
        }).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        with mock.patch.object(server, 'COOKIE_SECURE', True):
            with urlopen(request) as response:
                self.assertIn('; Secure', response.headers['Set-Cookie'])

        with urlopen(self.base + '/api/status') as response:
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main(verbosity=2)
