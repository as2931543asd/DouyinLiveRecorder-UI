"""SQLite 弹幕索引：仅刷新变化的 JSONL，查询不反复读取 NAS。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from danmaku_search import (
    SCOPE_FIELDS, SearchStats, chat_result, infer_anchor, iter_danmaku_files,
    matching_fields,
)
from src.video_locator import safe_recording_path


class IndexBusyError(RuntimeError):
    pass


class DanmakuIndex:
    def __init__(self, root: Path, database: Path):
        self.root = root.resolve()
        self.database = database
        self._refresh_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._ready = False

    @contextmanager
    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with self._schema_lock:
                if not self._ready:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                        CREATE TABLE IF NOT EXISTS files (
                            path TEXT PRIMARY KEY, identity TEXT, size INTEGER, mtime INTEGER,
                            position INTEGER, line INTEGER, metadata TEXT, fingerprint TEXT,
                            bad INTEGER, pending INTEGER
                        );
                        CREATE TABLE IF NOT EXISTS chats (
                            file TEXT, line INTEGER, timestamp_ms INTEGER,
                            anchor TEXT, content TEXT, viewer TEXT, result TEXT,
                            PRIMARY KEY (file, line)
                        );
                        CREATE INDEX IF NOT EXISTS chats_time ON chats(timestamp_ms, file, line);
                    """)
                    stored = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
                    if stored and stored[0] != str(self.root):
                        db.execute("DELETE FROM chats")
                        db.execute("DELETE FROM files")
                        db.execute("DELETE FROM settings")
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('root', ?)", (str(self.root),))
                    db.commit()
                    self._ready = True
            yield db
        finally:
            db.close()

    @staticmethod
    def _fingerprint(source, position: int) -> str:
        """Detect replacement/rewritten prefixes without rereading the entire log."""
        source.seek(0)
        head = source.read(min(position, 256))
        source.seek(max(0, position - 256))
        tail = source.read(min(position, 256))
        return hashlib.sha256(head + tail).hexdigest()

    def _update_file(self, db, path: Path) -> bool:
        relative = str(path.relative_to(self.root))
        path = safe_recording_path(self.root, relative, {".jsonl"})
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        old = db.execute("SELECT * FROM files WHERE path=?", (relative,)).fetchone()
        if old and (old['identity'], old['size'], old['mtime']) == (identity, stat.st_size, stat.st_mtime_ns):
            return False
        with path.open('rb') as source:
            append = bool(
                old and old['identity'] == identity and stat.st_size > old['size']
                and self._fingerprint(source, old['position']) == old['fingerprint']
            )
            if append:
                position, line = old['position'], old['line']
                metadata = json.loads(old['metadata'])
                bad = old['bad']
            else:
                db.execute("DELETE FROM chats WHERE file=?", (relative,))
                position, line, metadata, bad = 0, 0, {}, 0
            source.seek(position)
            anchor = infer_anchor(path, self.root)
            pending = 0
            # Snapshot length: never chase a live writer indefinitely.
            while source.tell() < stat.st_size:
                raw = source.readline(stat.st_size - source.tell())
                if not raw.endswith(b'\n'):
                    pending = 1
                    break  # Retry a partial final line on the next refresh.
                position = source.tell()
                line += 1
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode('utf-8-sig' if line == 1 else 'utf-8'))
                except (UnicodeError, ValueError):
                    bad += 1
                    continue
                if not isinstance(record, dict):
                    bad += 1
                    continue
                if record.get('type') == 'metadata':
                    metadata = record
                    continue
                if record.get('type') != 'chat':
                    continue
                result = chat_result(record, metadata, anchor=anchor, source=relative, line=line)
                if result is None:
                    bad += 1
                    continue
                viewer = ' '.join(v for v in (result['viewer_nickname'], result['viewer_id']) if v)
                # SQLite integers are signed 64-bit; malformed timestamps cannot abort a refresh.
                stamp = result['timestamp_ms']
                if stamp is not None and (result['time'] is None or not -(2**63) <= stamp < 2**63):
                    result['timestamp_ms'] = None
                db.execute(
                    "INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (relative, line, result['timestamp_ms'], anchor.casefold(),
                     result['content'].casefold(), viewer.casefold(), json.dumps(result, ensure_ascii=False)),
                )
            fingerprint = self._fingerprint(source, position)
        db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (relative, identity, stat.st_size, stat.st_mtime_ns, position, line,
             json.dumps(metadata), fingerprint, bad, pending),
        )
        return True

    def refresh(self) -> dict:
        if not self._refresh_lock.acquire(blocking=False):
            raise IndexBusyError('索引正在刷新，请稍后重试')
        started = time.monotonic()
        try:
            stats = SearchStats()
            seen, errors, changed = set(), [], 0
            with self.connect() as db, db:
                db.execute('BEGIN')
                for path in iter_danmaku_files(self.root, stats):
                    relative = str(path.relative_to(self.root))
                    seen.add(relative)
                    db.execute('SAVEPOINT source_file')
                    try:
                        changed += self._update_file(db, path)
                    except (OSError, ValueError) as exc:
                        db.execute('ROLLBACK TO source_file')
                        errors.append(f'{relative}: {exc}')
                    finally:
                        db.execute('RELEASE source_file')
                # Do not treat inaccessible directories as deleted recordings.
                if not stats.file_errors:
                    for row in db.execute('SELECT path FROM files').fetchall():
                        if row['path'] not in seen:
                            db.execute('DELETE FROM chats WHERE file=?', (row['path'],))
                            db.execute('DELETE FROM files WHERE path=?', (row['path'],))
                db.execute("INSERT OR REPLACE INTO settings VALUES ('updated_at', ?)", (str(int(time.time() * 1000)),))
            return {**self.status(), 'refreshing': False, 'changed_files': changed, 'errors': errors[:20],
                    'file_errors': stats.file_errors + len(errors),
                    'elapsed_seconds': round(time.monotonic() - started, 3)}
        finally:
            self._refresh_lock.release()

    def status(self) -> dict:
        with self.connect() as db:
            totals = db.execute('SELECT COUNT(*), COALESCE(SUM(bad),0), COALESCE(SUM(pending),0) FROM files').fetchone()
            updated = db.execute("SELECT value FROM settings WHERE key='updated_at'").fetchone()
            return {'root': str(self.root), 'files': totals[0], 'bad_records': totals[1],
                    'pending_files': totals[2], 'chats': db.execute('SELECT COUNT(*) FROM chats').fetchone()[0],
                    'updated_at': int(updated[0]) if updated else None,
                    'refreshing': self._refresh_lock.locked()}

    def search(self, *, q='', scope='both', match='any', anchor='', viewer='',
               since_ms=None, until_ms=None, page=1, page_size=50, order='desc') -> dict:
        if scope not in SCOPE_FIELDS or match not in ('any', 'all') or order not in ('asc', 'desc'):
            raise ValueError('无效搜索选项')
        if not 1 <= page <= 100000 or not 1 <= page_size <= 100:
            raise ValueError('分页参数超出范围')
        if since_ms is not None and until_ms is not None and since_ms > until_ms:
            raise ValueError('开始时间不能晚于结束时间')
        keywords = q.casefold().split()
        if len(keywords) > 20:
            raise ValueError('最多输入 20 个关键词')
        clauses, params = [], []
        for column, value in (('anchor', anchor), ('viewer', viewer)):
            if value.strip():
                clauses.append(f'instr({column}, ?) > 0')
                params.append(value.strip().casefold())
        for operator, value in (('>=', since_ms), ('<=', until_ms)):
            if value is not None:
                clauses.append(f'timestamp_ms {operator} ?')
                params.append(value)
        keyword_clauses = []
        for keyword in keywords:
            keyword_clauses.append('(' + ' OR '.join(f'instr({field}, ?) > 0' for field in SCOPE_FIELDS[scope]) + ')')
            params.extend([keyword] * len(SCOPE_FIELDS[scope]))
        if keyword_clauses:
            clauses.append('(' + (' AND ' if match == 'all' else ' OR ').join(keyword_clauses) + ')')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connect() as db, db:
            # Explicit read transaction keeps count and page on the same refresh snapshot.
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM chats' + where, params).fetchone()[0]
            rows = db.execute(
                'SELECT result FROM chats' + where +
                f' ORDER BY timestamp_ms {order}, file {order}, line {order} LIMIT ? OFFSET ?',
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        results = []
        for row in rows:
            result = json.loads(row[0])
            result['matched_fields'] = matching_fields(
                keywords=keywords, match_mode=match, selected_fields=SCOPE_FIELDS[scope],
                values={**result, 'viewer': ' '.join(result[k] or '' for k in ('viewer_nickname', 'viewer_id'))},
                case_sensitive=False,
            )
            results.append(result)
        return {'results': results, 'total': total, 'page': page, 'page_size': page_size}

    def get_chat(self, file: str, line: int) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT result FROM chats WHERE file=? AND line=?', (file, line)).fetchone()
            return json.loads(row[0]) if row else None
