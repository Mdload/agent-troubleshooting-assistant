"""Local semantic retrieval with a versioned SQLite document-vector cache."""
from collections import Counter
from contextlib import closing
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import sqlite3

CONCEPTS = (
    ('接管', '漂移', '接棒', '顶上', '切主', '切换', '倒换', '故障转移'),
    ('虚拟地址', '虚拟ip', '业务vip', '浮动地址', '漂移地址', '共享地址'),
    ('回程', '回包', '返回流量', '回流', '返回路径', '回复报文'),
    ('指定源', '源地址', '用错源', '从哪个口发', '绑出口', '挑出口', '策略路由'),
    ('载波', '拨号', '拨不上', '蜂窝连接', '流量卡', 'pppoe'),
    ('隧道', 'tun16', '虚拟网卡', '虚拟接口'),
    ('监听队列', 'recv-q', '接收队列', '积压', '排长队', '堵队列'),
    ('源地址转换', '源地址改写', '改源', 'snat', 'masquerade', '地址伪装'),
    ('健康检查', '探活', '健康探测', '连续失败', '失败次数', '掉几次'),
    ('看门狗', 'watchdog', '自动拉起', '自愈'),
    ('待机', '备用节点', '备机', 'backup', '候补'),
    ('非抢占', '不抢回来', '不抢主', '抢占', 'nopreempt', 'preempt_delay'),
    ('人为停', '手工停', '手动停止', '主动停'),
    ('固定主机', '固定主端', '固定主用', '单边测试', '固定主端验证'),
    ('恢复选路', '重新选路', '线路重选', '选回原线路'),
)

_EXACT_TERMS = re.compile(
    r'(?<![a-z0-9_.])(?:\d+(?:\.\d+){3}|\d{2,})(?![a-z0-9_.])'
    r'|[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+|[a-z]+\d+'
    r'|(?<![a-z0-9_])(?:croc|keepalived|watchdog|vrrp|snat|masquerade|systemd|'
    r'ip|ping|journalctl|ss)(?![a-z0-9_])'
)


def exact_query(text):
    return bool(_EXACT_TERMS.search(text.lower()))


def semantic_query(text):
    text = text.lower()
    networking_phrases = ('浮动地址', '漂移地址', '回包', '蜂窝连接', '流量卡', '探测包',
                          '无载波', '固定主端', '地址改写', '固定源地址', '虚拟网卡')
    return (any(term in text for term in networking_phrases)
            or sum(any(term in text for term in terms) for terms in CONCEPTS) >= 2)


def normalized(values):
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


class CharacterVectors:
    name = 'python-char-ngram+domain'
    version = 'char-v1'
    threshold = 0.06

    def encode(self, text):
        text = text.lower()
        features = Counter()
        for part in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+(?:[._-][a-z0-9]+)*', text):
            if part[0] < '\u4e00':
                features[part] += 2
            else:
                for size in (2, 3):
                    features.update(part[i:i + size] for i in range(len(part) - size + 1))
        features = {key: math.sqrt(value) for key, value in features.items()}
        features.update({'concept:' + str(i): 5.0 for i, terms in enumerate(CONCEPTS)
                         if any(term in text for term in terms)})
        return dict(zip(features, normalized(list(features.values()))))

    def similarity(self, query, doc):
        return sum(value * doc.get(key, 0.0) for key, value in query.items())


class OnnxVectors:
    name = 'onnx-bge-small-zh-v1.5'
    threshold = 0.40

    def __init__(self, directory, files, revision):
        import numpy as np
        import onnxruntime
        from tokenizers import Tokenizer

        self.np = np
        self.version = json.dumps([self.name, directory, revision, files])
        self.tokenizer = Tokenizer.from_file(str(Path(directory) / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = onnxruntime.InferenceSession(
            str(Path(directory) / 'model.onnx'), sess_options=options,
            providers=['CPUExecutionProvider'])

    def encode(self, text):
        encoded = self.tokenizer.encode(text)
        inputs = {'input_ids': encoded.ids, 'attention_mask': encoded.attention_mask,
                  'token_type_ids': encoded.type_ids}
        feed = {item.name: self.np.asarray([inputs[item.name]], dtype=self.np.int64)
                for item in self.session.get_inputs()}
        hidden = self.session.run(None, feed)[0]
        vector = hidden[0, 0] if hidden.ndim == 3 else hidden[0]
        return normalized(vector.tolist())

    def similarity(self, query, doc):
        return sum(a * b for a, b in zip(query, doc))


@lru_cache(maxsize=1)
def onnx_backend(directory, files, revision):
    try:
        return OnnxVectors(directory, files, revision)
    except Exception:
        return None


def preferred_backend():
    configured = os.environ.get('ATA_RAG_ONNX_DIR')
    if configured:
        directory = Path(configured).resolve()
        try:
            files = tuple((item.name, item.stat().st_mtime_ns, item.stat().st_size)
                          for item in sorted(directory.iterdir()) if item.is_file())
        except OSError:
            return CharacterVectors()
        model = onnx_backend(str(directory), files, os.environ.get('ATA_RAG_ONNX_REVISION', ''))
        if model is not None:
            return model
    return CharacterVectors()


class VectorCache:
    def __init__(self, path):
        self.path = Path(path)

    def vectors(self, documents, backend):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(self.path), timeout=0.25)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS vectors ('
                           'id TEXT PRIMARY KEY, version TEXT, body TEXT, vector TEXT)')
                cached = {row[0]: row[1:] for row in db.execute('SELECT * FROM vectors')}
                updates = []
                vectors = {}
                for ident, body in documents.items():
                    row = cached.get(ident)
                    if row and row[:2] == (backend.version, body):
                        vectors[ident] = json.loads(row[2])
                    else:
                        vector = backend.encode(body)
                        vectors[ident] = vector
                        updates.append((ident, backend.version, body, json.dumps(vector)))
                if updates:
                    db.executemany('INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)', updates)
                return vectors
        except (OSError, sqlite3.Error, ValueError):
            return {ident: backend.encode(body) for ident, body in documents.items()}


@lru_cache(maxsize=128)
def character_document_vector(body):
    return CharacterVectors().encode(body)


def vector_rank(question, records, persistent, fields, cache, *, lightweight=False):
    backend = CharacterVectors() if lightweight else preferred_backend()
    documents = {record['id']: '\n'.join(str(record.get(field, '')) for field in fields)
                 for record in records}

    def rank(encoder):
        query = encoder.encode(question)
        if lightweight:
            vectors = {ident: character_document_vector(body) for ident, body in documents.items()}
        else:
            vectors = cache.vectors({i: body for i, body in documents.items() if i in persistent}, encoder)
        for ident, body in documents.items():
            if ident not in vectors:
                vectors[ident] = encoder.encode(body)
        scores = {ident: encoder.similarity(query, vector) for ident, vector in vectors.items()}
        return [(ident, scores[ident]) for ident in sorted(scores, key=scores.get, reverse=True)
                if scores[ident] >= encoder.threshold], encoder.name

    if isinstance(backend, OnnxVectors):
        try:
            return rank(backend)
        except Exception:
            return rank(CharacterVectors())
    return rank(backend)


def reciprocal_rank_fusion(lexical, semantic, lexical_weight=2, k=20):
    scores = Counter()
    for ranking, weight in ((lexical, lexical_weight), (semantic, 1)):
        for rank, ident in enumerate(ranking, 1):
            scores[ident] += weight / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
