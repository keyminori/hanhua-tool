# -*- coding: utf-8 -*-
"""《为谁炼金》（タガタメ / 誰ガ為のアルケミスト）汉化进度实时面板（本地网页版，前台可见）

设计原则 —— 「事实显示」，不信任任何自报状态文件：
  * 总进度：把 chinese/ 产物与日文源逐行比对（源里含假名的行 = 需要译；
    产物里该行已无假名 = 已译）。产物即进度。
  * 活性：看 mt_cache.json 的落盘时间（引擎每 2000 行落盘一次，最灵敏；
    不要看 _run.log，它一轮才写一次，20 分钟不写也正常）。
  * 本轮 / 速度 / ETA：内存采样 + _run.log 轮次记录。
  * 引擎统计：解析 _run.log 里的「引擎统计 {...}」。

放在纯 ASCII 路径（J:\\tagatame\\）是为了能用计划任务拉起而不传中文参数。

启动:  python progress_server.py     ->  http://127.0.0.1:8777
"""
import os
import re
import io
import sys
import json
import glob
import time
import ast
import threading
import collections
import importlib
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import sens_mt                     # 隔离行机翻（谷歌/必应，见 sens_mt.py）
except Exception as _e:                # 模块缺失也不能让面板起不来
    sens_mt = None
    print('sens_mt 不可用：%r' % _e)

try:
    import manual                      # 手动工作台（改译文 / 手动发请求）
except Exception as _e:
    manual = None
    print('manual 不可用：%r' % _e)

# ---- 目录：工具本体（代码）与项目数据分开 ----
_FROZEN = getattr(sys, 'frozen', False)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 源码根
# 打包后 __file__ 在临时解压目录的根，相对它算 ui/ 会差一级；而且那里是只读的临时目录，
# 日志和端口文件都不能往那写。所以两个根分开。
CODE_ROOT = (getattr(sys, '_MEIPASS', BASE) if _FROZEN else BASE)
CORE = os.path.join(CODE_ROOT, 'core')
if not os.path.isdir(CORE):            # 源码形态下兜底
    CORE = os.path.join(BASE, 'core')
if CORE not in sys.path:
    sys.path.insert(0, CORE)

try:
    import project                     # 工作区（汉化项目）管理
except Exception as _e:
    project = None
    print('project 不可用：%r' % _e)
try:
    import glossary                    # 术语表（固定译法 + 回溯）
except Exception as _e:
    glossary = None
    print('glossary 不可用：%r' % _e)

# 下面这几个会随「当前项目」变，由 bind_project() 重绑 —— 不要写死。
D = ''          # 项目状态文件目录
JP = ''         # 日文源目录
CN = ''         # 中文产物目录
RUNLOG = ''
CACHEF = ''
SENSF = ''
SENS_WORDSF = ''
CUR_PROJECT = ''

# 会变的东西一律写在「数据根」（exe 所在目录），别写进临时解压目录
DATA_ROOT = None            # bind_project() 后按 project.EXE_DIR 确定
SRCF = os.path.join(BASE, '_prog_src.json')
SELFLOG = os.path.join(BASE, '_progress_server.log')
PORT = 8777
SAMPLE_GAP = 8          # 采样间隔（秒）


def bind_project(name=None):
    """切到某个项目：引擎路径、状态文件、面板显示一起换。"""
    global D, JP, CN, RUNLOG, CACHEF, SENSF, SENS_WORDSF, CUR_PROJECT
    if project is None:
        return None
    cfg = project.apply_all(name)      # mt / mt_story / manual 一起切
    if not cfg:
        return None
    home = project.workspace(cfg['name'])
    D = home
    JP = cfg.get('src_dir') or ''
    CN = cfg.get('out_dir') or ''
    RUNLOG = os.path.join(home, '_run.log')
    CACHEF = os.path.join(home, 'mt_cache.json')
    SENSF = os.path.join(home, 'sensitive.json')
    SENS_WORDSF = os.path.join(home, 'sens_words.json')
    CUR_PROJECT = cfg['name']
    global DATA_ROOT
    DATA_ROOT = (project.EXE_DIR if project else BASE)
    if sens_mt is not None:
        # 与面板用**同一份**清单：两边不一致就会出现
        # 「面板看得见、机翻翻的是另一份」的幽灵问题。
        sens_mt.SENSF = SENSF
    return cfg

KANA = re.compile(r'[\u3040-\u3096\u30a0-\u30fa]')

# 机翻通道中文名（与 sens_mt.VN_LABEL 一致，前端展示用）
VN_CN = {'google': '谷歌', 'tencent': '腾讯', 'bing': '必应',
         'baidu_llm': '百度大模型', 'baidu': '百度通用'}


