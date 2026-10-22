"""ClassTrack local API. Run with: python server.py

This is a small, dependency-free demonstration backend using SQLite. Deploy it
behind HTTPS with a managed database, secrets, backups, and monitoring before
using real student records.
"""
from __future__ import annotations

import csv
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import getpass
import smtplib
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CLASSTRACK_DB", ROOT / "classtrack.sqlite3"))
COOKIE_NAME = "classtrack_session"
SESSION_DAYS = 7
MAX_BODY = 2 * 1024 * 1024
RATE_LOCK = threading.Lock()
FAILED_CODES: dict[str, list[float]] = {}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be set to true or false.")


COOKIE_SECURE = env_flag("CLASSTRACK_COOKIE_SECURE")


def session_cookie(token: str, max_age: int) -> str:
    cookie = f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"
    return cookie + ("; Secure" if COOKIE_SECURE else "")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING_ACTIVATION','ACTIVE','SUSPENDED','DISABLED')),
 role TEXT NOT NULL CHECK(role IN ('ADMIN','TEACHER','STUDENT','PARENT')),
 is_activated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 last_login_at TEXT, display_name TEXT NOT NULL DEFAULT '', avatar_data TEXT NOT NULL DEFAULT '',
 email TEXT, is_super_admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS student_profiles(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE REFERENCES accounts(id),
 student_code TEXT NOT NULL UNIQUE COLLATE NOCASE, full_name TEXT NOT NULL,
 date_of_birth TEXT, gender TEXT, avatar TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS academic_years(
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, start_date TEXT, end_date TEXT,
 status TEXT NOT NULL CHECK(status IN ('UPCOMING','ACTIVE','ARCHIVED')), created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_year ON academic_years(status) WHERE status='ACTIVE';
CREATE TABLE IF NOT EXISTS classes(
 id TEXT PRIMARY KEY, academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 name TEXT NOT NULL, teacher_account_id TEXT NOT NULL REFERENCES accounts(id), created_at TEXT NOT NULL,
 is_deleted INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, deleted_name TEXT,
 unit_type TEXT NOT NULL DEFAULT 'TO' CHECK(unit_type IN ('TO','NHOM')),
 UNIQUE(academic_year_id,name));
CREATE TABLE IF NOT EXISTS class_memberships(
 id TEXT PRIMARY KEY, student_profile_id TEXT NOT NULL REFERENCES student_profiles(id),
 class_id TEXT NOT NULL REFERENCES classes(id), academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 group_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL CHECK(role IN ('STUDENT','GROUP_LEADER','CLASS_DEPUTY','CLASS_LEADER')),
 status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','LEFT','SUSPENDED')),
 joined_at TEXT NOT NULL, left_at TEXT, approved_by TEXT REFERENCES accounts(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(student_profile_id,academic_year_id));
CREATE TABLE IF NOT EXISTS activation_codes(
 id TEXT PRIMARY KEY, student_profile_id TEXT NOT NULL REFERENCES student_profiles(id),
 code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, invalidated_at TEXT,
 created_by TEXT NOT NULL REFERENCES accounts(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), csrf_token TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conduct_records(
 id TEXT PRIMARY KEY, student_profile_id TEXT NOT NULL REFERENCES student_profiles(id),
 class_id TEXT NOT NULL REFERENCES classes(id), academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 group_name TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL, points INTEGER NOT NULL,
 recorder_id TEXT NOT NULL REFERENCES accounts(id), evidence_name TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
 created_at TEXT NOT NULL, verified_at TEXT, verified_by TEXT REFERENCES accounts(id));
CREATE TABLE IF NOT EXISTS conduct_rules(
 id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id), academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 title TEXT NOT NULL, points INTEGER NOT NULL CHECK(points<>0 AND points BETWEEN -100 AND 100),
 active INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL REFERENCES accounts(id), created_at TEXT NOT NULL,
 UNIQUE(class_id,academic_year_id,title));
CREATE TABLE IF NOT EXISTS assignments(
 id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id), academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 title TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', due_at TEXT,
 max_points REAL NOT NULL DEFAULT 10, created_by TEXT NOT NULL REFERENCES accounts(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment_submissions(
 id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(id), student_profile_id TEXT NOT NULL REFERENCES student_profiles(id),
 answer TEXT NOT NULL DEFAULT '', file_name TEXT, status TEXT NOT NULL DEFAULT 'SUBMITTED', score REAL, feedback TEXT,
 submitted_at TEXT NOT NULL, graded_at TEXT, UNIQUE(assignment_id,student_profile_id));
CREATE TABLE IF NOT EXISTS forum_posts(
 id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id), academic_year_id TEXT NOT NULL REFERENCES academic_years(id),
 author_id TEXT NOT NULL REFERENCES accounts(id), body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 post_type TEXT NOT NULL DEFAULT 'POST' CHECK(post_type IN ('POST','ANNOUNCEMENT')));
CREATE TABLE IF NOT EXISTS forum_comments(
 id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,
 author_id TEXT NOT NULL REFERENCES accounts(id), body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forum_reactions(
 post_id TEXT NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE, account_id TEXT NOT NULL REFERENCES accounts(id),
 reaction TEXT NOT NULL DEFAULT 'LIKE', created_at TEXT NOT NULL, PRIMARY KEY(post_id,account_id));
CREATE TABLE IF NOT EXISTS audit_logs(
 id TEXT PRIMARY KEY, actor_id TEXT REFERENCES accounts(id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 result TEXT NOT NULL DEFAULT 'SUCCESS');
CREATE TABLE IF NOT EXISTS admin_permissions(
 account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, permission TEXT NOT NULL,
 granted_by TEXT REFERENCES accounts(id), created_at TEXT NOT NULL, PRIMARY KEY(account_id,permission));
CREATE TABLE IF NOT EXISTS admin_grants(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id), previous_role TEXT NOT NULL,
 granted_by TEXT REFERENCES accounts(id), created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS admin_invitations(
 id TEXT PRIMARY KEY, email TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, permissions_json TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES accounts(id), status TEXT NOT NULL DEFAULT 'PENDING',
 used_at TEXT, accepted_account_id TEXT REFERENCES accounts(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_batches(
 id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id), created_by TEXT NOT NULL REFERENCES accounts(id),
 rows_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PREVIEW', created_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def check_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        _, iterations, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback and close when used in a with block (sqlite default leaves it open)."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = str(path)
        self.init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def backup_legacy_database(self) -> None:
        source_path=Path(self.path)
        if not source_path.exists() or source_path.stat().st_size==0: return
        source=sqlite3.connect(f'file:{source_path.resolve()}?mode=ro',uri=True,timeout=10)
        try:
            account_columns={r[1] for r in source.execute('PRAGMA table_info(accounts)')}
            log_columns={r[1] for r in source.execute('PRAGMA table_info(audit_logs)')}
            tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required_tables={'admin_permissions','admin_grants','admin_invitations'}
            forum_columns={r[1] for r in source.execute('PRAGMA table_info(forum_posts)')} if 'forum_posts' in tables else set()
            class_columns={r[1] for r in source.execute('PRAGMA table_info(classes)')} if 'classes' in tables else set()
            needs_migration=(not {'avatar_data','email','is_super_admin'}.issubset(account_columns)
                or 'result' not in log_columns or not required_tables.issubset(tables) or 'post_type' not in forum_columns
                or not {'is_deleted','deleted_at','deleted_name'}.issubset(class_columns)
                or ('classes' in tables and 'unit_type' not in class_columns))
            if not needs_migration: return
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup_path=source_path.with_name(source_path.name+f'.pre-admin-migration-{stamp}.bak')
            backup=sqlite3.connect(str(backup_path),timeout=10)
            try: source.backup(backup)
            finally: backup.close()
        except (OSError,sqlite3.Error) as error:
            raise RuntimeError(f'Không thể sao lưu database cũ trước migration: {error}') from error
        finally:
            source.close()

    def init(self) -> None:
        self.backup_legacy_database()
        with self.connect() as con:
            con.executescript(SCHEMA)
            forum_columns={row['name'] for row in con.execute('PRAGMA table_info(forum_posts)')}
            if 'post_type' not in forum_columns:
                con.execute("ALTER TABLE forum_posts ADD COLUMN post_type TEXT NOT NULL DEFAULT 'POST'")
            class_columns={row['name'] for row in con.execute('PRAGMA table_info(classes)')}
            if 'is_deleted' not in class_columns:
                con.execute('ALTER TABLE classes ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0')
            if 'deleted_at' not in class_columns:
                con.execute('ALTER TABLE classes ADD COLUMN deleted_at TEXT')
            if 'deleted_name' not in class_columns:
                con.execute('ALTER TABLE classes ADD COLUMN deleted_name TEXT')
            if 'unit_type' not in class_columns:
                con.execute("ALTER TABLE classes ADD COLUMN unit_type TEXT NOT NULL DEFAULT 'TO' CHECK(unit_type IN ('TO','NHOM'))")
            for row in con.execute("SELECT id,name,deleted_name FROM classes WHERE is_deleted=1").fetchall():
                archived_name=f"__deleted__{row['id']}"
                if not row['deleted_name'] or row['name']!=archived_name:
                    con.execute("UPDATE classes SET deleted_name=COALESCE(NULLIF(deleted_name,''),name),name=? WHERE id=?",(archived_name,row['id']))
            columns={row['name'] for row in con.execute('PRAGMA table_info(accounts)')}
            if 'avatar_data' not in columns:
                con.execute("ALTER TABLE accounts ADD COLUMN avatar_data TEXT NOT NULL DEFAULT ''")
            if 'email' not in columns:
                con.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
            if 'is_super_admin' not in columns:
                con.execute("ALTER TABLE accounts ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0")
            log_columns={row['name'] for row in con.execute('PRAGMA table_info(audit_logs)')}
            if 'result' not in log_columns:
                con.execute("ALTER TABLE audit_logs ADD COLUMN result TEXT NOT NULL DEFAULT 'SUCCESS'")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS account_email_unique ON accounts(email COLLATE NOCASE) WHERE email IS NOT NULL AND email<>''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_super_admin ON accounts(is_super_admin) WHERE is_super_admin=1")

    def audit(self, con: sqlite3.Connection, actor: str | None, action: str, entity: str, entity_id: str | None, meta: dict | None = None) -> None:
        con.execute("INSERT INTO audit_logs(id,actor_id,action,entity_type,entity_id,metadata,created_at,result) VALUES(?,?,?,?,?,?,?,'SUCCESS')", (uid('log'), actor, action, entity, entity_id, json.dumps(meta or {}, ensure_ascii=False), now()))

    def bootstrap_super_admin(self, username: str, email: str, display_name: str, password: str) -> str:
        account_id, ts = uid('acc'), now()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM accounts WHERE is_super_admin=1 LIMIT 1").fetchone():
                raise ValueError('Super Admin đã được khởi tạo; bootstrap bị khóa.')
            con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email,is_super_admin) VALUES(?,?,?,'ACTIVE','ADMIN',1,?,?,?,?,1)", (account_id, username, password_hash(password), ts, ts, display_name, email))
            self.audit(con, account_id, 'BOOTSTRAP_SUPER_ADMIN', 'Account', account_id, {'username': username})
        return account_id

    def create_year(self, name: str, start: str | None, end: str | None, status: str, actor: str) -> str:
        name=str(name or '').strip()
        if not name or len(name)>40: raise ValueError('Tên năm học phải có từ 1 đến 40 ký tự.')
        start=str(start or '').strip() or None
        end=str(end or '').strip() or None
        if start:
            try: date.fromisoformat(start)
            except ValueError: raise ValueError('Ngày bắt đầu không hợp lệ.')
        if end:
            try: date.fromisoformat(end)
            except ValueError: raise ValueError('Ngày kết thúc không hợp lệ.')
        if start and end and start>end: raise ValueError('Ngày kết thúc phải sau hoặc bằng ngày bắt đầu.')
        status=str(status or 'UPCOMING').strip().upper()
        if status not in ('UPCOMING','ACTIVE','ARCHIVED'): raise ValueError('Trạng thái năm học không hợp lệ.')
        year_id=uid('year');ts=now()
        try:
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if con.execute('SELECT 1 FROM academic_years WHERE name=? COLLATE NOCASE',(name,)).fetchone():
                    raise ValueError('Tên năm học này đã tồn tại.')
                if status=='ACTIVE':
                    current_active=con.execute("SELECT id,name FROM academic_years WHERE status='ACTIVE'").fetchone()
                    if current_active:
                        con.execute("UPDATE academic_years SET status='ARCHIVED' WHERE id=?",(current_active['id'],))
                        self.audit(con,actor,'ARCHIVE_PREVIOUS_ACADEMIC_YEAR','AcademicYear',current_active['id'],{'name':current_active['name'],'replacedBy':year_id})
                con.execute("INSERT INTO academic_years(id,name,start_date,end_date,status,created_at) VALUES(?,?,?,?,?,?)",(year_id,name,start,end,status,ts))
                self.audit(con,actor,'CREATE_ACADEMIC_YEAR','AcademicYear',year_id,{'name':name,'status':status})
        except sqlite3.IntegrityError as error:
            raise ValueError('Không thể tạo năm học do tên bị trùng hoặc trạng thái không hợp lệ.') from error
        return year_id

    def create_class(self, year_id: str, name: str, teacher_id: str) -> str:
        name=' '.join(str(name or '').split())
        if not name or len(name)>80: raise ValueError('Tên lớp phải có từ 1 đến 80 ký tự.')
        normalized_name=name.casefold()
        class_id=uid('class');ts=now()
        try:
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                if not con.execute("SELECT 1 FROM academic_years WHERE id=?",(year_id,)).fetchone():
                    raise ValueError('Không tìm thấy năm học. Hãy tải lại trang và chọn năm học cần tồn tại.')
                # Older versions could leave a deleted class under its original name.
                for archived in con.execute("SELECT id,name FROM classes WHERE academic_year_id=? AND is_deleted=1",(year_id,)).fetchall():
                    archived_name=f"__deleted__{archived['id']}"
                    if archived['name']!=archived_name:
                        con.execute("UPDATE classes SET deleted_name=COALESCE(NULLIF(deleted_name,''),name),name=? WHERE id=?",(archived_name,archived['id']))
                active_classes=con.execute('SELECT name FROM classes WHERE academic_year_id=? AND is_deleted=0',(year_id,)).fetchall()
                if any(' '.join(str(row['name']).split()).casefold()==normalized_name for row in active_classes):
                    raise ValueError('Đang có lớp cùng tên chưa được đóng trong năm học này. Hãy kiểm tra danh sách lớp; tên sẽ được dùng lại sau khi đóng lớp cũ.')
                if not con.execute("SELECT 1 FROM accounts WHERE id=? AND status='ACTIVE'",(teacher_id,)).fetchone():
                    raise ValueError('Không tìm thấy tài khoản giáo viên đang hoạt động để phụ trách lớp.')
                con.execute("INSERT INTO classes(id,academic_year_id,name,teacher_account_id,created_at) VALUES(?,?,?,?,?)",(class_id,year_id,name,teacher_id,ts))
                self.audit(con,teacher_id,'CREATE_CLASS','Class',class_id,{'name':name,'academicYearId':year_id})
        except sqlite3.IntegrityError as error:
            raise ValueError('Không thể tạo lớp do tên lớp đã tồn tại hoặc dữ liệu năm học/giáo viên không hợp lệ.') from error
        return class_id

    def update_class(self, class_id: str, name: str, teacher_id: str, actor: str) -> None:
        name=' '.join(str(name or '').split())
        if not name or len(name)>80: raise ValueError('Tên lớp phải có từ 1 đến 80 ký tự.')
        normalized_name=name.casefold()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT academic_year_id,name,teacher_account_id FROM classes WHERE id=? AND is_deleted=0',(class_id,)).fetchone()
            if not row: raise ValueError('Không tìm thấy lớp đang hoạt động.')
            active_classes=con.execute('SELECT id,name FROM classes WHERE academic_year_id=? AND is_deleted=0 AND id<>?',(row['academic_year_id'],class_id)).fetchall()
            if any(' '.join(str(other['name']).split()).casefold()==normalized_name for other in active_classes):
                raise ValueError('Đang có lớp cùng tên chưa được đóng trong năm học này. Hãy đổi tên một trong hai lớp.')
            if not con.execute("SELECT 1 FROM accounts WHERE id=? AND status='ACTIVE'",(teacher_id,)).fetchone():
                raise ValueError('Không tìm thấy tài khoản giáo viên đang hoạt động.')
            con.execute('UPDATE classes SET name=?,teacher_account_id=? WHERE id=?',(name,teacher_id,class_id))
            self.audit(con,actor,'UPDATE_CLASS','Class',class_id,{'oldName':row['name'],'name':name,'oldTeacherId':row['teacher_account_id'],'teacherId':teacher_id})

    def create_deletion_backup(self) -> str:
        source_path=Path(self.path)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup_path=source_path.with_name(source_path.name+f'.before-delete-{stamp}-{uuid.uuid4().hex[:8]}.bak')
        source=sqlite3.connect(self.path,timeout=10)
        backup=sqlite3.connect(str(backup_path),timeout=10)
        try: source.backup(backup)
        except Exception:
            backup.close();source.close()
            try: backup_path.unlink(missing_ok=True)
            except OSError: pass
            raise
        else:
            backup.close();source.close()
        return backup_path.name

    @staticmethod
    def validate_database(path: Path | str) -> None:
        database_path = Path(path)
        if not database_path.is_file() or database_path.stat().st_size == 0:
            raise ValueError('Tệp database không tồn tại hoặc rỗng.')
        try:
            con = sqlite3.connect(f'{database_path.resolve().as_uri()}?mode=ro', uri=True, timeout=10)
            try:
                integrity = con.execute('PRAGMA integrity_check').fetchone()
                if not integrity or integrity[0] != 'ok':
                    raise ValueError('Database không vượt qua kiểm tra toàn vẹn SQLite.')
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'accounts', 'academic_years', 'classes'}.issubset(tables):
                    raise ValueError('Tệp không có cấu trúc database ClassTrack.')
                if con.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Database có tham chiếu khóa ngoại không hợp lệ.')
            finally:
                con.close()
        except sqlite3.DatabaseError as error:
            raise ValueError(f'Tệp không phải database SQLite hợp lệ: {error}') from error

    def backup_database(self, destination: Path | str | None = None) -> str:
        source_path = Path(self.path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f'Không tìm thấy database: {source_path}')
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            destination_path = source_path.with_name(f'{source_path.name}.backup-{stamp}.bak')
        else:
            destination_path = Path(destination).resolve()
        if destination_path == source_path:
            raise ValueError('Tệp sao lưu không được trùng với database đang dùng.')
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            raise FileExistsError(f'Tệp sao lưu đã tồn tại: {destination_path}')

        try:
            source = sqlite3.connect(self.path, timeout=10)
            try:
                backup = sqlite3.connect(str(destination_path), timeout=10)
                try:
                    source.backup(backup)
                finally:
                    backup.close()
            finally:
                source.close()
            self.validate_database(destination_path)
        except Exception:
            destination_path.unlink(missing_ok=True)
            raise
        return str(destination_path)

    def restore_database(self, backup_path: Path | str) -> str:
        source_path = Path(backup_path).resolve()
        destination_path = Path(self.path).resolve()
        if source_path == destination_path:
            raise ValueError('Tệp khôi phục không được trùng với database đang dùng.')
        self.validate_database(source_path)

        staged_path = destination_path.with_name(
            f'{destination_path.name}.restore-{uuid.uuid4().hex}.tmp'
        )
        try:
            source = sqlite3.connect(f'{source_path.as_uri()}?mode=ro', uri=True, timeout=10)
            try:
                staged = sqlite3.connect(str(staged_path), timeout=10)
                try:
                    source.backup(staged)
                finally:
                    staged.close()
            finally:
                source.close()
            self.validate_database(staged_path)
            safety_backup = self.backup_database()
            os.replace(staged_path, destination_path)
            return safety_backup
        finally:
            staged_path.unlink(missing_ok=True)

    @staticmethod
    def delete_class_data(con: sqlite3.Connection, class_id: str) -> dict[str,int]:
        assignment_ids=[r['id'] for r in con.execute('SELECT id FROM assignments WHERE class_id=?',(class_id,))]
        post_ids=[r['id'] for r in con.execute('SELECT id FROM forum_posts WHERE class_id=?',(class_id,))]
        for post_id in post_ids:
            con.execute('DELETE FROM forum_comments WHERE post_id=?',(post_id,))
            con.execute('DELETE FROM forum_reactions WHERE post_id=?',(post_id,))
        if assignment_ids:
            marks=','.join('?' for _ in assignment_ids)
            con.execute(f'DELETE FROM assignment_submissions WHERE assignment_id IN ({marks})',assignment_ids)
        counts={
            'memberships':con.execute('SELECT COUNT(*) FROM class_memberships WHERE class_id=?',(class_id,)).fetchone()[0],
            'conductRecords':con.execute('SELECT COUNT(*) FROM conduct_records WHERE class_id=?',(class_id,)).fetchone()[0],
            'conductRules':con.execute('SELECT COUNT(*) FROM conduct_rules WHERE class_id=?',(class_id,)).fetchone()[0],
            'assignments':len(assignment_ids),'forumPosts':len(post_ids),
            'importBatches':con.execute('SELECT COUNT(*) FROM import_batches WHERE class_id=?',(class_id,)).fetchone()[0]
        }
        con.execute('DELETE FROM import_batches WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM conduct_records WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM conduct_rules WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM assignments WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM forum_posts WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM class_memberships WHERE class_id=?',(class_id,))
        con.execute('DELETE FROM classes WHERE id=?',(class_id,))
        return counts

    def create_student(self, class_id: str, code: str, name: str, dob: str | None, group: str, actor: str, role: str = 'STUDENT') -> tuple[str, str, str]:
        with self.connect() as con:
            return self.create_student_in(con, class_id, code, name, dob, group, actor, role)

    def create_student_in(self, con: sqlite3.Connection, class_id: str, code: str, name: str, dob: str | None, group: str, actor: str, role: str = 'STUDENT') -> tuple[str, str, str]:
        profile_id, account_id, membership_id = uid('stu'), uid('acc'), uid('mem')
        username_base = ''.join(ch.lower() for ch in code if ch.isalnum()) or 'student'
        activation_code = f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        created = now()
        if not code or not name:
            raise ValueError('Cần có mã học sinh và họ tên.')
        if role not in ('STUDENT', 'GROUP_LEADER', 'TEAM_LEADER', 'CLASS_DEPUTY', 'CLASS_LEADER'):
            raise ValueError('Vai trò trong lớp không hợp lệ.')
        membership_role='GROUP_LEADER' if role in ('GROUP_LEADER','TEAM_LEADER') else role
        cl = con.execute("SELECT academic_year_id,unit_type FROM classes WHERE id=?", (class_id,)).fetchone()
        if not cl:
            raise ValueError('Không tìm thấy lớp.')
        if con.execute("SELECT 1 FROM student_profiles WHERE student_code=? COLLATE NOCASE", (code,)).fetchone():
            raise ValueError('Mã học sinh đã tồn tại.')
        username, suffix = username_base, 1
        while con.execute("SELECT 1 FROM accounts WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            suffix += 1
            username = f"{username_base}{suffix}"
        con.execute("INSERT INTO accounts(id,username,status,role,is_activated,created_at,updated_at,display_name) VALUES(?,?,?,?,?,?,?,?)", (account_id, username, 'PENDING_ACTIVATION', 'STUDENT', 0, created, created, name))
        con.execute("INSERT INTO student_profiles(id,account_id,student_code,full_name,date_of_birth,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (profile_id, account_id, code, name, dob, created, created))
        if membership_role=='GROUP_LEADER' and not group.strip():
            raise ValueError('Hãy phân học sinh vào một tổ/nhóm trước khi giao chức vụ quản lý.')
        if role in ('GROUP_LEADER','TEAM_LEADER') and (role=='TEAM_LEADER')!=(cl['unit_type']=='NHOM'):
            raise ValueError('Chức vụ quản lý không khớp với loại đơn vị của lớp.')
        if membership_role=='GROUP_LEADER' and con.execute("SELECT 1 FROM class_memberships WHERE class_id=? AND academic_year_id=? AND status='ACTIVE' AND role='GROUP_LEADER' AND group_name=? COLLATE NOCASE",(class_id,cl['academic_year_id'],group.strip())).fetchone():
            raise ValueError(f'Đơn vị {group.strip()} đã có người quản lý.')
        con.execute("INSERT INTO class_memberships(id,student_profile_id,class_id,academic_year_id,group_name,role,status,joined_at,left_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'ACTIVE',?,?,?,?,?)", (membership_id, profile_id, class_id, cl['academic_year_id'], group, membership_role, created, None, actor, created, created))
        con.execute("INSERT INTO activation_codes VALUES(?,?,?,?,?,?,?,?)", (uid('act'), profile_id, token_hash(activation_code), (datetime.now(timezone.utc)+timedelta(days=30)).isoformat(timespec='seconds'), None, None, actor, created))
        self.audit(con, actor, 'CREATE_STUDENT_ACCOUNT', 'StudentProfile', profile_id, {'studentCode': code, 'username': username, 'membershipId': membership_id})
        return profile_id, username, activation_code

    def next_student_code(self, con: sqlite3.Connection, reserved: set[str] | None = None) -> str:
        """Generate a readable HS code that is unique across all retained profiles."""
        import re
        reserved = reserved or set()
        used = {str(row[0]).casefold() for row in con.execute('SELECT student_code FROM student_profiles')}
        used.update(code.casefold() for code in reserved)
        largest = 0
        for code in used:
            match = re.fullmatch(r'hs(\d+)', code)
            if match:
                largest = max(largest, int(match.group(1)))
        number = largest + 1
        while True:
            candidate = f'HS{number:04d}'
            if candidate.casefold() not in used:
                return candidate
            number += 1

    def activate(self, code: str, student_code: str, password: str) -> str:
        with self.connect() as con:
            row = con.execute("SELECT ac.*,sp.student_code,sp.account_id FROM activation_codes ac JOIN student_profiles sp ON sp.id=ac.student_profile_id WHERE ac.code_hash=?", (token_hash(code),)).fetchone()
            if not row or row['used_at'] or row['invalidated_at'] or row['expires_at'] < now():
                raise ValueError('Mã kích hoạt không hợp lệ hoặc đã hết hạn.')
            if not hmac.compare_digest(row['student_code'].casefold(), student_code.strip().casefold()):
                raise ValueError('Mã học sinh không khớp.')
            timestamp = now()
            con.execute("UPDATE accounts SET password_hash=?,status='ACTIVE',is_activated=1,updated_at=? WHERE id=? AND status='PENDING_ACTIVATION'", (password_hash(password), timestamp, row['account_id']))
            if con.total_changes == 0:
                raise ValueError('Tài khoản không ở trạng thái chờ kích hoạt.')
            con.execute("UPDATE activation_codes SET used_at=? WHERE id=?", (timestamp, row['id']))
            self.audit(con, row['account_id'], 'ACTIVATE_ACCOUNT', 'Account', row['account_id'])
            return row['account_id']

    def promote(self, old_year: str, new_year: str, class_map: list[dict], actor: str) -> int:
        created = 0
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM academic_years WHERE id=?", (old_year,)).fetchone() or not con.execute("SELECT 1 FROM academic_years WHERE id=?", (new_year,)).fetchone():
                raise ValueError('Năm học không hợp lệ.')
            for item in class_map:
                old_class = con.execute("SELECT id FROM classes WHERE id=? AND academic_year_id=?", (item['oldClassId'], old_year)).fetchone()
                new_class = con.execute("SELECT id FROM classes WHERE id=? AND academic_year_id=?", (item['newClassId'], new_year)).fetchone()
                if not old_class or not new_class:
                    raise ValueError('Lớp không thuộc năm học đã chọn.')
                for student_id in item.get('studentProfileIds', []):
                    old = con.execute("SELECT id FROM class_memberships WHERE student_profile_id=? AND class_id=? AND academic_year_id=? AND status='ACTIVE'", (student_id, old_class['id'], old_year)).fetchone()
                    if not old:
                        raise ValueError('Học sinh không thuộc lớp cũ đã chọn.')
                    exists = con.execute("SELECT id FROM class_memberships WHERE student_profile_id=? AND academic_year_id=?", (student_id, new_year)).fetchone()
                    if exists:
                        raise ValueError('Học sinh đã có phân lớp trong năm học mới.')
                    ts = now()
                    con.execute("INSERT INTO class_memberships(id,student_profile_id,class_id,academic_year_id,group_name,role,status,joined_at,left_at,approved_by,created_at,updated_at) VALUES(?,?,?,?,?,'STUDENT','ACTIVE',?,?,?,?,?)", (uid('mem'), student_id, new_class['id'], new_year, item.get('group',''), ts, None, actor, ts, ts))
                    created += 1
            self.audit(con, actor, 'PROMOTE_STUDENTS', 'AcademicYear', new_year, {'from': old_year, 'membershipsCreated': created})
        return created


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    server_version = "ClassTrack/1.0"
    ADMIN_PERMISSIONS = {
        'dashboard.read', 'users.read', 'users.manage', 'classes.read', 'classes.manage',
        'conduct.read', 'conduct.manage', 'audit_logs.read', 'system.settings'
    }

    def log_message(self, fmt: str, *args) -> None:
        safe_path=urlparse(self.path).path
        print(f"{self.address_string()} - {fmt % (safe_path, *args[1:])}")

    def respond(self, status: int, body: dict | list | None = None, headers: dict | None = None) -> None:
        raw = b'' if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        if headers:
            for key, value in headers.items(): self.send_header(key, value)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        if raw: self.wfile.write(raw)

    def read_json(self) -> dict:
        size = int(self.headers.get('Content-Length', '0'))
        if size > MAX_BODY: raise ValueError('Yêu cầu vượt giới hạn 2 MB.')
        value = json.loads(self.rfile.read(size) or b'{}')
        if not isinstance(value, dict): raise ValueError('JSON phải là object.')
        return value

    def auth(self, roles: tuple[str, ...] | None = None, mutate: bool = False) -> sqlite3.Row:
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
        with STORE.connect() as con:
            row = con.execute("SELECT s.*,a.role,a.status,a.username,a.display_name,a.email,a.is_super_admin,a.id account_id FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires_at>?", (token_hash(token), now())).fetchone() if token else None
            if not row or row['status'] != 'ACTIVE': raise PermissionError('Cần đăng nhập.')
            if roles and row['role'] not in roles: raise PermissionError('Không đủ quyền.')
            if mutate and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), row['csrf_token']): raise PermissionError('CSRF token không hợp lệ.')
            return row

    def start_session(self, account_id: str) -> tuple[str, str]:
        token, csrf, ts = secrets.token_urlsafe(40), secrets.token_urlsafe(24), now()
        with STORE.connect() as con:
            con.execute("INSERT INTO sessions VALUES(?,?,?, ?,?)", (token_hash(token), account_id, csrf, (datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat(timespec='seconds'), ts))
            con.execute("UPDATE accounts SET last_login_at=?,updated_at=? WHERE id=?", (ts, ts, account_id))
        return token, csrf

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ('/', '/activate', '/admin/accept'):
            content=(ROOT/'index.html').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','strict-origin-when-cross-origin');self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()');self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'");self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content);return
        if path == '/api/status':
            with STORE.connect() as con:
                n = con.execute("SELECT COUNT(*) n FROM accounts").fetchone()['n']
                super_admin = con.execute("SELECT COUNT(*) n FROM accounts WHERE is_super_admin=1").fetchone()['n']
            return self.respond(200, {'setupRequired': n == 0, 'bootstrapRequired': super_admin == 0})
        if path == '/api/me':
            try:
                me = self.auth()
                data = {'id': me['account_id'], 'username': me['username'], 'displayName': me['display_name'], 'email': me['email'], 'role': 'SUPER_ADMIN' if me['is_super_admin'] else me['role'], 'isSuperAdmin': bool(me['is_super_admin']), 'csrfToken': me['csrf_token']}
                with STORE.connect() as con:
                    data['permissions'] = [x['permission'] for x in con.execute("SELECT permission FROM admin_permissions WHERE account_id=? ORDER BY permission", (me['account_id'],))]
                    data['avatarData']=con.execute("SELECT avatar_data FROM accounts WHERE id=?",(me['account_id'],)).fetchone()['avatar_data']
                    data['profile'] = dict(r) if (r := con.execute("SELECT id,student_code studentCode,full_name fullName FROM student_profiles WHERE account_id=?", (me['account_id'],)).fetchone()) else None
                    data['memberships'] = [dict(x) for x in con.execute("SELECT cm.id,cm.group_name groupName,CASE WHEN cm.role='GROUP_LEADER' AND c.unit_type='NHOM' THEN 'TEAM_LEADER' ELSE cm.role END role,cm.status,ay.id academicYearId,ay.name academicYear,ay.status yearStatus,c.id classId,COALESCE(c.deleted_name,c.name) className,c.unit_type unitType FROM class_memberships cm JOIN academic_years ay ON ay.id=cm.academic_year_id JOIN classes c ON c.id=cm.class_id WHERE cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?) ORDER BY ay.name DESC", (me['account_id'],))]
                return self.respond(200, data)
            except PermissionError as e: return self.respond(401, {'error': str(e)})
        try:
            me = self.auth()
            q = parse_qs(urlparse(self.path).query)
            if path == '/api/years':
                if me['role']=='ADMIN':
                    try:self.require_permission(me,'classes.read')
                    except PermissionError:self.require_permission(me,'classes.manage')
                with STORE.connect() as con: rows = [dict(x) for x in con.execute("SELECT id,name,start_date startDate,end_date endDate,status FROM academic_years ORDER BY name DESC")]
                return self.respond(200, rows)
            if path == '/api/dashboard/stats':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me, 'dashboard.read')
                year_id=q.get('yearId',[''])[0]
                scope="c.is_deleted=0 AND c.teacher_account_id=?" if me['role']=='TEACHER' else 'c.is_deleted=0'
                params=[me['account_id']] if me['role']=='TEACHER' else []
                if year_id:scope+=' AND c.academic_year_id=?';params.append(year_id)
                with STORE.connect() as con:
                    classes_count=con.execute(f"SELECT COUNT(*) FROM classes c WHERE {scope}",params).fetchone()[0]
                    student_count=con.execute(f"SELECT COUNT(DISTINCT cm.student_profile_id) FROM class_memberships cm JOIN classes c ON c.id=cm.class_id WHERE {scope} AND cm.status='ACTIVE'",params).fetchone()[0]
                    group_count=con.execute(f"SELECT COUNT(DISTINCT cm.group_name) FROM class_memberships cm JOIN classes c ON c.id=cm.class_id WHERE {scope} AND cm.status='ACTIVE' AND cm.group_name<>''",params).fetchone()[0]
                    pending_count=con.execute(f"SELECT COUNT(*) FROM conduct_records cr JOIN classes c ON c.id=cr.class_id WHERE {scope} AND cr.status='PENDING'",params).fetchone()[0]
                    assignment_count=con.execute(f"SELECT COUNT(*) FROM assignments a JOIN classes c ON c.id=a.class_id WHERE {scope} AND (a.due_at IS NULL OR a.due_at>=?)",params+[now()]).fetchone()[0]
                return self.respond(200,{'classCount':classes_count,'studentCount':student_count,'groupCount':group_count,'pendingConductCount':pending_count,'activeAssignmentCount':assignment_count})
            if path == '/api/teachers':
                self.require_roles(me,'ADMIN'); self.require_permission(me, 'users.read')
                with STORE.connect() as con: rows=[dict(x) for x in con.execute("SELECT id accountId,username,display_name displayName,status FROM accounts WHERE role='TEACHER' ORDER BY display_name")]
                return self.respond(200,rows)
            if path == '/api/classes':
                if me['role']=='ADMIN':
                    try:self.require_permission(me,'classes.read')
                    except PermissionError:self.require_permission(me,'classes.manage')
                year = q.get('yearId', [None])[0]
                with STORE.connect() as con:
                    if me['role'] in ('ADMIN','TEACHER'):
                        rows = con.execute("SELECT c.id,c.name,c.academic_year_id academicYearId,ay.name academicYear,c.teacher_account_id teacherId,c.unit_type unitType FROM classes c JOIN academic_years ay ON ay.id=c.academic_year_id WHERE c.is_deleted=0 AND (? IS NULL OR c.academic_year_id=?) AND (?='ADMIN' OR c.teacher_account_id=?) ORDER BY ay.name DESC,c.name", (year, year, me['role'], me['account_id'])).fetchall()
                    else:
                        rows = con.execute("SELECT c.id,c.name,c.academic_year_id academicYearId,ay.name academicYear,c.teacher_account_id teacherId,c.unit_type unitType FROM classes c JOIN academic_years ay ON ay.id=c.academic_year_id JOIN class_memberships cm ON cm.class_id=c.id WHERE c.is_deleted=0 AND cm.status='ACTIVE' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?) AND (? IS NULL OR c.academic_year_id=?)", (me['account_id'],year,year)).fetchall()
                return self.respond(200, [dict(x) for x in rows])
            if path == '/api/accounts':
                self.require_roles(me,'ADMIN','TEACHER');
                if me['role']=='ADMIN': self.require_permission(me, 'users.read')
                class_id=q.get('classId',[''])[0]
                self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    rows=[dict(x) for x in con.execute("SELECT sp.id studentProfileId,sp.student_code studentCode,sp.full_name fullName,a.id accountId,a.username,a.status,a.is_activated isActivated,cm.id membershipId,cm.group_name groupName,CASE WHEN cm.role='GROUP_LEADER' AND c.unit_type='NHOM' THEN 'TEAM_LEADER' ELSE cm.role END membershipRole FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN student_profiles sp ON sp.id=cm.student_profile_id JOIN accounts a ON a.id=sp.account_id WHERE cm.class_id=? ORDER BY sp.full_name",(class_id,))]
                return self.respond(200,rows)
            if path.startswith('/api/classes/') and path.endswith('/students'):
                if me['role']=='ADMIN': self.require_permission(me, 'users.read')
                class_id = path.split('/')[3]
                self.require_class_access(me, class_id, teacher=True)
                with STORE.connect() as con:
                    rows = con.execute("SELECT sp.id studentProfileId,sp.student_code studentCode,sp.full_name fullName,sp.date_of_birth dateOfBirth,sp.gender,cm.id membershipId,cm.group_name groupName,CASE WHEN cm.role='GROUP_LEADER' AND c.unit_type='NHOM' THEN 'TEAM_LEADER' ELSE cm.role END membershipRole,a.username,a.status accountStatus,a.is_activated isActivated FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN student_profiles sp ON sp.id=cm.student_profile_id JOIN accounts a ON a.id=sp.account_id WHERE cm.class_id=? AND cm.status='ACTIVE' ORDER BY sp.full_name", (class_id,)).fetchall()
                return self.respond(200, [dict(x) for x in rows])
            if path == '/api/forum':
                class_id = q.get('classId', [''])[0]
                self.require_class_access(me, class_id)
                with STORE.connect() as con:
                    post_type=q.get('type',['ALL'])[0].upper()
                    if post_type not in ('ALL','POST','ANNOUNCEMENT'): raise ValueError('Bộ lọc bảng tin không hợp lệ.')
                    rows = con.execute("SELECT p.id,p.class_id classId,p.body,p.post_type postType,p.created_at createdAt,a.display_name author,a.avatar_data authorAvatar, (SELECT COUNT(*) FROM forum_reactions r WHERE r.post_id=p.id) reactions,(SELECT COUNT(*) FROM forum_comments c WHERE c.post_id=p.id) commentCount,EXISTS(SELECT 1 FROM forum_reactions r WHERE r.post_id=p.id AND r.account_id=?) liked FROM forum_posts p JOIN accounts a ON a.id=p.author_id WHERE p.class_id=? AND (?='ALL' OR p.post_type=?) ORDER BY CASE p.post_type WHEN 'ANNOUNCEMENT' THEN 0 ELSE 1 END,p.created_at DESC LIMIT 100", (me['account_id'], class_id, post_type, post_type)).fetchall()
                    output=[]
                    for item in rows:
                        x=dict(item);x['comments']=[dict(c) for c in con.execute("SELECT c.id,c.body,c.created_at createdAt,a.display_name author,a.avatar_data authorAvatar FROM forum_comments c JOIN accounts a ON a.id=c.author_id WHERE c.post_id=? ORDER BY c.created_at", (x['id'],))];output.append(x)
                return self.respond(200, output)
            if path == '/api/student/dashboard':
                if me['role'] != 'STUDENT': raise PermissionError('Không đủ quyền.')
                with STORE.connect() as con:
                    profile=con.execute("SELECT id,student_code studentCode,full_name fullName FROM student_profiles WHERE account_id=?", (me['account_id'],)).fetchone()
                    if not profile: return self.respond(200, {'profile':None,'memberships':[],'conduct':[],'assignments':[]})
                    data={'profile':dict(profile)}
                    data['memberships']=[dict(x) for x in con.execute("SELECT ay.name academicYear,COALESCE(c.deleted_name,c.name) className,cm.group_name groupName,CASE WHEN cm.role='GROUP_LEADER' AND c.unit_type='NHOM' THEN 'TEAM_LEADER' ELSE cm.role END role FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.student_profile_id=? ORDER BY ay.name DESC",(profile['id'],))]
                    active=con.execute("SELECT cm.class_id,cm.academic_year_id,c.unit_type FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.student_profile_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE'",(profile['id'],)).fetchone()
                    if active:
                        data['activeClassId']=active['class_id']
                        data['unitType']=active['unit_type']
                        data['conduct']=[dict(x) for x in con.execute("SELECT reason,points,status,created_at createdAt FROM conduct_records WHERE student_profile_id=? AND academic_year_id=? ORDER BY created_at DESC",(profile['id'],active['academic_year_id']))]
                        data['assignments']=[dict(x) for x in con.execute("SELECT id,title,subject,content,due_at dueAt,created_at createdAt FROM assignments WHERE class_id=? ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,created_at DESC",(active['class_id'],))]
                    else: data.update(activeClassId=None,conduct=[],assignments=[])
                return self.respond(200,data)
            if path == '/api/assignments':
                class_id=q.get('classId',[''])[0]
                self.require_class_access(me,class_id)
                with STORE.connect() as con:
                    rows=[dict(x) for x in con.execute("SELECT id,title,subject,content,due_at dueAt,created_at createdAt FROM assignments WHERE class_id=? ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,created_at DESC",(class_id,))]
                return self.respond(200,rows)
            if path == '/api/group/conduct':
                self.require_roles(me,'STUDENT')
                class_id=q.get('classId',[''])[0]
                week_value=q.get('week',[''])[0]
                try:
                    if week_value:
                        match=re.fullmatch(r'(\d{4})-W(\d{2})',week_value)
                        if not match:raise ValueError
                        week_start=date.fromisocalendar(int(match.group(1)),int(match.group(2)),1)
                    else:
                        today=datetime.now(timezone.utc).date()
                        week_start=date.fromisocalendar(today.isocalendar().year,today.isocalendar().week,1)
                except ValueError:raise ValueError('Tuần cần chọn không hợp lệ.')
                week_end=week_start+timedelta(days=7)
                with STORE.connect() as con:
                    leader=con.execute("SELECT cm.academic_year_id,cm.group_name,cm.role,c.unit_type FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone()
                    if not leader or leader['role']!='GROUP_LEADER' or not leader['group_name']:
                        raise PermissionError('Chỉ tổ trưởng hoặc nhóm trưởng mới được xem ghi nhận trong đơn vị của mình.')
                    members=[dict(x) for x in con.execute("SELECT sp.id studentProfileId,sp.full_name fullName,sp.student_code studentCode,a.is_activated isActivated,COALESCE(SUM(cr.points),0) weeklyScore,COALESCE(SUM(CASE WHEN cr.points>0 THEN cr.points ELSE 0 END),0) positivePoints,COALESCE(SUM(CASE WHEN cr.points<0 THEN cr.points ELSE 0 END),0) negativePoints,COUNT(cr.id) verifiedRecords FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id JOIN accounts a ON a.id=sp.account_id LEFT JOIN conduct_records cr ON cr.student_profile_id=sp.id AND cr.class_id=cm.class_id AND cr.academic_year_id=cm.academic_year_id AND cr.group_name=cm.group_name AND cr.status='VERIFIED' AND cr.created_at>=? AND cr.created_at<? WHERE cm.class_id=? AND cm.academic_year_id=? AND cm.group_name=? AND cm.status='ACTIVE' GROUP BY cm.id ORDER BY weeklyScore DESC,sp.full_name",(week_start.isoformat(),week_end.isoformat(),class_id,leader['academic_year_id'],leader['group_name']))]
                    rules=[dict(x) for x in con.execute("SELECT id,title,points FROM conduct_rules WHERE class_id=? AND academic_year_id=? AND active=1 ORDER BY points DESC,title",(class_id,leader['academic_year_id']))]
                    pending=[dict(x) for x in con.execute("SELECT cr.id,sp.id studentProfileId,sp.full_name fullName,cr.reason,cr.points,cr.created_at createdAt FROM conduct_records cr JOIN student_profiles sp ON sp.id=cr.student_profile_id WHERE cr.class_id=? AND cr.academic_year_id=? AND cr.group_name=? AND cr.status='PENDING' AND cr.created_at>=? AND cr.created_at<? ORDER BY cr.created_at DESC LIMIT 100",(class_id,leader['academic_year_id'],leader['group_name'],week_start.isoformat(),week_end.isoformat()))]
                    unassigned=[dict(x) for x in con.execute("SELECT sp.id studentProfileId,sp.full_name fullName,sp.student_code studentCode FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id WHERE cm.class_id=? AND cm.academic_year_id=? AND cm.status='ACTIVE' AND cm.role='STUDENT' AND COALESCE(TRIM(cm.group_name),'')='' ORDER BY sp.full_name",(class_id,leader['academic_year_id']))]
                return self.respond(200,{'groupName':leader['group_name'],'unitType':leader['unit_type'],'week':f"{week_start.isocalendar().year}-W{week_start.isocalendar().week:02d}",'weekStart':week_start.isoformat(),'weekEnd':(week_end-timedelta(days=1)).isoformat(),'members':members,'unassigned':unassigned,'rules':rules,'pending':pending})
            if path == '/api/conduct-rules':
                class_id=q.get('classId',[''])[0]
                if me['role']=='ADMIN':
                    self.require_permission(me,'conduct.read')
                    self.require_class_access(me,class_id,teacher=True)
                    with STORE.connect() as con:
                        rows=[dict(x) for x in con.execute("SELECT cr.id,cr.title,cr.points,cr.active,cr.created_at createdAt FROM conduct_rules cr JOIN classes c ON c.id=cr.class_id AND c.academic_year_id=cr.academic_year_id WHERE cr.class_id=? ORDER BY cr.created_at DESC",(class_id,))]
                elif me['role']=='TEACHER':
                    self.require_class_access(me,class_id,teacher=True)
                    with STORE.connect() as con:
                        rows=[dict(x) for x in con.execute("SELECT cr.id,cr.title,cr.points,cr.active,cr.created_at createdAt FROM conduct_rules cr JOIN classes c ON c.id=cr.class_id AND c.academic_year_id=cr.academic_year_id WHERE cr.class_id=? ORDER BY cr.created_at DESC",(class_id,))]
                elif me['role']=='STUDENT':
                    self.require_class_access(me,class_id)
                    with STORE.connect() as con:
                        membership=con.execute("SELECT cm.academic_year_id FROM class_memberships cm JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone()
                        if not membership: raise PermissionError('Chỉ xem quy tắc của lớp và năm học hiện tại của bạn.')
                        rows=[dict(x) for x in con.execute("SELECT id,title,points,active,created_at createdAt FROM conduct_rules WHERE class_id=? AND academic_year_id=? AND active=1 ORDER BY created_at DESC",(class_id,membership['academic_year_id']))]
                else:
                    raise PermissionError('Không có quyền xem quy tắc điểm.')
                return self.respond(200,rows)
            if path == '/api/conduct/leaderboard':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'conduct.read')
                class_id=q.get('classId',[''])[0];self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    members=[dict(x) for x in con.execute("SELECT cm.group_name groupName,sp.id studentProfileId,sp.full_name fullName,sp.student_code studentCode,COALESCE(SUM(CASE WHEN cr.status='VERIFIED' THEN cr.points ELSE 0 END),0) score FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id LEFT JOIN conduct_records cr ON cr.student_profile_id=sp.id AND cr.class_id=cm.class_id AND cr.academic_year_id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' GROUP BY cm.id ORDER BY cm.group_name,score DESC,sp.full_name",(class_id,))]
                groups={}
                for member in members: groups.setdefault(member['groupName'] or 'Chưa xếp tổ',[]).append(member)
                return self.respond(200,{'groups':groups,'members':members})
            if path == '/api/conduct-records':
                if me['role']=='ADMIN': self.require_permission(me, 'conduct.read')
                class_id=q.get('classId',[''])[0]
                if me['role'] in ('ADMIN','TEACHER'):
                    self.require_class_access(me,class_id,teacher=True)
                    with STORE.connect() as con:
                        rows=[dict(x) for x in con.execute("SELECT cr.id,cr.student_profile_id studentProfileId,sp.full_name fullName,sp.student_code studentCode,cr.reason,cr.points,cr.status,cr.created_at createdAt,cr.group_name groupName,a.display_name recorder FROM conduct_records cr JOIN student_profiles sp ON sp.id=cr.student_profile_id JOIN accounts a ON a.id=cr.recorder_id WHERE cr.class_id=? ORDER BY cr.created_at DESC",(class_id,))]
                else:
                    self.require_class_access(me,class_id)
                    with STORE.connect() as con:
                        rows=[dict(x) for x in con.execute("SELECT cr.id,cr.reason,cr.points,cr.status,cr.created_at createdAt,cr.group_name groupName FROM conduct_records cr WHERE cr.class_id=? AND cr.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?) ORDER BY cr.created_at DESC",(class_id,me['account_id']))]
                return self.respond(200,rows)
            if path == '/api/students/memberships':
                if me['role'] in ('ADMIN','TEACHER'):
                    if me['role']=='ADMIN': self.require_permission(me,'users.read')
                    student_id=q.get('studentProfileId',[''])[0]
                    with STORE.connect() as con:
                        if me['role']=='TEACHER' and not con.execute("SELECT 1 FROM class_memberships cm JOIN classes c ON c.id=cm.class_id WHERE cm.student_profile_id=? AND c.teacher_account_id=?",(student_id,me['account_id'])).fetchone():
                            raise PermissionError('Không có quyền xem hồ sơ học sinh này.')
                        scope='' if me['role']=='ADMIN' else ' AND c.teacher_account_id=?'
                        args=(student_id,) if me['role']=='ADMIN' else (student_id,me['account_id'])
                        out=[dict(x) for x in con.execute("SELECT cm.*,ay.name academicYear,COALESCE(c.deleted_name,c.name) className FROM class_memberships cm JOIN academic_years ay ON ay.id=cm.academic_year_id JOIN classes c ON c.id=cm.class_id WHERE cm.student_profile_id=?"+scope+" ORDER BY ay.name",args)]
                else:
                    with STORE.connect() as con: out=[dict(x) for x in con.execute("SELECT cm.*,ay.name academicYear,COALESCE(c.deleted_name,c.name) className FROM class_memberships cm JOIN academic_years ay ON ay.id=cm.academic_year_id JOIN classes c ON c.id=cm.class_id WHERE cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?) ORDER BY ay.name",(me['account_id'],))]
                return self.respond(200,out)
            if path == '/api/admin/overview':
                self.require_super_admin(me)
                with STORE.connect() as con:
                    stats = {key: con.execute(sql).fetchone()[0] for key,sql in {
                        'accountCount':'SELECT COUNT(*) FROM accounts', 'adminCount':"SELECT COUNT(*) FROM accounts WHERE role='ADMIN' AND is_super_admin=0 AND status<>'DISABLED'",
                        'teacherCount':"SELECT COUNT(*) FROM accounts WHERE role='TEACHER'", 'studentCount':'SELECT COUNT(*) FROM student_profiles',
                        'classCount':'SELECT COUNT(*) FROM classes', 'pendingActivationCount':"SELECT COUNT(*) FROM accounts WHERE status='PENDING_ACTIVATION'"
                    }.items()}
                    stats['pendingInvitations']=con.execute("SELECT COUNT(*) FROM admin_invitations WHERE status='PENDING' AND expires_at>?",(now(),)).fetchone()[0]
                return self.respond(200, stats)
            if path == '/api/admin/administrators':
                self.require_super_admin(me)
                with STORE.connect() as con:
                    admins=[]
                    for x in con.execute("SELECT id,username,email,display_name displayName,status,last_login_at lastLoginAt,is_super_admin isSuperAdmin FROM accounts WHERE role='ADMIN' AND is_super_admin=0 ORDER BY display_name,username"):
                        item=dict(x);item['permissions']=[p[0] for p in con.execute('SELECT permission FROM admin_permissions WHERE account_id=? ORDER BY permission',(item['id'],))]
                        item['grant']=dict(g) if (g:=con.execute('SELECT created_at grantedAt,revoked_at revokedAt,granted_by grantedBy FROM admin_grants WHERE account_id=?',(item['id'],)).fetchone()) else None
                        admins.append(item)
                    invitations=[dict(x) for x in con.execute("SELECT id,email,permissions_json permissions,expires_at expiresAt,status,created_at createdAt FROM admin_invitations ORDER BY created_at DESC LIMIT 100")]
                    for item in invitations: item['permissions']=json.loads(item['permissions'])
                return self.respond(200,{'administrators':admins,'invitations':invitations,'permissions':sorted(self.ADMIN_PERMISSIONS)})
            if path == '/api/admin/audit-logs':
                if not me['is_super_admin']: self.require_permission(me,'audit_logs.read')
                else: self.require_super_admin(me)
                with STORE.connect() as con:
                    logs=[dict(x) for x in con.execute("SELECT l.id,l.actor_id actorId,a.username actorUsername,l.action,l.entity_type entityType,l.entity_id entityId,l.metadata,l.created_at createdAt,l.result FROM audit_logs l LEFT JOIN accounts a ON a.id=l.actor_id ORDER BY l.created_at DESC LIMIT 200")]
                for item in logs:
                    try: item['metadata']=json.loads(item['metadata'])
                    except (ValueError,TypeError): item['metadata']={}
                return self.respond(200,logs)
            return self.respond(404, {'error':'Không tìm thấy API.'})
        except PermissionError as e: return self.respond(403, {'error':str(e)})
        except Exception as e: return self.respond(400, {'error':str(e)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try: data = self.read_json()
        except Exception as e: return self.respond(400, {'error':str(e)})
        try:
            if path == '/api/setup':
                return self.respond(410, {'error':'Không hỗ trợ tạo quản trị viên công khai. Hãy chạy: python server.py bootstrap-super-admin'})
            if path == '/api/admin/invitations/accept':
                self.limit_attempts('admin-accept:'+self.client_ip())
                account_id, token_value = self.accept_admin_invitation(data)
                token,csrf=self.start_session(account_id)
                return self.respond(200,{'csrfToken':csrf,'role':'ADMIN','message':'Lời mời đã được chấp nhận.'},{'Set-Cookie':session_cookie(token,SESSION_DAYS*86400)})
            if path == '/api/teachers':
                me=self.auth(mutate=True);self.require_roles(me,'ADMIN');self.require_permission(me,'users.manage')
                username=self.username(data.get('username'));password=self.password(data.get('password'));display=str(data.get('displayName') or username).strip();email=str(data.get('email','')).strip().lower() or None
                if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): raise ValueError('Địa chỉ email không hợp lệ.')
                account=uid('acc');ts=now()
                with STORE.connect() as con:
                    con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','TEACHER',1,?,?,?,?)",(account,username,password_hash(password),ts,ts,display,email));STORE.audit(con,me['account_id'],'CREATE_TEACHER_ACCOUNT','Account',account,{'username':username,'email':email})
                return self.respond(201,{'accountId':account,'username':username,'status':'ACTIVE'})
            if path == '/api/login':
                key='login:'+self.client_ip()
                with RATE_LOCK:
                    recent=[x for x in FAILED_CODES.get(key,[]) if time.time()-x<900]
                    if len(recent)>=10:return self.respond(429,{'error':'Đăng nhập sai quá nhiều lần. Vui lòng thử lại sau 15 phút.'})
                username=str(data.get('username','')).strip().casefold()
                with STORE.connect() as con: account=con.execute("SELECT * FROM accounts WHERE username=? COLLATE NOCASE",(username,)).fetchone()
                if not account or account['status']!='ACTIVE' or not check_password(str(data.get('password','')),account['password_hash']):
                    with RATE_LOCK: FAILED_CODES[key]=recent+[time.time()]
                    return self.respond(401,{'error':'Tên đăng nhập hoặc mật khẩu không đúng.'})
                with RATE_LOCK: FAILED_CODES.pop(key,None)
                token,csrf=self.start_session(account['id'])
                return self.respond(200,{'csrfToken':csrf,'role':account['role']},{'Set-Cookie':session_cookie(token,SESSION_DAYS*86400)})
            if path == '/api/activation/complete':
                key=self.client_ip();
                with RATE_LOCK:
                    recent=[x for x in FAILED_CODES.get(key,[]) if time.time()-x<900]
                    if len(recent)>=10:return self.respond(429,{'error':'Thử lại sau 15 phút.'})
                code=str(data.get('activationCode','')).strip().upper();student_code=str(data.get('studentCode','')).strip();password=self.password(data.get('password'))
                try: account=STORE.activate(code,student_code,password)
                except ValueError:
                    with RATE_LOCK: FAILED_CODES[key]=recent+[time.time()]
                    raise
                token,csrf=self.start_session(account)
                return self.respond(200,{'csrfToken':csrf,'message':'Tài khoản đã kích hoạt.'},{'Set-Cookie':session_cookie(token,SESSION_DAYS*86400)})
            me=self.auth(mutate=True)
            if path == '/api/logout':
                cookie=SimpleCookie(self.headers.get('Cookie',''));token=cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
                with STORE.connect() as con:con.execute("DELETE FROM sessions WHERE token_hash=?",(token_hash(token),))
                return self.respond(200,{'ok':True},{'Set-Cookie':session_cookie('',0)})
            if path == '/api/me/credentials':
                requested_username=data.get('username')
                current_password=str(data.get('currentPassword',''))
                new_password=str(data.get('newPassword',''))
                if not current_password:raise ValueError('Nh\u1eadp m\u1eadt kh\u1ea9u hi\u1ec7n t\u1ea1i \u0111\u1ec3 x\u00e1c nh\u1eadn thay \u0111\u1ed5i.')
                if new_password:self.password(new_password)
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    account=con.execute("SELECT username,password_hash FROM accounts WHERE id=? AND status='ACTIVE'",(me['account_id'],)).fetchone()
                    if not account or not check_password(current_password,account['password_hash']):
                        raise PermissionError('M\u1eadt kh\u1ea9u hi\u1ec7n t\u1ea1i kh\u00f4ng \u0111\u00fang.')
                    if requested_username is not None and str(requested_username).strip().casefold()!=account['username'].casefold():
                        raise PermissionError('T\u00ean \u0111\u0103ng nh\u1eadp do qu\u1ea3n tr\u1ecb vi\u00ean qu\u1ea3n l\u00fd v\u00e0 kh\u00f4ng th\u1ec3 t\u1ef1 thay \u0111\u1ed5i.')
                    if not new_password:raise ValueError('Nh\u1eadp m\u1eadt kh\u1ea9u m\u1edbi n\u1ebfu b\u1ea1n mu\u1ed1n thay \u0111\u1ed5i m\u1eadt kh\u1ea9u.')
                    con.execute('UPDATE accounts SET password_hash=?,updated_at=? WHERE id=?',(password_hash(new_password),now(),me['account_id']))
                    con.execute('DELETE FROM sessions WHERE account_id=? AND token_hash<>?',(me['account_id'],me['token_hash']))
                    STORE.audit(con,me['account_id'],'UPDATE_OWN_CREDENTIALS','Account',me['account_id'],{'passwordChanged':True})
                return self.respond(200,{'username':account['username'],'passwordChanged':True})
            if path == '/api/me/profile':
                display=str(data.get('displayName','')).strip()
                if me['role']=='STUDENT':
                    with STORE.connect() as con:
                        student=con.execute('SELECT full_name FROM student_profiles WHERE account_id=?',(me['account_id'],)).fetchone()
                    if not student:raise ValueError('Kh\u00f4ng t\u00ecm th\u1ea5y h\u1ed3 s\u01a1 h\u1ecdc sinh.')
                    display=student['full_name']
                if not display or len(display)>80:raise ValueError('T\u00ean hi\u1ec3n th\u1ecb c\u1ea7n t\u1eeb 1 \u0111\u1ebfn 80 k\u00fd t\u1ef1.')
                avatar=str(data.get('avatarData',''))
                if avatar:
                    if len(avatar)>500_000 or ',' not in avatar:raise ValueError('Ảnh đại diện vượt quá giới hạn 350 KB hoặc không hợp lệ.')
                    header,payload=avatar.split(',',1)
                    allowed={'data:image/png;base64':'png','data:image/jpeg;base64':'jpeg','data:image/webp;base64':'webp'}
                    if header not in allowed:raise ValueError('Chỉ nhận ảnh PNG, JPEG hoặc WebP.')
                    try: raw=base64.b64decode(payload,validate=True)
                    except (binascii.Error,ValueError):raise ValueError('Dữ liệu ảnh không hợp lệ.')
                    if len(raw)>350_000:raise ValueError('Ảnh đại diện tối đa 350 KB.')
                    fmt=allowed[header]
                    valid=(fmt=='png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or (fmt=='jpeg' and raw.startswith(b'\xff\xd8\xff')) or (fmt=='webp' and len(raw)>12 and raw.startswith(b'RIFF') and raw[8:12]==b'WEBP')
                    if not valid:raise ValueError('Nội dung tệp không phải ảnh hợp lệ.')
                with STORE.connect() as con:
                    con.execute('UPDATE accounts SET display_name=?,avatar_data=?,updated_at=? WHERE id=?',(display,avatar,now(),me['account_id']))
                    STORE.audit(con,me['account_id'],'UPDATE_OWN_PROFILE','Account',me['account_id'],{'displayNameChanged':True,'avatarUpdated':bool(avatar)})
                return self.respond(200,{'displayName':display,'avatarData':avatar})
            if path == '/api/admin/invitations':
                self.require_super_admin(me)
                email=str(data.get('email','')).strip().lower();permissions=data.get('permissions',[])
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email) or len(email)>254: raise ValueError('Địa chỉ email không hợp lệ.')
                if not isinstance(permissions,list) or len(set(permissions))!=len(permissions) or any(x not in self.ADMIN_PERMISSIONS for x in permissions): raise ValueError('Danh sách quyền không hợp lệ.')
                with STORE.connect() as con:
                    existing=con.execute("SELECT role,status FROM accounts WHERE email=? COLLATE NOCASE",(email,)).fetchone()
                    if existing and existing['role']!='TEACHER': raise ValueError('Email này đã được gắn với tài khoản hiện có.')
                    if con.execute("SELECT 1 FROM admin_invitations WHERE email=? COLLATE NOCASE AND status='PENDING' AND expires_at>?",(email,now())).fetchone(): raise ValueError('Địa chỉ này đã có lời mời đang chờ.')
                    invite_id=uid('inv'); token_value=secrets.token_urlsafe(32); expires=(datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(timespec='seconds')
                    con.execute("INSERT INTO admin_invitations(id,email,token_hash,permissions_json,expires_at,created_by,status,created_at) VALUES(?,?,?,?,?,?,'PENDING',?)",(invite_id,email,token_hash(token_value),json.dumps(permissions),expires,me['account_id'],now()))
                    STORE.audit(con,me['account_id'],'CREATE_ADMIN_INVITATION','AdminInvitation',invite_id,{'email':email,'permissions':permissions,'expiresAt':expires})
                link=self.build_invitation_link(token_value)
                delivered=self.send_admin_invitation(email,link)
                return self.respond(201,{'id':invite_id,'email':email,'expiresAt':expires,'delivery':'EMAIL_SENT' if delivered else 'MANUAL_DELIVERY','invitationLink':link})
            if path.startswith('/api/admin/invitations/') and path.endswith('/revoke'):
                self.require_super_admin(me);invite_id=path.split('/')[4]
                with STORE.connect() as con:
                    changed=con.execute("UPDATE admin_invitations SET status='REVOKED' WHERE id=? AND status='PENDING'",(invite_id,)).rowcount
                    if not changed: raise ValueError('Không tìm thấy lời mời đang chờ.')
                    STORE.audit(con,me['account_id'],'REVOKE_ADMIN_INVITATION','AdminInvitation',invite_id)
                return self.respond(200,{'status':'REVOKED'})
            if path == '/api/years':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                status=data.get('status','UPCOMING')
                if not isinstance(status,str): raise ValueError('Trạng thái năm học không hợp lệ.')
                status=status.upper()
                if status not in ('UPCOMING','ACTIVE','ARCHIVED'):raise ValueError('Trạng thái năm học không hợp lệ.')
                year=STORE.create_year(str(data.get('name','')).strip(),data.get('startDate'),data.get('endDate'),status,me['account_id'])
                return self.respond(201,{'id':year})
            if path == '/api/classes':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                year=str(data.get('academicYearId',''));name=str(data.get('name','')).strip()
                if not name:raise ValueError('Nhập tên lớp.')
                owner=me['account_id']
                if me['role']=='ADMIN' and data.get('teacherAccountId'):
                    owner=str(data['teacherAccountId'])
                    with STORE.connect() as con:
                        if not con.execute("SELECT 1 FROM accounts WHERE id=? AND role='TEACHER' AND status='ACTIVE'",(owner,)).fetchone():raise ValueError('Chỉ có thể gán lớp cho tài khoản giáo viên đang hoạt động.')
                cid=STORE.create_class(year,name,owner)
                return self.respond(201,{'id':cid})
            if path.startswith('/api/classes/') and path.endswith('/students'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                class_id=path.split('/')[3];self.require_class_access(me,class_id,teacher=True)
                dob=str(data.get('dateOfBirth') or '').strip() or None
                if dob:
                    try: date.fromisoformat(dob)
                    except ValueError: raise ValueError('Invalid birth date; use YYYY-MM-DD.')
                profile,username,code=STORE.create_student(class_id,str(data.get('studentCode','')).strip(),str(data.get('fullName','')).strip(),dob,str(data.get('group','')).strip(),me['account_id'],str(data.get('role','STUDENT')).upper())
                return self.respond(201,{'studentProfileId':profile,'username':username,'status':'PENDING_ACTIVATION','activationCode':code,'activationLink':'/activate'})
            if path.startswith('/api/classes/') and path.endswith('/students/import/preview'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                class_id=path.split('/')[3];self.require_class_access(me,class_id,teacher=True)
                rows=data.get('rows',[])
                if not isinstance(rows,list) or len(rows)>1000:raise ValueError('Danh sách không hợp lệ (tối đa 1000 dòng).')
                with STORE.connect() as con:
                    existing={x[0].casefold() for x in con.execute("SELECT student_code FROM student_profiles")};seen=set();valid=[];errors=[];reserved={str(row.get('studentCode','')).strip().casefold() for row in rows if isinstance(row,dict) and str(row.get('studentCode','')).strip()}
                    for i,row in enumerate(rows, start=1):
                        if not isinstance(row,dict): errors.append({'row':i,'studentCode':'','errors':['Invalid row']});continue
                        code=str(row.get('studentCode','')).strip();auto_code=not code;name=str(row.get('fullName','')).strip();group=str(row.get('group','')).strip();dob=str(row.get('dateOfBirth') or '').strip()
                        issue=[]
                        if not code and not name and not dob and not group:issue.append('Blank row')
                        if not name:issue.append('Missing full name')
                        if code.casefold() in existing or code.casefold() in seen:issue.append('Duplicate student code')
                        if dob:
                            try: date.fromisoformat(dob)
                            except ValueError: issue.append('Invalid birth date; use YYYY-MM-DD')
                        if code:seen.add(code.casefold())
                        if issue:errors.append({'row':i,'studentCode':code,'errors':issue})
                        else:
                            if auto_code:
                                code=STORE.next_student_code(con,reserved)
                                reserved.add(code)
                            valid.append({'studentCode':code,'autoStudentCode':auto_code,'fullName':name,'dateOfBirth':dob or None,'group':group})
                    batch=uid('batch');con.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?)",(batch,class_id,me['account_id'],json.dumps(valid,ensure_ascii=False),'PREVIEW',now()))
                return self.respond(200,{'previewId':batch,'totalRows':len(rows),'validCount':len(valid),'errorCount':len(errors),'errors':errors,'preview':valid})
            if path.startswith('/api/imports/') and path.endswith('/confirm'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                batch_id=path.split('/')[3]
                with STORE.connect() as con:
                    batch=con.execute("SELECT * FROM import_batches WHERE id=? AND created_by=? AND status='PREVIEW'",(batch_id,me['account_id'])).fetchone()
                    if not batch:raise ValueError('Không tìm thấy bản xem trước hoặc đã xác nhận.')
                    self.require_class_access(me,batch['class_id'],teacher=True)
                    rows=json.loads(batch['rows_json']);con.execute('BEGIN IMMEDIATE');created=[];used_codes=set();explicit_codes={row['studentCode'].casefold() for row in rows if not row.get('autoStudentCode')}
                    for row in rows:
                        code=row['studentCode']
                        if row.get('autoStudentCode') and (code.casefold() in explicit_codes or code.casefold() in used_codes or con.execute('SELECT 1 FROM student_profiles WHERE student_code=? COLLATE NOCASE',(code,)).fetchone()):
                            code=STORE.next_student_code(con,explicit_codes|used_codes)
                        if code.casefold() in used_codes:
                            raise ValueError('Mã học sinh bị trùng trong lượt nhập. Hãy kiểm tra lại danh sách.')
                        used_codes.add(code.casefold())
                        result=STORE.create_student_in(con,batch['class_id'],code,row['fullName'],row.get('dateOfBirth'),row.get('group',''),me['account_id'])
                        created.append(({'studentCode':code,'fullName':row['fullName']},result))
                    con.execute("UPDATE import_batches SET status='CONFIRMED' WHERE id=?",(batch_id,))
                return self.respond(201,{'createdCount':len(created),'activationCodes':[{'studentCode':r['studentCode'],'fullName':r['fullName'],'username':x[1],'activationCode':x[2]} for r,x in created]})
            if path.startswith('/api/imports/') and path.endswith('/cancel'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                batch_id=path.split('/')[3]
                with STORE.connect() as con:
                    batch=con.execute("SELECT id,class_id,status FROM import_batches WHERE id=? AND created_by=?",(batch_id,me['account_id'])).fetchone()
                    if not batch or batch['status']!='PREVIEW': raise ValueError('Import preview is not pending.')
                    self.require_class_access(me,batch['class_id'],teacher=True)
                    con.execute("UPDATE import_batches SET status='CANCELLED' WHERE id=?",(batch_id,))
                    STORE.audit(con,me['account_id'],'CANCEL_STUDENT_IMPORT','ImportBatch',batch_id)
                return self.respond(200,{'status':'CANCELLED'})
            if path.startswith('/api/students/') and path.endswith('/activation-code'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                student_id=path.split('/')[3]
                with STORE.connect() as con:
                    row=con.execute("SELECT sp.account_id,a.status FROM student_profiles sp JOIN accounts a ON a.id=sp.account_id WHERE sp.id=?",(student_id,)).fetchone()
                    if not row:raise ValueError('Không tìm thấy học sinh.')
                    if row['status']!='PENDING_ACTIVATION':raise ValueError('Tài khoản đã kích hoạt hoặc bị khóa.')
                    if me['role']=='TEACHER' and not con.execute("SELECT 1 FROM class_memberships cm JOIN classes c ON c.id=cm.class_id WHERE cm.student_profile_id=? AND c.teacher_account_id=?",(student_id,me['account_id'])).fetchone():raise PermissionError('Không có quyền với học sinh này.')
                    code=f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}";ts=now()
                    con.execute("UPDATE activation_codes SET invalidated_at=? WHERE student_profile_id=? AND used_at IS NULL AND invalidated_at IS NULL",(ts,student_id))
                    con.execute("INSERT INTO activation_codes VALUES(?,?,?,?,?,?,?,?)",(uid('act'),student_id,token_hash(code),(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(timespec='seconds'),None,None,me['account_id'],ts));STORE.audit(con,me['account_id'],'REISSUE_ACTIVATION_CODE','StudentProfile',student_id)
                return self.respond(201,{'activationCode':code,'activationLink':'/activate'})
            if path == '/api/promote':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                created=STORE.promote(str(data.get('oldYearId','')),str(data.get('newYearId','')),data.get('classes',[]),me['account_id'])
                return self.respond(201,{'membershipsCreated':created})
            if path.startswith('/api/classes/') and path.endswith('/unit-type'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                class_id=path.split('/')[3]
                unit_type=str(data.get('unitType','')).upper()
                if unit_type not in ('TO','NHOM'):raise ValueError('Loại đơn vị phải là tổ hoặc nhóm.')
                self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    row=con.execute('SELECT unit_type FROM classes WHERE id=? AND is_deleted=0',(class_id,)).fetchone()
                    if not row:raise ValueError('Không tìm thấy lớp đang hoạt động.')
                    con.execute('UPDATE classes SET unit_type=? WHERE id=?',(unit_type,class_id))
                    STORE.audit(con,me['account_id'],'CHANGE_CLASS_UNIT_TYPE','Class',class_id,{'from':row['unit_type'],'to':unit_type})
                return self.respond(200,{'classId':class_id,'unitType':unit_type})
            if path.startswith('/api/memberships/') and path.endswith('/role'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                membership_id=path.split('/')[3];role=str(data.get('role','')).upper()
                if role not in ('STUDENT','GROUP_LEADER','TEAM_LEADER','CLASS_DEPUTY','CLASS_LEADER'):raise ValueError('Vai tr\u00f2 trong l\u1edbp kh\u00f4ng h\u1ee3p l\u1ec7.')
                membership_role='GROUP_LEADER' if role in ('GROUP_LEADER','TEAM_LEADER') else role
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    row=con.execute("SELECT cm.class_id,cm.academic_year_id,cm.role old_role,cm.group_name old_group,cm.group_name,cm.status,c.unit_type FROM class_memberships cm JOIN classes c ON c.id=cm.class_id WHERE cm.id=?",(membership_id,)).fetchone()
                    if not row:raise ValueError('Không tìm thấy phân lớp.')
                    self.require_class_access(me,row['class_id'],teacher=True)
                    group=' '.join(str(data.get('groupName',row['group_name']) or '').split())
                    if len(group)>80:raise ValueError('Tên tổ/nhóm không được vượt quá 80 ký tự.')
                    if row['status']=='ACTIVE' and membership_role=='GROUP_LEADER' and not group:
                        raise ValueError('Hãy xếp học sinh vào một tổ/nhóm trước khi giao chức vụ quản lý.')
                    if role in ('GROUP_LEADER','TEAM_LEADER') and (role=='TEAM_LEADER')!=(row['unit_type']=='NHOM'):
                        raise ValueError('Chức vụ tổ trưởng/nhóm trưởng phải khớp với loại đơn vị đã chọn cho lớp.')
                    if membership_role=='GROUP_LEADER' and row['status']=='ACTIVE':
                        leaders=con.execute("SELECT cm.group_name FROM class_memberships cm WHERE cm.class_id=? AND cm.academic_year_id=? AND cm.status='ACTIVE' AND cm.role='GROUP_LEADER' AND cm.id<>?",(row['class_id'],row['academic_year_id'],membership_id)).fetchall()
                        if any(' '.join(str(x['group_name'] or '').split()).casefold()==group.casefold() for x in leaders):
                            raise ValueError(f'Đơn vị {group} đã có tổ trưởng hoặc nhóm trưởng. Hãy chọn tổ/nhóm khác hoặc đổi người quản lý hiện tại trước.')
                    con.execute("UPDATE class_memberships SET role=?,group_name=?,updated_at=? WHERE id=?",(membership_role,group,now(),membership_id))
                    STORE.audit(con,me['account_id'],'CHANGE_MEMBERSHIP_ROLE','ClassMembership',membership_id,{'from':row['old_role'],'to':role,'unitType':row['unit_type'],'oldGroup':row['old_group'],'group':group})
                return self.respond(200,{'membershipId':membership_id,'role':role,'unitType':row['unit_type'],'groupName':group})
            if path == '/api/conduct-rules':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'conduct.manage')
                class_id=str(data.get('classId',''));self.require_class_access(me,class_id,teacher=True)
                title=str(data.get('title','')).strip();points=int(data.get('points',0))
                if not title or len(title)>120:raise ValueError('Tên quy tắc phải có từ 1 đến 120 ký tự.')
                if not points or abs(points)>100:raise ValueError('Điểm quy ước phải nằm trong khoảng -100 đến 100 và khác 0.')
                with STORE.connect() as con:
                    cl=con.execute('SELECT academic_year_id FROM classes WHERE id=?',(class_id,)).fetchone()
                    if not cl:raise ValueError('Không tìm thấy lớp.')
                    duplicate=con.execute('SELECT 1 FROM conduct_rules WHERE class_id=? AND academic_year_id=? AND title=?',(class_id,cl['academic_year_id'],title)).fetchone()
                    if duplicate:raise ValueError('Quy tắc này đã có trong lớp đang chọn.')
                    rule_id=uid('rule');con.execute("INSERT INTO conduct_rules(id,class_id,academic_year_id,title,points,created_by,created_at) VALUES(?,?,?,?,?,?,?)",(rule_id,class_id,cl['academic_year_id'],title,points,me['account_id'],now()))
                    STORE.audit(con,me['account_id'],'CREATE_CONDUCT_RULE','ConductRule',rule_id,{'classId':class_id,'title':title,'points':points})
                return self.respond(201,{'id':rule_id,'title':title,'points':points})
            if path == '/api/group/memberships/assign':
                self.require_roles(me,'STUDENT')
                class_id=str(data.get('classId',''));student_id=str(data.get('studentProfileId',''))
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    leader=con.execute("SELECT cm.academic_year_id,cm.group_name,c.unit_type FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE' AND cm.role='GROUP_LEADER' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone()
                    if not leader or not leader['group_name']:raise PermissionError('Chỉ tổ trưởng hoặc nhóm trưởng đang hoạt động của lớp mới được phân học sinh vào đơn vị của mình.')
                    target=con.execute("SELECT cm.id,cm.group_name,cm.role FROM class_memberships cm WHERE cm.student_profile_id=? AND cm.class_id=? AND cm.academic_year_id=? AND cm.status='ACTIVE'",(student_id,class_id,leader['academic_year_id'])).fetchone()
                    if not target:raise PermissionError('Student is not an active member of this class.')
                    if target['role']!='STUDENT' or (target['group_name'] or '').strip():raise ValueError('Student is already assigned to a group or has a class role. Ask the teacher to change the assignment.')
                    ts=now();con.execute('UPDATE class_memberships SET group_name=?,updated_at=? WHERE id=?',(leader['group_name'],ts,target['id']))
                    STORE.audit(con,me['account_id'],'GROUP_LEADER_ASSIGN_MEMBER','ClassMembership',target['id'],{'classId':class_id,'studentProfileId':student_id,'groupName':leader['group_name']})
                return self.respond(200,{'membershipId':target['id'],'groupName':leader['group_name']})
            if path == '/api/group/conduct/publish':
                self.require_roles(me,'STUDENT')
                class_id=str(data.get('classId',''))
                week_value=str(data.get('week',''))
                try:
                    match=re.fullmatch(r'(\d{4})-W(\d{2})',week_value)
                    if not match:raise ValueError
                    week_start=date.fromisocalendar(int(match.group(1)),int(match.group(2)),1)
                except ValueError:raise ValueError('Tuần cần chọn không hợp lệ.')
                week_end=week_start+timedelta(days=7)
                with STORE.connect() as con:
                    leader=con.execute("SELECT cm.academic_year_id,cm.group_name,c.unit_type FROM class_memberships cm JOIN classes c ON c.id=cm.class_id JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE' AND cm.role='GROUP_LEADER' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone()
                    if not leader or not leader['group_name']:
                        raise PermissionError('Chỉ tổ trưởng hoặc nhóm trưởng của lớp hiện tại mới được đăng kết quả đơn vị.')
                    members=[dict(x) for x in con.execute("SELECT sp.full_name fullName,COALESCE(SUM(CASE WHEN cr.status='VERIFIED' AND cr.created_at>=? AND cr.created_at<? THEN cr.points ELSE 0 END),0) score,COUNT(CASE WHEN cr.status='VERIFIED' AND cr.created_at>=? AND cr.created_at<? THEN cr.id END) verifiedRecords FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id LEFT JOIN conduct_records cr ON cr.student_profile_id=sp.id AND cr.class_id=cm.class_id AND cr.academic_year_id=cm.academic_year_id AND cr.group_name=cm.group_name WHERE cm.class_id=? AND cm.academic_year_id=? AND cm.group_name=? AND cm.status='ACTIVE' GROUP BY cm.id ORDER BY score DESC,sp.full_name",(week_start.isoformat(),week_end.isoformat(),week_start.isoformat(),week_end.isoformat(),class_id,leader['academic_year_id'],leader['group_name']))]
                    if not any(member['verifiedRecords'] for member in members):
                        raise ValueError('Chưa có điểm đã được giáo viên xác minh trong tuần này.')
                    cl=con.execute("SELECT c.name,ay.name academicYear FROM classes c JOIN academic_years ay ON ay.id=c.academic_year_id WHERE c.id=? AND c.academic_year_id=?",(class_id,leader['academic_year_id'])).fetchone()
                    week=f"{week_start.isocalendar().year}-W{week_start.isocalendar().week:02d}"
                    unit_type='nhóm' if leader['unit_type']=='NHOM' else 'tổ'
                    lines=[f"BẢNG ĐIỂM RÈN LUYỆN · {cl['name']} · {leader['group_name']} · {cl['academicYear']} · {week}","Chỉ gồm điểm đã được giáo viên xác minh; ghi nhận đang chờ duyệt chưa được tính."]
                    lines.extend(f"{index}. {member['fullName']} — {member['score']} điểm" for index,member in enumerate(members,1))
                    body='\n'.join(lines)
                    if len(body)>5000:raise ValueError('Bảng điểm tổ vượt giới hạn bài đăng.')
                    post_id=uid('post');ts=now()
                    con.execute("INSERT INTO forum_posts(id,class_id,academic_year_id,author_id,body,created_at,updated_at,post_type) VALUES(?,?,?,?,?,?,?,'ANNOUNCEMENT')",(post_id,class_id,leader['academic_year_id'],me['account_id'],body,ts,ts))
                    STORE.audit(con,me['account_id'],'GROUP_LEADER_PUBLISH_CONDUCT','ForumPost',post_id,{'classId':class_id,'groupName':leader['group_name'],'unitType':leader['unit_type'],'week':week,'memberCount':len(members)})
                return self.respond(201,{'id':post_id,'memberCount':len(members),'week':week,'unitType':leader['unit_type']})
            if path == '/api/group/conduct-records':
                self.require_roles(me,'STUDENT')
                class_id=str(data.get('classId',''));student_id=str(data.get('studentProfileId',''));rule_id=str(data.get('ruleId',''));reason=str(data.get('reason','')).strip()
                if ((not rule_id and not reason) or len(reason)>500):raise ValueError('Mô tả cần có từ 1 đến 500 ký tự.')
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    leader=con.execute("SELECT cm.academic_year_id,cm.group_name FROM class_memberships cm JOIN academic_years ay ON ay.id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' AND ay.status='ACTIVE' AND cm.role='GROUP_LEADER' AND cm.student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone()
                    if not leader or not leader['group_name']:raise PermissionError('Chỉ tổ trưởng hoặc nhóm trưởng của lớp hiện tại được ghi nhận cho đơn vị mình.')
                    target=con.execute("SELECT 1 FROM class_memberships WHERE student_profile_id=? AND class_id=? AND academic_year_id=? AND group_name=? AND status='ACTIVE'",(student_id,class_id,leader['academic_year_id'],leader['group_name'])).fetchone()
                    if not target:raise PermissionError('Chỉ được ghi nhận cho học sinh thuộc tổ mình.')
                    if rule_id:
                        rule=con.execute("SELECT id,title,points FROM conduct_rules WHERE id=? AND class_id=? AND academic_year_id=? AND active=1",(rule_id,class_id,leader['academic_year_id'])).fetchone()
                        if not rule:raise ValueError('Quy tắc điểm không tồn tại hoặc đã bị tắt.')
                        points=rule['points'];reason=f"{rule['title']}: {reason}" if reason else rule['title']
                    else:
                        raw_points=data.get('points')
                        try:
                            if isinstance(raw_points,bool):raise ValueError
                            points=int(raw_points)
                            if isinstance(raw_points,float) and raw_points!=points:raise ValueError
                            if isinstance(raw_points,str) and str(points)!=raw_points.strip():raise ValueError
                        except (TypeError,ValueError):raise ValueError('Nhập một mức điểm hợp lệ do giáo viên thiết lập.')
                        rule=con.execute("SELECT id FROM conduct_rules WHERE class_id=? AND academic_year_id=? AND active=1 AND points=? LIMIT 1",(class_id,leader['academic_year_id'],points)).fetchone()
                        if not rule:raise ValueError('Mức điểm này chưa được giáo viên thiết lập cho lớp. Hãy chọn một mức điểm hợp lệ.')
                    rid=uid('conduct');ts=now()
                    con.execute("INSERT INTO conduct_records(id,student_profile_id,class_id,academic_year_id,group_name,reason,points,recorder_id,status,created_at) VALUES(?,?,?,?,?,?,?,?, 'PENDING',?)",(rid,student_id,class_id,leader['academic_year_id'],leader['group_name'],reason,points,me['account_id'],ts))
                    STORE.audit(con,me['account_id'],'GROUP_LEADER_PROPOSE_CONDUCT','ConductRecord',rid,{'classId':class_id,'studentProfileId':student_id,'ruleId':rule['id'],'points':points})
                return self.respond(201,{'id':rid,'status':'PENDING','points':points})
            if path == '/api/conduct/leaderboard/publish':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'conduct.manage')
                class_id=str(data.get('classId',''));self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    cl=con.execute("SELECT c.name, c.academic_year_id, ay.name academic_year FROM classes c JOIN academic_years ay ON ay.id=c.academic_year_id WHERE c.id=?",(class_id,)).fetchone()
                    if not cl:raise ValueError('Không tìm thấy lớp.')
                    members=[dict(x) for x in con.execute("SELECT cm.group_name groupName,sp.full_name fullName,COALESCE(SUM(CASE WHEN cr.status='VERIFIED' THEN cr.points ELSE 0 END),0) score FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id LEFT JOIN conduct_records cr ON cr.student_profile_id=sp.id AND cr.class_id=cm.class_id AND cr.academic_year_id=cm.academic_year_id WHERE cm.class_id=? AND cm.status='ACTIVE' GROUP BY cm.id ORDER BY cm.group_name,score DESC,sp.full_name",(class_id,))]
                    groups={}
                    for member in members:groups.setdefault(member['groupName'] or 'Chưa xếp tổ',[]).append(member)
                    lines=[f"🏆 BẢNG THÀNH TÍCH RÈN LUYỆN · {cl['name']} · {cl['academic_year']}","Điểm tổng hợp từ các ghi nhận đã được giáo viên xác minh."]
                    medals=['🥇','🥈','🥉']
                    for group,items in groups.items():
                        lines.append(f"\n{group}")
                        for index,item in enumerate(items,1):lines.append(f"{medals[index-1]+' ' if index<=3 else str(index)+'. '}{item['fullName']} — {item['score']} điểm")
                    body='\n'.join(lines)
                    if len(body)>5000:raise ValueError('Bảng xếp hạng vượt giới hạn bài đăng. Hãy chia theo tổ hoặc rút gọn dữ liệu.')
                    post_id=uid('post');ts=now()
                    con.execute("INSERT INTO forum_posts(id,class_id,academic_year_id,author_id,body,created_at,updated_at,post_type) VALUES(?,?,?,?,?,?,?,'ANNOUNCEMENT')",(post_id,class_id,cl['academic_year_id'],me['account_id'],body,ts,ts))
                    STORE.audit(con,me['account_id'],'PUBLISH_CONDUCT_LEADERBOARD','ForumPost',post_id,{'classId':class_id,'memberCount':len(members)})
                return self.respond(201,{'id':post_id,'memberCount':len(members)})
            if path == '/api/conduct-records':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'conduct.manage')
                student_id=str(data.get('studentProfileId',''));class_id=str(data.get('classId',''));self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    member=con.execute("SELECT cm.academic_year_id,cm.group_name FROM class_memberships cm WHERE cm.student_profile_id=? AND cm.class_id=? AND cm.status='ACTIVE'",(student_id,class_id)).fetchone()
                    if not member:raise ValueError('Học sinh không thuộc lớp.')
                    rid=uid('conduct');con.execute("INSERT INTO conduct_records(id,student_profile_id,class_id,academic_year_id,group_name,reason,points,recorder_id,evidence_name,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?)",(rid,student_id,class_id,member['academic_year_id'],member['group_name'],str(data.get('reason','')).strip(),int(data.get('points',0)),me['account_id'],data.get('evidenceName'),now()));STORE.audit(con,me['account_id'],'CREATE_CONDUCT_RECORD','ConductRecord',rid)
                return self.respond(201,{'id':rid,'status':'PENDING'})
            if path.startswith('/api/conduct-records/') and path.endswith('/confirm'):
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'conduct.manage')
                rid=path.split('/')[3]
                with STORE.connect() as con:
                    record=con.execute("SELECT c.id,c.class_id,cl.teacher_account_id FROM conduct_records c JOIN classes cl ON cl.id=c.class_id WHERE c.id=?",(rid,)).fetchone()
                    if not record:raise ValueError('Không tìm thấy ghi nhận.')
                    if me['role']=='TEACHER' and record['teacher_account_id']!=me['account_id']:raise PermissionError('Không đủ quyền.')
                    con.execute("UPDATE conduct_records SET status='VERIFIED',verified_at=?,verified_by=? WHERE id=? AND status='PENDING'",(now(),me['account_id'],rid));STORE.audit(con,me['account_id'],'CONFIRM_CONDUCT_RECORD','ConductRecord',rid)
                return self.respond(200,{'id':rid,'status':'VERIFIED'})
            if path == '/api/forum/posts':
                class_id=str(data.get('classId',''));self.require_class_access(me,class_id);body=str(data.get('body','')).strip()
                post_type=str(data.get('postType','POST')).upper()
                if post_type not in ('POST','ANNOUNCEMENT'): raise ValueError('Lo\u1ea1i b\u00e0i vi\u1ebft kh\u00f4ng h\u1ee3p l\u1ec7.')
                if post_type=='ANNOUNCEMENT': self.require_roles(me,'ADMIN','TEACHER')
                if not body or len(body)>5000:raise ValueError('Nội dung bài viết cần từ 1 đến 5000 ký tự.')
                with STORE.connect() as con:
                    cl=con.execute("SELECT academic_year_id FROM classes WHERE id=?",(class_id,)).fetchone();pid=uid('post');ts=now();con.execute("INSERT INTO forum_posts(id,class_id,academic_year_id,author_id,body,created_at,updated_at,post_type) VALUES(?,?,?,?,?,?,?,?)",(pid,class_id,cl['academic_year_id'],me['account_id'],body,ts,ts,post_type));STORE.audit(con,me['account_id'],'CREATE_FORUM_POST','ForumPost',pid)
                return self.respond(201,{'id':pid})
            if path.startswith('/api/forum/posts/') and path.endswith('/comments'):
                pid=path.split('/')[4];body=str(data.get('body','')).strip()
                if not body or len(body)>2000:raise ValueError('Bình luận cần từ 1 đến 2000 ký tự.')
                with STORE.connect() as con:
                    post=con.execute("SELECT class_id FROM forum_posts WHERE id=?",(pid,)).fetchone()
                    if not post:raise ValueError('Không tìm thấy bài viết.')
                    self.require_class_access(me,post['class_id']);cid=uid('comment');con.execute("INSERT INTO forum_comments VALUES(?,?,?,?,?)",(cid,pid,me['account_id'],body,now()))
                return self.respond(201,{'id':cid})
            if path.startswith('/api/forum/posts/') and path.endswith('/reactions'):
                pid=path.split('/')[4]
                with STORE.connect() as con:
                    post=con.execute("SELECT class_id FROM forum_posts WHERE id=?",(pid,)).fetchone()
                    if not post:raise ValueError('Không tìm thấy bài viết.')
                    self.require_class_access(me,post['class_id']);old=con.execute("SELECT 1 FROM forum_reactions WHERE post_id=? AND account_id=?",(pid,me['account_id'])).fetchone()
                    if old:con.execute("DELETE FROM forum_reactions WHERE post_id=? AND account_id=?",(pid,me['account_id']))
                    else:con.execute("INSERT INTO forum_reactions VALUES(?,?,?,?)",(pid,me['account_id'],'LIKE',now()))
                return self.respond(200,{'liked':not bool(old)})
            if path == '/api/assignments':
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                class_id=str(data.get('classId',''));self.require_class_access(me,class_id,teacher=True)
                title=str(data.get('title','')).strip()
                if not title or len(title)>160:raise ValueError('Tiêu đề lời nhắc phải có từ 1 đến 160 ký tự.')
                with STORE.connect() as con:
                    cl=con.execute("SELECT academic_year_id FROM classes WHERE id=?",(class_id,)).fetchone();aid=uid('assignment');con.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?,?)",(aid,class_id,cl['academic_year_id'],title,str(data.get('subject',''))[:120],str(data.get('content',''))[:3000],data.get('dueAt'),0,me['account_id'],now()));STORE.audit(con,me['account_id'],'CREATE_CLASS_ASSIGNMENT_REMINDER','Assignment',aid,{'classId':class_id,'dueAt':data.get('dueAt')})
                return self.respond(201,{'id':aid})
            if path.startswith('/api/assignments/') and path.endswith('/submissions'):
                return self.respond(410,{'error':'Bài tập trên ClassTrack chỉ là thông báo nhắc việc; chức năng nộp bài không được bật.'})
            if path.startswith('/api/assignments/') and path.endswith('/grade'):
                return self.respond(410,{'error':'Chức năng chấm bài trên ClassTrack không được bật; mục Bài tập chỉ dùng để nhắc việc.'})
            return self.respond(404,{'error':'Không tìm thấy API.'})
        except PermissionError as e:return self.respond(403,{'error':str(e)})
        except sqlite3.IntegrityError as e:return self.respond(409,{'error':'Dữ liệu bị trùng hoặc không hợp lệ.'})
        except Exception as e:return self.respond(400,{'error':str(e)})

    def do_PATCH(self) -> None:
        path=urlparse(self.path).path.rstrip('/')
        try:
            me=self.auth(mutate=True);data=self.read_json()
            if path.startswith('/api/classes/') and path.count('/')==3:
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'classes.manage')
                class_id=path.split('/')[3];self.require_class_access(me,class_id,teacher=True)
                name=str(data.get('name','')).strip()
                with STORE.connect() as con:
                    old_class=con.execute('SELECT teacher_account_id FROM classes WHERE id=?',(class_id,)).fetchone()
                    if not old_class: raise ValueError('Class not found.')
                teacher_id=old_class['teacher_account_id']
                if me['role']=='ADMIN' and data.get('teacherAccountId'):
                    teacher_id=str(data['teacherAccountId'])
                    with STORE.connect() as con:
                        if not con.execute("SELECT 1 FROM accounts WHERE id=? AND role='TEACHER' AND status='ACTIVE'",(teacher_id,)).fetchone(): raise ValueError('Invalid teacher account.')
                STORE.update_class(class_id,name,teacher_id,me['account_id'])
                return self.respond(200,{'id':class_id,'name':name,'teacherAccountId':teacher_id})
            if path.startswith('/api/students/') and path.count('/')==3:
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                student_id=path.split('/')[3];class_id=str(data.get('classId',''));self.require_class_access(me,class_id,teacher=True)
                name=str(data.get('fullName','')).strip();group=str(data.get('group','')).strip();dob=data.get('dateOfBirth') or None
                if not name or len(name)>120: raise ValueError('Invalid student name.')
                with STORE.connect() as con:
                    member=con.execute("SELECT cm.id,cm.role,cm.academic_year_id,sp.account_id,sp.full_name FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id WHERE cm.student_profile_id=? AND cm.class_id=? AND cm.status='ACTIVE'",(student_id,class_id)).fetchone()
                    if not member: raise ValueError('Student is not in the selected class.')
                    if member['role']=='GROUP_LEADER':
                        if not group: raise ValueError('T\u1ed5 tr\u01b0\u1edfng c\u1ea7n \u0111\u01b0\u1ee3c x\u1ebfp v\u00e0o m\u1ed9t t\u1ed5.')
                        leaders=con.execute("SELECT cm.group_name FROM class_memberships cm WHERE cm.class_id=? AND cm.academic_year_id=? AND cm.status='ACTIVE' AND cm.role='GROUP_LEADER' AND cm.id<>?",(class_id,member['academic_year_id'],member['id'])).fetchall()
                        if any(' '.join(str(x['group_name'] or '').split()).casefold()==group.casefold() for x in leaders): raise ValueError(f'Tổ {group} đã có tổ trưởng. Hãy đổi tổ trưởng hiện tại trước.')
                    con.execute('UPDATE student_profiles SET full_name=?,date_of_birth=?,updated_at=? WHERE id=?',(name,dob,now(),student_id))
                    con.execute('UPDATE accounts SET display_name=?,updated_at=? WHERE id=?',(name,now(),member['account_id']))
                    con.execute('UPDATE class_memberships SET group_name=?,updated_at=? WHERE id=?',(group,now(),member['id']))
                    STORE.audit(con,me['account_id'],'UPDATE_STUDENT_PROFILE','StudentProfile',student_id,{'oldName':member['full_name'],'name':name,'classId':class_id,'group':group})
                return self.respond(200,{'studentProfileId':student_id,'fullName':name,'group':group})
            if path.startswith('/api/admin/administrators/'):
                self.require_super_admin(me);account_id=path.split('/')[4];action=str(data.get('action','')).upper()
                with STORE.connect() as con:
                    target=con.execute("SELECT id,role,is_super_admin,status FROM accounts WHERE id=?",(account_id,)).fetchone()
                    if not target or target['role']!='ADMIN' or target['is_super_admin']: raise ValueError('Không tìm thấy tài khoản quản trị viên có thể quản lý.')
                    if action=='PERMISSIONS':
                        permissions=data.get('permissions',[])
                        if not isinstance(permissions,list) or len(set(permissions))!=len(permissions) or any(p not in self.ADMIN_PERMISSIONS for p in permissions): raise ValueError('Danh sách quyền không hợp lệ.')
                        con.execute('DELETE FROM admin_permissions WHERE account_id=?',(account_id,))
                        con.executemany('INSERT INTO admin_permissions(account_id,permission,granted_by,created_at) VALUES(?,?,?,?)',[(account_id,p,me['account_id'],now()) for p in permissions])
                        STORE.audit(con,me['account_id'],'UPDATE_ADMIN_PERMISSIONS','Account',account_id,{'permissions':permissions})
                    elif action in ('LOCK','UNLOCK'):
                        status='SUSPENDED' if action=='LOCK' else 'ACTIVE'
                        con.execute('UPDATE accounts SET status=?,updated_at=? WHERE id=?',(status,now(),account_id))
                        if action=='LOCK': con.execute('DELETE FROM sessions WHERE account_id=?',(account_id,))
                        STORE.audit(con,me['account_id'],'ADMIN_'+action,'Account',account_id)
                    elif action=='REVOKE':
                        grant=con.execute('SELECT previous_role FROM admin_grants WHERE account_id=? AND revoked_at IS NULL',(account_id,)).fetchone()
                        if grant and grant['previous_role'] in ('TEACHER','PARENT'):
                            con.execute('UPDATE accounts SET role=?,status=CASE WHEN status="DISABLED" THEN status ELSE "ACTIVE" END,updated_at=? WHERE id=?',(grant['previous_role'],now(),account_id))
                        else:
                            con.execute("UPDATE accounts SET status='DISABLED',updated_at=? WHERE id=?",(now(),account_id))
                        con.execute('DELETE FROM sessions WHERE account_id=?',(account_id,));con.execute('DELETE FROM admin_permissions WHERE account_id=?',(account_id,))
                        con.execute('UPDATE admin_grants SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL',(now(),account_id))
                        STORE.audit(con,me['account_id'],'REVOKE_ADMIN_ACCESS','Account',account_id,{'previousRole':grant['previous_role'] if grant else 'NONE'})
                    else: raise ValueError('Thao tác quản trị không hợp lệ.')
                return self.respond(200,{'ok':True,'action':action})
            if path.startswith('/api/accounts/') and path.endswith('/status'):
                self.require_roles(me,'ADMIN','TEACHER');account_id=path.split('/')[3];status=str(data.get('status','')).upper()
                if status not in ('ACTIVE','SUSPENDED','DISABLED'):raise ValueError('Trạng thái không hợp lệ.')
                if me['role']=='ADMIN': self.require_permission(me,'users.manage')
                with STORE.connect() as con:
                    target=con.execute('SELECT role,is_super_admin FROM accounts WHERE id=?',(account_id,)).fetchone()
                    if not target or target['role']!='STUDENT' or target['is_super_admin']: raise PermissionError('Chỉ có thể đổi trạng thái tài khoản học sinh trong phạm vi được cấp.')
                    if me['role']=='TEACHER' and not con.execute("SELECT 1 FROM classes c JOIN class_memberships cm ON cm.class_id=c.id JOIN student_profiles sp ON sp.id=cm.student_profile_id WHERE sp.account_id=? AND c.teacher_account_id=?",(account_id,me['account_id'])).fetchone():raise PermissionError('Không có quyền.')
                    con.execute("UPDATE accounts SET status=?,updated_at=? WHERE id=?",(status,now(),account_id))
                    if status!='ACTIVE': con.execute('DELETE FROM sessions WHERE account_id=?',(account_id,))
                    STORE.audit(con,me['account_id'],'CHANGE_ACCOUNT_STATUS','Account',account_id,{'status':status})
                return self.respond(200,{'status':status})
            return self.respond(404,{'error':'Không tìm thấy API.'})
        except PermissionError as e:return self.respond(403,{'error':str(e)})
        except Exception as e:return self.respond(400,{'error':str(e)})

    def do_DELETE(self) -> None:
        path=urlparse(self.path).path.rstrip('/')
        try:
            me=self.auth(mutate=True);data=self.read_json()
            if path.startswith('/api/admin/classes/') and path.count('/')==4:
                self.require_roles(me,'ADMIN');self.require_permission(me,'classes.manage')
                class_id=path.split('/')[4]
                with STORE.connect() as con:
                    row=con.execute('SELECT name,academic_year_id FROM classes WHERE id=?',(class_id,)).fetchone()
                if not row:raise ValueError('Không tìm thấy lớp.')
                if str(data.get('confirmation','')).strip()!=row['name']:
                    raise ValueError('Tên xác nhận không khớp. Lớp chưa bị xóa vĩnh viễn.')
                backup_file=STORE.create_deletion_backup()
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    current=con.execute('SELECT name,academic_year_id FROM classes WHERE id=?',(class_id,)).fetchone()
                    if not current:raise ValueError('Lớp đã bị xóa hoặc không còn tồn tại.')
                    if current['name']!=row['name']:raise ValueError('Tên lớp đã thay đổi. Hãy tải lại trang trước khi xóa.')
                    counts=STORE.delete_class_data(con,class_id)
                    STORE.audit(con,me['account_id'],'PERMANENT_DELETE_CLASS','Class',class_id,{'name':row['name'],'academicYearId':row['academic_year_id'],'deletedCounts':counts,'backupFile':backup_file})
                return self.respond(200,{'id':class_id,'deleted':True,'permanent':True,'backupFile':backup_file,'deletedCounts':counts})
            if (path.startswith('/api/admin/academic-years/') and len(path.split('/'))==5) or (path.startswith('/api/years/') and len(path.split('/'))==4):
                self.require_roles(me,'ADMIN');self.require_permission(me,'classes.manage')
                year_id=path.split('/')[-1]
                with STORE.connect() as con:
                    year=con.execute('SELECT name,status FROM academic_years WHERE id=?',(year_id,)).fetchone()
                if not year:raise ValueError('Không tìm thấy năm học.')
                if str(data.get('confirmation','')).strip()!=year['name']:
                    raise ValueError('Tên xác nhận không khớp. Năm học chưa bị xóa.')
                backup_file=STORE.create_deletion_backup()
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    current=con.execute('SELECT name,status FROM academic_years WHERE id=?',(year_id,)).fetchone()
                    if not current:raise ValueError('Năm học đã bị xóa hoặc không còn tồn tại.')
                    if current['name']!=year['name']:raise ValueError('Năm học đã thay đổi. Hãy tải lại trang trước khi xóa.')
                    class_rows=con.execute('SELECT id,name FROM classes WHERE academic_year_id=?',(year_id,)).fetchall()
                    totals={'classes':len(class_rows),'memberships':0,'conductRecords':0,'conductRules':0,'assignments':0,'forumPosts':0,'importBatches':0}
                    for class_row in class_rows:
                        deleted=STORE.delete_class_data(con,class_row['id'])
                        for key,value in deleted.items():totals[key]+=value
                    con.execute('DELETE FROM academic_years WHERE id=?',(year_id,))
                    STORE.audit(con,me['account_id'],'PERMANENT_DELETE_ACADEMIC_YEAR','AcademicYear',year_id,{'name':year['name'],'status':year['status'],'deletedCounts':totals,'backupFile':backup_file})
                return self.respond(200,{'id':year_id,'deleted':True,'permanent':True,'backupFile':backup_file,'deletedCounts':totals})
            if path.startswith('/api/classes/') and path.count('/')==3:
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN':self.require_permission(me,'classes.manage')
                class_id=path.split('/')[3]
                self.require_class_access(me,class_id,teacher=True)
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    row=con.execute('SELECT name,is_deleted FROM classes WHERE id=?',(class_id,)).fetchone()
                    if not row:raise ValueError('Không tìm thấy lớp.')
                    if row['is_deleted']:raise ValueError('Lớp này đã được xóa khỏi danh sách.')
                    if str(data.get('confirmation','')).strip()!=row['name']:
                        raise ValueError('Tên xác nhận không khớp. Lớp chưa bị xóa.')
                    ts=now()
                    member_count=con.execute("SELECT COUNT(*) FROM class_memberships WHERE class_id=? AND status='ACTIVE'",(class_id,)).fetchone()[0]
                    con.execute("UPDATE classes SET is_deleted=1,deleted_at=?,deleted_name=name,name='__deleted__' || id WHERE id=?",(ts,class_id))
                    con.execute("UPDATE class_memberships SET status='LEFT',left_at=COALESCE(left_at,?),updated_at=? WHERE class_id=? AND status='ACTIVE'",(ts,ts,class_id))
                    STORE.audit(con,me['account_id'],'ARCHIVE_CLASS','Class',class_id,{'name':row['name'],'membershipsClosed':member_count,'historyPreserved':True})
                return self.respond(200,{'id':class_id,'deleted':True,'historyPreserved':True,'membershipsClosed':member_count})
            if path.startswith('/api/memberships/') and path.count('/')==3:
                self.require_roles(me,'ADMIN','TEACHER')
                if me['role']=='ADMIN':self.require_permission(me,'users.manage')
                membership_id=path.split('/')[3]
                with STORE.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    member=con.execute("SELECT cm.id,cm.class_id,cm.status,sp.id student_profile_id,sp.full_name,sp.student_code FROM class_memberships cm JOIN student_profiles sp ON sp.id=cm.student_profile_id WHERE cm.id=?",(membership_id,)).fetchone()
                    if not member:raise ValueError('Kh\u00f4ng t\u00ecm th\u1ea5y h\u1ecdc sinh trong l\u1edbp.')
                    self.require_class_access(me,member['class_id'],teacher=True)
                    if member['status']!='ACTIVE':raise ValueError('H\u1ecdc sinh n\u00e0y \u0111\u00e3 \u0111\u01b0\u1ee3c \u0111\u01b0a kh\u1ecfi danh s\u00e1ch l\u1edbp.')
                    if str(data.get('confirmation','')).strip()!=member['full_name']:
                        raise ValueError('T\u00ean x\u00e1c nh\u1eadn kh\u00f4ng kh\u1edbp. H\u1ecdc sinh ch\u01b0a b\u1ecb x\u00f3a kh\u1ecfi l\u1edbp.')
                    ts=now()
                    con.execute("UPDATE class_memberships SET status='LEFT',left_at=COALESCE(left_at,?),updated_at=? WHERE id=? AND status='ACTIVE'",(ts,ts,membership_id))
                    STORE.audit(con,me['account_id'],'REMOVE_STUDENT_FROM_CLASS','ClassMembership',membership_id,{'classId':member['class_id'],'studentProfileId':member['student_profile_id'],'studentCode':member['student_code'],'historyPreserved':True})
                return self.respond(200,{'membershipId':membership_id,'removedFromClass':True,'historyPreserved':True})
            return self.respond(404,{'error':'Không tìm thấy API.'})
        except PermissionError as e:return self.respond(403,{'error':str(e)})
        except sqlite3.IntegrityError as e:return self.respond(409,{'error':'Không thể xử lý lớp do dữ liệu liên quan.'})
        except Exception as e:return self.respond(400,{'error':str(e)})

    def require_roles(self, me: sqlite3.Row, *roles: str) -> None:
        if me['role'] not in roles:raise PermissionError('Không đủ quyền.')

    def require_super_admin(self, me: sqlite3.Row) -> None:
        if not me['is_super_admin']: raise PermissionError('Chức năng này chỉ dành cho Super Admin.')

    def require_permission(self, me: sqlite3.Row, permission: str) -> None:
        if me['is_super_admin']: return
        if me['role']!='ADMIN': raise PermissionError('Không đủ quyền.')
        with STORE.connect() as con:
            if not con.execute('SELECT 1 FROM admin_permissions WHERE account_id=? AND permission=?',(me['account_id'],permission)).fetchone():
                raise PermissionError('Tài khoản quản trị viên chưa được cấp quyền này.')

    def limit_attempts(self, key: str) -> None:
        with RATE_LOCK:
            recent=[x for x in FAILED_CODES.get(key,[]) if time.time()-x<900]
            if len(recent)>=10: raise PermissionError('Thử lại sau 15 phút.')
            FAILED_CODES[key]=recent+[time.time()]

    def accept_admin_invitation(self, data: dict) -> tuple[str, str]:
        token_value=str(data.get('token','')).strip()
        email=str(data.get('email','')).strip().lower()
        if len(token_value)<32: raise ValueError('Lời mời không hợp lệ hoặc đã hết hạn.')
        username=self.username(data.get('username'));password=self.password(data.get('password'))
        display=str(data.get('displayName') or username).strip()
        if not display or len(display)>80: raise ValueError('Tên hiển thị cần từ 1 đến 80 ký tự.')
        with STORE.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            invite=con.execute("SELECT * FROM admin_invitations WHERE token_hash=? AND email=? COLLATE NOCASE",(token_hash(token_value),email)).fetchone()
            if not invite or invite['status']!='PENDING' or invite['used_at'] or invite['expires_at']<now(): raise ValueError('Lời mời không hợp lệ hoặc đã hết hạn.')
            existing=con.execute('SELECT * FROM accounts WHERE email=? COLLATE NOCASE',(email,)).fetchone()
            if existing and existing['role']!='TEACHER': raise ValueError('Email đã thuộc một tài khoản không thể nâng quyền.')
            ts=now()
            if existing:
                account_id=existing['id']
                con.execute("UPDATE accounts SET role='ADMIN',password_hash=?,status='ACTIVE',is_activated=1,updated_at=?,display_name=? WHERE id=?",(password_hash(password),ts,display,account_id))
                previous='TEACHER'
            else:
                account_id=uid('acc')
                con.execute("INSERT INTO accounts(id,username,password_hash,status,role,is_activated,created_at,updated_at,display_name,email) VALUES(?,?,?,'ACTIVE','ADMIN',1,?,?,?,?)",(account_id,username,password_hash(password),ts,ts,display,email))
                previous='NONE'
            con.execute("INSERT INTO admin_grants(account_id,previous_role,granted_by,created_at,revoked_at) VALUES(?,?,?,?,NULL) ON CONFLICT(account_id) DO UPDATE SET previous_role=excluded.previous_role,granted_by=excluded.granted_by,created_at=excluded.created_at,revoked_at=NULL",(account_id,previous,invite['created_by'],ts))
            permissions=json.loads(invite['permissions_json'])
            con.execute('DELETE FROM admin_permissions WHERE account_id=?',(account_id,))
            con.executemany('INSERT INTO admin_permissions(account_id,permission,granted_by,created_at) VALUES(?,?,?,?)',[(account_id,p,invite['created_by'],ts) for p in permissions])
            con.execute("UPDATE admin_invitations SET status='ACCEPTED',used_at=?,accepted_account_id=? WHERE id=? AND status='PENDING'",(ts,account_id,invite['id']))
            STORE.audit(con,account_id,'ACCEPT_ADMIN_INVITATION','Account',account_id,{'invitationId':invite['id'],'email':email,'permissions':permissions})
        return account_id, token_value

    @staticmethod
    def build_invitation_link(token_value: str) -> str:
        base=os.environ.get('CLASSTRACK_PUBLIC_URL','').rstrip('/')
        return f"{base}/admin/accept?token={token_value}" if base else f"/admin/accept?token={token_value}"

    @staticmethod
    def send_admin_invitation(email: str, link: str) -> bool:
        host=os.environ.get('CLASSTRACK_SMTP_HOST','');sender=os.environ.get('CLASSTRACK_SMTP_FROM','')
        if not host or not sender: return False
        message=EmailMessage();message['Subject']='Lời mời quản trị ClassTrack';message['From']=sender;message['To']=email
        message.set_content(f"Bạn được mời làm quản trị viên ClassTrack. Mở liên kết sau trong vòng 48 giờ để thiết lập tài khoản: {link}\n")
        port=int(os.environ.get('CLASSTRACK_SMTP_PORT','587'));user=os.environ.get('CLASSTRACK_SMTP_USER','');password=os.environ.get('CLASSTRACK_SMTP_PASSWORD','')
        try:
            if port==465:
                with smtplib.SMTP_SSL(host,port,timeout=8) as smtp:
                    if user: smtp.login(user,password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host,port,timeout=8) as smtp:
                    smtp.starttls()
                    if user: smtp.login(user,password)
                    smtp.send_message(message)
            return True
        except (OSError,smtplib.SMTPException,ValueError): return False

    def require_class_access(self, me: sqlite3.Row, class_id: str, teacher: bool = False) -> None:
        with STORE.connect() as con:
            if me['role']=='ADMIN':
                self.require_permission(me,'classes.read')
                if not con.execute("SELECT 1 FROM classes WHERE id=? AND is_deleted=0",(class_id,)).fetchone():raise ValueError('Không tìm thấy lớp.')
                return
            if me['role']=='TEACHER':
                if not con.execute("SELECT 1 FROM classes WHERE id=? AND teacher_account_id=? AND is_deleted=0",(class_id,me['account_id'])).fetchone():raise PermissionError('Không có quyền với lớp này.')
                return
            if teacher:raise PermissionError('Chỉ giáo viên được thực hiện thao tác này.')
            if not con.execute("SELECT 1 FROM class_memberships WHERE class_id=? AND status='ACTIVE' AND student_profile_id=(SELECT id FROM student_profiles WHERE account_id=?)",(class_id,me['account_id'])).fetchone():raise PermissionError('Không thuộc lớp này.')

    @staticmethod
    def username(value: object) -> str:
        name=str(value or '').strip().lower()
        if len(name)<3 or len(name)>40 or not all(c.isalnum() or c in '._-' for c in name):raise ValueError('Username 3–40 ký tự, chỉ gồm chữ, số, dấu chấm, gạch ngang hoặc gạch dưới.')
        return name

    @staticmethod
    def password(value: object) -> str:
        password=str(value or '')
        if len(password)<10 or len(password)>128:raise ValueError('Mật khẩu cần từ 10 đến 128 ký tự.')
        return password

    def client_ip(self) -> str:
        return self.client_address[0]


    def do_OPTIONS(self) -> None:
        self.respond(204)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    # Windows terminals may use legacy code pages that cannot print Vietnamese.
    # Keep normal startup and bootstrap output from crashing on UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try: stream.reconfigure(errors='backslashreplace')
            except (OSError, ValueError): pass
    arguments = sys.argv[1:]
    if arguments and arguments[0] == 'backup':
        if len(arguments) > 2:
            raise SystemExit('Cách dùng: python server.py backup [đường-dẫn-tệp-sao-lưu]')
        try:
            backup_path = STORE.backup_database(arguments[1] if len(arguments) == 2 else None)
        except (OSError, sqlite3.Error, ValueError) as error:
            raise SystemExit(f'Không thể sao lưu database: {error}') from error
        print(f'Đã sao lưu database: {backup_path}')
        return
    if arguments and arguments[0] == 'restore':
        if len(arguments) != 2:
            raise SystemExit('Cách dùng: python server.py restore <đường-dẫn-tệp-sao-lưu>')
        print('Hãy dừng máy chủ ClassTrack trước khi khôi phục database.')
        if input('Nhập RESTORE để tiếp tục: ') != 'RESTORE':
            raise SystemExit('Đã hủy khôi phục; database không thay đổi.')
        try:
            safety_backup = STORE.restore_database(arguments[1])
        except (OSError, sqlite3.Error, ValueError) as error:
            raise SystemExit(f'Không thể khôi phục database: {error}') from error
        print(f'Đã khôi phục database. Bản database trước khi khôi phục: {safety_backup}')
        return
    if arguments:
        if arguments != ['bootstrap-super-admin']:
            raise SystemExit('Cách dùng: python server.py [bootstrap-super-admin | backup [đường-dẫn] | restore <đường-dẫn>]')
        username=Handler.username(os.environ.get('CLASSTRACK_SUPER_ADMIN_USERNAME') or input('Tên đăng nhập Super Admin (không dấu cách): '))
        email=(os.environ.get('CLASSTRACK_SUPER_ADMIN_EMAIL') or input('Email: ')).strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): raise SystemExit('Email không hợp lệ.')
        display=(os.environ.get('CLASSTRACK_SUPER_ADMIN_DISPLAY_NAME') or input('Tên hiển thị: ')).strip() or username
        password=os.environ.get('CLASSTRACK_SUPER_ADMIN_PASSWORD')
        if not password:
            password=getpass.getpass('Mật khẩu (10–128 ký tự): ')
            confirm=getpass.getpass('Nhập lại mật khẩu: ')
            if password!=confirm: raise SystemExit('Hai mật khẩu không khớp.')
        password=Handler.password(password)
        try: account_id=STORE.bootstrap_super_admin(username,email,display,password)
        except (ValueError,sqlite3.IntegrityError) as e: raise SystemExit(str(e))
        print(f'Đã tạo Super Admin {username} (ID: {account_id}). Bootstrap đã khóa vì Super Admin đã tồn tại.')
        return
    host=os.environ.get('CLASSTRACK_HOST','127.0.0.1');port=int(os.environ.get('CLASSTRACK_PORT','8000'))
    print(f'ClassTrack chạy tại http://{host}:{port} (Ctrl+C để dừng)')
    AppServer((host,port),Handler).serve_forever()


if __name__=='__main__':main()
