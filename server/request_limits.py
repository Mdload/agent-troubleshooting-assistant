"""进程内、按已验证归属和请求用途限流；不保存凭据或请求内容。"""
import threading
import time


class RequestLimiter:
    LIMITS = {'model': (12, 60), 'auth': (12, 60),
              'storage': (120, 600), 'control': (30, 120)}

    def __init__(self, clock=time.monotonic, max_buckets=4096):
        self.clock = clock
        self.max_buckets = max_buckets
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, owner, category):
        with self.lock:
            now = self.clock()
            for key in list(self.buckets):
                # 每种用途按自己的时间窗口清理，包括其他用途留下的桶。
                window = self.LIMITS[key[0]][1]
                recent = [stamp for stamp in self.buckets[key] if now - stamp < window]
                if recent:
                    self.buckets[key] = recent
                else:
                    del self.buckets[key]
            keys = [(category, owner), (category, None)]
            if len(self.buckets)+sum(key not in self.buckets for key in keys) > self.max_buckets:
                return False
            if any(len(self.buckets.get(key, [])) >= limit
                   for key, limit in zip(keys, self.LIMITS[category])):
                return False
            for key in keys:
                self.buckets.setdefault(key, []).append(now)
            return True


def category(path, task_action=None):
    if path in ('/api/chat', '/api/ask','/api/selection/explain') or task_action in ('resume','start'):
        return 'model'
    if path.endswith(('/login', '/register')):
        return 'auth'
    if path.endswith(('/cancel', '/logout')):
        return 'control'
    return 'storage'