def log(msg):
    try:
        _lg = os.path.join(DATA_ROOT or BASE, '_progress_server.log')
        with io.open(_lg, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


# ----------------------------------------------------------------- 源扫描
SRC_MAP = {}            # 文件名 -> set(需要译的行id)
SRC_STORY = set()       # 属于本运行器范围（剧情）的文件名
SRC_KEY = {'sig': None}


def is_story(name):
    """与 mt_story.is_story 保持一致：本运行器只跑剧情文本。
    除此之外的 txt（sys/external_item/...）属于物品·系统文本，
    不是这个任务的范围，必须分开统计，否则主进度永远上不去。"""
    x = name.lower()
    return (x.startswith('qe') or x.startswith('eq_') or x.startswith('cq_')
            or (x[:1].isdigit() and '_a_2d' in x) or '_a_2d' in x
            or '_win' in x or '_3d' in x)


def build_src(force=False):
    global SRC_MAP, SRC_STORY
    files = sorted(os.path.basename(p) for p in glob.glob(JP + '/*.txt'))
    if not files:
        log('!! 找不到日文源目录：%s' % JP)
        return
    mx = max(os.path.getmtime(os.path.join(JP, f)) for f in files)
    sig = 'v2|%d|%.0f' % (len(files), mx)
    if not force and SRC_KEY['sig'] == sig and SRC_MAP:
        return
    if not force:
        try:
            d = json.load(io.open(SRCF, encoding='utf-8'))
            if d.get('sig') == sig:
                SRC_MAP = dict((k, set(v)) for k, v in d['map'].items())
                SRC_STORY = set(d.get('story', []))
                SRC_KEY['sig'] = sig
                log('源缓存命中：%d 文件 / %d 待译行'
                    % (len(SRC_MAP), sum(len(v) for v in SRC_MAP.values())))
                return
        except Exception:
            pass
    m = {}
    t0 = time.time()
    for f in files:
        ids = set()
        try:
            for l in io.open(os.path.join(JP, f), encoding='utf-8',
                             errors='replace'):
                if not l.strip():
                    continue
                r = l.rstrip('\n').split('\t')
                if len(r) >= 2 and KANA.search(r[1]):
                    ids.add(r[0])
        except Exception:
            pass
        m[f] = ids
    SRC_MAP = m
    SRC_STORY = set(f for f in files if is_story(f))
    SRC_KEY['sig'] = sig
    try:
        json.dump({'sig': sig,
                   'map': dict((k, sorted(v)) for k, v in m.items()),
                   'story': sorted(SRC_STORY)},
                  io.open(SRCF, 'w', encoding='utf-8'))
    except Exception:
        pass
    log('源扫描完成：%d 文件 / %d 待译行 / 剧情范围 %d 文件 / 用时 %.1fs'
        % (len(m), sum(len(v) for v in m.values()), len(SRC_STORY),
           time.time() - t0))


# ----------------------------------------------------------------- 状态
ST = {
    'ts': 0.0, 'need': 0, 'done': 0, 'uniq_done': 0,
    'files_total': 0, 'files_with_cn': 0, 'files_finished': 0,
    'per': [], 'cache': -1, 'cache_mtime': 0, 'speed': 0.0, 'speed_src': '',
    'round': None, 'engine': {}, 'logtail': [], 'err': '',
    'done_round': 0, 'cache_age': -1,
    'need_other': 0, 'done_other': 0, 'files_other': 0,
    'files_other_done': 0, 'with_cn_other': 0,
}
SAMPLES = []            # [(t, done)]
LOCK = threading.Lock()
_cache_seen = [0.0, -1]
RB = {'n': None, 'cache': 0}     # 本轮基准：轮号 -> 该轮开始时的缓存条数

# ----------------------------------------------------------------- 实时事件流
# 引擎（mt.py）每发一次百度请求、每产出一条成稿译文，都往 _api_feed.jsonl 追加
# 一条 JSON。这里只做**增量读取**：记住文件偏移，只读新增部分，永不重读全文件。
# 页面拿到的就是磁盘上的事实，不是引擎自报的状态。
FEEDF = os.path.join(D, '_api_feed.jsonl')
SEEK_TAIL = 400 * 1024           # 首次启动最多回看 400KB（够铺满首屏，又不吃内存）
WIN = 180                        # 速率窗口（秒）

FEED = {
    'seq': 0,                    # 事件序号（页面按它做增量拉取）
    'off': 0,                    # 已读到的文件偏移
    'inited': False,
    'ln': collections.deque(maxlen=400),      # 译文流
    'rq': collections.deque(maxlen=240),      # 请求流水
    'ev': collections.deque(maxlen=200),      # 账号停用 / 自检等事件
    'inflight': {},              # qid -> 发起时刻（发起了但还没结果 = 在途）
    'acct': {},                  # 账号尾号 -> 计数/状态
    'ph': {},                    # pool -> {账号尾号: 1/0}（最近一次自检）
    'err': collections.Counter(),
    'sens': collections.deque(maxlen=200),     # 敏感行隔离（百度 20003 审核拒收）
    'rb': {}, 'lb': {},          # 秒桶：请求数 / 译文数
    'ok': 0, 'bad': 0, 'n_ln': 0,
    'lock': threading.Lock(),
}


def _err_code(s):
    """从错误串里抠出百度错误码（窗口按码归类，比按整串更有用）。"""
    m = re.search(r'\b(5\d{4}|2\d{4})\b', s or '')
    if m:
        return m.group(1)
    if 'timed out' in (s or '') or '超时' in (s or ''):
        return 'timeout'
    if 'URLError' in (s or '') or 'refused' in (s or ''):
        return 'network'
    return (s or '?')[:24]


def _absorb(e):
    """把一条事件并入内存统计。"""
    k = e.get('k')
    t = e.get('t') or time.time()
    F = FEED
    F['seq'] += 1
    e['s'] = F['seq']
    sec = int(t)
    with F['lock']:
        if k == 'ln':
            F['ln'].append(e)
            F['n_ln'] += 1
            F['lb'][sec] = F['lb'].get(sec, 0) + 1
        elif k == 'qs':
            F['inflight'][e.get('q')] = t
        elif k == 'qr':
            F['inflight'].pop(e.get('q'), None)
            F['rq'].append(e)
            F['rb'][sec] = F['rb'].get(sec, 0) + 1
            tag = e.get('acc') or '?'
            a = F['acct'].setdefault(tag, {
                'pool': e.get('pool'), 'ok': 0, 'bad': 0, 'last': 0,
                'err': '', 'st': ''})
            a['last'] = t
            a['pool'] = e.get('pool') or a['pool']
            if e.get('ok'):
                F['ok'] += 1
                a['ok'] += 1
                a['st'] = 'ok'
            else:
                F['bad'] += 1
                a['bad'] += 1
                a['st'] = 'err'
                a['err'] = (e.get('err') or '')[:60]
                F['err'][_err_code(e.get('err'))] += 1
        elif k == 'acct':
            F['ev'].append(e)
            tag = e.get('acc') or '*'
            a = F['acct'].setdefault(tag, {
                'pool': e.get('pool'), 'ok': 0, 'bad': 0, 'last': 0,
                'err': '', 'st': ''})
            a['st'] = e.get('st') or a['st']
            a['err'] = (e.get('err') or '')[:60]
            a['until'] = t + (e.get('sec') or 0)
            a['last'] = t
        elif k == 'probe':
            F['ev'].append(e)
            p = F['ph'].setdefault(e.get('pool') or '?', {})
            p[e.get('acc') or '?'] = 1 if e.get('ok') else 0
            if not e.get('ok'):
                F['err'][_err_code(e.get('err'))] += 1
        elif k == 'sens':
            # 百度内容审核拒收（20003）：引擎已登记人工清单并永久跳过该行。
            # 这不是故障——换账号也没用，所以单列出来，别混进"错误"里吓人。
            F['sens'].append(e)
            F['ev'].append(e)


def read_feed():
    """增量读取事件文件。文件被轮转（变小）则从头读。"""
    F = FEED
    try:
        sz = os.path.getsize(FEEDF)
    except Exception:
        return
    if sz < F['off']:                     # 轮转/重建
        F['off'] = 0
        F['inited'] = False
    if not F['inited']:
        F['inited'] = True
        if sz > SEEK_TAIL:                # 首次只回看尾部，且对齐到行边界
            with io.open(FEEDF, 'rb') as f:
                f.seek(sz - SEEK_TAIL)
                f.readline()
                F['off'] = f.tell()
    if sz <= F['off']:
        return
    with io.open(FEEDF, encoding='utf-8', errors='replace') as f:
        f.seek(F['off'])
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                _absorb(json.loads(line))
            except Exception:
                continue
        F['off'] = f.tell()
    cut = int(time.time()) - WIN
    with F['lock']:
        for b in (F['rb'], F['lb']):
            for s in [x for x in b if x < cut]:
                b.pop(s, None)
        now = time.time()
        for q in [q for q, t0 in F['inflight'].items() if now - t0 > 90]:
            F['inflight'].pop(q, None)


def feed_reader():
    while True:
        try:
            read_feed()
        except Exception as e:
            log('事件流读取异常 %r' % e)
        time.sleep(0.35)


def _rate(b, span):
    """近 span 秒的平均速率（用秒桶，不依赖采样线程的节奏）。"""
    cut = int(time.time()) - span
    return sum(v for s, v in b.items() if s > cut) / float(span)


_SENS_CACHE = [0.0, {'total': 0, 'pend': 0}]      # [mtime, 汇总]


def sens_summary():
    """敏感清单汇总（按 mtime 缓存，避免每秒重解析整个清单）"""
    try:
        mt = os.path.getmtime(SENSF)
    except Exception:
        return {'total': 0, 'pend': 0, 'mt': 0}
    if mt == _SENS_CACHE[0]:
        return _SENS_CACHE[1]
    info = {'total': 0, 'pend': 0, 'mt': 0}
    try:
        with io.open(SENSF, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            info['total'] = len(d)
            # pend = 还没人工确认的（含已有机翻草稿的）
            info['pend'] = sum(1 for v in d.values()
                               if not (v or {}).get('cn', '').strip())
            # mt   = 已有机器初翻草稿、等人工审核的
            info['mt'] = sum(1 for v in d.values()
                             if not (v or {}).get('cn', '').strip()
                             and (v or {}).get('mt', '').strip())
    except Exception:
        pass
    _SENS_CACHE[0] = mt
    _SENS_CACHE[1] = info
    return info


# --------------------------------------------- 敏感行：词表 / 出处 / 人工编辑
# 百度 20003 是**内容级**审核（字面词表命中即整条拒收）：实测 7 个账号全拒、
# 同一账号连发 3 次稳定拒收，而把命中词换成占位符后立刻通过 —— 换账号、加重试
# 都无解，唯一出路是隔离出来人工翻。以下给面板提供：词表提示、出处定位、
# 保存人工译文并立即写回产物。
_SENS_WDEF = ['シナリオ', 'シナイ']       # 实测被拒的字面词（仅界面提示用）
_SWI = {'mtime': None, 'words': []}
_SIDX = {'sig': None, 'map': {}, 'frag': {}, 'lock': threading.Lock()}
PANEL_STRUCT = re.compile(r'</?color[^<>]*>|<br\s*/?>|<p_name[^<>]*>', re.I)
_SENS_WLOCK = threading.Lock()


def sens_words():
    """提示用敏感词表：sens_words.json（可自行增删）优先，缺省用内置。"""
    mt = 0.0
    try:
        mt = os.path.getmtime(SENS_WORDSF)
    except Exception:
        pass
    if _SWI['mtime'] == mt and _SWI['words']:
        return _SWI['words']
    ws = list(_SENS_WDEF)
    try:
        d = json.load(io.open(SENS_WORDSF, encoding='utf-8'))
        if isinstance(d, dict):
            d = (d.get('words') or []) + (d.get('phrases') or [])
        ws = [str(x) for x in d if str(x)]
    except Exception:
        pass
    _SWI['mtime'] = mt
    _SWI['words'] = ws
    return ws


def hit_words(text, words=None):
    """标出原文里命中的敏感词位置（界面高亮 + 提示）。"""
    words = words if words is not None else sens_words()
    out = []
    for w in words:
        if not w:
            continue
        st = 0
        while True:
            i = text.find(w, st)
            if i < 0:
                break
            out.append({'w': w, 'i': i, 'n': len(w)})
            st = i + len(w)
    out.sort(key=lambda x: (x['i'], -x['n']))
    keep = []
    for h in out:                    # 去重叠：重叠时保留靠前/更长的那个
        if keep and h['i'] < keep[-1]['i'] + keep[-1]['n']:
            continue
        keep.append(h)
    return keep


def sens_sig():
    try:
        return '%.3f' % os.path.getmtime(SENSF)
    except Exception:
        return '0'


def sens_read():
    try:
        d = json.load(io.open(SENSF, encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def src_index(force=False):
    """返回 (exact, frag) 两个映射，值都是 [(文件名, 行id)]。

    exact = 该原文在源里是**完整一行**；frag = 只是某行的一个**片段**
    （引擎对长行会退到按 <br> 分片段送翻，被拒时登记的是片段原文）。

    查**日文源**而不是产物：源永远保持原文、行号稳定；产物一旦被译掉就不再等于
    原文，拿产物当出处会越查越少。只在清单变化时重建（放后台线程）。"""
    sig = sens_sig()
    with _SIDX['lock']:
        if (not force) and _SIDX['sig'] == sig:
            return _SIDX['map'], _SIDX['frag']
    want = set(sens_read().keys())
    m, fr = {}, {}
    t0 = time.time()
    if want:
        for p in glob.glob(JP + '/*.txt'):
            name = os.path.basename(p)
            try:
                for l in io.open(p, encoding='utf-8', errors='replace'):
                    if not l.strip():
                        continue
                    r = l.rstrip('\r\n').split('\t')
                    if len(r) < 2:
                        continue
                    txt = r[1]
                    if txt in want:
                        m.setdefault(txt, []).append((name, r[0]))
                        continue
                    for w in want:          # 片段兜底（条目少，逐条 in 的代价可忽略）
                        if len(w) >= 3 and w not in m and w in txt:
                            fr.setdefault(w, []).append((name, r[0]))
            except Exception:
                pass
    with _SIDX['lock']:
        _SIDX['sig'] = sig
        _SIDX['map'] = m
        _SIDX['frag'] = fr
    log('敏感出处索引：清单 %d 条 / 整行 %d 条 / 片段 %d 条 / %.1fs'
        % (len(want), len(m), len(fr), time.time() - t0))
    return m, fr


def sens_worker():
    """后台维护出处索引（索引要扫全量日文源，不能放在 HTTP 请求线程里）。"""
    while True:
        try:
            src_index()
        except Exception as e:
            log('敏感索引失败：%r' % e)
        time.sleep(20)


# ------------------------------------------------------------ 启停控制
# 铁律：长任务必须由**计划任务**拉起（会话里 nohup/Popen 都会被回收）。
# 所以这里的启停一律操作计划任务，而不是自己 Popen 一个进程。
PYEXE = sys.executable or 'python.exe'
TASK_ENGINE = 'HanhuaTranslate'        # 翻译引擎（core/runner.py）
TASK_PANEL = 'HanhuaProgress'          # 本面板
DETACHED = 0x00000008 | 0x00000200     # DETACHED_PROCESS | NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW：子进程一律不许弹黑窗


def _run(cmd, timeout=30):
    """跑系统命令 -> (rc, 文本)。Windows 中文输出按 GBK 解码。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout,
                           creationflags=NO_WINDOW)
    except Exception as e:
        return -1, '%r' % e
    raw = p.stdout or b''
    for enc in ('gbk', 'utf-8'):
        try:
            return p.returncode, raw.decode(enc)
        except Exception:
            continue
    return p.returncode, raw.decode('utf-8', 'replace')


_TASK_CACHE = {}                       # name -> (ts, rc, out)
_TASK_TTL = 3.0


def _task_query(name):
    """查计划任务（3 秒 TTL）。

    前端 /live 每 10 秒轮询 /api/ctl，一次 ctl_status 要问 3 遍 schtasks；
    开两个标签就是 6 遍。缓存后无论多少标签，最多 3 秒一次真查询。
    """
    now = time.time()
    hit = _TASK_CACHE.get(name)
    if hit and now - hit[0] < _TASK_TTL:
        return hit[1], hit[2]
    rc, out = _run(['schtasks', '/query', '/tn', name, '/fo', 'LIST'], 20)
    _TASK_CACHE[name] = (now, rc, out)
    return rc, out


def task_state(name):
    """计划任务状态：running / ready / gone / ?。

    schtasks 的字段名和取值都是本地化的（中文系统=「模式: 正在运行」），
    所以同时认中英文，认不出就交给活性兜底。"""
    rc, out = _task_query(name)
    if rc != 0:
        return 'gone'
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith('模式') or ln.startswith('Status'):
            v = ln.split(':', 1)[-1].strip().lower()
            if '运行' in v or v.startswith('run'):
                return 'running'
            return 'ready'
    return '?'


def engine_age():
    """引擎活性：mt_cache.json 落盘时间（每 2000 行落一次，最灵敏）。"""
    try:
        return int(time.time() - os.path.getmtime(CACHEF))
    except Exception:
        return -1


def ctl_status():
    st_e = task_state(TASK_ENGINE)
    age = engine_age()
    return {'engine_task': st_e,
            'cache_age': age,
            'engine_alive': (0 <= age < 150) or st_e == 'running',
            'panel_task': task_state(TASK_PANEL),
            'engine': TASK_ENGINE, 'panel': TASK_PANEL,
            'next_run': _task_next(TASK_ENGINE)}


def _task_next(name):
    rc, out = _task_query(name)
    if rc != 0:
        return ''
    for ln in out.splitlines():
        if '下次' in ln or ln.strip().startswith('Next'):
            return ln.split(':', 1)[-1].strip()
    return ''


def _detached(code):
    """脱离面板进程执行一串动作。

    面板不能同步「停/重启自己」—— schtasks /end 会连 HTTP 响应一起掐掉，
    浏览器只会看到「请求失败」。所以交给一个 1.5 秒后才动手的独立进程，
    我们的响应先发出去。"""
    try:
        subprocess.Popen([PYEXE, '-c', code], creationflags=DETACHED | NO_WINDOW,
                         close_fds=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log('脱离进程启动失败：%r' % e)
        return False


def _wait_state(name, want, secs=8):
    """等任务进到期望状态——schtasks /run 是异步的，立刻查还是旧状态。"""
    for _ in range(int(secs * 2)):
        if task_state(name) == want:
            return True
        time.sleep(0.5)
    return False


def ctl_act(what, target='engine'):
    """开/关/重启 翻译引擎或本面板。what: start|stop|restart。"""
    assert what in ('start', 'stop', 'restart'), what
    assert target in ('engine', 'panel'), target
    name = TASK_ENGINE if target == 'engine' else TASK_PANEL
    st_now = task_state(name)
    if what == 'start' and st_now == 'running':
        # 计划任务的多实例策略是 IgnoreNew，重复 /run 不会起第二个进程，
        # 但会返回一条看不懂的报错；这里直接说人话。
        return {'ok': True, 'msg': '%s 已经在运行中，无需重复启动' % name}
    if target == 'panel':
        if what == 'start':
            rc, _ = _run(['schtasks', '/run', '/tn', name])
            if rc == 0 and not _wait_state(name, 'running', 8):
                return {'ok': False, 'msg': '面板没起来：笔记本用电池时'
                        '计划任务默认不允许启动（请在电源设置里插电运行）'}
            return {'ok': rc == 0, 'msg': '面板启动指令已发送（本页可能无响应）'}
        code = ("import subprocess, time\n"
                "time.sleep(1.5)\n"
                "S = ['schtasks', '/end', '/tn', '%s']\n"
                "R = ['schtasks', '/run', '/tn', '%s']\n"
                "subprocess.run(S, creationflags=0x08000000)\n"
                "%s") % (name, name,
                         'time.sleep(2)\nsubprocess.run('
                         'R, creationflags=0x08000000)\n' if what == 'restart'
                         else '')
        if not _detached(code):
            return {'ok': False, 'msg': '无法启动脱离进程'}
        return {'ok': True,
                'msg': ('面板重启中，约 5 秒后刷新本页'
                        if what == 'restart' else '面板正在停止')}
    # ---- 引擎：同步执行，快 ----
    msgs = []
    if what in ('stop', 'restart'):
        rc, out = _run(['schtasks', '/end', '/tn', name])
        msgs.append('停止%s' % ('完成' if rc == 0 else '失败(rc=%d)' % rc))
        time.sleep(2.0)
    if what in ('start', 'restart'):
        rc, out = _run(['schtasks', '/run', '/tn', name])
        msgs.append('启动%s' % ('完成' if rc == 0 else '失败(rc=%d)' % rc))
        if rc == 0:
            if _wait_state(name, 'running', 10):
                msgs.append('（已起来；引擎会重新体检账号，约 20~40 秒后才见速率）')
            else:
                msgs.append('⚠ 任务未进入运行状态 —— 笔记本用电池时计划任务'
                            '默认不允许启动，请插电后重试')
    log('控制台：%s %s -> %s' % (what, target, ' / '.join(msgs)))
    return {'ok': True, 'msg': '引擎' + '、'.join(msgs)}


# ------------------------------------------------------------ 通道体检缓存
_MT_PROBE = [0.0, []]


def mt_probe(force=False, ttl=120):
    """各机翻通道可用性（缓存 ttl 秒，避免连点把免费接口刷爆）。"""
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用', 'items': []}
    if force or time.time() - _MT_PROBE[0] > ttl or not _MT_PROBE[1]:
        try:
            _MT_PROBE[1] = sens_mt.probe(log=log)
            _MT_PROBE[0] = time.time()
        except Exception as e:
            return {'ok': False, 'err': '体检异常：%r' % e, 'items': []}
    return {'ok': True, 'at': int(_MT_PROBE[0]),
            'choices': [list(x) for x in sens_mt.VENDOR_CHOICES],
            'items': _MT_PROBE[1]}


def sens_flush_files():
    """把清单里所有可用的译文（人工 cn 优先、其次机翻 mt）同步进 chinese/ 产物。

    为什么要全量刷一遍：面板重启、引擎重跑、或机翻在别处跑过之后，产物里可能
    还是日文原文；sens_write_back 只在「产物该行仍是日文原文含该片段」时才替换，
    幂等且不覆盖已有的正经译文，所以定期全量刷是安全又省心的兜底。"""
    d = sens_read()
    n = 0
    for jp, v in d.items():
        v = v if isinstance(v, dict) else {}
        mtx = (v.get('mt') or '').strip()
        txt = (v.get('cn') or '').strip() or mtx
        if not txt:
            continue
        try:
            n += len(sens_write_back(jp, txt, mtx if txt != mtx else ''))
        except Exception as e:
            log('补写回失败 %s：%r' % (jp[:20], e))
    return n


def sens_mt_fill(force=False, only='', vendor=''):
    """给隔离清单补机翻草稿，并立即写回 chinese/ 产物。

    机翻只是**初稿**：产物里先放中文（哪怕不完美）总好过日文原文，
    人工审核时只需校对润色。引擎取值顺序 cn > mt，人工一填就盖住草稿。"""
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用（J:\\tagatame\\sens_mt.py）'}
    t0 = time.time()
    before = {}
    for jp0, v0 in sens_read().items():
        if isinstance(v0, dict):
            before[jp0] = (v0.get('mt') or '').strip()
    try:
        r = sens_mt.translate_pending(force=force, only=only, log=log,
                                      vendor=vendor)
    except Exception as e:
        log('机翻失败：%r' % e)
        return {'ok': False, 'err': '机翻异常：%r' % e}
    _SENS_CACHE[0] = 0.0                 # 汇总缓存立即失效
    files = 0
    for jp, mt, _note in (r.get('msgs') or []):
        if mt:
            try:
                files += len(sens_write_back(jp, mt, before.get(jp, '')))
            except Exception as e:
                log('机翻写回失败 %s：%r' % (jp[:20], e))
    log('机翻草稿[%s]：待翻 %d / 成功 %d / 失败 %d，写回 %d 处，用时 %.1fs'
        % (r.get('vendor') or 'auto', r.get('todo', 0), r.get('done', 0),
           r.get('fail', 0), files, time.time() - t0))
    return {'ok': True, 'vendor': r.get('vendor') or 'auto',
            'todo': r.get('todo', 0), 'done': r.get('done', 0),
            'fail': r.get('fail', 0), 'files': files,
            'msgs': [(a, b, c) for a, b, c in (r.get('msgs') or [])]}


def sens_mt_pending():
    """还没有人工译文、也没有机翻草稿的行（后台线程据此决定是否要跑）。"""
    out = []
    for jp, v in sens_read().items():
        v = v if isinstance(v, dict) else {}
        if (v.get('cn') or '').strip() or (v.get('mt') or '').strip():
            continue
        out.append(jp)
    return out


def sens_mt_worker():
    """后台自动机翻：隔离行一出现就补初稿 + 写回产物，人工只管审核。

    不能做在 HTTP 请求线程里（一轮十几秒会卡住界面）；也不能不做 ——
    隔离行会随翻译推进不断出现，等人来点按钮就永远补不完。"""
    time.sleep(8)                        # 让出处索引先建好
    try:
        n0 = sens_flush_files()          # 启动补写回：换机/重启后同步已有译文
        if n0:
            log('启动补写回：%d 处产物已同步（人工译文/机翻草稿）' % n0)
    except Exception as e:
        log('启动补写回失败：%r' % e)
    while True:
        try:
            todo = sens_mt_pending()
            if todo:
                log('后台机翻：%d 条隔离行无译文，开始补草稿' % len(todo))
                sens_mt_fill()
            else:
                # 别处（控制台 ctl.py / CLI）补的草稿、人工在别的窗口改的译文，
                # 都要落进产物，否则玩家看到的还是日文。幂等，安全。
                n = sens_flush_files()
                if n:
                    log('后台补写回：%d 处产物已同步' % n)
        except Exception as e:
            log('后台机翻线程异常：%r' % e)
        time.sleep(60)


def sens_items():
    """隔离清单全量（含命中词位置、出处、机翻草稿），供编辑界面用。"""
    d = sens_read()
    ws = sens_words()
    idx, frag = src_index()
    items = []
    for jp, v in d.items():
        v = v if isinstance(v, dict) else {}
        loc = idx.get(jp) or []
        fg = frag.get(jp) or []
        use = loc or fg
        items.append({'jp': jp, 'cn': (v.get('cn') or '').strip(),
                      'mt': (v.get('mt') or '').strip(),
                      'mt_src': v.get('mt_src') or '',
                      'mt_src_cn': VN_CN.get(v.get('mt_src') or '', ''),
                      'mt_note': v.get('mt_note') or '',
                      'mt_at': v.get('mt_at', 0),
                      'n': int(v.get('n', 0) or 0),
                      'first': v.get('first', 0), 'last': v.get('last', 0),
                      'hits': hit_words(jp, ws),
                      'where': ('%s#%s' % (use[0][0], use[0][1])) if use
                               else (v.get('src') or ''),
                      'loc_kind': 'exact' if loc else ('frag' if fg else ''),
                      'locs': len(use)})
    items.sort(key=lambda x: (1 if x['cn'] else 0, -x['n'], x['jp']))
    wc = collections.Counter()
    for it in items:
        for h in it['hits']:
            wc[h['w']] += 1
    return {'sig': sens_sig(), 'words': ws,
            'wordc': [[k, v] for k, v in wc.most_common()],
            'items': items, 'total': len(items),
            'pend': sum(1 for x in items if not x['cn']),
            'mtc': sum(1 for x in items if not x['cn'] and x['mt']),
            'rev': sum(1 for x in items if x['cn'])}


def sens_write_back(jp, cn, prev=''):
    """把译文写回 chinese/ 产物：按日文源定位到的「文件 + 行id」精确替换。

    prev = 本行之前登记的机翻草稿（mt）。产物该行若**正好等于这段草稿**，
    说明它只是初稿，允许被正式译文盖掉；否则「已有中文即保留」，草稿会被钉死。

    行尾沿用该行原有风格（源是 LF，个别产物曾被改成 CRLF）；写盘用临时文件 +
    os.replace，避免引擎正在读时读到半截文件。"""
    cn = (cn or '').strip()
    prev = (prev or '').strip()
    done = []
    if not cn:
        return done
    idx, frag = src_index()
    for name, lid in (idx.get(jp) or frag.get(jp) or []):
        p = os.path.join(CN, name)
        if not os.path.exists(p):
            continue
        try:
            with io.open(p, encoding='utf-8', newline='') as f:
                lines = f.readlines()
        except Exception:
            continue
        hit = False
        for i, l in enumerate(lines):
            r = l.rstrip('\r\n').split('\t')
            if not r or r[0] != lid:
                continue
            if len(r) > 1:
                if r[1] == jp:                 # 整行仍是原文 -> 整行替换
                    val = cn
                elif jp in r[1]:               # 片段级隔离 -> 只换该片段，其余保留
                    val = r[1].replace(jp, cn, 1)
                elif prev and r[1] == prev:    # 覆盖我们之前写进去的机翻草稿
                    val = cn
                else:
                    break                      # 已被别的译文覆盖 -> 不动
                if val == r[1]:
                    break                      # 无变化，不必重写文件
                nl = '\r\n' if l.endswith('\r\n') else '\n'
                lines[i] = '\t'.join([r[0], val] + r[2:]) + nl
                hit = True
            break
        if not hit:
            continue
        try:
            tmp = '%s.%d.tmp' % (p, threading.get_ident())
            with io.open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.writelines(lines)
            os.replace(tmp, p)
            done.append(name)
        except Exception as e:
            log('写回失败 %s：%r' % (name, e))
    return done


def sens_save(jp, cn, unflag=False):
    """保存人工译文（或撤销隔离）。清单与产物一起改，失败回错误给前端。"""
    jp = jp or ''
    if not jp.strip():
        return {'ok': False, 'err': '缺少原文'}
    _idx, _frag = src_index()
    loc = _idx.get(jp) or _frag.get(jp) or []
    prev_mt = ['']
    with _SENS_WLOCK:
        d = sens_read()
        if jp not in d:
            return {'ok': False, 'err': '该行不在隔离清单里（可能已被撤销）'}
        if unflag:
            d.pop(jp, None)
        else:
            v = d[jp] if isinstance(d[jp], dict) else {}
            prev_mt[0] = (v.get('mt') or '').strip()   # 允许盖掉自己的草稿
            v['cn'] = (cn or '').strip()
            if loc and not v.get('src'):
                v['src'] = '%s#%s' % (loc[0][0], loc[0][1])
            v['last'] = int(time.time())
            d[jp] = v
        try:
            tmp = '%s.%d.%d.tmp' % (SENSF, os.getpid(),
                                    threading.get_ident())
            with io.open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, SENSF)
        except Exception as e:
            return {'ok': False, 'err': '写清单失败：%r' % e}
    _SENS_CACHE[0] = 0.0                 # 汇总缓存立即失效
    if unflag:
        log('敏感行撤销隔离：%s' % jp[:24])
        return {'ok': True, 'unflag': True, 'lines': 0, 'files': []}
    files = sens_write_back(jp, cn, prev_mt[0])
    log('敏感行保存：%s -> %s（写回 %d 文件）'
        % (jp[:24], (cn or '')[:20], len(files)))
    return {'ok': True, 'lines': len(files), 'files': files, 'locs': len(loc)}


# ------------------------------------------------------------ 手动工作台
# 页面单独放 manual_page.html：改版式/加按钮不用碰这个文件，重启面板即可生效。
MANF_FILE = os.path.join(CODE_ROOT, 'ui', 'page.html')
if not os.path.isfile(MANF_FILE):                 # 再兜一次源码形态
    MANF_FILE = os.path.join(BASE, 'ui', 'page.html')


def _load_man():
    try:
        h = io.open(MANF_FILE, encoding='utf-8').read()
    except Exception as e:
        return ('<!DOCTYPE html><meta charset="utf-8"><body '
                'style="font:14px system-ui;padding:24px">'
                '手动工作台页面读不到（%s）：%s</body>' % (MANF_FILE, e))
    try:
        vc = json.dumps([[c[0], c[1]] for c in sens_mt.VENDOR_CHOICES],
                        ensure_ascii=False)
    except Exception:
        vc = '[["auto","自动"],["google","谷歌"],["tencent","腾讯"]]'
    return h.replace('@@THEME@@', THEME).replace('@@VENDORS@@', vc)


def _q1(path, key, default=''):
    """取查询串里的一个参数（页面只传简单值，不做花哨解析）。"""
    try:
        v = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get(key)
        return v[0] if v else default
    except Exception:
        return default


# ---------------------------------------------------------------- 引擎调速
# ---------------------------------------------------------------- 项目（工作区）
def project_get(path):
    if project is None:
        return {'ok': False, 'err': 'project 模块不可用'}
    cur = project.current()
    return {'ok': True,
            'list': project.list_projects(),
            'current': (cur or {}).get('name', ''),
            'stat': project.stat(),
            'data_dir': project.PROJECTS_DIR,
            'accounts_path': project.accounts_path()}


def project_post(req):
    """action: create / update / switch / delete / browse"""
    if project is None:
        return {'ok': False, 'err': 'project 模块不可用'}
    act = (req.get('action') or '').strip()
    if act == 'browse':
        # 给前端用的目录一览（选目录时不用手打路径）
        d = (req.get('dir') or '').strip()
        try:
            if not d:
                import string
                drives = ['%s:\\' % c for c in string.ascii_uppercase
                          if os.path.isdir('%s:\\' % c)]
                return {'ok': True, 'dir': '', 'parent': '',
                        'entries': [{'name': x, 'dir': True} for x in drives]}
            d = os.path.abspath(d)
            out = []
            for e in sorted(os.listdir(d))[:400]:
                p = os.path.join(d, e)
                try:
                    out.append({'name': e, 'dir': os.path.isdir(p)})
                except Exception:
                    pass
            return {'ok': True, 'dir': d, 'parent': os.path.dirname(d),
                    'entries': out}
        except Exception as e:
            return {'ok': False, 'err': str(e)[:120]}
    if act == 'create':
        name = (req.get('name') or '').strip()
        if not name:
            return {'ok': False, 'err': '要给项目起个名字'}
        ok, msg, cfg = project.create(
            name, (req.get('src_dir') or '').strip(),
            (req.get('out_dir') or '').strip(),
            pattern=(req.get('pattern') or '*.txt'),
            recursive=bool(req.get('recursive')),
            cols=[int(x) for x in (req.get('cols') or [0, 1, 2])],
            sep=(req.get('sep') or '\t'),
            note=(req.get('note') or ''))
        if ok:
            bind_project(cfg['name'])
            log('新建项目：%s' % cfg['name'])
        return {'ok': ok, 'err': msg, 'cfg': cfg}
    if act == 'switch':
        cfg = bind_project((req.get('name') or '').strip())
        if not cfg:
            return {'ok': False, 'err': '切换失败：没有这个项目'}
        project.set_current(cfg['name'])
        log('切换项目：%s' % cfg['name'])
        return {'ok': True, 'cfg': cfg, 'stat': project.stat()}
    if act == 'update':
        name = (req.get('name') or '').strip() or project.current_name()
        patch = {k: v for k, v in (req.get('patch') or {}).items()
                 if k in ('src_dir', 'out_dir', 'pattern', 'recursive',
                          'cols', 'sep', 'note')}
        ok, msg, cfg = project.update(name, patch)
        if ok:
            bind_project(name)
        return {'ok': ok, 'err': msg, 'cfg': cfg}
    if act == 'delete':
        ok, msg, _ = project.delete((req.get('name') or '').strip())
        if ok:
            bind_project()
        return {'ok': ok, 'err': msg}
    return {'ok': False, 'err': '未知动作：%s' % act}


# ---------------------------------------------------------------- 术语表
def glossary_get(path):
    if glossary is None:
        return {'ok': False, 'err': 'glossary 模块不可用'}
    q = (path or '')
    if 'export=1' in q:
        return {'ok': True, 'rows': glossary.export_rows()}
    return {'ok': True, 'items': glossary.items(),
            'n': len(glossary.items()),
            'project': project.current_name() if project else ''}


def glossary_post(req):
    """action: set / del / import / retro"""
    if glossary is None:
        return {'ok': False, 'err': 'glossary 模块不可用'}
    act = (req.get('action') or 'set').strip()
    if act == 'set':
        ok, msg, changed = glossary.set_term(
            (req.get('jp') or '').strip(), (req.get('cn') or '').strip(),
            (req.get('note') or '').strip(), req.get('on', True) is not False)
        return {'ok': ok, 'err': msg, 'changed': changed,
                'items': glossary.items()}
    if act == 'del':
        ok, msg, _ = glossary.remove((req.get('jp') or '').strip())
        return {'ok': ok, 'err': msg, 'items': glossary.items()}
    if act == 'import':
        rows = req.get('rows') or []
        ok, msg, cnt = glossary.import_rows(
            rows, replace=bool(req.get('replace', True)))
        return {'ok': ok, 'err': msg, 'n': cnt, 'items': glossary.items()}
    if act == 'retro':
        # 把受影响的行的译文退回原文，下轮重翻 —— 改了术语必须做这一步
        jps = req.get('jps') or []
        if not jps:
            jps = [i['jp'] for i in glossary.items()]
        r = glossary.retro(jps, scope=(req.get('scope') or 'source'))
        log('术语回溯：%s' % r)
        return {'ok': r.get('ok', True), 'err': r.get('err', ''), 'retro': r}
    return {'ok': False, 'err': '未知动作：%s' % act}


def engine_get(path):
    """读当前调速配置 + 元信息；?engine=xxx 顺带给出该引擎的默认值。"""
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用（见面板日志）'}
    qeng = _q1(path, 'engine')
    try:
        cfg = sens_mt.run_cfg()
        meta = sens_mt.run_cfg_meta(qeng or None)
    except Exception as e:
        return {'ok': False, 'err': '引擎模块加载失败：%r' % e}
    try:
        live = {'engine_running': task_state(TASK_ENGINE) == 'running',
                'engine_age': engine_age(),
                'cache_age': engine_age()}
    except Exception:
        live = {}
    return {'ok': True, 'cfg': cfg, 'meta': meta, 'live': live}


def engine_post(req):
    """改调速配置。action: save（默认）/ preset / reset / restart。

    `restart: true` 或 action=save_and_restart 顺手重启引擎，立刻生效。
    """
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用（见面板日志）'}
    act = (req.get('action') or 'save').strip()
    if act == 'restart':
        return {'ok': True, 'msg': ctl_act('restart', 'engine'), 'cfg': None}
    eng = (req.get('engine') or '').strip()
    if act == 'reset':
        patch = sens_mt.run_cfg_defaults(eng or None)
    elif act == 'preset':
        key = (req.get('preset') or '').strip()
        hit = [p for p in sens_mt.run_cfg_presets() if p['key'] == key]
        if not hit:
            return {'ok': False, 'err': '没有这个预设：%s' % key}
        patch = dict(hit[0]['values'])
        if eng:
            patch['engine'] = eng
    else:
        patch = dict(req.get('cfg') or {})
    if not patch:
        return {'ok': False, 'err': '没有要改的字段'}
    ok, err, cfg = sens_mt.run_cfg_save(patch)
    out = {'ok': ok, 'err': err, 'cfg': cfg,
           'saved': {k: v for k, v in patch.items()}}
    if ok:
        log('调速配置：%s' % json.dumps(patch, ensure_ascii=False))
        if act == 'save_and_restart' or req.get('restart'):
            out['msg'] = ctl_act('restart', 'engine')
    return out


def manual_get(path):
    if manual is None:
        return {'ok': False, 'err': 'manual 模块不可用（见面板日志）'}
    if _q1(path, 'stat') == '1':
        return manual.stats()
    if _q1(path, 'recent') == '1':
        try:
            lim = int(_q1(path, 'limit', '60') or 60)
        except Exception:
            lim = 60
        return manual.ov_list(max(1, min(1000, lim)))
    try:
        lim = int(_q1(path, 'limit', '60') or 60)
    except Exception:
        lim = 60
    return manual.find(_q1(path, 'q'), limit=max(1, min(400, lim)),
                       scope=(_q1(path, 'scope', 'all') or 'all'))


def manual_post(req):
    if manual is None:
        return {'ok': False, 'err': 'manual 模块不可用（见面板日志）'}
    a = (req.get('action') or '').strip()
    if a == 'set':
        res = manual.set_line(req.get('file') or '', req.get('id') or '',
                              req.get('cn') or '')
    elif a == 'unset':
        res = manual.unset_line(req.get('file') or '', req.get('id') or '')
    elif a == 'tr':
        vs = req.get('vendors')
        if isinstance(vs, str):
            vs = [vs]
        if not vs:
            vs = [req.get('vendor')] if req.get('vendor') else ['google', 'tencent']
        res = manual.tr(req.get('text') or req.get('jp') or '', vs)
    else:
        res = {'ok': False, 'err': '未知动作：%s' % a}
    if a in ('set', 'unset'):
        log('手动工作台 %s：%s#%s -> %s%s'
            % (a, req.get('file'), req.get('id'), (req.get('cn') or '')[:24],
               '' if res.get('ok') else ('  失败：%s' % res.get('err'))))
    return res


# ---------------------------------------------------------------- 百度账号
def accounts_get(path):
    """两个池的账号清单：人工停用标记 + 实时状态（冷却/硬死）。"""
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用（见面板日志）'}
    try:
        return sens_mt.acc_list()
    except Exception as e:
        return {'ok': False, 'err': '账号模块加载失败：%r' % e}


def accounts_post(req):
    """动作：off / on / add / rm / test / allon（全部启用）。"""
    if sens_mt is None:
        return {'ok': False, 'err': 'sens_mt 模块不可用（见面板日志）'}
    a = (req.get('action') or '').strip()
    kind = (req.get('kind') or 'llm').strip()
    appid = (req.get('appid') or '').strip()
    try:
        if a == 'off':
            ok, msg = sens_mt.acc_disable(kind, appid, True)
        elif a == 'on':
            ok, msg = sens_mt.acc_disable(kind, appid, False)
        elif a == 'allon':
            ok, msg = True, ''
            for p in (sens_mt.acc_list().get('pools') or []):
                for i in p.get('items') or []:
                    if i.get('off'):
                        _ok2, _m2 = sens_mt.acc_disable(
                            p.get('kind'), i.get('appid'), False)
                        ok = ok and _ok2
                        msg += ('' if _ok2 else (' ' + str(_m2)))
            msg = msg.strip() or '已全部启用'
        elif a == 'add':
            ok, msg = sens_mt.acc_add(
                appid, (req.get('key') or '').strip(),
                (req.get('pool') or 'llm').strip(),
                (req.get('note') or '').strip())
        elif a == 'rm':
            ok, msg = sens_mt.acc_remove(appid,
                                         (req.get('pool') or '').strip() or None)
        elif a == 'test':
            res = sens_mt.acc_test(kind, appid)
            log('账号体检 %s %s -> %s' % (kind, appid[-4:], res.get('ok')))
            res['list'] = sens_mt.acc_list()
            return res
        else:
            return {'ok': False, 'err': '未知动作：%s' % a}
    except Exception as e:
        return {'ok': False, 'err': '账号操作失败：%r' % e}
    out = {'ok': ok, 'msg': msg}
    if ok:
        log('账号 %s：%s %s -> %s' % (a, kind, appid[-4:], msg))
        out['list'] = sens_mt.acc_list()
    return out


def live_payload(after):
    F = FEED
    now = time.time()
    with F['lock']:
        ln = [e for e in F['ln'] if e['s'] > after]
        rq = list(F['rq'])[-40:]
        ev = list(F['ev'])[-30:]
        acct = [(k, dict(v)) for k, v in F['acct'].items()]
        ph = [(k, dict(v)) for k, v in F['ph'].items()]
        err = F['err'].most_common(12)
        sens = list(F['sens'])[-40:]
        inflight = sum(1 for t0 in F['inflight'].values() if now - t0 < 90)
        seq, ok, bad, nln = F['seq'], F['ok'], F['bad'], F['n_ln']
        rps = _rate(F['rb'], 10)
        lps = _rate(F['lb'], 10)
        # 近 120 秒曲线，3 秒一点（40 点）
        series = []
        base = int(now) - 120
        for i in range(40):
            s0 = base + i * 3
            series.append([sum(v for s, v in F['lb'].items()
                              if s0 <= s < s0 + 3),
                           sum(v for s, v in F['rb'].items()
                               if s0 <= s < s0 + 3)])
    try:
        mtime = os.path.getmtime(FEEDF)
    except Exception:
        mtime = 0.0
    ssum = sens_summary()
    acct.sort(key=lambda x: -(x[1]['ok'] + x[1]['bad']))
    return {
        'ts': now, 'seq': seq, 'alive': (now - mtime) < 15,
        'feed_age': (now - mtime) if mtime else -1,
        'rps': round(rps, 2), 'lps': round(lps, 2), 'inflight': inflight,
        'ok': ok, 'bad': bad, 'n_ln': nln,
        'ln': ln[-120:], 'rq': rq, 'ev': ev, 'sens': sens,
        'sens_total': ssum['total'], 'sens_pend': ssum['pend'],
        'sens_mtc': ssum.get('mt', 0),
        'acct': [{'tag': k, 'pool': v.get('pool'), 'ok': v.get('ok', 0),
                  'bad': v.get('bad', 0), 'st': v.get('st', ''),
                  'err': v.get('err', ''), 'last': v.get('last', 0),
                  'until': v.get('until', 0)} for k, v in acct],
        'probe': [{'pool': k, 'acc': v} for k, v in ph],
        'err': err, 'series': series,
    }



def read_log():
    try:
        ls = [l.rstrip('\n') for l in io.open(RUNLOG, encoding='utf-8',
                                              errors='replace') if l.strip()]
    except Exception:
        return [], None, {}, []
    cur = None
    last_round_i = -1
    last_done_i = -1
    engine = {}
    last_speed = 0.0
    hist = []
    for i, l in enumerate(ls):
        m = re.search(r'第 (\d+) 轮：(\d+) 个文件 / 需翻 (\d+) 行 / 唯一 (\d+) 行', l)
        if m:
            cur = {'n': int(m.group(1)), 'files': int(m.group(2)),
                   'need': int(m.group(3)), 'uniq': int(m.group(4)),
                   'at': l[1:9]}
            last_round_i = i
        m = re.search(r'第 (\d+) 轮完成：译出 (\d+)/(\d+)，写回 (\d+) 文件'
                      r'\(校验通过 (\d+)\)，用时 ([\d.]+)s', l)
        if m:
            cur = None
            last_done_i = i
            try:
                sec = float(m.group(6))
                ok = int(m.group(2))
                last_speed = ok / max(sec, 0.1)
                hist.append({'n': int(m.group(1)), 'ok': ok,
                             'uniq': int(m.group(3)), 'files': int(m.group(4)),
                             'files_ok': int(m.group(5)), 'sec': round(sec, 1),
                             'speed': round(ok / max(sec, 0.1), 1)})
            except Exception:
                pass
        m = re.search(r'引擎统计 (\{.*\})', l)
        if m:
            try:
                engine = ast.literal_eval(m.group(1))
            except Exception:
                pass
    live = cur if last_round_i > last_done_i else None
    return ls, live, engine, ls[-12:], last_speed, hist[-12:]


def cache_count():
    try:
        mt = os.path.getmtime(CACHEF)
    except Exception:
        return -1, 0
    if mt == _cache_seen[0] and _cache_seen[1] >= 0:
        return _cache_seen[1], mt
    try:
        n = len(json.load(io.open(CACHEF, encoding='utf-8')))
    except Exception:
        n = -1
    _cache_seen[0] = mt
    _cache_seen[1] = n
    return n, mt


def do_sample():
    per = {}
    done = 0                # 剧情已译行
    done_o = 0              # 非剧情（物品/系统）已译行
    uniq = set()
    with_cn = 0
    with_cn_o = 0
    finished = 0
    finished_o = 0
    try:
        paths = glob.glob(CN + '/*.txt')
    except Exception:
        paths = []
    for p in paths:
        name = os.path.basename(p)
        ids = SRC_MAP.get(name)
        if ids is None:
            continue
        story = name in SRC_STORY
        if story:
            with_cn += 1
        else:
            with_cn_o += 1
        c = 0
        try:
            for l in io.open(p, encoding='utf-8', errors='replace'):
                if not l.strip():
                    continue
                r = l.rstrip('\n').split('\t')
                if len(r) < 2:
                    continue
                if r[0] in ids and r[1].strip() and not KANA.search(r[1]):
                    c += 1
                    uniq.add(name + '\x00' + r[0])
        except Exception:
            pass
        if story:
            if c:
                per[name] = c
            done += c
            if ids and c >= len(ids):
                finished += 1
        else:
            done_o += c
            if ids and c >= len(ids):
                finished_o += 1

    need = sum(len(v) for f, v in SRC_MAP.items() if f in SRC_STORY)
    need_o = sum(len(v) for f, v in SRC_MAP.items() if f not in SRC_STORY)
    files_todo = [f for f, v in SRC_MAP.items() if v and f in SRC_STORY]
    rows = []
    for f in files_todo:
        n = len(SRC_MAP[f])
        d = per.get(f, 0)
        rows.append([f, d, n, round(100.0 * d / n, 1) if n else 100.0])
    rows.sort(key=lambda r: (r[1] - r[2]))       # 剩余最多的排前面

    ls, live, engine, tail, last_speed, hist = read_log()
    cn, cmt = cache_count()
    now = time.time()
    SAMPLES.append((now, done))
    while SAMPLES and now - SAMPLES[0][0] > 360:
        SAMPLES.pop(0)
    spd = 0.0
    spd_src = ''
    live_spd = 0.0
    if len(SAMPLES) >= 3 and SAMPLES[-1][0] - SAMPLES[0][0] >= 20:
        dt = SAMPLES[-1][0] - SAMPLES[0][0]
        dd = SAMPLES[-1][1] - SAMPLES[0][1]
        if dd > 0:
            live_spd = dd / dt
    if last_speed > 0:
        spd, spd_src = last_speed, '上一轮实测'
    elif live_spd > 0:
        spd, spd_src = live_spd, '实时采样'

    # 本轮进度：用缓存条目增量估算（产物一轮才写回一次，轮中不会变）
    done_round = 0
    if live and cn >= 0:
        if RB['n'] != live['n']:
            RB['n'] = live['n']
            RB['cache'] = cn
        done_round = max(0, min(cn - RB['cache'], live['uniq']))

    with LOCK:
        ST.update({
            'ts': now, 'need': need, 'done': done, 'uniq_done': len(uniq),
            'files_total': len(files_todo), 'files_with_cn': with_cn,
            'files_finished': finished, 'per': rows[:60], 'cache': cn,
            'cache_mtime': cmt, 'speed': spd, 'speed_src': spd_src,
            'round': live, 'history': hist,
            'done_round': done_round, 'engine': engine, 'logtail': tail,
            'cache_age': (now - cmt) if cmt else -1, 'err': '',
            'need_other': need_o, 'done_other': done_o,
            'files_other': len([f for f, v in SRC_MAP.items()
                                if v and f not in SRC_STORY]),
            'files_other_done': finished_o, 'with_cn_other': with_cn_o,
        })


def sampler():
    build_src()
    while True:
        t0 = time.time()
        try:
            do_sample()
        except Exception as e:
            with LOCK:
                ST['err'] = repr(e)
            log('采样异常 %r' % e)
        time.sleep(max(1.0, SAMPLE_GAP - (time.time() - t0)))


# ----------------------------------------------------------------- 页面
# 主题变量抽出来共用（进度页 / 实时页同一套配色，改一处两页同步）
THEME = r'''
:root{
  --bg:#eef1f6;--panel:#ffffff;--panel2:#f7f9fc;--line:#dde3ec;
  --text:#141b27;--dim:#5f6b7c;--faint:#9aa5b4;
  --accent:#2f6fed;--ok:#0f9d58;--warn:#d98600;--bad:#d93a3a;--track:#e6ebf3;
  --shadow:0 1px 2px rgba(18,26,40,.06),0 6px 20px rgba(18,26,40,.06);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0d1016;--panel:#141a23;--panel2:#101620;--line:#232c39;
    --text:#e8eef6;--dim:#93a1b3;--faint:#657287;
    --accent:#5b9bff;--ok:#34c77b;--warn:#f0b429;--bad:#ff6b6b;--track:#1e2733;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 26px rgba(0,0,0,.35);
  }
}
'''

_PAGE_TPL = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>为谁炼金 · 汉化进度</title>
<style>
@@THEME@@
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  padding:22px}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;
  gap:16px;margin-bottom:18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),#7b5cff);color:#fff;
  font-size:20px;font-weight:700;box-shadow:var(--shadow)}
h1{margin:0;font-size:19px;letter-spacing:.4px}
.sub{color:var(--dim);font-size:12.5px;margin-top:2px}
.status{display:flex;align-items:center;gap:9px;padding:8px 15px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);
  font-weight:600;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--faint);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 20%,transparent);
  animation:pulse 1.8s infinite}
.dot.off{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 18%,transparent)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  box-shadow:var(--shadow)}
.hero{display:grid;grid-template-columns:220px 1fr;gap:18px;margin-bottom:16px}
@media(max-width:820px){.hero{grid-template-columns:1fr}}
.ringbox{display:grid;place-items:center;padding:16px;position:relative}
.ringbox svg{width:190px;height:190px;transform:rotate(-90deg)}
.ringbox circle{fill:none;stroke-width:13;stroke-linecap:round}
.tr{stroke:var(--track)}
.bar{stroke:url(#g);transition:stroke-dashoffset .8s cubic-bezier(.4,0,.2,1)}
.ringtxt{position:absolute;text-align:center}
.ringtxt b{display:block;font-size:34px;letter-spacing:-.5px}
.ringtxt span{color:var(--dim);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{padding:15px 16px}
.kpi .k{color:var(--dim);font-size:12px;letter-spacing:.3px}
.kpi .v{font-size:25px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
.kpi .u{font-size:12px;color:var(--faint);font-weight:400;margin-left:3px}
.kpi .d{font-size:11.5px;color:var(--faint);margin-top:4px}
.sec{padding:16px 18px;margin-bottom:16px}
.sec h2{margin:0 0 13px;font-size:13.5px;color:var(--dim);font-weight:600;
  letter-spacing:.6px;text-transform:uppercase}
.track{height:11px;border-radius:99px;background:var(--track);overflow:hidden}
.fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent),#7b5cff);
  width:0;transition:width .8s cubic-bezier(.4,0,.2,1)}
.rowhead{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:9px;gap:12px;flex-wrap:wrap}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:7px 11px;font-size:12.5px;display:flex;gap:7px;align-items:baseline}
.chip b{font-variant-numeric:tabular-nums;font-size:14px}
.chip span{color:var(--dim)}
.files{max-height:330px;overflow:auto;border-top:1px solid var(--line)}
.f{display:grid;grid-template-columns:1fr 132px 56px;gap:12px;align-items:center;
  padding:8px 2px;border-bottom:1px solid var(--line);font-size:12.5px}
.f:last-child{border-bottom:0}
.f .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim)}
.f .pc{text-align:right;color:var(--faint);font-variant-numeric:tabular-nums}
.f .tk{height:7px;border-radius:99px;background:var(--track);overflow:hidden}
.f .fl{height:100%;background:var(--accent);border-radius:99px}
.f.done .fl{background:var(--ok)}
pre.log{margin:0;font:11.5px/1.75 Consolas,"Cascadia Mono",monospace;color:var(--dim);
  white-space:pre-wrap;word-break:break-all;max-height:210px;overflow:auto}
.foot{color:var(--faint);font-size:11.5px;text-align:center;padding:6px 0 14px}
.btn{font:inherit;font-size:12.5px;padding:5px 13px;border-radius:999px;
  cursor:pointer;background:var(--panel);color:var(--text);
  border:1px solid var(--line)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.pri:hover{color:#fff;filter:brightness(1.06)}
.btn.danger:hover{border-color:var(--bad);color:var(--bad)}
.ctlbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ctlchip{display:flex;align-items:center;gap:7px;border:1px solid var(--line);
  border-radius:999px;padding:4px 11px;font-size:12px;background:var(--panel2)}
.ctlchip .dt{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.ctlchip.on .dt{background:var(--ok)}
.ctlchip.off .dt{background:var(--bad)}
.ctlchip b{font-weight:700}
.ctlmsg{font-size:12px;color:var(--dim)}
.ctlmsg.ok{color:var(--ok)}
.ctlmsg.bad{color:var(--bad)}
.ms{color:var(--faint);font-family:ui-monospace,Consolas,monospace}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">炼</div>
      <div>
        <h1>为谁炼金 · 剧情汉化进度</h1>
        <div class="sub" id="sub">正在读取…</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="status"><i class="dot" id="dot"></i><span id="stxt">检测中</span></div>
      <a href="/manual" style="color:var(--accent);text-decoration:none;font-weight:600;font-size:13px;padding:8px 14px;border-radius:999px;background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow)">✎ 手动工作台</a>
      <a href="/live" style="color:var(--accent);text-decoration:none;font-weight:600;font-size:13px;padding:8px 14px;border-radius:999px;background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow)">百度 API 实时窗口 →</a>
    </div>
  </header>

  <div class="card sec" style="margin-bottom:14px">
    <div class="rowhead"><h2 style="margin:0">翻译开关（计划任务运行，关掉本网页也照跑）</h2>
      <span class="ctlmsg" id="ctlmsg"></span></div>
    <div class="ctlbar">
      <button class="btn pri" id="ctlstart">▶ 开始翻译</button>
      <button class="btn danger" id="ctlstop">■ 停止翻译</button>
      <button class="btn" id="ctlrestart">↻ 重启翻译</button>
    </div>
    <div class="chips" id="ctlinfo" style="margin-top:10px"></div>
  </div>

  <div class="hero">
    <div class="card ringbox">
      <svg viewBox="0 0 160 160">
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#2f6fed"/><stop offset="100%" stop-color="#7b5cff"/>
        </linearGradient></defs>
        <circle class="tr" cx="80" cy="80" r="68"/>
        <circle class="bar" id="ring" cx="80" cy="80" r="68"/>
      </svg>
      <div class="ringtxt"><b id="pct">—</b><span id="pctsub">整体完成度</span></div>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="k">已译行</div>
        <div class="v" id="done">—</div><div class="d" id="doneuniq">—</div></div>
      <div class="card kpi"><div class="k">剩余行</div>
        <div class="v" id="left">—</div><div class="d" id="files">—</div></div>
      <div class="card kpi"><div class="k">当前速度</div>
        <div class="v" id="spd">—</div><div class="d" id="eta">—</div></div>
      <div class="card kpi"><div class="k">译文缓存</div>
        <div class="v" id="cache">—</div><div class="d" id="cacheage">—</div></div>
    </div>
  </div>

  <div class="card sec">
    <div class="rowhead">
      <h2 style="margin:0">本轮进度</h2>
      <div class="sub" id="roundinfo">—</div>
    </div>
    <div class="track"><div class="fill" id="rfill"></div></div>
  </div>

  <div class="card sec">
    <h2>引擎统计（最近一轮）</h2>
    <div class="chips" id="eng"><span class="chip"><span>暂无</span></b></span></div>
  </div>

  <div class="card sec" style="padding:12px 18px">
    <div class="sub" id="otherline">—</div>
  </div>

  <div class="card sec">
    <div class="rowhead"><h2 style="margin:0">分文件进度</h2>
      <div class="sub">按剩余量排序 · 前 60 个</div></div>
    <div class="files" id="files2"></div>
  </div>

  <div class="card sec">
    <div class="rowhead"><h2 style="margin:0">轮次历史</h2>
      <div class="sub">每轮端到端实测吞吐（缓存命中不耗 API，首轮速度会偏高）</div></div>
    <div class="chips" id="hist"><span class="chip"><span>暂无完整轮次</span></span></div>
  </div>

  <div class="card sec">
    <h2>运行器日志</h2>
    <pre class="log" id="logtail">—</pre>
  </div>
  <div class="foot">数据来源：chinese/ 产物与日文源逐行比对 · 活性取自 mt_cache.json 落盘时间 · 每 2 秒刷新</div>
</div>

<script>
var R = 2*Math.PI*68;
document.getElementById('ring').style.strokeDasharray = R;
document.getElementById('ring').style.strokeDashoffset = R;

function n(x){ return (x===null||x===undefined)?'—':x.toLocaleString('en-US'); }
function pad(x){ return x<10?('0'+x):(''+x); }

function render(d){
  var need=d.need||0, done=d.done||0, left=Math.max(need-done,0);
  var pct = need? (100*done/need) : 0;
  document.getElementById('pct').textContent = pct.toFixed(2)+'%';
  document.getElementById('ring').style.strokeDashoffset = R*(1-pct/100);
  document.getElementById('pctsub').textContent = '整体完成度';
  document.getElementById('done').textContent = n(done);
  document.getElementById('doneuniq').textContent = '唯一条目 '+n(d.uniq_done);
  document.getElementById('left').textContent = n(left);
  document.getElementById('files').textContent = '已完成文件 '+d.files_finished+'/'+d.files_total
      +'（产物 '+d.files_with_cn+'）';
  document.getElementById('spd').textContent = d.speed>0
      ? (d.speed.toFixed(1)+' 行/秒') : '测量中…';
  document.getElementById('eta').textContent = (d.speed>0 && left>0)
      ? ('预计还需 '+(left/d.speed/3600).toFixed(2)+' 小时'
         +(d.speed_src?('（'+d.speed_src+'）'):'')) : (left>0?'—':'已全部译完');
  document.getElementById('cache').textContent = n(d.cache);
  document.getElementById('cacheage').textContent = d.cache_age>=0
      ? ('落盘于 '+Math.round(d.cache_age)+' 秒前') : '—';

  var alive = d.cache_age>=0 && d.cache_age<600;
  var dot=document.getElementById('dot'), st=document.getElementById('stxt');
  dot.className = 'dot '+(alive?'on':'off');
  st.textContent = alive ? '翻译进行中' : '未检测到活动';
  document.getElementById('sub').textContent =
      '更新于 '+new Date(d.ts*1000).toLocaleTimeString('zh-CN');

  if(d.round){
    var r=d.round;
    var dr = d.done_round||0;
    document.getElementById('roundinfo').textContent =
      '第 '+r.n+' 轮 · '+(r.at||'')+' 开始 · 目标 '+n(r.uniq)+' 条唯一行 · 已译约 '+n(dr)+' 条';
    var frac = r.uniq? Math.min(1, dr/r.uniq) : 0;
    document.getElementById('rfill').style.width = (frac*100).toFixed(1)+'%';
  } else {
    document.getElementById('roundinfo').textContent = '轮次间隙（写回中或已完成）';
    document.getElementById('rfill').style.width = '100%';
  }

  var eng=document.getElementById('eng'); eng.innerHTML='';
  var keys=Object.keys(d.engine||{});
  if(!keys.length){ eng.innerHTML='<span class="chip"><span>暂无</span></span>'; }
  keys.forEach(function(k){
    var s=document.createElement('span'); s.className='chip';
    s.innerHTML='<span>'+k+'</span><b>'+d.engine[k]+'</b>'; eng.appendChild(s);
  });

  var box=document.getElementById('files2'); box.innerHTML='';
  (d.per||[]).forEach(function(row){
    var f=document.createElement('div'); f.className='f'+(row[1]>=row[2]?' done':'');
    f.innerHTML='<div class="nm">'+row[0]+'</div>'
      +'<div class="tk"><div class="fl" style="width:'+row[3]+'%"></div></div>'
      +'<div class="pc">'+row[1]+'/'+row[2]+'</div>';
    box.appendChild(f);
  });
  if(!(d.per||[]).length) box.innerHTML='<div class="f"><div class="nm">暂无文件</div></div>';

  document.getElementById('logtail').textContent = (d.logtail||[]).join('\n');

  var hb=document.getElementById('hist'); hb.innerHTML='';
  var hs=(d.history||[]).slice().reverse();
  if(!hs.length){ hb.innerHTML='<span class="chip"><span>暂无完整轮次</span></span>'; }
  hs.forEach(function(h){
    var s=document.createElement('span'); s.className='chip';
    s.innerHTML='<span>第 '+h.n+' 轮</span><b>'+h.speed+' 行/秒</b>'
      +'<span>译出 '+h.ok+' · 用时 '+h.sec+'s</span>';
    hb.appendChild(s);
  });

  var o=document.getElementById('otherline');
  var pcto = d.need_other? (100*d.done_other/d.need_other) : 0;
  o.textContent = '范围外文本（物品 / 系统 / 其他，不属于本任务）：已译 '
    + n(d.done_other)+' / '+n(d.need_other)+' 行（'+pcto.toFixed(1)+'%）· 文件 '
    + d.files_other_done+'/'+d.files_other+' 已完成';
}

function tick(){
  fetch('/api/progress',{cache:'no-store'}).then(function(r){return r.json();})
    .then(render).catch(function(e){
      var dot=document.getElementById('dot');
      dot.className='dot off';
      document.getElementById('stxt').textContent='面板连接中断';
    });
}
/* ---------------- 翻译开关（计划任务级） ---------------- */
function ctlChip(cls, label, val, extra){
  var s=document.createElement('span'); s.className='ctlchip '+cls;
  s.innerHTML='<span class="dt"></span><span>'+label+'</span><b>'+val+'</b>'
    +(extra?('<span class="ms">'+extra+'</span>'):'');
  return s;
}
function ctlPaint(d){
  var box=document.getElementById('ctlinfo'); if(!box) return;
  box.innerHTML='';
  var st={running:'运行中',ready:'已停止',gone:'任务缺失'}[d.engine_task]||d.engine_task;
  box.appendChild(ctlChip(d.engine_alive?'on':'off','翻译引擎',st,
    d.cache_age>=0?('缓存 '+d.cache_age+' 秒前落盘'):'无缓存'));
  box.appendChild(ctlChip(d.panel_task==='running'?'on':'off','本面板',
    d.panel_task==='running'?'运行中':(d.panel_task||'?'), '任务 '+d.panel));
  if(d.next_run) box.appendChild(ctlChip('','下次运行', d.next_run, ''));
}
function ctlLoad(){
  fetch('/api/ctl',{cache:'no-store'}).then(function(r){return r.json();})
    .then(ctlPaint).catch(function(){});
}
function ctlAct(what,target,btn,ask){
  if(ask && !window.confirm(ask)) return;
  var m=document.getElementById('ctlmsg');
  var old=btn.textContent; btn.disabled=true; btn.textContent='执行中…';
  m.className='ctlmsg'; m.textContent='正在执行…';
  fetch('/api/ctl',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({what:what,target:target})})
    .then(function(r){return r.json();})
    .then(function(res){
      btn.disabled=false; btn.textContent=old;
      m.className='ctlmsg '+(res.ok?'ok':'bad');
      m.textContent=(res.ok?'':'失败：')+(res.msg||res.err||'');
      setTimeout(ctlLoad,1500);
    })
    .catch(function(){ btn.disabled=false; btn.textContent=old;
      m.className='ctlmsg bad'; m.textContent='请求失败（面板未响应）'; });
}
document.getElementById('ctlstart').addEventListener('click',function(){ ctlAct('start','engine',this); });
document.getElementById('ctlstop').addEventListener('click',function(){ ctlAct('stop','engine',this); });
document.getElementById('ctlrestart').addEventListener('click',function(){
  ctlAct('restart','engine',this,'重启会打断当前这一轮（已译完的部分已进缓存，不会白干）。继续？'); });
ctlLoad(); setInterval(function(){ if(!document.hidden) ctlLoad(); }, 10000);

tick(); setInterval(tick,2000);
</script>
</body>
</html>
'''


PAGE = _PAGE_TPL.replace('@@THEME@@', THEME)

# 实时页：看的是「翻译动作」本身——每条百度请求的账号/耗时/成败，
# 以及每条成稿译文的原文→译文。数据全部来自引擎写的 _api_feed.jsonl。
LIVE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>为谁炼金 · 百度 API 实时窗口</title>
<style>
@@THEME@@
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  padding:20px}
.wrap{max-width:1420px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;
  margin-bottom:14px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),#7b5cff);color:#fff;
  font-size:20px;font-weight:700;box-shadow:var(--shadow)}
h1{margin:0;font-size:19px;letter-spacing:.4px}
.sub{color:var(--dim);font-size:12.5px;margin-top:2px}
.hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.status{display:flex;align-items:center;gap:9px;padding:8px 15px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);
  font-weight:600;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--faint);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 20%,transparent);
  animation:pulse 1.8s infinite}
.dot.off{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 18%,transparent)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
a.lnk{color:var(--accent);text-decoration:none;font-weight:600;font-size:13px;
  padding:8px 14px;border-radius:999px;background:var(--panel);
  border:1px solid var(--line);box-shadow:var(--shadow)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:10px;margin-bottom:14px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:11px 14px;box-shadow:var(--shadow)}
.kpi .k{color:var(--dim);font-size:12px}
.kpi .v{font-size:20px;font-weight:700;margin-top:3px;letter-spacing:.3px}
.grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:14px}
@media (max-width:1080px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:14px 16px;box-shadow:var(--shadow);margin-bottom:14px}
h2{font-size:14px;margin:0 0 10px}
.rowhead{display:flex;align-items:center;justify-content:space-between;gap:12px;
  margin-bottom:10px;flex-wrap:wrap}
.rowhead h2{margin:0}
.tools{display:flex;align-items:center;gap:12px;font-size:12.5px;color:var(--dim)}
button{font:inherit;font-size:12.5px;padding:5px 13px;border-radius:999px;cursor:pointer;
  background:var(--panel2);color:var(--text);border:1px solid var(--line)}
button:hover{border-color:var(--accent);color:var(--accent)}
label{cursor:pointer;user-select:none}
.stream{max-height:72vh;overflow:auto}
.row{display:grid;grid-template-columns:62px minmax(0,1fr);gap:10px;
  padding:7px 0;border-bottom:1px dashed var(--line);animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.row:last-child{border-bottom:0}
.row .tm{color:var(--faint);font-size:11.5px;padding-top:2px}
.jp{color:var(--dim);font-size:12.5px;word-break:break-word}
.cn{font-size:14px;font-weight:600;word-break:break-word;margin-top:1px}
.seg{display:inline-block;font-size:10.5px;color:var(--faint);border:1px solid var(--line);
  border-radius:6px;padding:0 5px;margin-left:6px;vertical-align:1px}
.reqs{max-height:300px;overflow:auto;font-size:12.5px}
.rq{display:grid;grid-template-columns:60px 52px 46px 1fr 58px;gap:8px;align-items:center;
  padding:5px 0;border-bottom:1px dashed var(--line)}
.rq:last-child{border-bottom:0}
.rq .a{font-weight:700}
.rq .d{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rq .ms{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.rq.bad .d{color:var(--bad)}
.rq .ok{color:var(--ok);font-weight:700}
.rq .no{color:var(--bad);font-weight:700}
.accts{display:flex;flex-wrap:wrap;gap:7px}
.ac{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;
  background:var(--panel2);border:1px solid var(--line);font-size:12.5px}
.ac .d{width:8px;height:8px;border-radius:50%;background:var(--faint)}
.ac .d.ok{background:var(--ok)}.ac .d.err{background:var(--bad)}
.ac .d.dead{background:var(--bad);animation:pulse 1.4s infinite}
.ac .d.cool{background:var(--warn)}
.ac b{font-variant-numeric:tabular-nums}
.ac small{color:var(--faint)}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{display:inline-flex;gap:7px;align-items:center;padding:4px 11px;border-radius:999px;
  background:var(--panel2);border:1px solid var(--line);font-size:12.5px}
.chip b{color:var(--bad)}
.chip.z b{color:var(--ok)}
svg{width:100%;height:96px;display:block}
.legend{display:flex;gap:16px;color:var(--dim);font-size:11.5px;margin-top:6px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}
.foot{color:var(--faint);font-size:12px;text-align:center;margin-top:6px}
/* —— 敏感行人工翻译区 —— */
.senscard .chips{margin-bottom:10px}
.senscard .tools{max-width:62%;line-height:1.5;text-align:right}
.sensbox{display:flex;flex-direction:column;gap:9px;max-height:66vh;overflow:auto;
  padding-right:4px}
.sensrow{border:1px solid var(--line);border-radius:10px;padding:9px 11px;
  background:var(--panel2)}
.sensrow.done{border-color:var(--ok)}
.sensrow .sh{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font-size:11.5px;color:var(--dim);margin-bottom:6px}
.sensrow .idx{font-family:ui-monospace,Consolas,monospace;color:var(--faint)}
.sensrow .loc{font-family:ui-monospace,Consolas,monospace;font-size:11px;
  color:var(--faint);max-width:46%;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.sensrow .tag{margin-left:auto;border:1px solid var(--line);border-radius:999px;
  padding:1px 8px;font-size:11px}
.sensrow .tag.pend{color:var(--warn);border-color:var(--warn)}
.sensrow .tag.ok{color:var(--ok);border-color:var(--ok)}
.sensrow .tag.unk{color:var(--dim);border-color:var(--dim)}
.sensrow .tag.mt{color:var(--warn);border-color:var(--warn)}
.sensbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.sensbar .mtmsg{font-size:12px;color:var(--dim)}
.sensrow .mtbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-size:12px;color:var(--dim);border-left:2px solid var(--warn);
  padding-left:9px;margin-bottom:8px}
.sensrow .mtbar .mtk{color:var(--warn);font-weight:700}
.sensrow .mtbar .mtn{color:var(--bad)}
.sensrow .sjp{font-size:13.5px;line-height:1.65;word-break:break-word;margin-bottom:7px}
mark.sens{background:rgba(217,58,58,.13);color:var(--bad);font-weight:700;
  border-bottom:1px dashed var(--bad);border-radius:3px;padding:0 2px}
.sensrow textarea{width:100%;resize:vertical;min-height:30px;font:inherit;
  font-size:13px;line-height:1.5;padding:6px 9px;border-radius:8px;
  border:1px solid var(--line);background:var(--panel);color:var(--text)}
.sensrow textarea:focus{outline:none;border-color:var(--accent)}
.sensrow .brhint{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-size:11.5px;color:var(--warn);background:rgba(217,134,0,.08);
  border:1px dashed var(--warn);border-radius:8px;padding:4px 8px;margin-bottom:6px}
.sensrow .brhint code{font-family:ui-monospace,Consolas,monospace}
.btn.mini{font-size:11px;padding:1px 8px}
.sact{display:flex;align-items:center;gap:8px;margin-top:7px;flex-wrap:wrap}
.btn{font:inherit;font-size:12.5px;padding:4px 12px;border-radius:999px;
  cursor:pointer;background:var(--panel);color:var(--text);border:1px solid var(--line)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.undo{color:var(--dim)}
.btn.undo:hover{border-color:var(--bad);color:var(--bad)}
.smsg{font-size:11.5px;color:var(--dim)}
.smsg.ok{color:var(--ok)}
.smsg.bad{color:var(--bad)}
.ms{color:var(--faint);font-family:ui-monospace,Consolas,monospace}
.mtsel{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim)}
.mtsel select{font:inherit;font-size:12px;padding:3px 8px;border-radius:8px;
  border:1px solid var(--line);background:var(--panel);color:var(--text);
  cursor:pointer}
.mtsel select:focus{outline:none;border-color:var(--accent)}
.pv{display:flex;align-items:center;gap:7px;font-size:12px;
  border:1px solid var(--line);border-radius:999px;padding:3px 11px;
  background:var(--panel2)}
.pv b{font-weight:700}
.pv.ok{border-color:var(--ok)}
.pv.ok b{color:var(--ok)}
.pv.bad{border-color:var(--bad)}
.pv.bad b{color:var(--bad)}
.ctlbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.pri:hover{color:#fff;filter:brightness(1.06)}
.btn.danger:hover{border-color:var(--bad);color:var(--bad)}
.ctlmsg{font-size:12px;color:var(--dim)}
.ctlmsg.ok{color:var(--ok)}
.ctlmsg.bad{color:var(--bad)}
.ctlchip{display:flex;align-items:center;gap:7px;border:1px solid var(--line);
  border-radius:999px;padding:4px 11px;font-size:12px;background:var(--panel2)}
.ctlchip .dt{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.ctlchip.on .dt{background:var(--ok)}
.ctlchip.off .dt{background:var(--bad)}
.ctlchip b{font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">译</div>
      <div><h1>为谁炼金 · 百度 API 实时窗口</h1>
        <div class="sub" id="sub">连接中…</div></div>
    </div>
    <div class="hd">
      <div class="status"><span class="dot" id="dot"></span><span id="stxt">连接中</span></div>
      <a class="lnk" href="/manual">✎ 手动工作台</a>
      <a class="lnk" href="/">← 进度总览</a>
    </div>
  </header>

  <div class="card">
    <div class="rowhead">
      <h2>翻译开关（计划任务运行，关掉本网页也照跑）</h2>
      <div class="tools"><span class="ctlmsg" id="ctlmsg"></span></div>
    </div>
    <div class="ctlbar">
      <button class="btn pri" id="ctlstart">▶ 开始翻译</button>
      <button class="btn danger" id="ctlstop">■ 停止翻译</button>
      <button class="btn" id="ctlrestart">↻ 重启翻译</button>
      <span style="width:8px"></span>
      <button class="btn" id="ctlprun">▶ 启动面板</button>
      <button class="btn" id="ctlprestart">↻ 重启面板</button>
    </div>
    <div class="chips" id="ctlinfo" style="margin-top:10px"></div>
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="grid">
    <div class="card">
      <div class="rowhead">
        <h2>实时译文流（新译在上）</h2>
        <div class="tools">
          <button id="pause">暂停滚动</button>
          <label><input type="checkbox" id="onlyerr"> 只看请求失败</label>
        </div>
      </div>
      <div class="stream" id="stream"></div>
    </div>

    <div>
      <div class="card">
        <h2>速率（近 120 秒 · 3 秒一格）</h2>
        <svg id="chart" viewBox="0 0 320 96" preserveAspectRatio="none" aria-label="速率曲线"></svg>
        <div class="legend">
          <span><i style="background:var(--accent)"></i>翻译请求 / 3 秒</span>
          <span><i style="background:var(--ok)"></i>成稿译文 / 3 秒</span>
        </div>
      </div>
      <div class="card">
        <h2>请求流水</h2>
        <div class="reqs" id="reqs"></div>
      </div>
      <div class="card">
        <h2>账号（按用量）</h2>
        <div class="accts" id="accts"></div>
      </div>
      <div class="card">
        <h2>错误归类</h2>
        <div class="chips" id="errs"></div>
      </div>
    </div>
  </div>

  <div class="card senscard">
    <div class="rowhead">
      <h2>敏感行隔离 · 直接翻译（百度内容审核按字面词表拒收，与账号无关）</h2>
      <div class="tools" id="senshint">加载中…</div>
    </div>
    <div class="chips" id="sens"></div>
    <div class="sensbar">
      <button class="btn mini" id="mtall">一键机翻全部待审</button>
      <label class="mtsel">机翻渠道
        <select id="mtvendor">
          <option value="auto">自动（谷歌 → 腾讯 → 必应）</option>
          <option value="google">谷歌</option>
          <option value="tencent">腾讯</option>
          <option value="bing">必应</option>
        </select>
      </label>
      <button class="btn mini" id="mtprobe">测通道</button>
      <span class="mtmsg" id="mtmsg"></span>
    </div>
    <div class="chips" id="mtpv" style="margin-bottom:10px"></div>
    <div class="sensbox" id="sensedit"></div>
  </div>
  <div class="foot">数据来源：引擎逐请求/逐条写入的 _api_feed.jsonl（增量读取）· 每 1 秒刷新 · 敏感行清单 sensitive.json</div>
</div>

<script>
var lastSeq = 0, paused = false, rows = 0, MAXROWS = 300, reqCache = [];

function n(x){ return (x===null||x===undefined)?'—':x.toLocaleString('en-US'); }
function pad(x){ return x<10?('0'+x):(''+x); }
function hhmmss(t){
  var d = new Date(t*1000);
  return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
}
function esc(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function kpi(k,v){
  var d=document.createElement('div'); d.className='kpi';
  d.innerHTML='<div class="k">'+k+'</div><div class="v">'+v+'</div>';
  return d;
}

function addRow(e){
  var box=document.getElementById('stream');
  var d=document.createElement('div'); d.className='row';
  d.innerHTML='<div class="tm">'+hhmmss(e.t)+'</div><div>'
    +'<div class="jp">'+esc(e.jp)+'</div>'
    +'<div class="cn">'+esc(e.cn)
    +(e.via==='seg'?'<span class="seg">片段级</span>':'')+'</div></div>';
  box.insertBefore(d, box.firstChild);
  rows++;
  while(rows>MAXROWS && box.lastChild){ box.removeChild(box.lastChild); rows--; }
}

function renderChart(series){
  var W=320,H=96,padb=6;
  var mx=1, i;
  for(i=0;i<series.length;i++){ mx=Math.max(mx, series[i][0], series[i][1]); }
  function path(idx){
    var pts=[];
    for(var j=0;j<series.length;j++){
      var x=series.length>1? j*(W/(series.length-1)) : 0;
      var y=H-padb-(series[j][idx]/mx)*(H-padb*2);
      pts.push(x.toFixed(1)+','+y.toFixed(1));
    }
    return pts.join(' ');
  }
  var g='<line x1="0" y1="'+(H-padb)+'" x2="'+W+'" y2="'+(H-padb)+'" stroke="var(--line)" stroke-width="1"/>';
  g+='<polyline fill="none" stroke="var(--accent)" stroke-width="1.6" points="'+path(1)+'"/>';
  g+='<polyline fill="none" stroke="var(--ok)" stroke-width="1.6" points="'+path(0)+'"/>';
  g+='<text x="2" y="11" fill="var(--faint)" font-size="9">峰值 '+mx+' / 3 秒</text>';
  document.getElementById('chart').innerHTML=g;
}

function renderReqs(list){
  var onlyerr=document.getElementById('onlyerr').checked;
  var box=document.getElementById('reqs'); box.innerHTML='';
  var arr=list.slice().reverse();
  var shown=0;
  arr.forEach(function(e){
    if(onlyerr && e.ok) return;
    shown++;
    var d=document.createElement('div');
    d.className='rq'+(e.ok?'':' bad');
    d.innerHTML='<span>'+hhmmss(e.t)+'</span>'
      +'<span class="a">'+(e.pool==='std'?'S:':'L:')+esc(e.acc)+'</span>'
      +'<span class="'+(e.ok?'ok':'no')+'">'+(e.ok?('×'+e.out):'失败')+'</span>'
      +'<span class="d">'+esc(e.ok?'':'('+e.err+')')+'</span>'
      +'<span class="ms">'+(e.ms>=1000?(e.ms/1000).toFixed(1)+'s':e.ms+'ms')+'</span>';
    box.appendChild(d);
  });
  if(!shown) box.innerHTML='<div class="rq"><span></span><span class="d">暂无记录</span></div>';
}

function renderAccts(list){
  var now=Date.now()/1000;
  var box=document.getElementById('accts'); box.innerHTML='';
  if(!list.length){ box.innerHTML='<span class="ac">暂无请求</span>'; return; }
  list.forEach(function(a){
    var st=a.st||'';
    var cls='';
    if(st==='dead') cls='dead';
    else if(st==='err') cls='err';
    else if(st==='cool') cls='cool';
    else if(st==='ok') cls='ok';
    var extra='';
    if(a.until>now) extra='<small>停用 '+Math.round(a.until-now)+'s</small>';
    else if(st==='err') extra='<small>'+esc((a.err||'').slice(0,18))+'</small>';
    var d=document.createElement('span'); d.className='ac';
    d.innerHTML='<span class="d '+cls+'"></span>'
      +'<span>'+(a.pool==='std'?'标准 ':'LLM ')+esc(a.tag)+'</span>'
      +'<b>'+a.ok+'/'+(a.ok+a.bad)+'</b>'+extra;
    box.appendChild(d);
  });
}

function renderErr(list){
  var box=document.getElementById('errs'); box.innerHTML='';
  if(!list.length){ box.innerHTML='<span class="chip z"><span>无错误</span><b>0</b></span>'; return; }
  list.forEach(function(x){
    var s=document.createElement('span'); s.className='chip';
    s.innerHTML='<span>'+esc(x[0])+'</span><b>'+x[1]+'</b>';
    box.appendChild(s);
  });
}

/* ---------------- 敏感行：界面直接翻译 ----------------
   百度 20003 是内容级审核（字面词表命中即整条拒收，换账号无解），
   所以这里把隔离出来的行做成可编辑清单：原文高亮标出触发词，下面直接填中文，
   保存即写回 chinese/ 产物，引擎下轮自动采用。 */
var SENS = {sig:'', pending:null};

function getDraft(){
  try { return JSON.parse(localStorage.getItem('sens_draft')||'{}'); }
  catch(e){ return {}; }
}
function setDraft(o){
  try { localStorage.setItem('sens_draft', JSON.stringify(o)); } catch(e){}
}
function hlJp(jp, hits){
  if(!hits || !hits.length) return esc(jp);
  var out='', cur=0;
  hits.forEach(function(h){
    if(h.i<cur) return;
    out += esc(jp.slice(cur,h.i))
      + '<mark class="sens" title="命中百度审核词表：'+esc(h.w)+'">'
      + esc(jp.slice(h.i,h.i+h.n)) + '</mark>';
    cur = h.i+h.n;
  });
  return out + esc(jp.slice(cur));
}
function sensChips(d){
  var box=document.getElementById('sens'); box.innerHTML='';
  function chip(t,v,z){
    var s=document.createElement('span'); s.className='chip'+(z?' z':'');
    s.innerHTML='<span>'+t+'</span><b>'+v+'</b>'; box.appendChild(s);
  }
  chip('累计隔离', d.total, true);
  if((d.rev||0)>0) chip('已人工确认', d.rev, false);
  if((d.mtc||0)>0) chip('机翻待审', d.mtc, false);
  if(((d.pend||0)-(d.mtc||0))>0) chip('连草稿也没有', d.pend-d.mtc, false);
  (d.wordc||[]).forEach(function(x){
    chip('疑似触发词 '+esc(x[0]), x[1], false);
  });
  var h=document.getElementById('senshint');
  if(h) h.innerHTML=
    '百度对整条按字面词表拒收（换账号无解）· 红色即命中的词<br>'
    + '隔离行已自动用 <b>谷歌</b> 初翻（机翻草稿，卡片里标「机翻待审」）'
    + '并已写进 chinese/ 产物<br>'
    + '人工只需<b>校对润色</b> → 点「保存并写回」即成为正式译文（人工译文永远盖住机翻）';
}
function saveOne(jp, cn, msg, row, unflag){
  if(!unflag){
    var a=(jp.match(/<br\s*\/?>/gi)||[]).length;
    var b=((cn||'').match(/<br\s*\/?>/gi)||[]).length;
    if(a!==b && !window.confirm('原文有 '+a+' 个 <br>，译文有 '+b+' 个。\n'
        + '个数不一致会让成品缺换行或多换行。仍要保存吗？')) return;
  }
  msg.textContent='保存中…'; msg.className='smsg';
  fetch('/api/sens',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({jp:jp,cn:cn,unflag:!!unflag})})
    .then(function(r){return r.json();})
    .then(function(res){
      if(!res.ok){ msg.textContent='失败：'+(res.err||'未知错误');
        msg.className='smsg bad'; return; }
      if(unflag){
        msg.textContent='已撤销隔离，下轮会重新送百度';
        msg.className='smsg ok'; row.remove();
      } else {
        var d=getDraft(); delete d[jp]; setDraft(d);
        msg.textContent = res.lines
          ? ('已写回 '+res.lines+' 个文件：'+res.files.join(', '))
          : '已保存（产物里暂无对应行，引擎下轮自动采用）';
        msg.className='smsg ok'; row.classList.add('done');
      }
      loadSens(true, true);
    })
    .catch(function(){ msg.textContent='请求失败（面板未响应）';
      msg.className='smsg bad'; });
}
var MT_VENDORS=[['auto','自动'],['google','谷歌'],['tencent','腾讯'],
                ['bing','必应']];
function vendorLabel(v){
  for(var i=0;i<MT_VENDORS.length;i++){ if(MT_VENDORS[i][0]===v) return MT_VENDORS[i][1]; }
  return v||'自动';
}
function curVendor(){
  try{ var v=localStorage.getItem('mtvendor'); return v||'auto'; }catch(e){ return 'auto'; }
}
function setVendor(v){
  try{ localStorage.setItem('mtvendor', v); }catch(e){}
  var sl=document.getElementById('mtvendor'); if(sl) sl.value=v;
  /* 每行的按钮实时显示将走哪条通道，免得点完才发现翻错了渠道 */
  Array.prototype.forEach.call(document.querySelectorAll('.mtre'), function(b){
    var t=b.textContent;
    if(t.indexOf('重新机翻')===0) b.textContent='重新机翻 · '+vendorLabel(v);
    else if(t.indexOf('机翻这条')===0) b.textContent='机翻这条 · '+vendorLabel(v);
  });
}
function probeChannels(force){
  var box=document.getElementById('mtpv');
  var btn=document.getElementById('mtprobe'), old;
  if(btn){ old=btn.textContent; btn.disabled=true; btn.textContent='测通道…'; }
  box.innerHTML='<span class="pv"><b>检测中…</b><span>（约 3 秒）</span></span>';
  fetch('/api/mtprobe'+(force?'?force=1':''),{cache:'no-store'})
    .then(function(r){return r.json();})
    .then(function(d){
      if(btn){ btn.disabled=false; btn.textContent=old; }
      if(!d.ok){ box.innerHTML='<span class="pv bad"><b>体检失败</b></span>'; return; }
      box.innerHTML='<span class="ms">实测：</span>';
      (d.items||[]).forEach(function(x){
        var el=document.createElement('span');
        el.className='pv '+(x.ok?'ok':'bad');
        el.title=x.ok?(x.out||''):(x.err||'');
        el.innerHTML='<b>'+esc(x.label)+'</b><span class="ms">'+x.ms+'ms</span>'
          +'<span>'+(x.ok?esc((x.out||'').slice(0,26))
                         :esc((x.err||'不可用').slice(0,30)))+'</span>';
        box.appendChild(el);
      });
    })
    .catch(function(){
      if(btn){ btn.disabled=false; btn.textContent=old; }
      box.innerHTML='<span class="pv bad"><b>请求失败</b></span>';
    });
}
/* ---------------- 翻译开关（计划任务级，与网页在不在无关） ---------------- */
function ctlChip(cls, label, val, extra){
  var s=document.createElement('span'); s.className='ctlchip '+cls;
  s.innerHTML='<span class="dt"></span><span>'+label+'</span><b>'+val+'</b>'
    +(extra?('<span class="ms">'+extra+'</span>'):'');
  return s;
}
function ctlPaint(d){
  var box=document.getElementById('ctlinfo'); if(!box) return;
  box.innerHTML='';
  var st={running:'运行中',ready:'已停止',gone:'任务缺失'}[d.engine_task]||d.engine_task;
  box.appendChild(ctlChip(d.engine_alive?'on':'off','翻译引擎',st,
    d.cache_age>=0?('缓存 '+d.cache_age+' 秒前落盘'):'无缓存'));
  box.appendChild(ctlChip(d.panel_task==='running'?'on':'off','本面板',
    d.panel_task==='running'?'运行中':(d.panel_task||'?'), '任务 '+d.panel));
  if(d.next_run) box.appendChild(ctlChip('','下次运行', d.next_run, ''));
}
function ctlLoad(){
  fetch('/api/ctl',{cache:'no-store'}).then(function(r){return r.json();})
    .then(ctlPaint).catch(function(){});
}
function ctlAct(what, target, btn, ask){
  if(ask && !window.confirm(ask)) return;
  var m=document.getElementById('ctlmsg');
  var old=btn.textContent; btn.disabled=true; btn.textContent='执行中…';
  m.className='ctlmsg'; m.textContent='正在执行…';
  fetch('/api/ctl',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({what:what,target:target})})
    .then(function(r){return r.json();})
    .then(function(res){
      btn.disabled=false; btn.textContent=old;
      m.className='ctlmsg '+(res.ok?'ok':'bad');
      m.textContent=(res.ok?'':'失败：')+(res.msg||res.err||'');
      setTimeout(ctlLoad, 1500);
    })
    .catch(function(){ btn.disabled=false; btn.textContent=old;
      m.className='ctlmsg bad'; m.textContent='请求失败（面板正在重启？）'; });
}
function mtOne(jp, btn){
  var old=btn.textContent; btn.disabled=true; btn.textContent='机翻中…';
  var v=curVendor();
  fetch('/api/sens',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'mt',jp:jp,force:true,vendor:v})})
    .then(function(r){return r.json();})
    .then(function(res){
      btn.disabled=false; btn.textContent=old;
      if(!res.ok){ alert('机翻失败：'+(res.err||'未知错误')); return; }
      if(!res.done) alert('这条没翻出来：'+(res.err||'通道返回空'));
      loadSens(true,true);
    })
    .catch(function(){ btn.disabled=false; btn.textContent=old;
      alert('请求失败（面板未响应）'); });
}
function mtAll(btn){
  var old=btn.textContent, m=document.getElementById('mtmsg');
  var v=curVendor();
  btn.disabled=true; btn.textContent='机翻中（每条约 2 秒）…';
  m.textContent='正在调用'+(v==='auto'?'谷歌/腾讯':vendorLabel(v))+'…';
  fetch('/api/sens',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'mt',vendor:v})})
    .then(function(r){return r.json();})
    .then(function(res){
      btn.disabled=false; btn.textContent=old;
      m.textContent = res.ok
        ? ('本次机翻 '+res.done+' 条 / 待翻 '+res.todo+' 条，写回 '
           +res.files+' 处'+(res.fail?('，失败 '+res.fail):''))
        : ('失败：'+(res.err||'未知错误'));
      loadSens(true,true);
    })
    .catch(function(){ btn.disabled=false; btn.textContent=old;
      m.textContent='请求失败（面板未响应）'; });
}
function renderSensEditor(d){
  var box=document.getElementById('sensedit');
  var draft=getDraft();
  Array.prototype.forEach.call(box.querySelectorAll('textarea[data-jp]'),
    function(t){ draft[t.getAttribute('data-jp')]=t.value; });
  setDraft(draft);
  box.innerHTML='';
  var items=d.items||[];
  if(!items.length){
    box.innerHTML='<div class="rq"><span></span>'
      +'<span class="d">暂无（百度审核未拒收任何行）</span></div>';
    return;
  }
  items.forEach(function(it, ix){
    var row=document.createElement('div');
    row.className='sensrow'+(it.cn?' done':'');
    var head=document.createElement('div'); head.className='sh';
    head.innerHTML='<span class="idx">#'+(ix+1)+'</span>'
      + '<span>被拒 '+it.n+' 次</span>'
      + (it.where?('<span class="loc" title="'+esc(it.where)+'">'+esc(it.where)
          + (it.loc_kind==='frag' ? ' · 片段' : '') + '</span>'):'')
      + (it.hits && it.hits.length ? ''
          : '<span class="tag unk">整句触发 · 词表未收录</span>')
      + (it.cn ? '<span class="tag ok">已人工确认</span>'
        : (it.mt ? '<span class="tag mt">机翻待审</span>'
                 : '<span class="tag pend">待翻译</span>'));
    var jp=document.createElement('div'); jp.className='sjp';
    jp.innerHTML=hlJp(it.jp, it.hits);
    var ta=document.createElement('textarea');
    ta.rows=1; ta.setAttribute('data-jp', it.jp);
    ta.placeholder='对照上面的日文填中文译文…（Ctrl+Enter 保存）';
    /* 机翻草稿直接填进文本框当底稿，人工在此之上改，省掉从零翻 */
    ta.value = it.cn || (draft[it.jp]||'') || it.mt || '';
    var nbr=(it.jp.match(/<br\s*\/?>/gi)||[]).length;
    var act=document.createElement('div'); act.className='sact';
    var b=document.createElement('button'); b.className='btn save';
    b.textContent='保存并写回';
    var u=document.createElement('button'); u.className='btn undo';
    u.textContent='撤销隔离';
    var msg=document.createElement('span'); msg.className='smsg';
    act.appendChild(b); act.appendChild(u); act.appendChild(msg);
    row.appendChild(head); row.appendChild(jp);
    if(nbr){
      var hint=document.createElement('div'); hint.className='brhint';
      hint.innerHTML='<span>结构标记 &lt;br&gt; × '+nbr
        + ' —— 译文里必须保留同样个数，否则成品会缺换行</span>';
      var ib=document.createElement('button'); ib.className='btn mini';
      ib.textContent='插入 <br>';
      ib.addEventListener('click', function(){
        var s0=ta.selectionStart||ta.value.length, s1=ta.selectionEnd||s0;
        ta.value=ta.value.slice(0,s0)+'<br>'+ta.value.slice(s1);
        draft[it.jp]=ta.value; setDraft(draft); grow();
        ta.focus(); ta.setSelectionRange(s0+4, s0+4);
      });
      hint.appendChild(ib);
      row.appendChild(hint);
    }
    if(!it.cn){
      var VC={google:'谷歌',tencent:'腾讯',bing:'必应'};
      var vcn=VC[it.mt_src]||it.mt_src||'机翻';
      var mb=document.createElement('div'); mb.className='mtbar';
      mb.innerHTML='<span class="mtk">'
        +(it.mt ? ('机翻草稿 · '+vcn+' · 已填入下方文本框，校对后点「保存并写回」即采用')
                : '尚无草稿 · 点右侧按钮先出一版底稿')
        +'</span>'
        +(it.mt_note?('<span class="mtn">'+esc(it.mt_note)+'</span>'):'');
      if(it.mt){
        var ab=document.createElement('button'); ab.className='btn mini';
        ab.textContent='采用机翻并保存';
        ab.addEventListener('click', function(){ saveOne(it.jp, ta.value, msg, row, false); });
        mb.appendChild(ab);
      }
      var rb=document.createElement('button'); rb.className='btn mini mtre';
      rb.textContent=(it.mt?'重新机翻 · ':'机翻这条 · ')+vendorLabel(curVendor());
      rb.addEventListener('click', function(){ mtOne(it.jp, rb); });
      mb.appendChild(rb);
      row.appendChild(mb);
    }
    row.appendChild(ta); row.appendChild(act);
    box.appendChild(row);
    function grow(){
      ta.style.height='auto';
      ta.style.height=Math.min(160, Math.max(30, ta.scrollHeight))+'px';
    }
    ta.addEventListener('input', function(){
      draft[it.jp]=ta.value; setDraft(draft); grow();
    });
    ta.addEventListener('keydown', function(ev){
      if((ev.ctrlKey||ev.metaKey) && ev.key==='Enter'){ ev.preventDefault(); b.click(); }
    });
    b.addEventListener('click', function(){ saveOne(it.jp, ta.value, msg, row, false); });
    u.addEventListener('click', function(){
      if(window.confirm('撤销对该行的隔离？下轮会重新送百度（很可能仍被拒收）。'))
        saveOne(it.jp, '', msg, row, true);
    });
    grow();
  });
}
function loadSens(force, noGuard){
  fetch('/api/sens',{cache:'no-store'}).then(function(r){return r.json();})
    .then(function(d){
      if(!force && d.sig===SENS.sig){ SENS.pending=null; return; }
      var ae=document.activeElement;
      if(!noGuard && ae && ae.tagName==='TEXTAREA' && ae.hasAttribute('data-jp')){
        SENS.pending=d; return;      /* 正在输入：不重绘，避免打断 */
      }
      SENS.sig=d.sig; SENS.pending=null;
      sensChips(d); renderSensEditor(d);
    }).catch(function(){});
}
function sensTick(){
  if(SENS.pending){
    var ae=document.activeElement;
    if(!(ae && ae.tagName==='TEXTAREA' && ae.hasAttribute('data-jp'))){
      var d=SENS.pending; SENS.pending=null; SENS.sig=d.sig;
      sensChips(d); renderSensEditor(d);
    }
    return;
  }
  loadSens(false);
}

function render(d){
  lastSeq = d.seq;
  var dot=document.getElementById('dot'), st=document.getElementById('stxt');
  dot.className='dot '+(d.alive?'on':'off');
  st.textContent = d.alive ? '翻译中' : '事件流已停';
  document.getElementById('sub').textContent =
    '近 15 秒有事件写入 · 最近事件 '+Math.round(d.feed_age*10)/10+' 秒前 · '
    + new Date(d.ts*1000).toLocaleTimeString('zh-CN');

  var k=document.getElementById('kpis'); k.innerHTML='';
  k.appendChild(kpi('请求速率', d.rps.toFixed(2)+' req/s'));
  k.appendChild(kpi('译文速率', d.lps.toFixed(1)+' 行/秒'));
  k.appendChild(kpi('在途请求', n(d.inflight)));
  k.appendChild(kpi('请求 成功/失败', n(d.ok)+' / '+n(d.bad)));
  k.appendChild(kpi('本轮累计译文', n(d.n_ln)));
  k.appendChild(kpi('敏感隔离', n(d.sens_total||0)
    + (((d.sens_mtc||0)>0) ? (' · 机翻待审 '+d.sens_mtc) : '')
    + (((d.sens_pend||0)>0) ? (' · 待人工 '+d.sens_pend) : '')));
  k.appendChild(kpi('事件序号', n(d.seq)));

  if(!paused){ (d.ln||[]).forEach(addRow); }
  renderReqs(d.rq||[]);
  renderAccts(d.acct||[]);
  renderErr(d.err||[]);
  renderChart(d.series||[]);
}

function tick(){
  fetch('/api/live?after='+lastSeq,{cache:'no-store'})
    .then(function(r){return r.json();}).then(render)
    .catch(function(){
      document.getElementById('dot').className='dot off';
      document.getElementById('stxt').textContent='面板连接中断';
    });
}

document.getElementById('pause').addEventListener('click',function(){
  paused=!paused;
  this.textContent = paused?'继续滚动':'暂停滚动';
  this.style.color = paused?'var(--warn)':'';
});
document.getElementById('onlyerr').addEventListener('change',function(){
  tick();
});
tick(); setInterval(tick,1000);
document.getElementById('mtall').addEventListener('click',function(){ mtAll(this); });
document.getElementById('mtprobe').addEventListener('click',function(){ probeChannels(true); });
document.getElementById('mtvendor').addEventListener('change',function(){ setVendor(this.value); });
document.getElementById('ctlstart').addEventListener('click',function(){ ctlAct('start','engine',this); });
document.getElementById('ctlstop').addEventListener('click',function(){ ctlAct('stop','engine',this); });
document.getElementById('ctlrestart').addEventListener('click',function(){
  ctlAct('restart','engine',this,'重启会打断当前这一轮（已译完的部分已进缓存，不会白干）。继续？'); });
document.getElementById('ctlprun').addEventListener('click',function(){ ctlAct('start','panel',this); });
document.getElementById('ctlprestart').addEventListener('click',function(){
  ctlAct('restart','panel',this,'面板会断开约 5 秒，请稍后手动刷新本页。继续？'); });
setVendor(curVendor());
probeChannels(false);
ctlLoad(); setInterval(function(){ if(!document.hidden) ctlLoad(); }, 10000);
loadSens(true, true); setInterval(function(){ if(!document.hidden) sensTick(); }, 5000);
</script>
</body>
</html>
'''
LIVE = LIVE.replace('@@THEME@@', THEME)

# 手动工作台页：同样要等 THEME 就位后才能拼（页面本身在 manual_page.html）
MAN = _load_man()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_POST(self):
        p = self.path
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except Exception:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b''
        try:
            req = json.loads(raw.decode('utf-8'))
            if not isinstance(req, dict):
                req = {}
        except Exception:
            req = {}
        if p.startswith('/api/ctl'):
            res = ctl_act((req.get('what') or 'start').strip(),
                          (req.get('target') or 'engine').strip())
        elif p.startswith('/api/mtprobe'):
            res = mt_probe(force=bool(req.get('force')))
        elif p.startswith('/api/accounts'):
            res = accounts_post(req)
        elif p.startswith('/api/project'):
            res = project_post(req)
        elif p.startswith('/api/glossary'):
            res = glossary_post(req)
        elif p.startswith('/api/manual'):
            res = manual_post(req)
        elif p.startswith('/api/engine'):
            res = engine_post(req)
        elif p.startswith('/api/sens'):
            if (req.get('action') or '') == 'mt':
                # 手动触发机翻：jp 为空 = 全部待审；force = 连已有草稿的重翻
                # vendor 指定渠道（google/tencent/bing），空/auto = 自动降级
                res = sens_mt_fill(force=bool(req.get('force')),
                                   only=(req.get('jp') or '').strip(),
                                   vendor=(req.get('vendor') or '').strip())
            elif not (req.get('jp') or '').strip():
                res = {'ok': False, 'err': '缺少原文'}
            else:
                res = sens_save(req.get('jp') or '', req.get('cn') or '',
                                bool(req.get('unflag')))
        else:
            res = {'ok': False, 'err': '未知接口'}
        self._send(json.dumps(res, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def do_GET(self):
        p = self.path
        if p.startswith('/api/ctl'):
            self._send(json.dumps(ctl_status(), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/api/mtprobe'):
            self._send(json.dumps(mt_probe(force=('force=1' in p)),
                                  ensure_ascii=False).encode('utf-8'),
                       'application/json; charset=utf-8')
        elif p.startswith('/api/live'):
            m = re.search(r'after=(\d+)', p)
            after = int(m.group(1)) if m else 0
            body = json.dumps(live_payload(after), ensure_ascii=False).encode('utf-8')
            self._send(body, 'application/json; charset=utf-8')
        elif p.startswith('/api/progress'):
            with LOCK:
                d = dict(ST)
            d['left'] = max(d['need'] - d['done'], 0)
            d['pct'] = (100.0 * d['done'] / d['need']) if d['need'] else 0.0
            self._send(json.dumps(d, ensure_ascii=False).encode('utf-8'),
                       'application/json; charset=utf-8')
        elif p.startswith('/api/sens'):
            body = json.dumps(sens_items(), ensure_ascii=False)
            self._send(body.encode('utf-8'),
                       'application/json; charset=utf-8')
        elif p.startswith('/api/accounts'):
            self._send(json.dumps(accounts_get(p), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/api/project'):
            self._send(json.dumps(project_get(p), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/api/glossary'):
            self._send(json.dumps(glossary_get(p), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/api/manual'):
            self._send(json.dumps(manual_get(p), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/api/engine'):
            self._send(json.dumps(engine_get(p), ensure_ascii=False)
                       .encode('utf-8'), 'application/json; charset=utf-8')
        elif p.startswith('/manual'):
            self._send(MAN.encode('utf-8'), 'text/html; charset=utf-8')
        elif p.startswith('/live'):
            self._send(LIVE.encode('utf-8'), 'text/html; charset=utf-8')
        else:
            self._send(PAGE.encode('utf-8'), 'text/html; charset=utf-8')


def pick_port(start=PORT, tries=20):
    """端口被人占着就往后挪一个（旧版面板/别的软件可能已经用了 8777）。

    占着不报错直接崩，是很难排查的那种失败，所以宁可换端口并把实际端口打出来。
    """
    import socket
    for p in range(start, start + tries):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return p
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return start


def main():
    global PORT
    PORT = pick_port()
    log('=' * 50)
    log('progress_server 启动 pid=%d port=%d' % (os.getpid(), PORT))
    cfg = bind_project()               # 先绑到当前项目，再起线程
    log('当前项目：%s' % ((cfg or {}).get('name') or '（还没有项目）'))
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=feed_reader, daemon=True).start()
    threading.Thread(target=sens_worker, daemon=True).start()
    threading.Thread(target=sens_mt_worker, daemon=True).start()
    time.sleep(0.2)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    try:
        _pd = DATA_ROOT or (project.EXE_DIR if project else BASE)
        with io.open(os.path.join(_pd, '_port.txt'), 'w', encoding='utf-8') as f:
            f.write(str(PORT))          # 命令行/菜单靠它知道面板开在哪
    except Exception:
        pass
    log('监听 http://127.0.0.1:%d  （/ 进度总览 · /live 实时窗口）' % PORT)
    print('网页工作台已启动： http://127.0.0.1:%d' % PORT)
    print('  总览 /  ·  实时窗口 /live  ·  工作台 /manual   （Ctrl+C 停止）')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
