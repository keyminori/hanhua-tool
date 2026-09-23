#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用翻译工具 · 后端（Python 标准库，无第三方依赖）
- 项目管理（名/目录/账号文件/术语表文件）
- 账号增删停用、连通测试
- 术语表增删改导
- 引擎 启动/暂停/停止/调速(rps/batch/dryrun/上下文)
- 进度汇总 + SSE 实时事件
- 文件行读取、人工校对保存、跨文件搜索
"""
import os, json, time, uuid, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(HERE, 'projects.json')
PORT = int(os.environ.get('PORT', '8731'))

STATE = {'projects': [], 'active': None, 'engines': {}}
_lock = threading.Lock()
SERVER = None
START_TIME = time.time()
SERVER = None
START_TIME = time.time()


# ---------------- 项目持久化 ----------------
def load_projects():
    if os.path.exists(PROJECTS_FILE):
        try:
            data = json.load(open(PROJECTS_FILE, encoding='utf-8'))
            if isinstance(data, dict):
                STATE['projects'] = data.get('projects', [])
                STATE['active'] = data.get('active')
            else:  # 兼容旧格式（纯列表）
                STATE['projects'] = data
                STATE['active'] = None
        except Exception:
            STATE['projects'] = []
    if not STATE['projects']:
        seed_tagatame()
    # 活动项目若已不存在则回退到首个
    if STATE['active'] not in [p['id'] for p in STATE['projects']]:
        STATE['active'] = STATE['projects'][0]['id'] if STATE['projects'] else None


def save_projects():
    json.dump({'active': STATE['active'], 'projects': STATE['projects']},
              open(PROJECTS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def seed_tagatame():
    """用真实 tagatame 解包数据做种子项目，开箱即用。
    待翻译 = .../japanese；已翻译 = .../chinese（文件名与原文一致）。"""
    d = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
    if not os.path.isdir(d):
        return
    p = {
        'id': 'tagatame', 'name': 'タガタメ 剧情文档', 'dir': d,
        'out_dir': r'J:\tagatame\解包\_extract\剧情文档\Loc\chinese',
        'accounts_path': r'J:\tagatame\解包\汉化\accounts.json',
        'glossary_path': r'J:\tagatame\解包\_extract\剧情文档\Loc\glossary.json',
    }
    STATE['projects'] = [p]
    STATE['active'] = p['id']
    save_projects()


def get_project(pid=None):
    pid = pid or STATE['active']
    for p in STATE['projects']:
        if p['id'] == pid:
            return p
    return None


def get_engine(pid=None):
    p = get_project(pid)
    if not p:
        return None
    with _lock:
        eng = STATE['engines'].get(p['id'])
        if eng is None:
            eng = engine.Engine(
                proj_dir=p['dir'],
                out_dir=p.get('out_dir') or p['dir'],
                accounts_path=p.get('accounts_path') or os.path.join(HERE, 'data', p['id'], 'accounts.json'),
                glossary_path=p.get('glossary_path') or os.path.join(HERE, 'data', p['id'], 'glossary.json'),
                # 临时文件(缓存/进度)放工具数据区，绝不污染待翻译/已翻译文件夹
                metadir=os.path.join(HERE, 'data', p['id'], '.tt'),
            )
            STATE['engines'][p['id']] = eng
        return eng


def drop_engine(pid=None):
    p = get_project(pid)
    if not p:
        return
    with _lock:
        STATE['engines'].pop(p['id'], None)


# ---------------- 账号文件操作 ----------------
def read_accounts_file(path):
    if not os.path.exists(path):
        return {'disabled': {}, 'extra': []}
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return {'disabled': {}, 'extra': []}


def write_accounts_file(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默，事件走 SSE

    def _send(self, code, obj=None, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if obj is not None:
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            return {}

    def _ok(self, obj=None):
        self._send(200, obj)

    def _err(self, msg, code=400):
        self._send(code, {'error': msg})

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        if path in ('/', '/index.html'):
            return self._serve_index()
        if path == '/api/projects':
            return self._ok(STATE['projects'])
        if path == '/api/active':
            p = get_project()
            if not p:
                return self._err('无活动项目', 404)
            eng = get_engine()
            return self._ok({'project': p, 'progress': eng.progress if eng else {}})
        if path == '/api/accounts':
            return self._ok(self._accounts_list())
        if path == '/api/glossary':
            return self._ok(self._glossary_list())
        if path == '/api/glossary/export':
            return self._glossary_export()
        if path == '/api/server/status':
            return self._ok({'running': True, 'uptime': int(time.time() - START_TIME),
                             'pid': os.getpid()})
        if path == '/api/progress':
            eng = get_engine()
            return self._ok(eng.progress if eng else {})
        if path == '/api/lowq':
            eng = get_engine()
            return self._ok(eng.get_lowq() if eng else [])
        if path == '/api/manual':
            eng = get_engine()
            return self._ok(eng.get_manual() if eng else [])
        if path == '/api/scan':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            force = q.get('force', ['0'])[0] == '1'
            return self._ok(eng.scan_progress(0 if force else 30))
        if path == '/api/files':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            return self._ok(eng.list_files())
        if path == '/api/events/recent':
            eng = get_engine()
            return self._ok(eng.recent_events() if eng else [])
        if path == '/api/events/history':
            eng = get_engine()
            if not eng:
                return self._ok([])
            lvl = q.get('level', [''])[0] or None
            kw = q.get('q', [''])[0] or None
            try:
                lim = min(int(q.get('limit', ['300'])[0]), 5000)
            except Exception:
                lim = 300
            return self._ok(eng.event_history(level=lvl, q=kw, limit=lim))
        if path == '/api/events':
            return self._sse(q.get('pid', [None])[0])
        if path.startswith('/api/file/') and path.endswith('/lines'):
            name = path[len('/api/file/'):-len('/lines')]
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            return self._ok(eng.get_file_lines(name))
        if path == '/api/search':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            return self._ok(eng.search(q.get('q', [''])[0]))
        return self._err('未知路径 ' + path, 404)

    # ---- POST ----
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        body = self._body()
        if path == '/api/projects':
            return self._create_project(body)
        if path == '/api/projects/select':
            pid = body.get('pid')
            if get_project(pid):
                STATE['active'] = pid
                save_projects()
                return self._ok({'active': pid})
            return self._err('项目不存在', 404)
        if path == '/api/accounts':
            return self._account_add(body)
        if path == '/api/accounts/test':
            return self._account_test(body)
        if path == '/api/glossary':
            return self._glossary_upsert(body)
        if path == '/api/glossary/import':
            return self._glossary_import(body)
        if path == '/api/glossary/rewrite':
            return self._glossary_rewrite(body)
        if path == '/api/engine/start':
            return self._engine_start()
        if path == '/api/engine/stop':
            eng = get_engine()
            if eng:
                eng.stop()
            return self._ok({'status': 'stopping'})
        if path == '/api/engine/pause':
            eng = get_engine()
            if eng:
                eng.pause()
            return self._ok({'status': 'paused'})
        if path == '/api/engine/resume':
            eng = get_engine()
            if eng:
                eng.resume()
            return self._ok({'status': 'running'})
        if path == '/api/engine/speed':
            eng = get_engine()
            if eng:
                eng.set_speed(rps=body.get('rps'), batch=body.get('batch'),
                              dryrun=body.get('dryrun'), context_on=body.get('context_on'))
            return self._ok({'ok': True})
        if path == '/api/engine/retr':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            jp = (body.get('text') or '').strip()
            if not jp:
                return self._err('text 必填')
            tr = eng._retry_single(jp)
            return self._ok({'tr': tr or ''})
        if path == '/api/engine/reset':
            # 清空已完成清单，允许整项目重翻
            eng = get_engine()
            if eng:
                eng.done_files = set()
                eng._save_state()
            return self._ok({'ok': True})
        if path == '/api/lowq/resolve':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            key = (body.get('key') or '').strip()
            if not key:
                return self._err('key 必填')
            eng.resolve_lowq(key, body.get('tr', ''))
            return self._ok({'ok': True})
        if path == '/api/lowq/requeue':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            return self._ok(eng.requeue_failed())
        if path == '/api/lowq/dismiss':
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            eng.dismiss_lowq((body.get('key') or '').strip())
            return self._ok({'ok': True})
        if path == '/api/server/shutdown':
            return self._server_shutdown()
        if path.startswith('/api/file/') and path.endswith('/save'):
            name = path[len('/api/file/'):-len('/save')]
            eng = get_engine()
            if not eng:
                return self._err('无活动项目', 404)
            eng.save_file_lines(name, body.get('edits', []))
            return self._ok({'ok': True})
        if path.startswith('/api/file/') and path.endswith('/undone'):
            name = path[len('/api/file/'):-len('/undone')]
            eng = get_engine()
            if eng and name in eng.done_files:
                eng.done_files.discard(name)
                eng._save_state()
            return self._ok({'ok': True})
        return self._err('未知路径 ' + path, 404)

    # ---- DELETE ----
    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path.startswith('/api/project/'):
            pid = path[len('/api/project/'):]
            before = len(STATE['projects'])
            STATE['projects'] = [p for p in STATE['projects'] if p['id'] != pid]
            if len(STATE['projects']) == before:
                return self._err('项目不存在', 404)
            if STATE['active'] == pid:
                STATE['active'] = STATE['projects'][0]['id'] if STATE['projects'] else None
            STATE['engines'].pop(pid, None)
            save_projects()
            return self._ok({'ok': True})
        if path.startswith('/api/glossary/'):
            jp = path[len('/api/glossary/'):]
            return self._glossary_delete(jp)
        if path.startswith('/api/accounts/') and path.endswith('/disable'):
            appid = path[len('/api/accounts/'):-len('/disable')]
            return self._account_set(appid, disable=True)
        if path.startswith('/api/accounts/') and path.endswith('/enable'):
            appid = path[len('/api/accounts/'):-len('/enable')]
            return self._account_set(appid, disable=False)
        return self._err('未知路径 ' + path, 404)

    # ---------------- 具体实现 ----------------
    def _serve_index(self):
        idx = os.path.join(HERE, 'index.html')
        if not os.path.exists(idx):
            return self._err('index.html 缺失', 404)
        with open(idx, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(data)

    def _create_project(self, b):
        name = (b.get('name') or '').strip()
        d = (b.get('dir') or '').strip()
        if not name or not d:
            return self._err('项目名与目录必填')
        if not os.path.isdir(d):
            return self._err('目录不存在: ' + d)
        pid = b.get('id') or ('p' + uuid.uuid4().hex[:8])
        od = (b.get('out_dir') or '').strip() or d
        if not os.path.isdir(od):
            try:
                os.makedirs(od, exist_ok=True)
            except Exception:
                return self._err('已翻译目录不存在且创建失败: ' + od)
        # 账号/术语表路径：显式给则用给的，否则放工具数据区默认
        acc = b.get('accounts_path') or os.path.join(HERE, 'data', pid, 'accounts.json')
        glo = b.get('glossary_path') or os.path.join(HERE, 'data', pid, 'glossary.json')
        proj = {'id': pid, 'name': name, 'dir': d, 'out_dir': od,
                'accounts_path': acc, 'glossary_path': glo}
        os.makedirs(os.path.dirname(acc), exist_ok=True)
        # 创建默认文件（若不存在）
        if not os.path.exists(acc):
            write_accounts_file(acc, {'disabled': {}, 'extra': []})
        if not os.path.exists(glo):
            json.dump({}, open(glo, 'w', encoding='utf-8'), ensure_ascii=False)
        STATE['projects'].append(proj)
        STATE['active'] = pid
        save_projects()
        return self._ok({'project': proj})

    def _accounts_list(self):
        p = get_project()
        if not p:
            return []
        data = read_accounts_file(p['accounts_path'])
        eng = get_engine()
        exhausted = eng.exhausted if eng else set()
        out = []
        for e in data.get('extra', []):
            pool = e.get('pool')
            disabled = e['appid'] in data.get('disabled', {}).get(pool, [])
            status = 'disabled' if disabled else ('exhausted' if e['appid'] in exhausted else 'active')
            out.append({'appid': e['appid'], 'note': e.get('note', ''), 'pool': pool,
                        'key': e.get('key', ''), 'status': status})
        return out

    def _account_add(self, b):
        p = get_project()
        if not p:
            return self._err('无活动项目')
        appid = (b.get('appid') or '').strip()
        key = (b.get('key') or '').strip()
        pool = b.get('pool') or 'llm'
        note = b.get('note', '')
        if not appid or not key:
            return self._err('appid 与 key 必填')
        data = read_accounts_file(p['accounts_path'])
        data.setdefault('extra', [])
        data.setdefault('disabled', {})
        # 若之前被停用，重新启用
        if appid in data['disabled'].get(pool, []):
            data['disabled'][pool].remove(appid)
        data['extra'] = [e for e in data['extra'] if e['appid'] != appid]
        data['extra'].append({'appid': appid, 'key': key, 'note': note, 'pool': pool})
        write_accounts_file(p['accounts_path'], data)
        drop_engine()
        return self._ok({'ok': True})

    def _account_set(self, appid, disable):
        p = get_project()
        if not p:
            return self._err('无活动项目')
        data = read_accounts_file(p['accounts_path'])
        data.setdefault('disabled', {})
        # 找到其 pool
        pool = None
        for e in data.get('extra', []):
            if e['appid'] == appid:
                pool = e.get('pool')
                break
        if pool is None:
            return self._err('账号不存在')
        data['disabled'].setdefault(pool, [])
        if disable:
            if appid not in data['disabled'][pool]:
                data['disabled'][pool].append(appid)
        else:
            if appid in data['disabled'][pool]:
                data['disabled'][pool].remove(appid)
        write_accounts_file(p['accounts_path'], data)
        drop_engine()
        return self._ok({'ok': True, 'status': 'disabled' if disable else 'active'})

    def _account_test(self, b):
        pool = b.get('pool') or 'llm'
        appid = b.get('appid', '')
        key = b.get('key', '')
        if not appid or not key:
            return self._err('appid 与 key 必填')
        ok, r = engine.Engine.test_cred(pool, appid, key)
        return self._ok({'ok': ok, 'result': r})

    def _glossary_list(self):
        p = get_project()
        if not p:
            return []
        if not os.path.exists(p['glossary_path']):
            return []
        try:
            g = json.load(open(p['glossary_path'], encoding='utf-8'))
        except Exception:
            return []
        return [{'jp': k, 'zh': v} for k, v in g.items()]

    def _glossary_upsert(self, b):
        p = get_project()
        if not p:
            return self._err('无活动项目')
        jp = (b.get('jp') or '').strip()
        zh = (b.get('zh') or '').strip()
        if not jp:
            return self._err('日文词必填')
        g = {}
        if os.path.exists(p['glossary_path']):
            try:
                g = json.load(open(p['glossary_path'], encoding='utf-8'))
            except Exception:
                g = {}
        g[jp] = zh
        json.dump(g, open(p['glossary_path'], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        drop_engine()  # 重新载入术语表
        return self._ok({'ok': True, 'count': len(g)})

    def _glossary_delete(self, jp):
        p = get_project()
        if not p:
            return self._err('无活动项目')
        jp = urllib.parse.unquote(jp)
        g = {}
        if os.path.exists(p['glossary_path']):
            try:
                g = json.load(open(p['glossary_path'], encoding='utf-8'))
            except Exception:
                g = {}
        if jp in g:
            del g[jp]
            json.dump(g, open(p['glossary_path'], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            drop_engine()
            return self._ok({'ok': True})
        return self._err('词条不存在', 404)

    def _glossary_import(self, b):
        p = get_project()
        if not p:
            return self._err('无活动项目')
        text = b.get('text', '')
        g = {}
        if os.path.exists(p['glossary_path']):
            try:
                g = json.load(open(p['glossary_path'], encoding='utf-8'))
            except Exception:
                g = {}
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 支持 日文=中文 / 日文：中文 / 日文\t中文 / 日文,中文(CSV)
            if '=' in line:
                k, v = line.split('=', 1)
            elif '：' in line:
                k, v = line.split('：', 1)
            elif '\t' in line:
                k, v = line.split('\t', 1)
            elif ',' in line:
                k, v = line.split(',', 1)
            else:
                continue
            k, v = k.strip(), v.strip()
            if k:
                g[k] = v
                added += 1
        json.dump(g, open(p['glossary_path'], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        drop_engine()
        return self._ok({'ok': True, 'added': added, 'count': len(g)})

    def _glossary_rewrite(self, b):
        """改术语译名 + 把旧译名回写到全部产物与缓存。一次请求完成。
        引擎运行中拒绝（与 process_file 写同一批产物文件会竞态）。"""
        p = get_project()
        if not p:
            return self._err('无活动项目')
        jp = (b.get('jp') or '').strip()
        new = (b.get('new') or '').strip()
        old = (b.get('old') or '').strip()
        if not jp or not new:
            return self._err('jp 与 new 必填')
        eng = get_engine()
        if not eng:
            return self._err('无活动项目', 404)
        if eng.running:
            return self._err('引擎正在翻译，请先暂停/停止再回写（避免文件写竞态）')
        r = eng.rewrite_term(old, new) if (old and old != new) else {
            'files_changed': 0, 'replaced': 0, 'cache_replaced': 0}
        # 更新术语表并重载引擎（新行翻译用新译法）
        g = {}
        if os.path.exists(p['glossary_path']):
            try:
                g = json.load(open(p['glossary_path'], encoding='utf-8'))
            except Exception:
                g = {}
        g[jp] = new
        json.dump(g, open(p['glossary_path'], 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        drop_engine()
        return self._ok(r)

    def _glossary_export(self):
        p = get_project()
        if not p:
            self._err('无活动项目', 404)
            return
        g = {}
        if os.path.exists(p['glossary_path']):
            try:
                g = json.load(open(p['glossary_path'], encoding='utf-8'))
            except Exception:
                g = {}
        lines = ['%s,%s' % (k.replace('\n', ' '), v.replace('\n', ' '))
                 for k, v in g.items()]
        body = '\ufeff' + '\n'.join(lines)  # BOM 让 Excel 正确识别 UTF-8
        self.send_response(200)
        self.send_header('Content-Type', 'text/csv; charset=utf-8')
        self.send_header('Content-Disposition', 'attachment; filename="glossary.csv"')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def _server_shutdown(self):
        import threading
        # 延迟退出：先让响应送达客户端，再结束进程
        def _exit():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return self._ok({'ok': True})

    def _engine_start(self):
        eng = get_engine()
        if not eng:
            return self._err('无活动项目')
        if eng.running:
            return self._ok({'status': 'already_running'})
        ok = eng.start()
        return self._ok({'started': ok, 'status': eng.progress.get('status')})

    # ---------------- SSE ----------------
    def _sse(self, pid):
        eng = get_engine(pid)
        if not eng:
            self._err('无活动项目', 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        q = eng.broker.subscribe()
        try:
            self.wfile.write(b': connected\n\n')
            self.wfile.flush()
            last = time.time()
            while True:
                try:
                    ev = q.get(timeout=15)
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(('data: ' + payload + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                    last = time.time()
                except Exception:
                    if time.time() - last > 30:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                        last = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            eng.broker.close(q)


def main():
    global SERVER
    load_projects()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    SERVER = srv
    print('翻译工具后端已启动: http://localhost:%d' % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == '__main__':
    main()
