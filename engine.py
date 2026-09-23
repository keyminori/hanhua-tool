#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译引擎（通用）：百度「大模型文本翻译」+「标准翻译」双池，带术语钉名、敏感词预筛、
上下文注入、调速、事件广播。可被 server.py 调用，也可独立复用。

关键约束（来自实战，别破）：
- 58003 = 同 IP 并发多 APPID 封禁。引擎内「单账号轮换」是安全模式；若要并行，必须每实例不同 IP。
- 格式字节级保真：逐行保留 \\r\\n / \\n / 无结尾；VO_id 尾随空格、双 TAB 空列、BOM、《》、<br> 全保留。
- 人工校对结果可持久化：重跑时优先 cache，其次已有 _zh 译文，最后才重新翻译，绝不覆盖人工改动。
"""
import os, json, time, hashlib, random, re, socket, urllib.request, urllib.error, urllib.parse, threading
from collections import deque
from queue import Queue, Empty

LLM_URL = 'https://fanyi-api.baidu.com/ait/api/aiTextTranslate'
STD_URL = 'https://fanyi-api.baidu.com/api/trans/vip/translate'
SEP = ' 〖S〗 '
MASK_RE = re.compile(r'Z(\d+)Q\1Z')
# 模型偷懒占位：如 "译文：" / "翻译：" / "翻译结果：" —— 视为低质，不写缓存
PLACEHOLDER_RE = re.compile(r'^(译文|翻译(结果)?|结果)[：:　 ]*$')


class Broker:
    """极简发布/订阅：SSE 客户端订阅后，引擎事件实时推送给所有订阅者。"""
    def __init__(self):
        self.subs = []
        self.lock = threading.Lock()
    def subscribe(self):
        q = Queue()
        with self.lock:
            self.subs.append(q)
        return q
    def publish(self, ev):
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass
    def close(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


def ts():
    return time.strftime('%H:%M:%S')


def split_term(raw):
    """拆出 (正文, 行尾)。行尾精确保留 '\\r\\n' / '\\n' / ''。"""
    if raw.endswith('\r\n'):
        return raw[:-2], '\r\n'
    if raw.endswith('\n'):
        return raw[:-1], '\n'
    return raw, ''


class TransError(Exception):
    def __init__(self, code, msg):
        self.code = str(code)
        self.msg = msg
        super().__init__('%s %s' % (code, msg))


class Engine:
    def __init__(self, proj_dir, accounts_path, glossary_path, out_dir=None,
                 rps=1.0, batch=5, dryrun=False, context_on=True,
                 part=0, parts=1, broker=None, metadir=None):
        self.proj_dir = proj_dir
        self.jap_dir = proj_dir
        self.out_dir = out_dir or proj_dir
        self.accounts_path = accounts_path
        self.glossary_path = glossary_path
        # 临时文件(缓存/进度)默认放 out_dir/.tt；server 会显式传 metadir 指到工具数据区，
        # 保证待翻译/已翻译两个文件夹都只有纯文本文件，不被污染。
        self.metadir = metadir or os.path.join(self.out_dir, '.tt')
        os.makedirs(self.metadir, exist_ok=True)
        self.cache_path = os.path.join(self.metadir, 'cache.json')
        self.state_path = os.path.join(self.metadir, 'state.json')
        self.lowq_path = os.path.join(self.metadir, 'lowq.json')
        self.manual_path = os.path.join(self.metadir, 'manual_ov.json')
        self.events_path = os.path.join(self.metadir, 'events.jsonl')
        # 硬性限速：1 req/s 上限（百度安全线，超过必被盯上）
        self.rps = min(float(rps), 1.0)
        self.batch = int(batch)
        self.dryrun = dryrun
        self.context_on = context_on
        self.part = int(part)
        self.parts = int(parts)
        self.broker = broker or Broker()
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.paused = False
        self.stop_flag = False
        self.last_req = 0.0
        self.last_net = 0
        self.stop_ipban = False
        self.event_hist = deque(maxlen=500)   # 事件历史：切页签/重连不丢
        self._lps_win = deque(maxlen=64)      # 吞吐窗口 (t, 累计行数)：实测速率
        self._lines_ctr = 0
        self._scan_cache = None
        self.progress = {'total': 0, 'done': 0, 'pending': 0, 'cache': 0,
                         'account': None, 'status': 'idle', 'errors': 0,
                         'current_file': '', 'queue': [], 'out_dir': self.out_dir,
                         'started': '', 'note': ''}
        self.gloss = {}
        self.mask = []          # 术语钉名表 [(jp, zh)] 按长度降序
        self.accounts = []
        self.exhausted = set()
        self.acc_idx = 0
        self.cache = {}
        self.done_files = set()
        self._load_glossary()
        self._load_state()      # 内部会按 exhausted 重载 accounts
        self._fix_acc_idx()
        self._load_lowq()

    # ---------------- 载入 ----------------
    def _load_glossary(self):
        if os.path.exists(self.glossary_path):
            try:
                g = json.load(open(self.glossary_path, encoding='utf-8'))
                if isinstance(g, dict):
                    self.gloss = g
            except Exception:
                self.gloss = {}
        self.mask = sorted(((k, v) for k, v in self.gloss.items() if k and v),
                           key=lambda x: -len(x[0]))

    def _load_accounts(self):
        acc = {'disabled': {}, 'extra': []}
        if os.path.exists(self.accounts_path):
            try:
                acc = json.load(open(self.accounts_path, encoding='utf-8'))
            except Exception:
                pass
        disabled = acc.get('disabled', {})
        extra = acc.get('extra', [])

        def part_pool(p):
            out = [e for e in extra
                   if e.get('pool') == p
                   and e['appid'] not in disabled.get(p, [])
                   and e['appid'] not in self.exhausted]
            if self.parts <= 1:
                return out
            return out[self.part::self.parts]

        llm = part_pool('llm')
        std = part_pool('std')
        self.accounts = llm + std
        if self.parts > 1:
            self.emit('info', '分片 PART=%d/%d 本副本账号 %s'
                      % (self.part, self.parts, [e['appid'] for e in self.accounts]))

    def _load_state(self):
        if os.path.exists(self.state_path):
            try:
                s = json.load(open(self.state_path, encoding='utf-8'))
                self.exhausted = set(s.get('exhausted', []))
                self.acc_idx = s.get('acc_idx', 0)
                self.done_files = set(s.get('done_files', []))
            except Exception:
                pass
        if os.path.exists(self.cache_path):
            try:
                self.cache = json.load(open(self.cache_path, encoding='utf-8'))
            except Exception:
                self.cache = {}
        self.progress['cache'] = len(self.cache)
        self._load_accounts()

    def _fix_acc_idx(self):
        while self.acc_idx < len(self.accounts) and self.accounts[self.acc_idx]['appid'] in self.exhausted:
            self.acc_idx += 1

    # ---------------- 事件 ----------------
    def emit(self, level, msg):
        ev = {'t': ts(), 'at': int(time.time()), 'level': level, 'msg': msg}
        self.event_hist.append(ev)
        self._append_event(ev)
        self.broker.publish(ev)

    def _append_event(self, ev):
        """事件落盘（JSONL 追加，留痕待查）。超 5MB 轮转为 events.jsonl.1（保留一代）。
        落盘失败绝不影响翻译主流程。"""
        try:
            if os.path.exists(self.events_path) \
                    and os.path.getsize(self.events_path) > 5 * 1024 * 1024:
                old = self.events_path + '.1'
                if os.path.exists(old):
                    os.remove(old)
                os.replace(self.events_path, old)
            with open(self.events_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(ev, ensure_ascii=False) + '\n')
        except Exception:
            pass

    def event_history(self, level=None, q=None, limit=300):
        """从落盘文件倒序查历史事件（当前文件优先，再翻轮转的 .1）。
        level: 按级别过滤(ok/info/warn/error/translate)；q: 关键字含于 msg。"""
        out = []
        for p in (self.events_path, self.events_path + '.1'):
            if len(out) >= limit:
                break
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
            except Exception:
                continue
            for ln in reversed(lines):
                if len(out) >= limit:
                    break
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue
                if level and ev.get('level') != level:
                    continue
                if q and q not in str(ev.get('msg', '')):
                    continue
                out.append(ev)
        return out

    def recent_events(self, n=200):
        return list(self.event_hist)[-n:]

    # ---------------- 术语钉名 / 敏感词 ----------------
    def apply_sens_replace(self, t):
        return t.replace('シナリオ', 'ストーリー')   # scenario -> story，避开シナ

    def has_sensitive(self, t):
        return 'シナ' in self.apply_sens_replace(t)

    def mask_text(self, text):
        pre = self.apply_sens_replace(text)
        tok = {}
        i = 0
        for k, v in self.mask:
            if k in pre:
                i += 1
                tk = 'Z%dQ%dZ' % (i, i)
                tok[tk] = v
                pre = pre.replace(k, tk)
        return pre, tok

    def unmask(self, text, tok):
        for tk, v in tok.items():
            text = text.replace(tk, v)
        return MASK_RE.sub(lambda m: tok.get(m.group(0), m.group(0)), text)

    @staticmethod
    def needs_trans(t):
        return bool(t) and bool(re.search(r'[ぁ-んァ-ヶ]', t))

    def is_low_quality(self, jp, tr):
        """检测模型空回/偷懒/占位：译文为空、仅为占位词、仅标点、或相对原文骤缩。
        低质结果不写入缓存（避免锁死坏译文），交由人工或重置后重翻。"""
        if not tr:
            return True
        t2 = MASK_RE.sub('', tr).strip()
        if t2 == '':
            return True
        if PLACEHOLDER_RE.match(t2):
            return True
        # 译文里完全没有中文字符 = 模型没翻出来（漏翻）
        if not re.search(r'[一-鿿]', t2):
            return True
        # 译文仍残留日文假名 = 没翻干净（括号内注音除外，如 蜥蜴（とかげ））
        t3 = re.sub(r'（[ぁ-んァ-ヶ]+）', '', t2)
        if re.search(r'[ぁ-んァ-ヶ]', t3):
            return True
        return False

    @staticmethod
    def sanitize_tr(tr):
        """译文绝不能含 TAB/换行（会破坏 TSV 行结构）。原文列与行尾永不触碰。"""
        if not tr:
            return tr
        return tr.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')

    def _advance(self):
        """切到下一个账号（严格逐账号串行），并广播切换事件。"""
        self.acc_idx += 1
        self._save_state()
        nxt = self.accounts[self.acc_idx] if self.acc_idx < len(self.accounts) else None
        self.progress['account'] = nxt['appid'] if nxt else None
        self.emit('warn', '切换账号 → %s' % (nxt['appid'] if nxt else '（账号池已空）'))

    def _retry_one(self, text):
        """单条低质重试：从当前账号起最多尝试2次（可换账号），仍失败则留原文。"""
        last = text
        for _ in range(2):
            if self.acc_idx >= len(self.accounts):
                break
            acc = self.accounts[self.acc_idx]
            if acc['pool'] != 'llm':
                self.acc_idx += 1
                continue
            m, tk = self.mask_text(text)
            self.throttle()
            try:
                p = self.llm_translate(acc['appid'], acc['key'], [m])
                tr = self.unmask(p[0], tk) if p else text
            except TransError as e:
                act = self.handle_error(e.code, e.msg, acc)
                if act == 'stop':
                    raise StopIteration()
                if act == 'next_acc':
                    self.acc_idx += 1
                    self._save_state()
                    continue
                if act in ('retry', 'shrink'):
                    continue
                last = text
                break
            if not self.is_low_quality(text, tr):
                return tr
            last = tr
            self.acc_idx += 1  # 换账号再试
        return last

    def _trans_plain(self, jp, extra=''):
        """单段(不含<br>)翻译：llm -> std 各自独立遍历池子。
        skip 不跨调用/跨通道传递：死号已由 54004 全局拉黑(exhausted)，_pick 会自动排除。"""
        return self._try_llm_once(jp, extra) or self._try_std_once(jp)

    def _pick(self, pool, skip):
        for acc in self.accounts:
            if acc['pool'] == pool and acc['appid'] not in skip \
                    and acc['appid'] not in self.exhausted:
                return acc
        return None

    def _try_llm_once(self, jp, extra='', skip=None):
        """遍历池子取一个可用 llm 账号试一次。54004(欠费)记入全局 exhausted。"""
        skip = set() if skip is None else skip
        while True:
            acc = self._pick('llm', skip)
            if acc is None:
                return None
            skip.add(acc['appid'])
            self.progress['account'] = acc['appid']
            m, tk = self.mask_text(jp)
            self.throttle()
            try:
                p = self.llm_translate(acc['appid'], acc['key'], [m], ref_extra=extra)
                tr = self.unmask(p[0], tk) if p else None
            except TransError as e:
                # 补强通道轻量处理：不走 handle_error（其拉黑/重试语义会污染后续拆段）
                if e.code == '58003':      # IP 封禁：必须立即停
                    raise StopIteration()
                if e.code in ('54004', '52003', '58002', '58001', '90107'):
                    self.exhausted.add(acc['appid'])   # 账号真死，全局拉黑
                self.emit('warn', '补强换号 %s: %s' % (e.code, e.msg[:60]))
                continue                   # 换下一个账号
            if tr and not self.is_low_quality(jp, tr):
                return tr
            # 低质/原文回显：换下一个账号
        # unreachable

    def _try_std_once(self, jp, skip=None):
        skip = set() if skip is None else skip
        while True:
            acc = self._pick('std', skip)
            if acc is None:
                return None
            skip.add(acc['appid'])
            self.progress['account'] = acc['appid']
            m, tk = self.mask_text(jp)
            self.throttle()
            try:
                dst = self.std_translate(acc['appid'], acc['key'], m)
                tr = self.unmask(dst, tk)
            except TransError as e:
                if e.code == '58003':
                    raise StopIteration()
                if e.code in ('54004', '52003', '58002', '58001', '90107'):
                    self.exhausted.add(acc['appid'])
                self.emit('warn', '补强换号(std) %s: %s' % (e.code, e.msg[:60]))
                continue
            if tr and not self.is_low_quality(jp, tr):
                return tr

    def _retry_single(self, jp):
        """单句补强重试：完整翻译 + <br> 数量精确。
        策略梯度：llm整句 -> 含br则按br拆段(段级 llm->std)拼回 -> 无br走std。
        仍失败返回 None，由调用方回写原文待重跑。"""
        skip = set()
        extra = '该句必须完整翻译，逐字对应原句全部内容，不得省略、合并或只翻译开头。'
        n = jp.count('<br>')
        if n:
            extra += '原句含 %d 个 <br> 标签，译文必须恰好包含 %d 个 <br>，位置对应原文停顿。' % (n, n)
        # 1) llm 整句（最多试3个账号；br 不符早停，把池子留给拆段）
        tried = 0
        while tried < 3:
            tr = self._try_llm_once(jp, extra)
            if tr is None:
                break
            tried += 1
            if jp.count('<br>') == tr.count('<br>'):
                return tr
        # 2) 含 br：拆段逐段翻译，拼回（br 数量天然精确；每段独立遍历池子）
        if n:
            parts = jp.split('<br>')
            trs = []
            for p in parts:
                if not p.strip():
                    trs.append('')
                    continue
                t = self._trans_plain(p)
                if t is None:
                    return None
                trs.append(t)
            cand = '<br>'.join(trs)
            if not self.is_low_quality(jp, cand):
                return cand
            return None
        # 3) 无 br：std 兜底
        return self._try_std_once(jp)

    # ---------------- 网络 ----------------
    def _post(self, req, timeout, retries=3, base_wait=2):
        last = None
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except (TimeoutError, socket.timeout, urllib.error.URLError,
                    ConnectionError, ConnectionResetError) as e:
                last = e
                if attempt < retries:
                    self.emit('warn', '网络抖动(%s) 第%d次，%ds后重试'
                              % (type(e).__name__, attempt, base_wait * attempt))
                    time.sleep(base_wait * attempt)
                else:
                    raise TransError('NETERR', '%s' % type(e).__name__)
        raise last

    def llm_translate(self, appid, key, masked_batch, ctx='', ref_extra=''):
        q = SEP.join(masked_batch)
        ref = ('你是一名严谨的日文游戏文本本地化翻译，请将输入逐段翻译为简体中文。\n'
               '要求：\n'
               '1. 忠实直译，保持原文句式、标点和换行，不得擅自意译、增删、润色或添加解释。\n'
               '2. 严禁使用“日式RPG游戏台词”式的配音腔、文言腔、中二腔；不要添加原文没有的'
               '语气词、称呼或“……啊/呢/哦”等过度语气。\n'
               '3. 原文中的 <br> 换行标签与 Z数字Q数字Z 形式的伪标记必须原样保留，不得翻译或改动。\n'
               '4. 多段时用 〖S〗 分隔，段数与顺序须与原文一致；每一段都必须给出中文译文，不得遗漏或留空。')
        if ctx:
            ref += '\n前文语境(仅供理解剧情与人称，不影响译文体例，不译出): ' + ctx
        if ref_extra:
            ref += '\n' + ref_extra
        body = {'appid': appid, 'from': 'jp', 'to': 'zh', 'q': q,
                'model_type': 'llm', 'reference': ref}
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(LLM_URL, data=data,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % key})
        try:
            raw = self._post(req, 40)
            j = json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            raise TransError('HTTP%d' % e.code, e.read().decode()[:200])
        if 'error_code' in j:
            raise TransError(j['error_code'], j.get('error_msg', ''))
        dst = j['trans_result'][0]['dst']
        return [p.strip() for p in dst.split('〖S〗')]

    def std_translate(self, appid, key, masked_text):
        salt = str(random.randint(10000, 99999))
        sign = hashlib.md5((appid + masked_text + salt + key).encode()).hexdigest()
        params = {'q': masked_text, 'from': 'jp', 'to': 'zh',
                  'appid': appid, 'salt': salt, 'sign': sign}
        url = STD_URL + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            raw = self._post(req, 30)
            j = json.loads(raw.decode())
        except urllib.error.HTTPError as e:
            raise TransError('HTTP%d' % e.code, e.read().decode()[:200])
        if 'error_code' in j:
            raise TransError(j['error_code'], j.get('error_msg', ''))
        return j['trans_result'][0]['dst']

    # ---------------- 限速 / 错误处理 ----------------
    def throttle(self):
        wait = (1.0 / self.rps) - (time.time() - self.last_req)
        if wait > 0:
            time.sleep(wait)
        self.last_req = time.time()

    def handle_error(self, code, msg, acc):
        self.progress['errors'] += 1
        self.emit('error', 'API错误 %s: %s (账号 %s/%s)'
                  % (code, msg[:120], acc.get('note'), acc.get('pool')))
        if code == 'NETERR':
            self.last_net += 1
            if self.last_net >= 5:
                self.last_net = 0
                self._mark_exhausted(acc['appid'])
                return 'next_acc'
            time.sleep(8)
            return 'retry'
        self.last_net = 0
        if code == '58003':
            self.stop_ipban = True
            return 'stop'
        if code in ('54004', '52003', '58002', '58001', '90107'):
            self._mark_exhausted(acc['appid'])
            return 'next_acc'
        if code in ('54003', '59004', '52001', '52002', '30000', 'HTTP429', 'HTTP403'):
            # 30000=百度AI服务瞬时链路异常，属可重试，切勿当未知码留原文跳过
            time.sleep(10)
            return 'retry'
        if code == '59003':
            return 'shrink'
        if code == '20003':
            return 'skip_text'
        self.emit('error', '!! 未知错误码 %s，留原文跳过' % code)
        return 'skip_text'

    def _mark_exhausted(self, appid):
        self.exhausted.add(appid)
        self._save_state()
        self.emit('warn', '账号 %s 已停用(额度耗尽/封禁)' % appid)

    def _save_state(self):
        st = {'exhausted': sorted(self.exhausted), 'acc_idx': self.acc_idx,
              'done_files': sorted(self.done_files)}
        json.dump(st, open(self.state_path, 'w', encoding='utf-8'), ensure_ascii=False)

    def _save_cache(self):
        json.dump(self.cache, open(self.cache_path, 'w', encoding='utf-8'), ensure_ascii=False)
        self.progress['cache'] = len(self.cache)

    # ---------------- 批量翻译 ----------------
    def peel(self, texts, acc):
        """整批含敏感词/段数不符：逐句各发一次，命中者留原文；杜绝重复发送敏感句。"""
        appid, key = acc['appid'], acc['key']
        res = []
        for t in texts:
            m, tk = self.mask_text(t)
            self.throttle()
            try:
                p = self.llm_translate(appid, key, [m])
                res.append(self.unmask(p[0], tk) if p else t)
            except TransError as e2:
                a2 = self.handle_error(e2.code, e2.msg, acc)
                if a2 == 'stop':
                    raise StopIteration()
                if a2 == 'next_acc':
                    self.acc_idx += 1
                    self._save_state()
                    return self.llm_batch(texts)
                res.append(t)
        return res

    def llm_batch(self, texts):
        while True:
            if self.acc_idx >= len(self.accounts):
                return list(texts)
            acc = self.accounts[self.acc_idx]
            appid, key, pool = acc['appid'], acc['key'], acc['pool']
            self.progress['account'] = appid
            if pool == 'llm':
                masked = [self.mask_text(t) for t in texts]
                mlist = [m for m, _ in masked]
                tlist = [tk for _, tk in masked]
                self.throttle()
                try:
                    parts = self.llm_translate(appid, key, mlist)
                except TransError as e:
                    act = self.handle_error(e.code, e.msg, acc)
                    if act == 'stop':
                        raise StopIteration()
                    if act == 'next_acc':
                        self._advance()
                        continue
                    if act == 'retry':
                        continue
                    if act == 'shrink':
                        texts = texts[:1]
                        continue
                    if act == 'skip_text':
                        return self.peel(texts, acc)
                    return list(texts)
                if len(parts) != len(texts):
                    return self.peel(texts, acc)
                self.last_net = 0
                return [self.unmask(parts[k], tlist[k]) for k in range(len(texts))]
            else:
                res = []
                for t in texts:
                    m, tk = self.mask_text(t)
                    self.throttle()
                    try:
                        dst = self.std_translate(appid, key, m)
                        res.append(self.unmask(dst, tk))
                    except TransError as e:
                        act = self.handle_error(e.code, e.msg, acc)
                        if act == 'stop':
                            raise StopIteration()
                        if act == 'next_acc':
                            self.acc_idx += 1
                            self._save_state()
                            return self.llm_batch(texts)
                        if act == 'retry':
                            return self.llm_batch(texts)
                        res.append(t)
                return res

    # ---------------- 文件行读取 ----------------
    def _read_text(self, path):
        """读文件并自动探测编码（utf-8 → cp932(Shift-JIS) → latin-1 兜底）。
        输出 _zh.txt 一律写 UTF-8（中文无法存于 Shift-JIS），故只需读端鲁棒。"""
        with open(path, 'rb') as f:
            data = f.read()
        enc = 'utf-8'
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            try:
                data.decode('cp932')
                enc = 'cp932'
            except UnicodeDecodeError:
                enc = 'latin-1'
        return data.decode(enc), enc

    def read_pairs(self, path):
        text, _ = self._read_text(path)
        # 只按 \r\n / \n 切行（str.splitlines 会把 \u2028\x0b\x85 等也当换行，会劈碎原文行）
        out = []
        i, n = 0, len(text)
        while i < n:
            j = text.find('\n', i)
            if j == -1:
                out.append((text[i:], '', text[i:]))
                break
            if j > i and text[j - 1] == '\r':
                out.append((text[i:j - 1], '\r\n', text[i:j + 1]))
            else:
                out.append((text[i:j], '\n', text[i:j + 1]))
            i = j + 1
        return out

    def _zh_path(self, fn):
        """已翻译目录独立时：文件名与原文完全一致（不加 _zh）。
        仅当输出目录=源目录（独立脚本兜底场景）时回退加 _zh 后缀，避免覆盖源文件。"""
        if os.path.abspath(self.out_dir) == os.path.abspath(self.jap_dir):
            base = (fn[:-4] + '_zh.txt') if fn.endswith('.txt') else (fn + '_zh')
            return os.path.join(self.out_dir, base)
        return os.path.join(self.out_dir, fn)

    # ---------------- 单文件翻译（保留人工校对） ----------------
    def process_file(self, fn, ctx_lines=2):
        sp = os.path.join(self.jap_dir, fn)
        zp = self._zh_path(fn)
        src = self.read_pairs(sp)
        existing = {}
        if os.path.exists(zp):
            for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                b, _ = split_term(raw)
                c = b.split('\t')
                if len(c) >= 2:
                    existing[i] = c[1]
        out = [None] * len(src)
        pending = []  # (idx, jp, ctx)
        n_able = n_cache = n_keep = n_sens = 0
        for i, (body, term, raw) in enumerate(src):
            if body == '':
                out[i] = raw
                continue
            cols = body.split('\t')
            if len(cols) < 2 or not self.needs_trans(cols[1]):
                out[i] = raw
                continue
            n_able += 1
            jp = cols[1]
            if jp in self.cache:
                n_cache += 1
                cols[1] = self.cache[jp]
                out[i] = '\t'.join(cols) + term
            else:
                ex_tr = existing.get(i)
                if ex_tr not in (None, '', jp):
                    n_keep += 1
                    cols[1] = ex_tr           # 保留人工/上次译文
                    out[i] = '\t'.join(cols) + term
                elif self.has_sensitive(jp):
                    n_sens += 1
                    self.cache[jp] = jp
                    out[i] = raw              # 敏感词：留原文、零请求
                else:
                    ctx = ''
                    if self.context_on:
                        ctx_bits = []
                        for j in range(max(0, i - ctx_lines), i):
                            sc = src[j][0].split('\t')
                            if len(sc) >= 2 and self.needs_trans(sc[1]):
                                ctx_bits.append(sc[1])
                        ctx = ' / '.join(ctx_bits)
                    pending.append((i, jp, ctx))
                    out[i] = None
        self.emit('info', '▶ %s：可译 %d 行 = 缓存 %d + 保留已有 %d + 敏感留原 %d + 待翻译 %d'
                  % (fn, n_able, n_cache, n_keep, n_sens, len(pending)))
        # 实时进度：当前文件 + 行级进度
        self.progress['current_file'] = fn
        self.progress['file_lines_total'] = n_able
        self.progress['file_lines_done'] = n_cache + n_keep + n_sens
        lowq_changed = False
        total_batches = max(1, (len(pending) + self.batch - 1) // self.batch)
        for start in range(0, len(pending), self.batch):
            chunk = pending[start:start + self.batch]
            self.emit('info', '⏳ %s：百度翻译批次 %d/%d（本批 %d 句）'
                      % (fn, start // self.batch + 1, total_batches, len(chunk)))
            try:
                trans = self.llm_batch([t for _, t, _ in chunk])
            except StopIteration:
                raise
            batch_items = []
            for (i, jp, ctx), tr in zip(chunk, trans):
                tr = self.sanitize_tr(tr)
                low = self.is_low_quality(jp, tr) or jp.count('<br>') != tr.count('<br>')
                if low:
                    fixed = self._retry_single(jp)
                    if fixed is not None:
                        tr, low = self.sanitize_tr(fixed), False
                if low:
                    self.emit('error', '!! 低质/br不符(补强重试无效)，回写原文待重跑: %s' % jp[:30])
                    self.record_lowq(fn, i, jp, tr)
                    cols = src[i][0].split('\t')
                    cols[1] = jp                    # 回写原文：内容不丢，重跑会再次尝试
                    out[i] = '\t'.join(cols) + src[i][1]
                else:
                    self.cache[jp] = tr
                    if self.lowq.pop('%s#%d' % (fn, i), None) is not None:
                        lowq_changed = True
                    cols = src[i][0].split('\t')
                    cols[1] = tr
                    out[i] = '\t'.join(cols) + src[i][1]
                batch_items.append((jp, tr, low))
            self.progress['file_lines_done'] = min(
                self.progress.get('file_lines_total', 0),
                self.progress.get('file_lines_done', 0) + len(chunk))
            # 实时回显本批「原文 → 译文」给前端
            try:
                self.emit('translate', {'file': fn, 'items': [
                    {'jp': j[:80], 'tr': t[:140], 'low': lo} for j, t, lo in batch_items]})
            except Exception:
                pass
        if lowq_changed:
            self._save_lowq()
        with open(zp, 'w', encoding='utf-8', newline='') as fo:
            fo.write(''.join(out))
        self._lines_ctr += n_cache + n_keep + n_sens + len(pending)
        self._lps_win.append((time.time(), self._lines_ctr))

    def _dryrun_file(self, fn):
        sp = os.path.join(self.jap_dir, fn)
        zp = self._zh_path(fn)
        with open(sp, 'rb') as f:
            data = f.read()
        with open(zp, 'wb') as f:
            f.write(data)
        self.emit('info', '[DRYRUN] %s 字节级复制(未调用API)' % fn)

    # ---------------- 主循环 ----------------
    def run(self):
        try:
            self.running = True
            self.stop_flag = False
            self.progress['status'] = 'running'
            self.progress['started'] = ts()
            self.emit('ok', '引擎启动：目录 %s，账号 %d' % (self.jap_dir, len(self.accounts)))
            files = sorted(f for f in os.listdir(self.jap_dir)
                           if f.endswith('.txt') and not f.endswith('_zh.txt'))
            self.progress['total'] = len(files)
            nf = 0
            pending = deque(f for f in files if f not in self.done_files)
            self.progress['pending'] = len(pending)
            while pending:
                fn = pending.popleft()
                if self.stop_flag:
                    break
                while self.paused and not self.stop_flag:
                    time.sleep(0.5)
                if fn in self.done_files:
                    continue
                self.progress['queue'] = list(pending)[:8]   # 正在列队：接下来 8 个
                try:
                    if self.dryrun:
                        self._dryrun_file(fn)
                    else:
                        self.process_file(fn)
                except StopIteration:
                    self.emit('error', '!! 命中 IP 封禁(58003) 或停止信号，终止。')
                    break
                self.done_files.add(fn)
                nf += 1
                self.progress['done'] = len(self.done_files)
                self.progress['pending'] = len(pending)
                self.progress['queue'] = list(pending)[:8] if not self.stop_flag else []
                self.emit('ok', '✓ %s (已完成 %d/%d)' % (fn, len(self.done_files), len(files)))
                if nf % 20 == 0:
                    self._save_cache()
                    self._save_state()
            self._save_cache()
            self._save_state()
            if self.stop_ipban:
                self.progress['status'] = 'ipbanned'
                self.emit('error', '!! 触发 IP 封禁(58003)，今日停止。请明日再跑（缓存与进度已保留）。')
            else:
                self.progress['status'] = 'stopped' if self.stop_flag else 'idle'
                self.emit('ok', '批次完成：%d/%d 文件，缓存 %d 条'
                          % (len(self.done_files), len(files), len(self.cache)))
        except Exception as e:
            self.emit('error', '!! 未捕获异常: %r' % e)
        finally:
            self.running = False
            self.progress['current_file'] = ''
            self.progress['queue'] = []
            self._save_cache()
            self._save_state()

    # ---------------- 控制 ----------------
    def start(self):
        if self.running:
            return False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_flag = True
        self.paused = False

    def pause(self):
        if self.running:
            self.paused = True
            self.progress['status'] = 'paused'

    def resume(self):
        self.paused = False
        self.progress['status'] = 'running'

    def set_speed(self, rps=None, batch=None, dryrun=None, context_on=None):
        if rps is not None:
            self.rps = min(float(rps), 1.0)   # 硬限 1 req/s
            self.emit('info', '限速已设为 %.2f req/s（硬上限 1）' % self.rps)
        if batch is not None:
            self.batch = int(batch)
        if dryrun is not None:
            self.dryrun = bool(dryrun)
        if context_on is not None:
            self.context_on = bool(context_on)

    # ---------------- 人工校对接口 ----------------
    def get_file_lines(self, fn):
        sp = os.path.join(self.jap_dir, fn)
        zp = self._zh_path(fn)
        src = self.read_pairs(sp)
        existing = {}
        if os.path.exists(zp):
            for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                b, _ = split_term(raw)
                c = b.split('\t')
                if len(c) >= 2:
                    existing[i] = c[1]
        out = []
        for i, (body, term, raw) in enumerate(src):
            if body == '':
                out.append({'idx': i, 'empty': True, 'raw': body + term})
                continue
            cols = body.split('\t')
            jp = cols[1] if len(cols) >= 2 else ''
            vo = cols[-1] if cols else ''
            tr = existing.get(i, '')
            able = len(cols) >= 2 and self.needs_trans(jp)
            out.append({'idx': i, 'jp': jp, 'tr': tr, 'vo': vo, 'able': able})
        return out

    def save_file_lines(self, fn, edits):
        """保存人工校对结果；同时写入缓存，保证重跑不覆盖。"""
        sp = os.path.join(self.jap_dir, fn)
        zp = self._zh_path(fn)
        src = self.read_pairs(sp)
        existing = {}
        if os.path.exists(zp):
            for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                b, _ = split_term(raw)
                c = b.split('\t')
                if len(c) >= 2:
                    existing[i] = c[1]
        for e in edits:
            existing[e['idx']] = self.sanitize_tr(e.get('tr', ''))
        out = []
        for i, (body, term, raw) in enumerate(src):
            if body == '':
                out.append(raw)
                continue
            cols = body.split('\t')
            if len(cols) >= 2 and self.needs_trans(cols[1]):
                jp0 = cols[1]
                newtr = existing.get(i)
                if newtr is not None:
                    cols[1] = newtr
                    self.cache[jp0] = newtr
            out.append('\t'.join(cols) + term)
        with open(zp, 'w', encoding='utf-8', newline='') as fo:
            fo.write(''.join(out))
        # 校对页人工保存：命中低质队列的行自动出队并记入人工历史
        for e in edits:
            key = '%s#%d' % (fn, e['idx'])
            if key in self.lowq:
                old = self.lowq.pop(key, {}).get('tr', '')
                self._manual_record(key, e.get('tr', ''), old)
                self._save_lowq()
        self._save_cache()
        return True

    def file_status(self, fn):
        sp = os.path.join(self.jap_dir, fn)
        zp = self._zh_path(fn)
        src = self.read_pairs(sp)
        total = trans = 0
        existing = {}
        if os.path.exists(zp):
            for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                b, _ = split_term(raw)
                c = b.split('\t')
                if len(c) >= 2:
                    existing[i] = c[1]
        for i, (body, term, raw) in enumerate(src):
            if body == '':
                continue
            cols = body.split('\t')
            if len(cols) >= 2 and self.needs_trans(cols[1]):
                total += 1
                tr = existing.get(i, '')
                if tr and tr != cols[1]:
                    trans += 1
        return {'file': fn, 'total': total, 'translated': trans,
                'done': fn in self.done_files}

    def list_files(self):
        files = sorted(f for f in os.listdir(self.jap_dir)
                       if f.endswith('.txt') and not f.endswith('_zh.txt'))
        return [self.file_status(f) for f in files]

    def search(self, q, limit=200):
        q = q.strip()
        if not q:
            return []
        res = []
        for f in sorted(os.listdir(self.jap_dir)):
            if not (f.endswith('.txt') and not f.endswith('_zh.txt')):
                continue
            for ln in self.get_file_lines(f):
                if ln.get('empty'):
                    continue
                if q in (ln.get('jp', '') or '') or q in (ln.get('tr', '') or ''):
                    res.append({'file': f, 'idx': ln['idx'], 'jp': ln.get('jp', ''),
                                'tr': ln.get('tr', ''), 'vo': ln.get('vo', '')})
                    if len(res) >= limit:
                        return res
        return res

    # ---------------- 低质记录 / 人工翻译通道 ----------------
    def _load_lowq(self):
        self.lowq = {}
        if os.path.exists(self.lowq_path):
            try:
                self.lowq = json.load(open(self.lowq_path, encoding='utf-8'))
            except Exception:
                self.lowq = {}

    def _save_lowq(self):
        json.dump(self.lowq, open(self.lowq_path, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

    @staticmethod
    def _lowq_reason(jp, tr):
        if not tr or tr.strip() == '':
            return '空回'
        if PLACEHOLDER_RE.match(tr.strip()):
            return '占位'
        if not re.search(r'[一-鿿]', tr):
            return '无中文'
        t3 = re.sub(r'（[ぁ-んァ-ヶ]+）', '', tr)
        if re.search(r'[ぁ-んァ-ヶ]', t3):
            return '残留假名'
        if jp.count('<br>') != tr.count('<br>'):
            return 'br不符'
        return '低质'

    def record_lowq(self, fn, idx, jp, tr):
        self.lowq['%s#%d' % (fn, idx)] = {
            'jp': jp, 'tr': tr, 'reason': self._lowq_reason(jp, tr),
            'at': int(time.time())}
        self._save_lowq()

    def get_lowq(self):
        out = []
        for key in sorted(self.lowq):
            v = self.lowq[key]
            fn, idx = key.rsplit('#', 1)
            out.append({'key': key, 'file': fn, 'idx': int(idx),
                        'jp': v.get('jp', ''), 'tr': v.get('tr', ''),
                        'reason': v.get('reason', ''), 'at': v.get('at', 0)})
        return out

    def _manual_record(self, key, cn, old):
        """人工翻译历史，格式对齐旧汉化系统 manual_ov.json：{at, cn, old, n}。"""
        ov = {}
        if os.path.exists(self.manual_path):
            try:
                ov = json.load(open(self.manual_path, encoding='utf-8'))
            except Exception:
                ov = {}
        e = ov.get(key) or {'n': 0}
        e.update({'at': int(time.time()), 'cn': cn, 'old': old})
        e['n'] = e.get('n', 0) + 1
        ov[key] = e
        json.dump(ov, open(self.manual_path, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)

    def resolve_lowq(self, key, tr):
        """人工译文落盘(写文件+缓存) + 记历史 + 出队。
        先出队再落盘：save_file_lines 的自动出队逻辑不会重复记历史。"""
        v = self.lowq.get(key) or {}
        self.lowq.pop(key, None)
        self._save_lowq()
        fn, idx = key.rsplit('#', 1)
        self.save_file_lines(fn, [{'idx': int(idx), 'tr': tr}])
        self._manual_record(key, tr, v.get('tr', ''))
        return True

    def rewrite_term(self, old, new):
        """术语改译名后回写全部已产出文件：只替换译文列(第2列)中的 old→new，
        原文列与行尾一个字节不碰；同步替换缓存值（重跑命中缓存时不会写回旧译名）。
        引擎运行中调用会有写文件竞态，server 层已挡。"""
        if not old or old == new:
            return {'files_changed': 0, 'replaced': 0, 'cache_replaced': 0}
        files = sorted(f for f in os.listdir(self.jap_dir)
                       if f.endswith('.txt') and not f.endswith('_zh.txt'))
        files_changed = replaced = 0
        for fn in files:
            zp = self._zh_path(fn)
            if not os.path.exists(zp):
                continue
            rows = []
            changed = 0
            for body, term, raw in self.read_pairs(zp):
                if body == '':
                    rows.append(raw)
                    continue
                cols = body.split('\t')
                if len(cols) >= 2 and old in cols[1]:
                    changed += cols[1].count(old)
                    cols[1] = cols[1].replace(old, new)
                rows.append('\t'.join(cols) + term)
            if changed:
                with open(zp, 'w', encoding='utf-8', newline='') as fo:
                    fo.write(''.join(rows))
                files_changed += 1
                replaced += changed
                self.emit('ok', '术语回写 %s：%d 处 %s → %s' % (fn, changed, old, new))
        cache_replaced = 0
        for k, v in self.cache.items():
            if isinstance(v, str) and old in v:
                self.cache[k] = v.replace(old, new)
                cache_replaced += 1
        if cache_replaced:
            self._save_cache()
        self.emit('ok', '术语回写完成：%d 文件 %d 处（%s → %s），缓存更新 %d 条'
                  % (files_changed, replaced, old, new, cache_replaced))
        return {'files_changed': files_changed, 'replaced': replaced,
                'cache_replaced': cache_replaced}

    def requeue_failed(self):
        """扫产物重建低质队列（找回历史失败行的唯一可靠途径——事件流是内存的，
        但失败行当年被"回写原文"落盘，产物即证据）。
        判据：译文为空 / 低质(含=原文的残留假名) / br 数不符。
        敏感词故意留原文的行(cache[jp]==jp)跳过。所属文件从 done_files 摘除，
        下次引擎重跑：缓存命中行零请求，只重翻失败行。"""
        files = sorted(f for f in os.listdir(self.jap_dir)
                       if f.endswith('.txt') and not f.endswith('_zh.txt'))
        added = 0
        affected = set()
        for fn in files:
            zp = self._zh_path(fn)
            if not os.path.exists(zp):
                continue
            src = self.read_pairs(os.path.join(self.jap_dir, fn))
            prev = {}
            for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                b, _ = split_term(raw)
                c = b.split('\t')
                if len(c) >= 2:
                    prev[i] = c[1]
            for i, (body, term, raw) in enumerate(src):
                if body == '':
                    continue
                cols = body.split('\t')
                if len(cols) < 2 or not self.needs_trans(cols[1]):
                    continue
                jp = cols[1]
                tr = prev.get(i, '')
                if self.cache.get(jp) == jp:
                    continue          # 敏感词/人工指定留原文，不算失败
                bad = (tr == '') or self.is_low_quality(jp, tr) \
                    or jp.count('<br>') != tr.count('<br>')
                if not bad:
                    continue
                key = '%s#%d' % (fn, i)
                if key not in self.lowq:
                    self.lowq[key] = {'jp': jp, 'tr': tr,
                                      'reason': ('未翻译' if tr == ''
                                                 else self._lowq_reason(jp, tr)),
                                      'at': int(time.time())}
                    added += 1
                affected.add(fn)
        self._save_lowq()
        for fn in affected:
            self.done_files.discard(fn)
        if affected:
            self._save_state()
        return {'added': added, 'files_affected': len(affected),
                'queue_total': len(self.lowq)}

    def dismiss_lowq(self, key):
        """标注留原文/无需翻译，出队（不记译文历史）。"""
        gone = self.lowq.pop(key, None) is not None
        self._save_lowq()
        return gone

    def get_manual(self):
        if not os.path.exists(self.manual_path):
            return []
        try:
            ov = json.load(open(self.manual_path, encoding='utf-8'))
        except Exception:
            return []
        out = []
        for key in sorted(ov):
            e = ov[key]
            fn, idx = key.rsplit('#', 1)
            out.append({'key': key, 'file': fn, 'idx': int(idx),
                        'cn': e.get('cn', ''), 'old': e.get('old', ''),
                        'n': e.get('n', 0), 'at': e.get('at', 0)})
        return out

    # ---------------- 真实进度扫描（产物即进度，不看状态文件） ----------------
    def _measured_lps(self):
        win = self._lps_win
        if len(win) < 2:
            return 0.0
        (t0, c0), (t1, c1) = win[0], win[-1]
        if t1 - t0 < 5 or c1 <= c0:
            return 0.0
        return (c1 - c0) / (t1 - t0)

    def scan_progress(self, max_age=30):
        now = time.time()
        if self._scan_cache and max_age > 0 and now - self._scan_cache[0] < max_age:
            return self._scan_cache[1]
        files = sorted(f for f in os.listdir(self.jap_dir)
                       if f.endswith('.txt') and not f.endswith('_zh.txt'))
        lines_total = lines_done = 0
        f_done = f_part = f_todo = 0
        last_write = 0
        for fn in files:
            sp = os.path.join(self.jap_dir, fn)
            zp = self._zh_path(fn)
            src = self.read_pairs(sp)
            prev = {}
            if os.path.exists(zp):
                last_write = max(last_write, os.path.getmtime(zp))
                for i, (_, _, raw) in enumerate(self.read_pairs(zp)):
                    b, _ = split_term(raw)
                    c = b.split('\t')
                    if len(c) >= 2:
                        prev[i] = c[1]
            n_here = d_here = 0
            for i, (body, term, raw) in enumerate(src):
                if body == '':
                    continue
                cols = body.split('\t')
                if len(cols) < 2 or not self.needs_trans(cols[1]):
                    continue
                n_here += 1
                cur = prev.get(i, '')
                if cur and cur != cols[1] and not re.search(r'[ぁ-んァ-ヶ]', cur):
                    d_here += 1
            lines_total += n_here
            lines_done += d_here
            if n_here and d_here >= n_here:
                f_done += 1
            elif d_here > 0:
                f_part += 1
            else:
                f_todo += 1
        left = lines_total - lines_done
        lps = self._measured_lps()
        measured = lps > 0
        if not measured:
            lps = self.rps * max(1, self.batch) * 0.85   # 0.85 = 补强重试损耗
        eta_s = (left / lps) if (lps > 0 and left > 0) else (0 if left == 0 else None)
        res = {'files_total': len(files), 'files_done': f_done,
               'files_partial': f_part, 'files_todo': f_todo,
               'lines_total': lines_total, 'lines_done': lines_done,
               'lines_left': left,
               'pct': round(100.0 * lines_done / max(lines_total, 1), 2),
               'lps': round(lps, 2), 'lps_measured': measured,
               'eta_sec': eta_s, 'batch': max(1, self.batch),
               'lowq_open': len(self.lowq), 'cache': len(self.cache),
               'last_write': int(last_write), 'scanned_at': int(now)}
        self._scan_cache = (now, res)
        return res

    # ---------------- 账号连通测试 ----------------
    def test_account(self, appid, key, pool):
        try:
            if pool == 'llm':
                r = self.llm_translate(appid, key, ['Z1Q1Z'], ctx='')
                return True, r
            else:
                r = self.std_translate(appid, key, 'Z1Q1Z')
                return True, r
        except TransError as e:
            return False, '%s %s' % (e.code, e.msg)
        except Exception as e:
            return False, '%r' % e

    @staticmethod
    def test_cred(pool, appid, key):
        """不依赖项目，直接探活一个账号。"""
        e = Engine(proj_dir='.', accounts_path=os.devnull, glossary_path=os.devnull)
        return e.test_account(appid, key, pool)
