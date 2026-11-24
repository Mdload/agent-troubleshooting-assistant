"""Agent v2 分层记忆、设备只读授权、完整对话轨迹与自动滚动摘要存储（纯标准库，兼容 Python 3.6+）。"""
import collections
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from device_registry import DEVICES
import qa


MEMORY_KINDS = ('preference', 'correction', 'topology_fact', 'troubleshooting_lesson')
PENDING_TTL_SECONDS = 1800


def _norm_title(text):
    return re.sub(r'\s+', ' ', (text or '').strip().lower())[:120]


def _token_jaccard(a, b):
    ta = set(qa.tokens(a or ''))
    tb = set(qa.tokens(b or ''))
    if len(ta) < 2 or len(tb) < 2:
        return 0.0
    inter = ta.intersection(tb)
    if len(inter) < 2:
        return 0.0
    return float(len(inter)) / float(len(ta.union(tb)))


class AgentMemoryStore:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS device_grants ('
                'owner TEXT NOT NULL, device TEXT NOT NULL, granted_at REAL NOT NULL, '
                'PRIMARY KEY(owner, device))'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS conversation_turns ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, conversation_id TEXT NOT NULL, '
                'user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, tool_summary TEXT NOT NULL DEFAULT "", '
                'created_at REAL NOT NULL)'
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_turns_owner_conv '
                'ON conversation_turns(owner, conversation_id, id)'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS conversation_summaries ('
                'owner TEXT NOT NULL, conversation_id TEXT NOT NULL, summary TEXT NOT NULL, '
                'last_turn_id INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, '
                'PRIMARY KEY(owner, conversation_id))'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS memories ('
                'id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, '
                'title TEXT NOT NULL, norm_title TEXT NOT NULL, content TEXT NOT NULL, '
                'scope TEXT NOT NULL DEFAULT "", source_conversation TEXT NOT NULL DEFAULT "", '
                'created_at REAL NOT NULL, updated_at REAL NOT NULL, '
                'shared INTEGER NOT NULL DEFAULT 0, confidence INTEGER NOT NULL DEFAULT 1, '
                'hit_count INTEGER NOT NULL DEFAULT 0, superseded_by TEXT NOT NULL DEFAULT "", '
                'tags TEXT NOT NULL DEFAULT "")'
            )
            existing_cols = {row['name'] for row in db.execute('PRAGMA table_info(memories)').fetchall()}
            migrations = (
                ('shared', 'INTEGER NOT NULL DEFAULT 0'),
                ('confidence', 'INTEGER NOT NULL DEFAULT 1'),
                ('hit_count', 'INTEGER NOT NULL DEFAULT 0'),
                ('superseded_by', 'TEXT NOT NULL DEFAULT ""'),
                ('tags', 'TEXT NOT NULL DEFAULT ""'),
            )
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    db.execute('ALTER TABLE memories ADD COLUMN {} {}'.format(col_name, col_def))
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_memories_owner_updated '
                'ON memories(owner, updated_at DESC)'
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_memories_shared_updated '
                'ON memories(shared, updated_at DESC)'
            )
            db.execute(
                'CREATE TABLE IF NOT EXISTS pending_runs ('
                'id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL, '
                'payload TEXT NOT NULL, expires_at REAL NOT NULL)'
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            db.execute('PRAGMA busy_timeout=5000')
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    # ---------- 首次设备只读授权（同账号直通，可随时撤销） ----------

    def granted_devices(self, owner):
        with self.connect() as db:
            rows = db.execute(
                'SELECT device FROM device_grants WHERE owner=? ORDER BY granted_at ASC',
                (owner,)
            ).fetchall()
        return [row['device'] for row in rows if row['device'] in DEVICES]

    def grant_devices(self, owner, devices):
        valid = [d for d in devices if isinstance(d, str) and d in DEVICES]
        if not valid:
            return self.granted_devices(owner)
        now = self.clock()
        with self.connect() as db:
            for device in valid:
                db.execute(
                    'INSERT OR REPLACE INTO device_grants(owner, device, granted_at) VALUES (?, ?, ?)',
                    (owner, device, now)
                )
        return self.granted_devices(owner)

    def revoke_device(self, owner, device):
        with self.connect() as db:
            db.execute('DELETE FROM device_grants WHERE owner=? AND device=?', (owner, device))
        return self.granted_devices(owner)

    def revoke_all_devices(self, owner):
        with self.connect() as db:
            db.execute('DELETE FROM device_grants WHERE owner=?', (owner,))
        return []

    # ---------- 完整对话轨迹与自动滚动摘要 ----------

    def append_turn(self, owner, conversation_id, user_text, assistant_text, tool_summary=''):
        now = self.clock()
        with self.connect() as db:
            cursor = db.execute(
                'INSERT INTO conversation_turns(owner, conversation_id, user_text, assistant_text, tool_summary, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (owner, conversation_id, (user_text or '')[:4000], (assistant_text or '')[:8000],
                 (tool_summary or '')[:4000], now)
            )
            return cursor.lastrowid

    def recent_turns(self, owner, conversation_id, limit=8):
        with self.connect() as db:
            rows = db.execute(
                'SELECT id, user_text, assistant_text, tool_summary, created_at '
                'FROM conversation_turns WHERE owner=? AND conversation_id=? '
                'ORDER BY id DESC LIMIT ?',
                (owner, conversation_id, limit)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def turns_needing_summary(self, owner, conversation_id, keep_recent=6):
        summary_row = self.get_summary(owner, conversation_id)
        last_id = summary_row['last_turn_id'] if summary_row else 0
        with self.connect() as db:
            rows = db.execute(
                'SELECT id, user_text, assistant_text, tool_summary, created_at '
                'FROM conversation_turns WHERE owner=? AND conversation_id=? AND id>? '
                'ORDER BY id ASC',
                (owner, conversation_id, last_id)
            ).fetchall()
        if len(rows) <= keep_recent + 2:
            return None, []
        older = [dict(row) for row in rows[:-keep_recent]]
        return (summary_row['summary'] if summary_row else ''), older

    def get_summary(self, owner, conversation_id):
        with self.connect() as db:
            row = db.execute(
                'SELECT summary, last_turn_id, updated_at FROM conversation_summaries '
                'WHERE owner=? AND conversation_id=?',
                (owner, conversation_id)
            ).fetchone()
        return dict(row) if row else None

    def save_summary(self, owner, conversation_id, summary, last_turn_id):
        text = (summary or '').strip()[:3000]
        if not text:
            return
        now = self.clock()
        with self.connect() as db:
            db.execute(
                'INSERT OR REPLACE INTO conversation_summaries(owner, conversation_id, summary, last_turn_id, updated_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (owner, conversation_id, text, int(last_turn_id), now)
            )

    def clear_conversation(self, owner, conversation_id):
        with self.connect() as db:
            db.execute('DELETE FROM conversation_turns WHERE owner=? AND conversation_id=?', (owner, conversation_id))
            db.execute('DELETE FROM conversation_summaries WHERE owner=? AND conversation_id=?', (owner, conversation_id))
            db.execute('DELETE FROM pending_runs WHERE owner=? AND conversation_id=?', (owner, conversation_id))

    # ---------- 长期记忆与自学习经验 ----------

    def save_memory(self, owner, kind, title, content, scope='', source_conversation='',
                    shared=False, tags='', merge_target_id=None, supersede_id=None, confidence_delta=0):
        if kind not in MEMORY_KINDS:
            kind = 'troubleshooting_lesson'
        title = (title or '').strip()[:120]
        content = (content or '').strip()[:1500]
        scope = (scope or '').strip()[:120]
        tags = (tags or '').strip()[:240]
        shared_flag = 1 if (shared and kind != 'preference') else 0
        if not title or not content:
            raise ValueError('Memory title and content required')
        norm = _norm_title(title)
        now = self.clock()
        with self.connect() as db:
            existing = None
            if merge_target_id:
                existing = db.execute(
                    'SELECT id, created_at, shared, confidence, tags FROM memories '
                    'WHERE id=? AND owner=? AND superseded_by=""',
                    (merge_target_id, owner)
                ).fetchone()
            if not existing:
                existing = db.execute(
                    'SELECT id, created_at, shared, confidence, tags FROM memories '
                    'WHERE owner=? AND kind=? AND norm_title=? AND superseded_by=""',
                    (owner, kind, norm)
                ).fetchone()
            if not existing and not supersede_id:
                # 语义近重复合并：同类记忆标题+范围重合度 >= 0.65 时自动融合，避免碎片重复
                candidates = db.execute(
                    'SELECT id, title, scope, created_at, shared, confidence, tags FROM memories '
                    'WHERE owner=? AND kind=? AND superseded_by="" ORDER BY updated_at DESC LIMIT 80',
                    (owner, kind)
                ).fetchall()
                target_sig = title + ' ' + scope
                for cand in candidates:
                    if _token_jaccard(target_sig, cand['title'] + ' ' + cand['scope']) >= 0.65:
                        existing = cand
                        break

            if existing:
                ident = existing['id']
                created_at = existing['created_at']
                new_shared = 1 if (existing['shared'] or shared_flag) and kind != 'preference' else 0
                new_conf = max(1, min(10, int(existing['confidence'] or 1) + max(1, int(confidence_delta))))
                merged_tags = tags or existing['tags'] or ''
                db.execute(
                    'UPDATE memories SET kind=?, title=?, norm_title=?, content=?, scope=?, '
                    'source_conversation=?, updated_at=?, shared=?, confidence=?, tags=? '
                    'WHERE id=? AND owner=?',
                    (kind, title, norm, content, scope, source_conversation or '',
                     now, new_shared, new_conf, merged_tags, ident, owner)
                )
            else:
                ident = uuid.uuid4().hex
                created_at = now
                new_shared = shared_flag
                new_conf = max(1, min(10, 1 + int(confidence_delta)))
                merged_tags = tags
                db.execute(
                    'INSERT INTO memories(id, owner, kind, title, norm_title, content, scope, '
                    'source_conversation, created_at, updated_at, shared, confidence, hit_count, superseded_by, tags) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, "", ?)',
                    (ident, owner, kind, title, norm, content, scope, source_conversation or '',
                     created_at, now, new_shared, new_conf, merged_tags)
                )
                overflow = db.execute(
                    'SELECT id FROM memories WHERE owner=? AND superseded_by="" '
                    'ORDER BY (kind="preference") DESC, shared DESC, confidence DESC, updated_at DESC '
                    'LIMIT -1 OFFSET 200',
                    (owner,)
                ).fetchall()
                for row in overflow:
                    db.execute('DELETE FROM memories WHERE id=? AND owner=?', (row['id'], owner))

            if supersede_id and supersede_id != ident:
                db.execute(
                    'UPDATE memories SET superseded_by=?, updated_at=? WHERE id=? AND owner=? AND superseded_by=""',
                    (ident, now, supersede_id, owner)
                )
        return {
            'id': ident, 'kind': kind, 'title': title, 'content': content,
            'scope': scope, 'tags': merged_tags, 'shared': bool(new_shared),
            'confidence': new_conf, 'hit_count': 0, 'is_mine': True,
            'created_at': created_at, 'updated_at': now,
        }

    def list_memories(self, owner, limit=60):
        with self.connect() as db:
            rows = db.execute(
                'SELECT id, owner, kind, title, content, scope, tags, shared, confidence, hit_count, '
                'source_conversation, created_at, updated_at '
                'FROM memories WHERE owner=? AND superseded_by="" ORDER BY updated_at DESC LIMIT ?',
                (owner, limit)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item['shared'] = bool(item['shared'])
            item['is_mine'] = True
            out.append(item)
        return out

    def list_shared_memories(self, owner=None, limit=60):
        with self.connect() as db:
            rows = db.execute(
                'SELECT id, owner, kind, title, content, scope, tags, shared, confidence, hit_count, '
                'source_conversation, created_at, updated_at '
                'FROM memories WHERE shared=1 AND superseded_by="" '
                'ORDER BY confidence DESC, updated_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item['shared'] = True
            item['is_mine'] = (owner is not None and item['owner'] == owner)
            out.append(item)
        return out

    def set_shared(self, owner, memory_id, shared=True):
        now = self.clock()
        with self.connect() as db:
            row = db.execute(
                'SELECT id, kind FROM memories WHERE id=? AND owner=? AND superseded_by=""',
                (memory_id, owner)
            ).fetchone()
            if not row:
                return False
            if shared and row['kind'] == 'preference':
                raise ValueError('个人回答偏好仅限本人使用，不支持共享给全实验室')
            db.execute(
                'UPDATE memories SET shared=?, updated_at=? WHERE id=? AND owner=?',
                (1 if shared else 0, now, memory_id, owner)
            )
            return True

    def boost_memory(self, owner, memory_id, delta=1):
        with self.connect() as db:
            row = db.execute(
                'SELECT confidence FROM memories WHERE id=? AND owner=? AND superseded_by=""',
                (memory_id, owner)
            ).fetchone()
            if not row:
                return False
            new_conf = max(0, min(10, int(row['confidence'] or 1) + int(delta)))
            db.execute('UPDATE memories SET confidence=? WHERE id=? AND owner=?', (new_conf, memory_id, owner))
            return True

    def delete_memory(self, owner, memory_id):
        with self.connect() as db:
            cursor = db.execute('DELETE FROM memories WHERE owner=? AND id=?', (owner, memory_id))
            return cursor.rowcount > 0

    def recall_memories(self, owner, query='', limit=6, include_shared=True, record_hits=False):
        personal = self.list_memories(owner, limit=200)
        seen = {r['id'] for r in personal}
        rows = list(personal)
        if include_shared:
            for item in self.list_shared_memories(owner=owner, limit=120):
                if item['id'] not in seen:
                    rows.append(item)
                    seen.add(item['id'])
        if not rows or limit <= 0:
            return []
        preferences = [r for r in rows if r['is_mine'] and r['kind'] == 'preference'][:min(3, limit)]
        if not (query or '').strip():
            preference_ids = {r['id'] for r in preferences}
            rest = [r for r in rows if r['id'] not in preference_ids]
            return (preferences + rest)[:limit]
        remaining = limit - len(preferences)
        if not remaining:
            return preferences

        q_terms = set(qa.tokens(query))
        docs = []
        for row in rows:
            if row['kind'] == 'preference':
                continue
            text = ' '.join((row['title'], row['title'], row.get('tags', ''), row['content'], row['scope']))
            docs.append((row, collections.Counter(qa.tokens(text))))
        df = collections.Counter(tok for _, c in docs for tok in c)
        n_docs = len(docs)
        avg_len = sum(sum(c.values()) for _, c in docs) / float(n_docs) if n_docs else 1.0
        scored = []
        for row, counts in docs:
            matched = q_terms.intersection(counts)
            if not matched:
                continue
            score = 0.0
            norm = 1.2 * (0.25 + 0.75 * sum(counts.values()) / max(avg_len, 1.0))
            for tok in sorted(matched):
                freq = counts[tok]
                idf = math.log(1.0 + (n_docs - df[tok] + 0.5) / (df[tok] + 0.5))
                score += idf * freq * 2.2 / (freq + norm)
            kind_mult = {'correction': 1.20, 'troubleshooting_lesson': 1.15, 'topology_fact': 1.10}.get(row['kind'], 1.0)
            conf = int(row.get('confidence') or 1)
            conf_mult = 1.0 + min(0.35, max(-0.25, (conf - 1) * 0.08))
            shared_mult = 1.08 if row.get('shared') else 1.0
            score = score * kind_mult * conf_mult * shared_mult
            scored.append((score, conf, row['updated_at'], row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        chosen = [row for _, _, _, row in scored[:remaining]]
        if record_hits and chosen:
            with self.connect() as db:
                for row in chosen:
                    db.execute('UPDATE memories SET hit_count=hit_count+1 WHERE id=?', (row['id'],))
        return preferences + chosen

    # ---------- 首次设备授权暂停态持久化 ----------

    def save_pending(self, owner, conversation_id, payload):
        now = self.clock()
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('DELETE FROM pending_runs WHERE expires_at<=?', (now,))
            db.execute(
                'INSERT INTO pending_runs(id, owner, conversation_id, payload, expires_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (ident, owner, conversation_id, json.dumps(payload, ensure_ascii=False), now + PENDING_TTL_SECONDS)
            )
        return ident

    def pop_pending(self, owner, conversation_id, pending_id):
        now = self.clock()
        with self.connect() as db:
            row = db.execute(
                'SELECT payload, expires_at FROM pending_runs '
                'WHERE id=? AND owner=? AND conversation_id=?',
                (pending_id, owner, conversation_id)
            ).fetchone()
            db.execute('DELETE FROM pending_runs WHERE id=? AND owner=?', (pending_id, owner))
            db.execute('DELETE FROM pending_runs WHERE expires_at<=?', (now,))
        if not row or row['expires_at'] <= now:
            return None
        return json.loads(row['payload'])
