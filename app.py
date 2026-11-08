#!/usr/bin/env python3
"""统一问答与受控只读排查；仅监听回环。"""
import json
import threading
import time
import secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import qa
import diagnostic_web
import unified_chat
from device_tools import DEVICES

ROOT = Path(__file__).resolve().parent
SLOTS = threading.BoundedSemaphore(2)
RATE_LOCK = threading.Lock()
REQUEST_TIMES = []
CSRF_TOKEN = secrets.token_urlsafe(32)

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(300)

    def log_message(self, fmt, *args):
        # 不记录问题、配置和上游响应。
        pass

    def send(self, code, value, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_host():
            self.send(403, {'error': '请通过本机SSH隧道访问'}); return
        path = self.path.split("?", 1)[0]
        routes = {"/api/health": {"project": "Agent-Troubleshooting-Assistant", "version": "0.7.0",
            "status": "ok", "case_count": len(qa.CASES), "knowledge_count": len(qa.KNOWLEDGE), "model_configured": qa.model_ready(),
            "device_execution_enabled": True, "repair_enabled": False},
            "/api/cases": {"cases": qa.CASES}, "/api/sources": qa.SOURCES}
        if path == "/":
            page = (ROOT / "web/index.html").read_text(encoding='utf-8').replace('__CSRF_TOKEN__', CSRF_TOKEN)
            self.send(200, page.encode('utf-8'), "text/html; charset=utf-8")
        elif path in routes:
            self.send(200, routes[path])
        else:
            self.send(404, {"error": "接口不存在"})

    def do_POST(self):
        if not self.local_host():
            self.send(403, {'error': '请通过本机SSH隧道访问'}); return
        if self.path not in ("/api/ask", "/api/diagnose", "/api/chat"):
            self.send(404, {"error": "接口不存在"}); return
        is_diagnosis = self.path == '/api/diagnose'
        if self.path in ("/api/diagnose", "/api/chat") and not secrets.compare_digest(self.headers.get('X-ATA-Token', '').encode('utf-8'), CSRF_TOKEN.encode('ascii')):
            self.send(403, {'error': '页面凭据失效，请刷新后重试'}); return
        # 阻止其他网页借用本机隧道调用付费模型；无跨域授权。
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send(403, {"error": "不接受跨站请求"}); return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send(415, {"error": "需要 JSON 请求"}); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError()
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            allow_new_topology = data.get("allow_new_topology", False)
            if not isinstance(allow_new_topology, bool):
                raise ValueError()
            question = data.get("question", "")
            mode, group = data.get("mode", "unknown"), data.get("group", "unknown")
            if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
                raise ValueError()
            if mode not in qa.MODES or group not in qa.GROUPS:
                raise ValueError()
            if self.path == '/api/chat':
                history = unified_chat.validate_history(data.get('history', []))
                consent = data.get('allow_context', False)
                if not isinstance(consent, bool): raise ValueError()
            if is_diagnosis:
                devices = data.get('devices')
                if (not isinstance(devices, list) or not 1 <= len(devices) <= 2 or
                    any(not isinstance(d, str) or d not in DEVICES for d in devices)):
                    raise ValueError()
                consent = data.get('allow_live_data', False)
                if not isinstance(consent, bool):
                    raise ValueError()
        except (ValueError, UnicodeError, TypeError):
            self.send(400, {"error": "请输入1～1000字的问题，并选择有效模式与设备范围"}); return
        with RATE_LOCK:
            now = time.monotonic()
            REQUEST_TIMES[:] = [t for t in REQUEST_TIMES if now - t < 60]
            limited = len(REQUEST_TIMES) >= 12
            if not limited:
                REQUEST_TIMES.append(now)
        if limited or not SLOTS.acquire(False):
            self.send(429, {"error": "当前请求较多，请稍后再试"}); return
        try:
            if self.path == '/api/chat':
                result = unified_chat.chat(question.strip(), history, consent)
            elif is_diagnosis:
                result = diagnostic_web.diagnose(question.strip(), devices, consent)
            else:
                result = qa.answer(question.strip(), mode, group, allow_new_topology)
        finally:
            SLOTS.release()
        self.send(200, result)

    def local_host(self):
        host = self.headers.get('Host', '')
        # 隧道本地端口可不同；域名限定防止DNS重绑定获取页面凭据。
        name, sep, port = host.partition(':')
        return name in ('127.0.0.1', 'localhost') and (not sep or (port.isdigit() and 0 < int(port) < 65536))

class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    print("ATA v0.7.0 listening on 127.0.0.1:17865", flush=True)
    Server(("127.0.0.1", 17865), Handler).serve_forever()
