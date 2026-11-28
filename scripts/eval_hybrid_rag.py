"""Compare the three production retrieval rankers before rule-based citation expansion."""
from collections import defaultdict, Counter
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qa
from hybrid_retrieval import VectorCache


QUERIES = {
    'colloquial': [
        ('远端那台倒下了，候补什么时候能接棒？', ('KB-004',)),
        ('无线都掉了，网线还通，为啥另一台没顶上？', ('HA-001',)),
        ('局端原来的主机好了，会马上把活抢回来吗？', ('KB-013',)),
        ('进程换了一个 PID，里面的数据还是跑不动', ('HA-005',)),
        ('两边明明有动静，咋后台四台全灰了？', ('HA-012',)),
        ('怎么从另一个口发探测包才算数？', ('KB-007', 'HA-013')),
        ('流量卡刚换好，重拨以后会不会又从老地址发？', ('HA-011',)),
        ('局端好像收得到，回复却绕不回来，走的是卫星', ('HA-009',)),
    ],
    'synonym': [
        ('固定主端验证中途卡住要怎么恢复', ('HA-006', 'KB-006')),
        ('候补手工启动以后，原来的线路不再重选', ('HA-004',)),
        ('局端的人为停机为何被监控判成故障？', ('KB-016', 'HA-008')),
        ('蜂窝连接有地址，返回流量缺运营商网段', ('HA-010',)),
        ('轮流挑出口探活漏掉了还能工作的蜂窝链路', ('HA-002',)),
        ('看门狗是自己起来还是叫候补顶上？', ('KB-015',)),
        ('源地址改写在系统启动后变回旧配置', ('HA-011',)),
        ('远端倒换为什么让局端也一起故障转移？', ('HA-003',)),
    ],
    'exact': [
        ('ppp5 carrier=0 无 IPv4', ('HA-007',)),
        ('POSTROUTING SNAT masquerade 换卡', ('HA-011',)),
        ('tun16 地址缺失 Croc 刚重启', ('HA-005',)),
        ('9965 Recv-Q 堆积多久会触发 watchdog？', ('KB-015', 'KB-018')),
        ('局端 max_owner_failures 以及 fall/rise', ('KB-014',)),
        ('owner-fault BACKUP 手动启动 Croc 无法恢复选主', ('HA-004',)),
        ('远端 Keepalived 切走 198.51.100.29 的业务 VIP', ('KB-004',)),
        ('croc-ha-health.sh 可以当普通状态查询吗？', ('KB-007',)),
    ],
}


def measure(answers, relevant):
    relevant = set(relevant)
    recall = [len(relevant.intersection(answers[:cutoff])) / len(relevant) for cutoff in (3, 5)]
    reciprocal = next((1.0 / rank for rank, ident in enumerate(answers, 1) if ident in relevant), 0.0)
    return recall + [reciprocal]


def main():
    corpus = {record['id'] for record in qa.RECORDS}
    scores = defaultdict(list)
    backends = Counter()
    with TemporaryDirectory(prefix='ata-hybrid-eval-') as tmp:
        qa.VECTOR_CACHE = VectorCache(Path(tmp) / 'vectors.sqlite3')
        for group, queries in QUERIES.items():
            for question, expected in queries:
                assert set(expected) <= corpus, expected
                for ranker in ('bm25', 'vector', 'hybrid'):
                    info = {}
                    hits = qa.retrieve(question, ranking=ranker, rank_only=True, retrieval_info=info)
                    if ranker != 'bm25':
                        backends[info.get('vector_backend', 'scope-gated')] += 1
                    ids = [hit['case']['id'] for hit in hits]
                    row = measure(ids, expected)
                    scores[group, ranker].append(row)
                    scores['all', ranker].append(row)

    print('Queries: {} | vector backend calls: {}'.format(len(scores['all', 'hybrid']), dict(backends)))
    print('Query type | Ranker | Recall@3 | Recall@5 | MRR')
    for (group, ranker), rows in sorted(scores.items()):
        averages = [sum(column) / len(rows) for column in zip(*rows)]
        print('{} | {} | {:.3f} | {:.3f} | {:.3f}'.format(group, ranker, *averages))


if __name__ == '__main__':
    main()
