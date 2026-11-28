"""单进程模型故障隔离；不保存请求或凭据。"""
import math
import threading
import time


class CircuitOpen(RuntimeError):
    def __init__(self, retry_after, limit_kind=None):
        self.retry_after = max(0.0, retry_after)
        self.limit_kind=limit_kind
        super().__init__('model_circuit_open')


class ModelAvailability:
    def __init__(self, threshold=3, cooldown=30.0, window=300.0, clock=time.monotonic):
        if (type(threshold) is not int or threshold < 1 or
                any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0
                    for n in (cooldown, window))):
            raise ValueError('Invalid circuit settings')
        self.threshold = threshold
        self.cooldown = cooldown
        self.window = window
        self.clock = clock
        self.lock = threading.Lock()
        self.key = None
        self.generation = 0
        self.sequence = 0
        self.failures = []
        self.open_until = None
        self.limit_kind = None
        self.probing = False
        self.outstanding = set()
        self.fallbacks = {}

    def fallback_circuits(self, keys):
        # 按当前备用配置保留独立冷却，移除不再配置的状态。
        with self.lock:
            current = {}
            for key in keys:
                circuit = self.fallbacks.get(key)
                if circuit is None:
                    circuit = ModelAvailability(self.threshold, self.cooldown, self.window, self.clock)
                current[key] = circuit
            self.fallbacks = current
            return [current[key] for key in keys]

    def reserve(self, key):
        """冷却后只放行一个真实用户请求探测；没有后台计费探测。"""
        with self.lock:
            if key != self.key:
                self.key = key
                self.generation += 1
                self.failures = []
                self.open_until = None
                self.limit_kind = None
                self.probing = False
                self.outstanding.clear()
            now = self.clock()
            if self.open_until is not None:
                if now < self.open_until or self.probing:
                    raise CircuitOpen(self.open_until - now,self.limit_kind)
                self.probing = True
            self.sequence += 1
            ticket = (self.generation, self.sequence)
            self.outstanding.add(ticket)
            return ticket

    def finish(self, ticket, failed, retry_after=None, limit_kind=None):
        if retry_after is not None and (type(retry_after) not in (int,float)
                or not math.isfinite(retry_after) or retry_after<=0):
            raise ValueError('Invalid retry delay')
        if limit_kind not in (None,'quota','provider_capacity','concurrency','token_rate','request_rate','unknown'):
            raise ValueError('Invalid limit category')
        with self.lock:
            if ticket not in self.outstanding:
                return  # 配置变化或熔断前已在途的旧响应不能恢复新一代状态。
            self.outstanding.remove(ticket)
            if failed:
                now = self.clock()
                self.failures = [t for t in self.failures if now - t <= self.window] + [now]
                if retry_after is not None or self.probing or len(self.failures) >= self.threshold:
                    self.open_until = now + max(self.cooldown,retry_after or 0)
                    self.limit_kind=limit_kind
                    self.probing = False
                    self.generation += 1
                    self.outstanding.clear()
            else:
                self.failures = []
                if self.probing:
                    self.generation += 1
                    self.outstanding.clear()
                self.open_until = None
                self.limit_kind = None
                self.probing = False
