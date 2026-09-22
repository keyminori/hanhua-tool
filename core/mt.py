# -*- coding: utf-8 -*-
"""
批量翻译引擎（Google gtx 为主，Bing 为备）

架构（v2，2026-09-21 重写调度层）：
  行 -> 按游戏标记(<br>/<p_name>)切成片段 -> 只对"文本片段"做术语占位保护
     -> 片段合批送翻 -> 回填术语 -> 与标记原样拼回成行

为什么不再把标记送进引擎：
  实测 Google 合批时会把跨 <br> 的两句并成一句，把标记吞掉
  （含标记行守恒率仅 64%）；Bing 同样会吞占位符（54%）。
  标记不进引擎 = 位置 100% 精确，且片段是短句，翻译质量反而更好。

闸门（三层，任一不满足即丢弃该条保留原文，绝不静默产出坏成品）：
  1. 行数守恒：返回行数 != 输入行数 -> 二分降级；单行仍不等 -> 整批作废
  2. 占位符残留：术语占位符没还原干净 -> 丢弃
  3. 标记守恒：拼回后标记计数必须等于原文（拼回法下理论上恒成立，仍校验）
"""
import os
import re
import sys
import json
import time
import hashlib
import random
import threading
import urllib.request
import urllib.parse
import collections
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 工作区（可切换）
# 工具代码和项目数据分开：换项目 = set_home(那个项目的目录)。
# 引擎内部引用的都是下面这些**全局名**，Python 函数运行时现查 globals，
# 所以改字典就能整体搬家，引擎内部一行都不用改。
HOME = BASE               # 当前工作区（状态文件都在这）
PROBE_DIR = ''            # 体检用的真实日文样本目录（= 项目的日文源目录）

_STATE_FILES = ('tm.json', 'mt_cache.json', '_api_feed.jsonl', 'sensitive.json',
                'manual_ov.json', 'accounts.json', 'engine_cfg.json')


def set_home(home):
    """把全部状态文件搬到 home，返回实际生效的目录。"""
    home = home or BASE
    try:
        os.makedirs(home, exist_ok=True)
    except Exception:
        pass
    g = globals()
    g['HOME'] = home
    g['TM_PATH'] = os.path.join(home, 'tm.json')
    g['CACHE_PATH'] = os.path.join(home, 'mt_cache.json')
    g['EVT_PATH'] = os.environ.get('MT_EVT') or os.path.join(home, '_api_feed.jsonl')
    g['SENS_PATH'] = os.environ.get('MT_SENS') or os.path.join(home, 'sensitive.json')
    g['MAN_PATH'] = os.environ.get('MT_MAN') or os.path.join(home, 'manual_ov.json')
    g['ACC_PATH'] = os.environ.get('MT_ACC') or os.path.join(home, 'accounts.json')
    g['RUN_CFG_PATH'] = os.path.join(home, 'engine_cfg.json')
    g['DUMP_FLAG'] = os.path.join(home, '_dump_q.on')
    g['DUMP_DIR'] = os.path.join(home, '_dump_q')
    return home


def set_probe_dir(d):
    """体检样本从哪取真日文（一般是项目的日文源目录）。"""
    globals()['PROBE_DIR'] = d or ''
    globals()['_PROBE_JP'] = None          # 换项目就别再用上一项目的样本
    return d


TM_PATH = os.path.join(BASE, 'tm.json')
CACHE_PATH = os.path.join(BASE, 'mt_cache.json')

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'}

SRC_LANG = 'ja'
DST_LANG = 'zh-Hans'

# ---- 各引擎的合批参数（实测得出，勿凭感觉改）----
# Google: 40 行/请求 -> 75 行/秒；80 行(URL 编码后 16K)仍 OK，120 行 400 报错
# Bing  : 多行合批行数经常对不上，触发大量二分降级；实测单行 + 高并发才稳
ENGINE_CFG = {
    'baidu':     {'MAX_LINES': 50, 'MAX_CHARS': 2000, 'DEF_WORKERS': 2},
    'baidu_llm': {'MAX_LINES': 40, 'MAX_CHARS': 3000, 'DEF_WORKERS': 6},
    'cht':       {'MAX_LINES': 50, 'MAX_CHARS': 2000, 'DEF_WORKERS': 5},
    'google':    {'MAX_LINES': 40, 'MAX_CHARS': 1500, 'DEF_WORKERS': 24},
    'bing':      {'MAX_LINES': 1,  'MAX_CHARS': 1200, 'DEF_WORKERS': 40},
}
DEFAULT_ENGINE = 'baidu'
_PROBED = False            # 账号自检每进程只做一次


# ---------------------------------------------------------------- 实时事件流
# 供前台「百度 API 实时窗口」消费：每次 HTTP 请求的发起/结果、每条成稿译文、
# 账号停用与池自检结果。写 JSONL，缓冲 0.4s 或 20 条落一次盘。
#
# 硬约束：**监控绝不能影响翻译**。所以 evt() 内部一切异常全部吞掉，
# 落盘失败（磁盘满/被占用）只会静默丢失事件，绝不向调用方抛。
EVT_PATH = os.environ.get('MT_EVT') or os.path.join(BASE, '_api_feed.jsonl')
_EVT_ON = os.environ.get('MT_EVT_OFF') != '1'
EVT_MAX = 5 * 1024 * 1024           # 单文件上限
EVT_KEEP = 1 * 1024 * 1024          # 超限后截尾保留的最新字节数
EVT_JP = 220                        # 事件里原文/译文的最大字符数（防事件膨胀）

_EVT_LOCK = threading.Lock()
_EVT_BUF = []
_EVT_T0 = [0.0]                     # 上一次落盘时刻
_EVT_BYTES = [0]                    # 本文件已写字节数
_EVT_QID = [0]
_EVT_THREAD = [None]


def _evt_flush():
    """把缓冲区写盘（调用方须持锁）。只在文件超限时做一次截尾重写。"""
    if not _EVT_BUF:
        return
    data = ''.join(_EVT_BUF)
    del _EVT_BUF[:]
    _EVT_T0[0] = time.time()
    try:
        if _EVT_BYTES[0] and _EVT_BYTES[0] + len(data) > EVT_MAX:
            old = ''
            try:
                with open(EVT_PATH, encoding='utf-8', errors='replace') as f:
                    old = f.read()
            except Exception:
                old = ''
            tail = old[-EVT_KEEP:] if old else ''
            cut = tail.find('\n')            # 从行边界截断，避免半行 JSON
            _EVT_BYTES[0] = (len(tail) - cut) if cut > 0 else len(tail)
            with open(EVT_PATH, 'w', encoding='utf-8') as f:
                f.write(tail[cut + 1:] if cut > 0 else tail)
        with open(EVT_PATH, 'a', encoding='utf-8') as f:
            f.write(data)
        _EVT_BYTES[0] += len(data)
    except Exception:
        pass


def _evt_loop():
    """兜底刷盘线程：保证空闲时事件也不会滞留在内存里（实时窗口不能等）。"""
    while True:
        time.sleep(0.4)
        try:
            with _EVT_LOCK:
                _evt_flush()
        except Exception:
            pass


def evt(kind, **kw):
    """写一条实时事件（永不抛异常）。"""
    if not _EVT_ON:
        return None
    try:
        if kind == 'qs':
            with _EVT_LOCK:
                _EVT_QID[0] += 1
                qid = _EVT_QID[0]
            kw['q'] = qid
        kw['k'] = kind
        kw['t'] = round(time.time(), 3)
        line = json.dumps(kw, ensure_ascii=False) + '\n'
        if _EVT_THREAD[0] is None:
            with _EVT_LOCK:
                if _EVT_THREAD[0] is None:
                    th = threading.Thread(target=_evt_loop, daemon=True)
                    th.start()
                    _EVT_THREAD[0] = th
        with _EVT_LOCK:
            _EVT_BUF.append(line)
            if len(_EVT_BUF) >= 20 or time.time() - _EVT_T0[0] >= 0.4:
                _evt_flush()
        return kw.get('q')
    except Exception:
        return None


def evt_snip(s):
    """事件里的文本截断（超长句只留头部，避免事件文件被少数超长行撑爆）。"""
    if not s:
        return ''
    s = s.replace('\t', ' ')
    return s if len(s) <= EVT_JP else s[:EVT_JP] + '…'


def acct_tag(appid):
    """账号显示名：只留尾 4 位，够区分又不至于把 appid 整个写进日志。"""
    return appid[-4:] if appid else '?'


# ---------------------------------------------------------------- 敏感行隔离
# 百度大模型通道带**内容审核**：某些行会被 20003 稳定拒绝，且与账号无关
# （实测：7 个不同账号全拒 / 同一账号连发 3 次全拒 / 同长度对照句全通）
# ——换账号无解，重试也只是白烧配额。
#
# 策略：确诊一次即登记 sensitive.json 并**永久跳过**，不再送百度；
#       人工翻译后把中文填进该条的 cn 字段即自动生效（无需改代码、无需清缓存）。
#
# 字段：cn = 人工译文（最高优先级）；mt = 机翻草稿（谷歌/必应，见 sens_mt.py，
#       给人工省事的初稿）。取值顺序 cn > mt，都空则保留日文原文。
SENS_PATH = os.environ.get('MT_SENS') or os.path.join(BASE, 'sensitive.json')
_SENS = {}                # 原文 -> {'n':命中次数,'first','last','cn':人工译文,'src':出处}
_SENS_LOCK = threading.Lock()
_SENS_READY = [False]


def load_sensitive(force=False):
    """读敏感清单。首次加载后热路径无锁直接返回。

    重载用「构造新表 + 整体替换」，不要 clear 后原地填充：清空与填充之间读方
    （sens_state，无锁）会看到空表，于是把已确诊的行又送去百度（被拒），
    而 mark_sensitive 重新登记时会把**人工译文 cn 冲掉**。
    """
    global _SENS
    if _SENS_READY[0] and not force:
        return _SENS
    with _SENS_LOCK:
        if _SENS_READY[0] and not force:
            return _SENS
        new = {}
        try:
            if os.path.exists(SENS_PATH):
                with open(SENS_PATH, encoding='utf-8') as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k, v in d.items():
                        new[k] = v if isinstance(v, dict) else {'cn': ''}
        except Exception:
            pass
        _SENS = new
        _SENS_READY[0] = True
        return _SENS


_SENS_MT = [0.0, 0.0]     # [上次检查时间, 已载入清单的 mtime]


def _sens_peek():
    """清单文件变了就重载（3 秒节流）。

    面板是**另一个进程**：它把人工译文写进 sensitive.json 后，本进程内存里的
    _SENS 还是旧的（cn 为空），本轮已 collect 的行写回时会拿不到中文，
    进而把日文原文写回产物，**把人工译文冲掉**。"""
    now = time.time()
    if now - _SENS_MT[0] < 3.0:
        return
    _SENS_MT[0] = now
    try:
        mt = os.path.getmtime(SENS_PATH)
    except Exception:
        return
    if mt != _SENS_MT[1]:
        _SENS_MT[1] = mt
        load_sensitive(force=True)
        try:
            evt('sensreload', n=len(_SENS))
        except Exception:
            pass


def sens_state(src):
    """返回 (是否已确诊为敏感, 可用译文或 '')

    译文取值顺序 **cn（人工）> mt（机翻草稿）**：机翻只是给人工省事的
    初稿，人工一旦填了 cn 就自动盖住它；两者都空才留日文原文。
    """
    _sens_peek()
    load_sensitive()
    v = _SENS.get(src)
    if v is None:
        return False, ''
    return True, (v.get('cn') or v.get('mt') or '')


def sens_draft(src):
    """该行登记的机翻草稿（mt），没有则 ''。

    用途：产物里已经是中文，但可能只是我们写的机翻**草稿**。人工审校后的
    正式译文必须能盖掉它 —— 否则写回逻辑的「已有中文即保留」会把草稿永久
    钉在产物里（人工改了也进不去）。
    """
    _sens_peek()
    load_sensitive()
    v = _SENS.get(src)
    return (v.get('mt') or '') if isinstance(v, dict) else ''


def sens_frag_hit(line):
    """行里是否含**已登记的敏感片段**（片段级隔离：行本身不在清单里）。

    命中就该绕过「整行送翻」—— 整行必被 20003 拒收（白烧几次请求），而且
    片段级路径会从清单里取到译文，整行反而能拼出来。清单只有几十条，
    逐条 in 的代价可忽略（且只在缓存未命中的行上调用）。
    """
    _sens_peek()
    load_sensitive()
    if not _SENS:
        return False
    for k in _SENS:
        if len(k) >= 3 and k != line and k in line:
            return True
    return False


def mark_sensitive(src, where=''):
    """确诊一行敏感：记账 + 原子落盘 + 广播事件。

    落盘用「临时文件 + os.replace」，避免写一半被读到；标记是低频事件
    （每行只登记一次），整表重写的代价可以忽略。"""
    if not src:
        return
    load_sensitive()
    now = int(time.time())
    with _SENS_LOCK:
        v = _SENS.get(src)
        if v is None:
            _SENS[src] = {'n': 1, 'first': now, 'last': now,
                          'cn': '', 'src': where}
        else:
            v['n'] = int(v.get('n', 0)) + 1
            v['last'] = now
            if where and not v.get('src'):
                v['src'] = where
        snap = dict((k, dict(x)) for k, x in _SENS.items())
        snap = _sens_merge_disk(snap)   # 别冲掉面板写的机翻草稿/人工译文
    _sens_save(snap)
    try:
        _SENS_MT[1] = os.path.getmtime(SENS_PATH)   # 自己写的，别触发无谓重载
    except Exception:
        pass
    evt('sens', jp=evt_snip(src), where=where)


def _sens_merge_disk(snap):
    """把磁盘上的新字段并进即将落盘的快照（防覆盖机翻草稿/人工译文）。

    面板是另一个进程，它写的 mt（机翻草稿）本进程可能还没重载到；
    登记新敏感行时整表重写就会把那些字段冲掉。这里以「内存有值优先、
    内存为空则取磁盘」的规则补齐，磁盘独有的条目也一并保留。
    只读文件、不加锁 —— 调用方已持 _SENS_LOCK（threading.Lock 不可重入）。
    """
    try:
        with open(SENS_PATH, encoding='utf-8') as f:
            disk = json.load(f)
    except Exception:
        return snap
    if not isinstance(disk, dict):
        return snap
    KEEP = ('cn', 'mt', 'mt_src', 'mt_at', 'mt_note', 'src')
    for k, dv in disk.items():
        if not isinstance(dv, dict):
            continue
        if k not in snap:
            snap[k] = dict(dv)
            continue
        sv = snap[k]
        for f2 in KEEP:
            if not sv.get(f2) and dv.get(f2):
                sv[f2] = dv[f2]
    return snap


def _sens_save(snap):
    """原子落盘（失败不抛）。

    必须持锁 + 用唯一临时名：登记是在 worker 线程池里并发触发的，固定 tmp
    名会让两个线程同时写同一个文件 -> 内容交错 -> replace 后清单整体损坏。
    """
    try:
        tmp = '%s.%d.tmp' % (SENS_PATH, threading.get_ident())
        with _SENS_LOCK:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snap, f, ensure_ascii=False, indent=1,
                          sort_keys=True)
            os.replace(tmp, SENS_PATH)
    except Exception:
        pass

# ---------------------------------------------------------------- 人工改稿台账
# 面板的「手动工作台」（/manual）与 ctl.py set 会把某行的译文直接写进 chinese/
# 产物；但引擎一轮要跑两万行，**轮首就把产物读成 prev 快照**，轮末才写回 ——
# 那份旧快照会把人工译文盖回日文/机翻。这里留一份台账（`文件#行号` -> 译文），
# mt_story 的 collect/write_back 都查它，命中即以台账为准（最高优先级）。
#
# 读法与会话隔离清单一致：mtime + TTL 重载（面板是另一个进程，必须看得见它的写）。
MAN_PATH = os.environ.get('MT_MAN') or os.path.join(BASE, 'manual_ov.json')
_MAN = {'m': None, 't': 0.0, 'd': {}}
_MAN_LOCK = threading.RLock()      # manual_set 持锁时会再进 manual_load，必须可重入


def manual_load(force=False, ttl=1.0):
    """取台账（dict）。ttl 秒内直接复用内存副本，避免逐行 getmtime 打爆 syscall。"""
    now = time.time()
    with _MAN_LOCK:
        if (not force) and _MAN['t'] and (now - _MAN['t']) < ttl:
            return _MAN['d']
    try:
        m = os.path.getmtime(MAN_PATH)
    except Exception:
        m = None
    with _MAN_LOCK:
        if (not force) and _MAN['t'] and _MAN['m'] == m:
            _MAN['t'] = now
            return _MAN['d']
    d = {}
    if m is not None:
        try:
            with open(MAN_PATH, encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
    with _MAN_LOCK:
        _MAN['m'] = m
        _MAN['t'] = now
        _MAN['d'] = d
    return d


def manual_ov(name, idx):
    """某行的人工改稿（无则 ''）。write_back/collect 每行都要问，必须便宜。"""
    v = manual_load().get('%s#%s' % (name, idx))
    if v is None:
        return ''
    if isinstance(v, dict):
        return (v.get('cn') or '').strip()
    return v.strip() if isinstance(v, str) else ''


def manual_get(name, idx):
    """单行台账详情（含改稿时间、被谁覆盖过几次）。"""
    v = manual_load(force=True).get('%s#%s' % (name, idx))
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    return {'cn': v} if isinstance(v, str) else {}


def manual_setd(d):
    """整表原子落盘（面板与本进程共用同一份文件）。"""
    try:
        tmp = '%s.%d.tmp' % (MAN_PATH, threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, MAN_PATH)
        return True
    except Exception:
        return False


def manual_set(name, idx, cn, old=''):
    """登记/更新/删除一条人工改稿（cn 为空 = 删除）。返回该条。"""
    k = '%s#%s' % (name, idx)
    with _MAN_LOCK:
        d = dict(manual_load(force=True))
        if not (cn or '').strip():
            d.pop(k, None)
        else:
            v = d.get(k) if isinstance(d.get(k), dict) else {}
            v['cn'] = cn.strip()
            v['at'] = int(time.time())
            v['n'] = int(v.get('n', 0) or 0) + 1
            if old and not v.get('old'):
                v['old'] = old
            d[k] = v
        manual_setd(d)
        manual_load(force=True)
    return manual_get(name, idx)


PH_L = 'ZQX'          # 术语占位符形如 ZQX7QXZ
PH_R = 'QXZ'
# 结构性标记：不参与翻译，必须原样保留。
# 注意不能写成 `<[^<>]{1,24}>`——那会把 <強欲> / <有効な状態異常> 这类
# **需要翻译的内容** 也当成标记跳过，导致日文原样漏进成品。
STRUCT_RE = re.compile(r'</?color[^<>]*>|<br\s*/?>|<p_name[^<>]*>', re.I)
SPLIT_RE = re.compile(r'(' + STRUCT_RE.pattern + r')')
MARKUP_RE = STRUCT_RE                     # 兼容旧引用
BR_BASE = 9000                            # 结构标记占位符编号基址（避开术语 0..999）
STRUCT_SENT = '\x01'                      # 结构标记哨兵，restore 后剥掉即还原原标记


# ---------------------------------------------------------------- 术语表
def load_tm():
    # 允许 tm.json 缺失（工作区重置后首次运行）——缺失视为空术语表
    if not os.path.exists(TM_PATH):
        return {}
    tm = json.load(open(TM_PATH, encoding='utf-8'))
    pairs = {}                       # 日文 -> 中文
    for sec in ('term', 'country', 'zodiac', 'profile', 'favorite', 'hobby'):
        for k, v in tm.get(sec, {}).items():
            if k.startswith('_') or not isinstance(v, str):
                continue
            if k and v:
                pairs[k] = v
    # 角色名册：jp(片假名) -> cn
    for cid, d in tm.get('name', {}).items():
        if not isinstance(d, dict):
            continue
        jp, cn = d.get('jp'), d.get('cn')
        if jp and cn:
            pairs[jp] = cn
    return pairs


def build_protect(pairs, min_len=2):
    """长词优先，避免短词先替换把长词切碎"""
    items = [(k, v) for k, v in pairs.items() if len(k) >= min_len]
    items.sort(key=lambda x: -len(x[0]))
    return items


_KANA_EDGE = r'[\u30a1-\u30fa\u30fc]'     # 片假名 + 长音符（不含中点 ・）


def _all_kana(s):
    return all((0x30a1 <= ord(c) <= 0x30fa) or ord(c) == 0x30fc for c in s)


def protect(text, items):
    """日文专名 -> 占位符；返回 (保护后文本, {占位符: 中文})
    注意：游戏标记在调用本函数之前已被切走，不再进引擎。

    纯片假名术语改用【词边界】匹配：避免短名命中更长片假名词内部
    （例：アル 不应吃掉 アルケミスト；ソル 不应吃掉 ソルジャー）。
    含汉字/假名混排的术语仍走朴素替换（中文语境下无此歧义）。"""
    slots = {}
    out = text
    for i, (jp, cn) in enumerate(items):
        if not jp or jp not in out:
            continue
        ph = '%s%d%s' % (PH_L, i, PH_R)
        if _all_kana(jp):
            pat = re.compile(r'(?<!' + _KANA_EDGE + r')' + re.escape(jp)
                             + r'(?!' + _KANA_EDGE + r')')
            if pat.search(out):
                out = pat.sub(ph, out)
                slots[ph] = cn
        else:
            out = out.replace(jp, ph)
            slots[ph] = cn
    return out, slots


def restore(text, slots, pairs):
    """占位符 -> 中文；再全词表兜底替换（长词优先）"""
    for ph, cn in slots.items():
        idx = ph[len(PH_L):-len(PH_R)]   # 取出中间数字
        # 容忍引擎在占位符内部/两侧插入的空格：ZQX1152 QXZ 等
        pat = (re.escape(PH_L) + r'\s*' + re.escape(idx) + r'\s*'
               + re.escape(PH_R))
        text = re.sub(r'[ \t]*' + pat + r'[ \t]*', cn, text)

    # 引擎有时会改写占位符（实测 ZQX224QXZ -> PZQ224QXZ），且可能夹空格。
    # 按中间的数字索引找回原词，避免丢术语。
    def _loose(m):
        idx = int(m.group(1))
        return slots.get('%s%d%s' % (PH_L, idx, PH_R), '')
    text = re.sub(r'[A-Za-z]*' + PH_L + r'\s*(\d+)\s*Q[A-Za-z]*XZ', _loose, text)
    # 再兜一遍标准形态（含空格变体）
    text = re.sub(r'[ \t]*' + PH_L + r'\s*\d*\s*' + PH_R + r'[ \t]*', '', text)
    for jp, cn in sorted(pairs.items(), key=lambda x: -len(x[0])):
        if jp in text:
            text = text.replace(jp, cn)
    return text


_PUNCT_ONLY = re.compile(r'^[，、。；：！？…‥・\s]+$')
_BR_SPLIT = re.compile(r'<br\s*/?>', re.I)
_BR_LEAD = re.compile(r'(<br\s*/?>)[ \t]*[，、]+', re.I)


def mask_struct(text, slots):
    """结构性标记（<br>/<color>/<p_name>）掩成占位符。

    为什么必须掩：实测把带 <br> 的整行直接送翻，引擎会把 <br> 当软换行
    合并掉（12 条里丢 4 条）——句中位置的换行必然丢失；而退回片段级翻译
    又会把句子切断成「苍炎骑士团的 / 竟然被指派为向导」这种悬空病句。
    掩成占位符后引擎不敢动它：实测 24/24 换行位置逐字守恒，且语境完整。
    """
    cnt = [0]

    def _m(m):
        cnt[0] += 1
        ph = '%s%d%s' % (PH_L, BR_BASE + cnt[0], PH_R)
        slots[ph] = STRUCT_SENT + m.group(0)
        return ph
    return STRUCT_RE.sub(_m, text), cnt[0]


def unmask_struct(t):
    return t.replace(STRUCT_SENT, '')


def cleanup_breaks(t):
    """收拢换行处残留的标点。

    掩码保留了换行位置，但引擎是按「句子」生成中文的，语气停顿处的标点
    会跑到换行之后/之前：「就到这里吧，<br>！」「我也去！！<br>，我们分头行动！」
    """
    t = _BR_LEAD.sub(r'\1', t)                 # 换行后紧跟逗号 -> 去掉
    parts = _BR_SPLIT.split(t)
    if len(parts) > 1:
        out = [parts[0]]
        for p in parts[1:]:
            if p.strip() and _PUNCT_ONLY.match(p):
                out[-1] += p                   # 纯标点片段并回上一段（换行被吃回）
            else:
                out.append(p)
        t = '<br>'.join(out)
    t = re.sub(r'[，、](?=[！？。])', '', t)      # 「，！」->「！」
    return t


def split_markup(s):
    """行 -> (文本片段列表, 标记列表)。拼回 = 交错 文本+标记"""
    toks = SPLIT_RE.split(s)
    texts = [t for i, t in enumerate(toks) if i % 2 == 0]
    marks = [t for i, t in enumerate(toks) if i % 2 == 1]
    return texts, marks


def join_markup(texts, marks):
    out = texts[0] if texts else ''
    for m, t in zip(marks, texts[1:]):
        out += m + t
    return out


# ---------------------------------------------------------------- 缓存
class Cache:
    def __init__(self, path=None):
        # 默认参数只在定义时求值一次 —— 切了工作区就会写回旧目录，
        # 所以留空、到运行时再取当时的 CACHE_PATH。
        self.path = path or CACHE_PATH
        self.lock = threading.Lock()
        self.d = {}
        if os.path.exists(self.path):
            try:
                self.d = json.load(open(self.path, encoding='utf-8'))
            except Exception:
                self.d = {}

    def get(self, k):
        with self.lock:
            return self.d.get(k)

    def put(self, k, v):
        with self.lock:
            self.d[k] = v

    def save(self, retry=8):
        """落盘。**绝不因为落盘失败把翻译搞崩**。

        Windows 上只要别的进程正读着 mt_cache.json，os.replace 就抛
        WinError 5（实测：一次 19MB 的读取把整个引擎搞退了 8 分钟没人发现）。
        所以重试几次，实在不行跳过这一块 —— 下一块还会再存，最多丢一块缓存。
        """
        with self.lock:
            tmp = self.path + '.tmp'
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.d, f, ensure_ascii=False)
            except Exception:
                return False
            for i in range(max(1, int(retry))):
                try:
                    os.replace(tmp, self.path)
                    return True
                except Exception:
                    time.sleep(0.15 * (i + 1))
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False


# ---------------------------------------------------------------- 引擎
class BatchError(Exception):
    pass


class Google:
    """translate.googleapis.com/translate_a/single (client=gtx)
    优点：无需鉴权、合批可靠（40 行 100% 行数匹配）、75 行/秒。
    缺点：非官方接口，可能被限流；有 key 时应优先百度/DeepL。"""

    def call(self, text, retry=3):
        last = None
        for i in range(retry):
            try:
                rate_gate()
                note_req()
                url = ('https://translate.googleapis.com/translate_a/single'
                       '?client=gtx&sl=%s&tl=%s&dt=t&q=%s'
                       % (SRC_LANG, 'zh-CN', urllib.parse.quote(text)))
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=25) as r:
                    res = json.loads(r.read().decode('utf-8'))
                return ''.join(x[0] for x in res[0] if x[0])
            except Exception as e:
                last = e
                if i + 1 < retry:
                    time.sleep(0.4 * (i + 1))
        raise BatchError('Google 请求失败: %s: %s' % (type(last).__name__, last))


# 鉴权参数全局共享：每个线程各抓一次 652KB 的 translator 页面，
# 在 32 并发下会变成几十次重复下载，白白拖慢启动。
_AUTH = {'ig': None, 'key': None, 'aid': None, 'ts': 0.0}
_AUTH_LOCK = threading.Lock()
AUTH_TTL = 1800          # 秒


class Bing:
    def __init__(self):
        self.ig = None
        self.key = None
        self.aid = None
        self.refresh()

    def refresh(self, force=False):
        with _AUTH_LOCK:
            fresh = _AUTH['ig'] and (time.time() - _AUTH['ts']) < AUTH_TTL
            if not force and fresh:
                self.ig, self.key, self.aid = _AUTH['ig'], _AUTH['key'], _AUTH['aid']
                return
            html = self._get('https://cn.bing.com/translator')
            m = re.search(r'IG:"([^"]+)"', html)
            m2 = re.search(r'params_AbusePreventionHelper\s*=\s*\[\s*(\d+)\s*,'
                           r'\s*"([^"]+)"\s*,\s*(\d+)', html)
            if not m or not m2:
                raise RuntimeError('抓取 Bing 鉴权参数失败')
            _AUTH['ig'] = m.group(1)
            _AUTH['aid'], _AUTH['key'] = m2.group(1), m2.group(2)
            _AUTH['ts'] = time.time()
            self.ig, self.key, self.aid = _AUTH['ig'], _AUTH['key'], _AUTH['aid']

    def _get(self, url, timeout=20):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')

    def call(self, text, retry=3):
        """text 可含 \\n。返回译文字符串(同样含 \\n)"""
        last = None
        for i in range(retry):
            try:
                rate_gate()
                note_req()
                url = ('https://cn.bing.com/ttranslatev3?isVertical=1&&IG=%s'
                       '&IID=translator.5024.3' % self.ig)
                data = urllib.parse.urlencode({
                    'fromLang': SRC_LANG, 'to': DST_LANG,
                    'text': text, 'token': self.key, 'key': self.aid,
                }).encode()
                req = urllib.request.Request(url, data=data, headers={
                    **UA,
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': 'https://cn.bing.com/translator',
                })
                with urllib.request.urlopen(req, timeout=15) as r:
                    res = json.loads(r.read().decode('utf-8'))
                return res[0]['translations'][0]['text']
            except Exception as e:
                last = e
                if i + 1 < retry:
                    time.sleep(0.5 * (i + 1))
                    # 只在第二次仍失败时才强制换鉴权（多数失败是超时，不是鉴权过期）
                    if i >= 1:
                        try:
                            self.refresh(force=True)
                        except Exception:
                            pass
        raise BatchError('Bing 请求失败: %s: %s' % (type(last).__name__, last))


# ---------------------------------------------------------------- 百度账号池
# ---------------------------------------------------------------- 账号管理
# 早先「停用某个账号 / 加一个新账号」只能改源码再重启进程 —— 引擎一跑就是
# 几小时，等于做不到。现在落到 accounts.json：人工停用与后加账号都在这里，
# 引擎进程 mtime + 1 秒 TTL 热重载，运行中就能生效（不用重启）。
#
#   disabled  人工停用的 appid，按池分开：{"std": [...], "llm": [...]}
#   extra     后加的账号：[{"appid","key","pool":"std"|"llm","note","at"}]
#
# 坑：同一个 appid 在「通用翻译」和「大模型」两个通道的 key **不一样**
#     （通用是 20 位短 key，大模型是 _dao 开头那串），所以 extra 按
#     (appid, pool) 存一条，一个 appid 最多两条。
ACC_PATH = os.environ.get('MT_ACC') or os.path.join(BASE, 'accounts.json')
_ACC = {'m': None, 't': 0.0, 'd': None, 'sig': None}
_ACC_LOCK = threading.RLock()


def _acc_blank():
    return {'disabled': {'std': [], 'llm': []}, 'extra': [], 'updated_at': ''}


def acc_load(force=False, ttl=1.0):
    """读 accounts.json（mtime + TTL）。面板是另一个进程，必须看得见它的写。"""
    now = time.time()
    with _ACC_LOCK:
        if (not force) and _ACC['d'] is not None and _ACC['t'] \
                and (now - _ACC['t']) < ttl:
            return _ACC['d']
    try:
        m = os.path.getmtime(ACC_PATH)
    except Exception:
        m = None
    with _ACC_LOCK:
        if (not force) and _ACC['d'] is not None and _ACC['t'] \
                and _ACC['m'] == m:
            _ACC['t'] = now
            return _ACC['d']
    d = _acc_blank()
    if m is not None:
        try:
            with open(ACC_PATH, encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                dis = raw.get('disabled') or {}
                for k in ('std', 'llm'):
                    v = dis.get(k) if isinstance(dis, dict) else None
                    if isinstance(v, list):
                        d['disabled'][k] = [str(x) for x in v]
                ex = raw.get('extra')
                if isinstance(ex, list):
                    for e in ex:
                        if not isinstance(e, dict):
                            continue
                        a = str(e.get('appid') or '').strip()
                        k = str(e.get('key') or '').strip()
                        p = str(e.get('pool') or 'llm').strip()
                        if a and k and p in ('std', 'llm'):
                            d['extra'].append({'appid': a, 'key': k, 'pool': p,
                                               'note': str(e.get('note') or ''),
                                               'at': str(e.get('at') or '')})
                d['updated_at'] = str(raw.get('updated_at') or '')
        except Exception:
            d = _acc_blank()
    with _ACC_LOCK:
        _ACC['m'] = m
        _ACC['t'] = now
        _ACC['d'] = d
    return d


def acc_setd(d):
    """整表原子落盘（面板与本进程共用同一份文件）。"""
    try:
        d = dict(d)
        d['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        tmp = '%s.%d.tmp' % (ACC_PATH, threading.get_ident())
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, ACC_PATH)
        acc_load(force=True)
        return True
    except Exception:
        return False


def acc_off(kind, appid):
    """该账号是否被人工停用（kind = 'std' / 'llm'）。每个请求都问，必须便宜。"""
    try:
        return str(appid) in (acc_load().get('disabled', {}).get(kind) or [])
    except Exception:
        return False


def acc_disable(kind, appid, on=True):
    """人工停用 / 恢复一个账号。返回 (ok, 说明)。"""
    appid = str(appid or '').strip()
    if kind not in ('std', 'llm'):
        return (False, '池名只能是 std（通用）/ llm（大模型）')
    if not appid:
        return (False, '缺少 appid')
    with _ACC_LOCK:
        d = acc_load(force=True)
        dis = dict(d.get('disabled') or {'std': [], 'llm': []})
        cur = list(dis.get(kind) or [])
        has = appid in cur
        if on and not has:
            cur.append(appid)
        elif (not on) and has:
            cur.remove(appid)
        elif on:
            return (True, '%s 本已停用' % acct_tag(appid))
        else:
            return (True, '%s 本来就开着' % acct_tag(appid))
        dis[kind] = cur
        d['disabled'] = dis
        ok = acc_setd(d)
    evt('acc', pool=kind, acc=acct_tag(appid), st=('off' if on else 'on'))
    return (ok, '%s %s·%s' % ('已停用' if on else '已启用', kind, acct_tag(appid)))


def acc_add(appid, key, pool='llm', note=''):
    """新增账号。同 (appid, pool) 已存在则覆盖 key —— 也就是「改密钥」走同一条路。"""
    a = str(appid or '').strip()
    k = str(key or '').strip()
    if pool not in ('std', 'llm'):
        return (False, '池名只能是 std（通用）/ llm（大模型）')
    if not a or not k:
        return (False, 'appid 和 key 都要填')
    if len(a) < 8 or len(k) < 8:
        return (False, 'appid / key 看着不像（太短）')
    note = str(note or '').strip()[:60]
    with _ACC_LOCK:
        d = acc_load(force=True)
        full = d.get('extra') or []
        if not note:
            # 「改密钥」时不带备注：沿用这条自己的旧备注，别顺手抹空。
            # 坑：要按 (appid, pool) 在**完整**清单里找 —— 从过滤掉自己之后
            # 的列表里找永远找不到（_test_acc.py 抓到过一次，备注被清空）。
            for e in full:
                if e.get('appid') == a and e.get('pool') == pool:
                    note = str(e.get('note') or '')
                    break
        ex = [e for e in full
              if not (e.get('appid') == a and e.get('pool') == pool)]
        ex.append({'appid': a, 'key': k, 'pool': pool, 'note': note,
                   'at': time.strftime('%Y-%m-%d %H:%M:%S')})
        d['extra'] = ex
        ok = acc_setd(d)
    if ok:
        acc_sync(force=True)          # 立刻把新账号装进轮转池，不用等重启
        evt('acc', pool=pool, acc=acct_tag(a), st='add')
    return (ok, '已加入%s池：%s' % ('大模型' if pool == 'llm' else '通用',
                                    acct_tag(a)))


def acc_remove(appid, pool=None):
    """删掉后加的账号（源码清单里的删不掉，只能停用）。"""
    a = str(appid or '').strip()
    with _ACC_LOCK:
        d = acc_load(force=True)
        ex = d.get('extra') or []
        left = [e for e in ex
                if e.get('appid') != a or (pool and e.get('pool') != pool)]
        if len(left) == len(ex):
            return (False, '没找到这个后加账号：%s（源码清单里的只能停用）'
                    % acct_tag(a))
        d['extra'] = left
        # 顺手把它的停用记录也清掉：留着的话，下次同一个 appid 再加进来会
        # 带着一条看不见的「已停用」—— 界面显示可用，实际永不轮到它。
        dis = dict(d.get('disabled') or {'std': [], 'llm': []})
        for k in ('std', 'llm'):
            if pool and k != pool:
                continue
            v = [x for x in (dis.get(k) or []) if x != a]
            if v != (dis.get(k) or []):
                dis[k] = v
        d['disabled'] = dis
        ok = acc_setd(d)
    if ok:
        acc_sync(force=True)
        evt('acc', pool=str(pool or '*'), acc=acct_tag(a), st='del')
    return (ok, '已删除 %s' % acct_tag(a))


def acc_extra_pairs(pool):
    """某池的后加账号 [(appid, key)]，去重、按加入顺序（后加的排前面先用）。"""
    out = []
    seen = set()
    for e in acc_load().get('extra') or []:
        if e.get('pool') != pool:
            continue
        kk = (e.get('appid'), e.get('key'))
        if kk in seen:
            continue
        seen.add(kk)
        out.append(kk)
    return out


def _acc_merge(base, pool):
    """源码清单 + 后加账号（后加的排前面：新号额度一般更足）。按 appid 去重。"""
    out = [(str(a), str(k)) for a, k in acc_extra_pairs(pool)]
    seen = set(a for a, _k in out)
    for a, k in base:
        a = str(a)
        if a in seen:
            continue
        seen.add(a)
        out.append((a, str(k)))
    return out


def _acc_reuse(old_map, appid, key, cls):
    """同 appid 的实例复用（限流节奏与冷却不丢），但 key 必须同步。

    为什么单独抽出来：走这条路最常见的是「改密钥」—— 复用旧实例会把旧 key
    留在池里，界面上显示改成功了，实际请求还在用旧密钥，是典型的
    「不报错但结果是错的」。测出来过一次（_test_acc.py）。
    """
    ac = old_map.get(appid)
    if ac is None:
        return cls(appid, key)
    ac.key = key
    return ac


def acc_sync(force=False):
    """把 accounts.json 的后加账号装进轮转池 —— 运行中也生效，不用重启。

    返回 True = 池被重建过。已有实例按 appid **复用**（限流节奏与冷却状态
    不丢），只把新增的补进去、把删掉的去掉。
    """
    global BAIDU_POOL, BAIDU_LLM_POOL, BAIDU_ACCOUNTS, BAIDU_LLM_ACCOUNTS
    global BAIDU_APPID, BAIDU_KEY
    d = acc_load(force=force)
    sig = tuple(sorted((str(e.get('appid')), str(e.get('key')),
                        str(e.get('pool')))
                       for e in (d.get('extra') or [])))
    with _ACC_LOCK:
        if (not force) and _ACC['sig'] == sig:
            return False
        _ACC['sig'] = sig
        std_old = dict((a.appid, a) for a in BAIDU_POOL)
        llm_old = dict((a.appid, a) for a in BAIDU_LLM_POOL)
        BAIDU_ACCOUNTS = _load_baidu_accounts()
        BAIDU_LLM_ACCOUNTS = _load_llm_accounts()
        BAIDU_POOL = [_acc_reuse(std_old, a, k, BaiduAccount)
                      for a, k in BAIDU_ACCOUNTS]
        BAIDU_LLM_POOL = [_acc_reuse(llm_old, a, k, BaiduLLMAccount)
                          for a, k in BAIDU_LLM_ACCOUNTS]
        if BAIDU_ACCOUNTS:
            BAIDU_APPID, BAIDU_KEY = BAIDU_ACCOUNTS[-1]
        try:
            ENGINE_CFG['baidu']['DEF_WORKERS'] = \
                max(2, min(len(BAIDU_ACCOUNTS) + 1, 8))
        except Exception:
            pass
        return True


def acc_state(acct):
    """给界面看的状态：ok / cool（暂时避让）/ dead（硬错停用一天）。"""
    left = acct.disabled_until - time.time()
    if left > 3600:
        return ('dead', int(left))
    if left > 0:
        return ('cool', int(left))
    return ('ok', 0)


def acc_list():
    """界面用：两个池的账号明细（含人工停用标记与实时状态）。"""
    acc_sync()
    out = []
    for kind, name, pool in (('llm', '大模型池', BAIDU_LLM_POOL),
                             ('std', '通用池', BAIDU_POOL)):
        ex = dict((e.get('appid'), e)
                  for e in (acc_load().get('extra') or [])
                  if e.get('pool') == kind)
        items = []
        for ac in pool:
            st, left = acc_state(ac)
            e = ex.get(ac.appid) or {}
            items.append({'appid': ac.appid, 'tag': acct_tag(ac.appid),
                          'key_tail': (ac.key or '')[-6:],
                          'off': acc_off(kind, ac.appid),
                          'state': st, 'cool_left': left,
                          'src': 'extra' if ac.appid in ex else 'hard',
                          'note': str(e.get('note') or ''),
                          'at': str(e.get('at') or '')})
        out.append({'kind': kind, 'name': name, 'n': len(items),
                    'avail': sum(1 for i in items
                                 if (not i['off']) and i['state'] != 'dead'),
                    'items': items})
    return {'ok': True, 'pools': out, 'path': ACC_PATH,
            'updated_at': acc_load().get('updated_at') or ''}


DUMP_FLAG = os.path.join(BASE, '_dump_q.on')


def _dump_q(tag, text):
    """抓包：`解包/汉化/_dump_q.on` 存在时，把实际送出去的 q 原样落盘。

    诊断用（查「为什么自己发就行、引擎发就超时」）。开关是个文件而不是
    环境变量 —— 引擎由计划任务拉起，改不了它的环境，但文件开关随时可开可关，
    一次 os.path.exists 的开销可以忽略。
    """
    try:
        if not os.path.exists(DUMP_FLAG):
            return
        d = globals().get('DUMP_DIR') or os.path.join(HOME, '_dump_q')
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, '%s_%d_%d.txt'
                          % (tag, int(time.time() * 1000), len(text)))
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception:
        pass


_PROBE_JP = None


def probe_text(n=20):
    """体检用的真实日文载荷（n 行）。

    为什么不用「こんにちは」：2026-09-22 实测 —— 5 字符的请求 234ms 就回来，
    而引擎真实载荷（40 行 / 2454 字符）每次都是精确的 25 秒超时。短请求压根
    不进模型、也不占额度，测出来全绿却一点预测力都没有。所以体检拿真文本。
    """
    global _PROBE_JP
    if _PROBE_JP is None:
        lines = []
        # 优先用项目的日文源目录；没配就退回工作区上级找 japanese/
        d = PROBE_DIR or os.path.join(HOME, 'japanese')
        if not os.path.isdir(d):
            for alt in (os.path.join(os.path.dirname(HOME), 'japanese'),
                        os.path.join(os.path.dirname(HOME), '_extract',
                                     '\u5267\u60c5\u6587\u6863', 'Loc', 'japanese')):
                if os.path.isdir(alt):
                    d = alt
                    break
        try:
            import glob
            for f in sorted(glob.glob(os.path.join(d, '*.txt')))[:20]:
                with open(f, encoding='utf-8', errors='ignore') as fh:
                    for ln in fh:
                        ln = ln.strip()
                        if ln and len(ln) > 4:
                            lines.append(ln)
                if len(lines) >= 200:
                    break
        except Exception:
            lines = []
        if not lines:
            lines = ['\u521d\u9663\u306e\u76f4\u5f8c\u306b\u3001\u84bc\u708e\u9a0e\u58eb\u56e3\u306e\u6848\u5185\u5f79\u3092\u547d\u3058\u3089\u308c\u308b',
                     '\u79c1\u306f\u3042\u306a\u305f\u306e\u3053\u3068\u3092\u305a\u3063\u3068\u898b\u3066\u3044\u305f',
                     '\u305d\u3093\u306a\u3053\u3068\u3092\u8a00\u308f\u306a\u3044\u3067\u304f\u308c',
                     '\u307e\u3055\u304b\u306b\u305d\u3046\u3060\u3068\u306f\u601d\u308f\u306a\u304b\u3063\u305f',
                     '\u3069\u3046\u3057\u3066\u3082\u3042\u306e\u4eba\u3092\u6b7b\u3070\u305b\u308b\u308f\u3051\u306b\u306f\u3044\u304b\u306a\u3044']
        _PROBE_JP = lines
    src = _PROBE_JP
    return '\n'.join(src[i % len(src)] for i in range(n))


def acc_test(kind, appid, lines=20):
    """单账号体检：发一段**真实载荷**（默认 20 行日文），报成/败、耗时、返回行数。

    lines=0 才退回那句「こんにちは」（只想看密钥对不对时用，快但不说明能不能干活）。
    """
    pool = BAIDU_LLM_POOL if kind == 'llm' else BAIDU_POOL
    ac = None
    for x in pool:
        if x.appid == str(appid):
            ac = x
            break
    if ac is None:
        return {'ok': False, 'err': '池里没有这个账号：%s' % acct_tag(appid)}
    text = 'こんにちは' if not lines else probe_text(int(lines))
    t0 = time.time()
    try:
        r = ac.call(text)
        out = r if isinstance(r, str) else '\n'.join(r)
        return {'ok': True, 'ms': int((time.time() - t0) * 1000),
                'lines': len(out.split('\n')), 'want': text.count('\n') + 1,
                'chars': len(text),
                'out': out[:40]}
    except Exception as e:
        return {'ok': False, 'ms': int((time.time() - t0) * 1000),
                'lines': 0, 'want': text.count('\n') + 1, 'chars': len(text),
                'err': str(e)[:120]}
# 凭证来源（优先级）：
#   1) 环境变量 BAIDU_ACCOUNTS（JSON 数组，如 [["appid","key"],...]）—— 便于"后续继续增加"
#   2) 下方硬编码清单（用户提供，2026-09-21）
# 个人版 QPS=1，多账号并发轮询可倍数放大吞吐；某账号触发限流/余额不足自动切下一个。
_BAIDU_HARDCODED = [
    # ---- 第 8 批（2026-09-21 最新，体检：大模型通道全部可用）----
    ('20260921002688969', 'aLsZhGq8yZZLPR66x1Tc'),
    ('20260921002688985', 'LRqJIhcwT11haTdwVzQN'),
    # ---- 第 7 批（20260114002539231：已开通大模型，通用翻译未开通）----
    ('20260114002539231', 'mqkLqgEBDfcGcyY4ACyJ'),
    # ---- 第 6 批（10 个；前 5 个通用翻译可用，后 5 个仅大模型）----
    ('20260921002688815', 'Vw4zNzE8hkvNhzLRDz1y'),
    ('20260921002688814', 'ZOsAJL95NNricMZxGYI6'),
    ('20260921002688813', 'OafYMtfRZHFEyVoDJFi4'),
    ('20260921002688811', 'XPYl76VdJaBOIP54Nwb5'),
    ('20260921002688816', 'Ep2VR60E056_f6zUpJ6t'),
    ('20260921002688812', 'McrqJ3RYiwRTIHQoHnpJ'),
    ('20260921002688820', 'GHpWBDmnsZrTf5uKG1MD'),
    ('20260921002688822', 'Zt7vJl0nO_qXnqIBWjPv'),
    ('20260921002688830', 'qEkEG7l6DB7V7PPo4NuK'),
    ('20260921002688873', 'i4qJX4fiO2SIy8mmSobC'),
    # ---- 更早批次（2026-09-21 19:57 复测：仅 …796 标准通道仍可用；其余 54004 已耗尽）----
    ('20260921002688765', 'f3E9MSiVfUS_X4MxRi0F'),
    ('20260921002688796', 'op70Cf0eQYC1BdoMjyJS'),
    ('20260921002688726', 'qhRAYonctl4tisSqbCTB'),
    ('20260921002688723', 'UZyscdXM3KmpmAS7ZmwO'),
    (os.environ.get('BAIDU_APPID', '20230616001714296'),
     os.environ.get('BAIDU_KEY', 'fp0uyzsCa3OUs2sLpDE6')),
]


def _load_baidu_accounts():
    raw = os.environ.get('BAIDU_ACCOUNTS')
    base = _BAIDU_HARDCODED
    if raw:
        try:
            lst = json.loads(raw)
            if isinstance(lst, list) and lst:
                base = [(str(a), str(k)) for a, k in lst]
        except Exception:
            pass
    return _acc_merge(base, 'std')


BAIDU_ACCOUNTS = _load_baidu_accounts()
BAIDU_APPID = BAIDU_ACCOUNTS[-1][0]   # 兼容旧引用
BAIDU_KEY = BAIDU_ACCOUNTS[-1][1]


class BaiduAccount:
    """单个百度账号：自带 QPS=1 限流锁，跨线程共享同一实例以正确限流。"""
    MIN_INTERVAL = 1.1              # 秒，QPS=1 留余量，避免 54003
    ENDPOINT = 'https://fanyi-api.baidu.com/api/trans/vip/translate'

    def __init__(self, appid, key):
        self.appid = appid
        self.key = key
        self.last = 0.0
        self.disabled_until = 0.0     # 失败后短暂冷却，避免反复空耗限流窗口
        self.lock = threading.Lock()

    def _throttle(self):
        with self.lock:
            wait = self.MIN_INTERVAL - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()

    def call(self, text, from_lang='jp'):
        """单次请求。任何百度错误码 / 网络异常都以 BatchError 抛出，
        由上层账号池切到下一个账号重试（限流、余额不足、鉴权错都不再本账号死等）。
        from_lang 默认 'jp'；'cht' 可用于繁->简转换。"""
        if acc_off('std', self.appid):
            raise BatchError('账号已人工停用 %s' % acct_tag(self.appid))
        _dump_q('std', text)
        self._throttle()
        rate_gate()
        note_req()
        qid = evt('qs', pool='std', acc=acct_tag(self.appid),
                  lines=text.count('\n') + 1, chars=len(text), lang=from_lang)
        t0 = time.time()
        try:
            salt = '%d%d' % (int(time.time() * 1000), random.randint(0, 9999))
            sign = hashlib.md5(
                (self.appid + text + salt + self.key).encode('utf-8')).hexdigest()
            data = urllib.parse.urlencode({
                'q': text, 'from': from_lang, 'to': 'zh',
                'appid': self.appid, 'salt': salt, 'sign': sign,
            }).encode('utf-8')
            req = urllib.request.Request(self.ENDPOINT, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read().decode('utf-8'))
            if 'error_code' in res:
                raise BatchError('百度 %s: %s'
                                 % (res['error_code'], res.get('error_msg', '')))
            outs = [t['dst'] for t in res.get('trans_result', [])]
        except Exception as e:
            evt('qr', q=qid, pool='std', acc=acct_tag(self.appid), ok=0,
                ms=int((time.time() - t0) * 1000), err=evt_snip(str(e)))
            raise
        evt('qr', q=qid, pool='std', acc=acct_tag(self.appid), ok=1,
            ms=int((time.time() - t0) * 1000), out=len(outs))
        return '\n'.join(outs)


# 账号池（模块级单例，跨线程共享）—— 保证单账号全局 QPS=1，多账号并行放大吞吐
BAIDU_POOL = [BaiduAccount(a, k) for a, k in BAIDU_ACCOUNTS]
_BAIDU_CURSOR = 0
_BAIDU_CURSOR_LOCK = threading.Lock()


def _next_baidu_account():
    """取下一个可用账号；全部被停用/冷却时返回 None（由调用方决定怎么办）。"""
    global _BAIDU_CURSOR
    acc_sync()
    now = time.time()
    with _BAIDU_CURSOR_LOCK:
        n = len(BAIDU_POOL)
        for step in range(n):
            idx = (_BAIDU_CURSOR + step) % n
            ac = BAIDU_POOL[idx]
            if now >= ac.disabled_until and not acc_off('std', ac.appid):
                _BAIDU_CURSOR = idx + 1
                return ac
        return None


class Baidu:
    """百度账号池：轮询多账号，单账号 QPS=1，N 账号 => 约 N×吞吐。
    任一账号限流(54003)/余额不足(54004)/异常都自动切下一个账号，不中断整批；
    失败账号进入 60s 冷却（激活/恢复后自动复用）。"""
    def call(self, text, retry=8):
        last = None
        for _ in range(retry):
            acct = _next_baidu_account()
            if acct is None:
                raise BatchError('百度池没有可用账号（全部人工停用或冷却中）')
            try:
                return acct.call(text)
            except BatchError as e:
                s = str(e)
                # 内容命中敏感词(20003)：百度对整批直接拒绝且不可重试，
                # 立即上交上层做拆行降级（只丢真正触发的行）。
                if '20003' in s or '敏感' in s or 'sensitive' in s.lower():
                    raise
                last = e
                if '人工停用' in s:
                    continue        # 人为关掉的号不冷却、不空等，直接换下一个
                acct.disabled_until = time.time() + (86400 if _hard_dead(s) else 60)
                time.sleep(0.3)
            except Exception as e:
                last = e
                acct.disabled_until = time.time() + 60
                time.sleep(0.3)
        raise BatchError('百度池全部账号失败: %s' % last)


# ---------------------------------------------------------------- 百度大模型翻译（账号池）
# 用户提供的大模型文本翻译 API Key（2026-09-21，含本次两个新账号）。
# 鉴权 = Authorization: Bearer <KEY>，body 仍需 appid（与该 key 同账号）。
# 质量显著优于通用机翻；标记已在送翻前切走，术语由 protect 层处理。
# 关键：百度 LLM API 的 q 必须是【字符串】（不能是数组，否则 53001 parse error）
#       —— 多行用 \n 拼接成一个字符串发送；API 返回 trans_result 数组（每行一份）。
BAIDU_LLM_HARDCODED = [
    # ---- 第 8 批（2026-09-21 最新，体检可用）----
    ('20260921002688969', 'Rzcg_daof109rnfi8h9dab5i0'),
    ('20260921002688985', '5DxX_daofav0f57ridk37qus0'),
    # ---- 第 7 批（20260114002539231：体检已开通，可用）----
    ('20260114002539231', '71s7_daodj3k665nr80kh6nqg'),
    # ---- 第 6 批（10 个，体检全部可用）----
    ('20260921002688815', 'zQtV_daobm8523l2nmlqf1ieg'),
    ('20260921002688814', 'LEfe_daobkjc665nr80kh6is0'),
    ('20260921002688813', 'jsCN_daobl4prnfi8h9daasp0'),
    ('20260921002688811', '7i9n_daobj2flqic8voc6op50'),
    ('20260921002688816', 'eHGs_daobl4dupokb0ptl3ot0'),
    ('20260921002688812', 'v8yN_daobjilmlv871va0eahg'),
    ('20260921002688820', 'FUj5_daobrgmlgkvkevn457s0'),
    ('20260921002688822', '2LEY_daobtds665nr80kh6jb0'),
    ('20260921002688830', '3xUX_daoc63gf57ridk37qlr0'),
    ('20260921002688873', 'nG0J_daodbsdupokb0ptl3tag'),
    # ↑ 19:57 实测：...3tag 有效（OK）；另一版本 ...3ot0 报 54001 invalid token，**勿换**
    # ---- 更早批次（2026-09-21 19:57 复测：仅 …796 可用；…765/…723/…726 与 2023…296 已 54004）----
    ('20260921002688765', 'mQ1k_daob1pl23l2nmlqf1gr0'),
    ('20260921002688796', 'RcAc_daoav60qedmctf16s44g'),
    ('20260921002688723', '3Lqw_daoar5rj9m46itcohsn0'),
    ('20260921002688726', 'xQWM_daoasn8f57ridk37qj00'),
    ('20230616001714296', 'fO5u_daoak0oqedmctf16s2o0'),   # 原始账号（开源 LLM key，余额可能不足）
]


def _load_llm_accounts():
    raw = os.environ.get('BAIDU_LLM_ACCOUNTS')
    base = BAIDU_LLM_HARDCODED
    if raw:
        try:
            lst = json.loads(raw)
            if isinstance(lst, list) and lst:
                base = [(str(a), str(k)) for a, k in lst]
        except Exception:
            pass
    return _acc_merge(base, 'llm')


BAIDU_LLM_ACCOUNTS = _load_llm_accounts()
BAIDU_LLM_EP = 'https://fanyi-api.baidu.com/ait/api/aiTextTranslate'


class BaiduLLMAccount:
    """单个百度大模型翻译账号：自带限流锁，跨线程共享以保证全局限流。
    q 必须发送为字符串（\n 分隔）。返回 trans_result 数组（每行一份 dst）。"""
    MIN_INTERVAL = 0.4          # 初始保守，bench 实测再调

    def __init__(self, appid, key):
        self.appid = appid
        self.key = key
        self.last = 0.0
        self.disabled_until = 0.0
        self.lock = threading.Lock()

    def _throttle(self):
        with self.lock:
            wait = self.MIN_INTERVAL - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()

    def call(self, text):
        if acc_off('llm', self.appid):
            raise BatchError('账号已人工停用 %s' % acct_tag(self.appid))
        _dump_q('llm', text)
        self._throttle()
        _global_throttle()
        rate_gate()
        note_req()
        qid = evt('qs', pool='llm', acc=acct_tag(self.appid),
                  lines=text.count('\n') + 1, chars=len(text))
        t0 = time.time()
        try:
            body = json.dumps(
                {'appid': self.appid, 'from': 'jp', 'to': 'zh', 'q': text},
                ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(
                BAIDU_LLM_EP, data=body,
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + self.key})
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.loads(r.read().decode('utf-8'))
            if 'error_code' in res:
                code = res['error_code']
                msg = res.get('error_msg', '')
                evt('qr', q=qid, pool='llm', acc=acct_tag(self.appid), ok=0,
                    ms=int((time.time() - t0) * 1000),
                    err=evt_snip('%s %s' % (code, msg)))
                if '20003' in str(code) or '敏感' in msg:
                    raise BatchError('百度LLM敏感词')
                raise BatchError('百度LLM %s: %s' % (code, msg))
            outs = [t.get('dst', '') for t in res.get('trans_result', [])]
        except BatchError:
            raise
        except Exception as e:
            evt('qr', q=qid, pool='llm', acc=acct_tag(self.appid), ok=0,
                ms=int((time.time() - t0) * 1000), err=evt_snip(str(e)))
            raise
        evt('qr', q=qid, pool='llm', acc=acct_tag(self.appid), ok=1,
            ms=int((time.time() - t0) * 1000), out=len(outs))
        return outs


BAIDU_LLM_POOL = [BaiduLLMAccount(a, k) for a, k in BAIDU_LLM_ACCOUNTS]
_BAIDU_LLM_CURSOR = 0
_BAIDU_LLM_CURSOR_LOCK = threading.Lock()

# 余额耗尽(54004)/服务未开通(58003)属**不可自愈**错误：每轮都撞一次是纯浪费
# （实测 15 个 LLM 账号里 3 个余额空，15 个标准账号里 8 个不可用，
#  轮询时每个坏账号都白烧一次重试，吞吐被拖到 1/4）。
_HARD_ERR = ('54004', '58003', '52003', '余额不足', 'recharge', 'service invalid')


def _hard_dead(err):
    return any(k in err for k in _HARD_ERR)


# ---------------------------------------------------------------- 残留兜底
# 实测（5046 行小样）：LLM 偶发把日文汉字词原样吐回，其中「大丈夫」16 次最突出。
# 假名残留仅 2 行、和制汉语残留集中在下面这张表里，故用确定性的后处理兜底，
# 不去动 protect 层——普通词若进术语表反而会打乱句法（会把"大丈夫"当名词）。
# 顺序要紧：长词必须排在短词之前（"俺様" 先于 "俺"，"一緒に" 先于 "一緒"）。
_POSTFIX = [
    (re.compile(r'真是个大丈夫'), '真是坚强'),
    (re.compile(r'真是大丈夫'), '真是坚强'),
    (re.compile(r'何为大丈夫'), '何谓坚强'),
    (re.compile(r'是个大丈夫'), '是个坚强的人'),
    (re.compile(r'是大丈夫'), '没事'),
    (re.compile(r'大丈夫\s*[！!？?]+'), '没事吧？'),
    (re.compile(r'大丈夫'), '没事'),
    (re.compile(r'俺様'), '本大爷'),
    (re.compile(r'俺'), '我'),
    (re.compile(r'綺麗'), '漂亮'),
    (re.compile(r'気持ち'), '心情'),
    (re.compile(r'気分'), '心情'),
    (re.compile(r'無理やり'), '硬是'),
    (re.compile(r'無理'), '不行'),
    (re.compile(r'一緒に'), '一起'),
    (re.compile(r'一緒'), '一起'),
    (re.compile(r'本当に'), '真的'),
    (re.compile(r'本当'), '真的'),
    (re.compile(r'心配'), '担心'),
    (re.compile(r'頑張って'), '加油'),
    (re.compile(r'頑張'), '努力'),
    (re.compile(r'素敵'), '很棒'),
    (re.compile(r'沢山'), '很多'),
    (re.compile(r'残念'), '遗憾'),
    (re.compile(r'仕方ない'), '没办法'),
    (re.compile(r'我慢'), '忍耐'),
    (re.compile(r'お花'), '小花'),
]


def postfix(text):
    """译文后处理：清掉 LLM 偶发照抄的日文汉字词。"""
    if not text:
        return text
    for pat, rep in _POSTFIX:
        if pat.search(text):
            text = pat.sub(rep, text)
    return text


def probe_accounts(verbose=True):
    """启动自检：并发探测两个池子，把不可自愈的账号在本进程内停用。"""
    import concurrent.futures as cf

    def _probe(kind, acct):
        try:
            acct.call('こんにちは')
            evt('probe', pool=kind, acc=acct_tag(acct.appid), ok=1)
            return kind, acct.appid, ''
        except Exception as e:
            evt('probe', pool=kind, acc=acct_tag(acct.appid), ok=0,
                err=evt_snip(str(e)))
            return kind, acct.appid, str(e)

    jobs = ([('llm', a) for a in BAIDU_LLM_POOL]
            + [('std', a) for a in BAIDU_POOL])
    stat = {}
    with cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        for kind, appid, err in ex.map(lambda j: _probe(*j), jobs):
            if not err:
                stat[kind] = stat.get(kind, (0, 0))
                stat[kind] = (stat[kind][0] + 1, stat[kind][1])
                continue
            stat[kind] = stat.get(kind, (0, 0))
            if _hard_dead(err):
                stat[kind] = (stat[kind][0], stat[kind][1] + 1)
                for ac in (BAIDU_LLM_POOL if kind == 'llm' else BAIDU_POOL):
                    if ac.appid == appid:
                        ac.disabled_until = time.time() + 86400
                if verbose:
                    print('  [自检停用] %s %s <- %s' % (kind, appid, err[:44]),
                          flush=True)
            else:
                stat[kind] = (stat[kind][0], stat[kind][1])
                if verbose:
                    print('  [自检告警] %s %s <- %s' % (kind, appid, err[:44]),
                          flush=True)
    for kind, (ok, dead) in stat.items():
        if verbose:
            name = '大模型池' if kind == 'llm' else '标准池'
            print('  %s: 可用 %d / 总数 %d' % (name, ok, ok + dead), flush=True)
    return stat


_GLOBAL_GAP = 0.34              # 全进程请求间隔下限（≈3 req/s）
_GLOBAL_LAST = 0.0
_GLOBAL_LOCK = threading.Lock()


def _global_throttle():
    """全进程级请求节流。

    为什么需要：并发 32 时突发流量会把服务端打成限速，表现是「11 个账号全部
    读超时」，BaiduLLM.call 重试 8 次全败 → 整批作废，实测一个全量任务因此
    空转 4 分 47 秒零产出。单账号 MIN_INTERVAL 只管住单个账号的节奏，管不住
    「多个账号同时被同一个 IP 打出去」这件事，所以要在进程出口再加一道。
    """
    global _GLOBAL_LAST
    with _GLOBAL_LOCK:
        wait = _GLOBAL_GAP - (time.time() - _GLOBAL_LAST)
        if wait > 0:
            time.sleep(wait)
        _GLOBAL_LAST = time.time()


_CONSEC_TO = 0                  # 连续读超时计数（跨线程共享）
_COOLDOWN_UNTIL = 0.0           # 全池冷却截止时刻
_TO_LOCK = threading.Lock()


def _note_timeout():
    global _CONSEC_TO
    with _TO_LOCK:
        _CONSEC_TO += 1
        return _CONSEC_TO


def _note_ok():
    global _CONSEC_TO
    with _TO_LOCK:
        _CONSEC_TO = 0


def _global_cooldown(seconds):
    """全池级冷却：所有线程一起等到同一个截止时刻。

    为什么需要：服务端限速时会出现「所有账号同时读超时」。若每个线程各自把
    8 次重试 × 25s 超时全烧完，一批就要空转 3 分钟且零产出（实测一个全量
    任务因此停摆，日志里连续两次「百度LLM池全部账号失败」）。
    改成「连续 3 次超时 → 全体暂停 20s 再继续」：既快速止血，又不放弃任务。
    """
    global _COOLDOWN_UNTIL
    with _TO_LOCK:
        now = time.time()
        if now >= _COOLDOWN_UNTIL:
            _COOLDOWN_UNTIL = now + seconds
            wait = seconds
        else:
            wait = _COOLDOWN_UNTIL - now
    if wait > 0:
        time.sleep(wait)


def _next_llm_account():
    """取下一个可用大模型账号；全部被停用/冷却时返回 None。"""
    global _BAIDU_LLM_CURSOR
    acc_sync()
    now = time.time()
    with _BAIDU_LLM_CURSOR_LOCK:
        n = len(BAIDU_LLM_POOL)
        for step in range(n):
            idx = (_BAIDU_LLM_CURSOR + step) % n
            ac = BAIDU_LLM_POOL[idx]
            if now >= ac.disabled_until and not acc_off('llm', ac.appid):
                _BAIDU_LLM_CURSOR = idx + 1
                return ac
        return None


class BaiduLLM:
    """百度大模型翻译账号池：质量远高于通用机翻。N 账号轮询放大吞吐，
    任一账号限流(54003)/鉴权错自动切下一个；敏感词(20003)上抛由调度层二分降级。"""
    def call(self, text, retry=6):
        lines = text.split('\n')
        n = len(lines)
        last = None
        for _ in range(retry):
            acct = _next_llm_account()
            if acct is None:
                raise BatchError('百度LLM池没有可用账号（全部人工停用或冷却中）')
            try:
                outs = acct.call(text)
                _note_ok()
                if len(outs) == n:
                    return '\n'.join(outs)
                if len(outs) == 1:
                    # 整段返回（API 有时把多行合并成一段，含 \n）：按 \n 拆回
                    parts = outs[0].split('\n')
                    if len(parts) == n:
                        return '\n'.join(parts)
                # 行数不符 -> 不在此抛错，返回拼接结果交调度层二分降级，
                # 否则整批作废；二分降级能保住干净行，只丢真正触发的行。
                return '\n'.join(outs)
            except BatchError as e:
                s = str(e)
                if '20003' in s or '敏感' in s:
                    raise
                last = e
                if '人工停用' in s:
                    continue        # 人为关掉的号不冷却、不空等，直接换下一个
                if _hard_dead(s):
                    acct.disabled_until = time.time() + 86400
                    evt('acct', pool='llm', acc=acct_tag(acct.appid), st='dead',
                        sec=86400, err=evt_snip(s))
                elif 'timed out' in s or '超时' in s:
                    # 读超时**不是账号故障**：早先按故障停用 60s，几个超时就把整池停掉，
                    # 接着每次重试都硬等一个 25s 超时（实测一个 20 行批卡 393s）。
                    # 现在只短暂避让；且连续 3 次超时即全池冷却 20s 后放弃本批，
                    # 让外层调度跳过这批去下批，避免把重试全烧在空转上。
                    acct.disabled_until = time.time() + 2
                    if _note_timeout() >= 3:
                        _note_ok()
                        evt('acct', pool='llm', acc='*', st='cool', sec=20,
                            err='连续超时·全池冷却')
                        _global_cooldown(20)
                        raise BatchError('百度LLM连续超时(疑似限速)，本批放弃: %s' % s)
                    time.sleep(0.2)
                else:
                    acct.disabled_until = time.time() + 60
                    evt('acct', pool='llm', acc=acct_tag(acct.appid), st='cool',
                        sec=60, err=evt_snip(s))
                    time.sleep(0.3)
            except Exception as e:
                last = e
                acct.disabled_until = time.time() + 2      # 网络异常：短暂避让
                time.sleep(0.2)
        raise BatchError('百度LLM池全部账号失败: %s' % last)


class Cht2Zh:
    """繁体 -> 简体（百度标准翻译 API，from=cht）。复用百度账号池。
    用于把参考仓库的台湾(繁体)人工译文转成简体，回填主线。"""
    def call(self, text, retry=6):
        last = None
        for i in range(retry):
            acct = _next_baidu_account()
            if acct is None:
                raise BatchError('百度池没有可用账号（全部人工停用或冷却中）')
            try:
                rate_gate()
                note_req()
                salt = '%d%d' % (int(time.time() * 1000), random.randint(0, 9999))
                sign = hashlib.md5(
                    (acct.appid + text + salt + acct.key).encode('utf-8')).hexdigest()
                data = urllib.parse.urlencode({
                    'q': text, 'from': 'cht', 'to': 'zh',
                    'appid': acct.appid, 'salt': salt, 'sign': sign,
                }).encode('utf-8')
                req = urllib.request.Request(acct.ENDPOINT, data=data, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read().decode('utf-8'))
                if 'error_code' in res:
                    code = res['error_code']
                    msg = res.get('error_msg', '')
                    if code in ('54003', '54005', '52001', '52002', '52003', '54000'):
                        last = '%s:%s' % (code, msg)
                        time.sleep(1.0 * (i + 1))
                        continue
                    raise BatchError('cht %s: %s' % (code, msg))
                return '\n'.join(t['dst'] for t in res.get('trans_result', []))
            except BatchError:
                raise
            except Exception as e:
                last = e
                if i + 1 < retry:
                    time.sleep(0.5 * (i + 1))
        raise BatchError('cht失败: %s' % last)


ENGINES = {'baidu': Baidu, 'baidu_llm': BaiduLLM, 'cht': Cht2Zh,
           'google': Google, 'bing': Bing}
# 百度默认并发 = 账号数 +1（保证每个账号都有线程喂，吞吐最大化），封顶 8
ENGINE_CFG['baidu']['DEF_WORKERS'] = max(2, min(len(BAIDU_ACCOUNTS) + 1, 8))
# 大模型通道刻意**压低并发**：实测服务端会惩罚并发——w=6/40行 得 17.7 行/秒，
# w=12/40行 反而掉到 3.9 行/秒，w=24/20行 只有 6.1 行/秒（并发下单请求延迟从
# 2-4s 膨胀到 60s+，重试 8 次全超时会让整批作废、任务空转数分钟）。
# 所以策略是「少并发 + 大批次」：请求数少了，队列不堆积，吞吐反而最高。
ENGINE_CFG['baidu_llm']['DEF_WORKERS'] = 6

# ---------------------------------------------------------------- 调速配置
# 「一次发多少请求、发多快」全在这里调（面板「引擎调速」卡 / `ctl.py cfg` 都写它）：
#   engine           走哪个通道（换引擎要重启进程）
#   workers          并发：同时在飞的请求数
#   batch_lines      单请求最多几行
#   batch_chars      单请求最多字符数
#   block_lines      每块行数（缓存落盘粒度，也是改配置的生效粒度上限）
#   round_lines      每轮行数上限（run_tagatame 读，下一轮生效）
#   sleep_ms         两次请求之间最小间隔（全局，压「发送请求次数」）
#   max_rpm          每分钟请求数上限（0 = 不限）
#   batch_pause_ms   每波批次之间暂停
#
# 文件 engine_cfg.json；引擎端 **mtime + 1 秒 TTL 热重载**：并发/批次改完，
# 最迟一波批次（≈25 秒）或一块（block_lines）内生效，不用重启进程。
RUN_CFG_PATH = os.path.join(BASE, 'engine_cfg.json')
DEFAULT_RUN_ENGINE = 'baidu_llm'
RUN_FIELDS = (
    ('workers',         1,     64, '并发（同时在飞的请求数）', ''),
    ('batch_lines',     1,    200, '单请求最多行数', '行'),
    ('batch_chars',   100,  20000, '单请求最多字符数', '字符'),
    ('block_lines',   200, 100000, '每块行数（配置生效粒度）', '行'),
    ('round_lines',   100, 200000, '每轮行数上限（下一轮生效）', '行'),
    ('sleep_ms',        0,  60000, '两次请求之间最小间隔', 'ms'),
    ('batch_pause_ms',  0,  60000, '每波批次之间暂停', 'ms'),
    ('max_rpm',         0,   6000, '每分钟请求上限（0=不限）', '次/分'),
)
RUN_PRESETS = (
    ('eco', '省号慢速', '低并发 + 长间隔；账号快挂时用',
     {'workers': 2, 'batch_lines': 20, 'sleep_ms': 500, 'max_rpm': 0}),
    ('gentle', '温和', '比标准低一档；5xx / 超时变多时用',
     {'workers': 4, 'batch_lines': 30, 'sleep_ms': 200, 'max_rpm': 0}),
    ('normal', '标准', '实测最优点（大模型通道）',
     {'workers': 6, 'batch_lines': 40, 'sleep_ms': 0, 'max_rpm': 0}),
    ('fast', '激进', '高并发大请求；大模型通道会惩罚并发，可能反而更慢',
     {'workers': 12, 'batch_lines': 50, 'sleep_ms': 0, 'max_rpm': 0}),
    ('limit', '限流保号', '并发不变，只把总量卡到 60 次/分',
     {'workers': 6, 'batch_lines': 40, 'sleep_ms': 0, 'max_rpm': 60}),
)
# ------------------------------------------------------ 通道载荷红线（护栏）
# 2026-09-22 实测：超时只看字符数不看行数
#   baidu_llm  677 字符 2.4s OK；1056 / 1286 / 1845 / 2454 全部精确 25 秒整超时
#   baidu      2454 字符 2.5s OK（40 行整批，掩码完好）
# 夹紧发生在 norm_run_cfg（**加载侧**），所以手改 engine_cfg.json 也绕不过。
ENGINE_CHAR_CAP = {'baidu_llm': 800, 'baidu': 6000}
CHAR_CAP_DEFAULT = 20000
_CFG_CAP_NOTE = ''


def char_cap(engine=None):
    """当前通道允许的单请求字符上限（红线）。"""
    eng = engine or load_run_cfg()['engine']
    return int(ENGINE_CHAR_CAP.get(eng, CHAR_CAP_DEFAULT))


def run_cfg_cap_note():
    """最近一次配置归一化里发生的「夹紧」说明（没有则空串）。"""
    return _CFG_CAP_NOTE


_CFG_STATE = {'t': 0.0, 'mt': None, 'd': None}
_CFG_LOCK = threading.RLock()
_RATE = {'lock': threading.Lock(), 'last': 0.0}
_REQ = {'n': 0, 'lock': threading.Lock()}


def note_req(k=1):
    """真实发出去的 HTTP 请求计数（含重试）。run_tagatame 每轮报增量。"""
    with _REQ['lock']:
        _REQ['n'] += k
    return _REQ['n']


def req_count():
    return _REQ['n']


def _run_dflt(engine, key):
    """引擎默认值：并发/批次沿用 ENGINE_CFG（实测调好的），其余取保守常量。"""
    c = ENGINE_CFG.get(engine) or ENGINE_CFG[DEFAULT_RUN_ENGINE]
    return {'workers': c['DEF_WORKERS'], 'batch_lines': c['MAX_LINES'],
            'batch_chars': c['MAX_CHARS'], 'block_lines': 2000,
            'round_lines': 20000}.get(key, 0)


def norm_run_cfg(raw, engine=None):
    """任意输入 -> 完整配置（字段全为可用的数字）。非法值就地夹到区间内。"""
    raw = dict(raw or {})
    eng = str(raw.get('engine') or engine or DEFAULT_RUN_ENGINE)
    if eng not in ENGINE_CFG:
        eng = DEFAULT_RUN_ENGINE
    out = {'engine': eng}
    for k, lo, hi, _t, _u in RUN_FIELDS:
        try:
            v = int(raw.get(k))
        except Exception:
            v = None
        if v is None:
            v = _run_dflt(eng, k)
        out[k] = max(lo, min(hi, int(v)))
    global _CFG_CAP_NOTE
    cap = int(ENGINE_CHAR_CAP.get(eng, CHAR_CAP_DEFAULT))
    if out['batch_chars'] > cap:
        _CFG_CAP_NOTE = ('batch_chars %d 超过 %s 通道红线，已夹到 %d'
                         '（不夹就是每次 25 秒整超时）'
                         % (out['batch_chars'], eng, cap))
        out['batch_chars'] = cap
    else:
        _CFG_CAP_NOTE = ''
    out['char_cap'] = cap
    out['note'] = str(raw.get('note') or '')[:200]
    out['updated_at'] = str(raw.get('updated_at') or '')
    return out


def read_run_cfg_file():
    try:
        with open(RUN_CFG_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_run_cfg(force=False):
    """热重载调速配置（mtime + 1 秒 TTL）。返回完整 dict，可直接取用。"""
    st = _CFG_STATE
    now = time.time()
    if not force and st['d'] is not None and now - st['t'] < 1.0:
        return st['d']
    st['t'] = now
    try:
        mt = os.path.getmtime(RUN_CFG_PATH)
    except Exception:
        mt = None
    if not force and st['d'] is not None and mt == st['mt']:
        return st['d']
    d = norm_run_cfg(read_run_cfg_file() if mt is not None else {})
    old = st['d']
    with _CFG_LOCK:
        st['mt'] = mt
        st['d'] = d
    if old is not None:
        diff = dict((k, '%s>%s' % (old.get(k), d.get(k)))
                    for k, _l, _h, _t, _u in RUN_FIELDS
                    if old.get(k) != d.get(k))
        if old.get('engine') != d.get('engine'):
            diff['engine'] = '%s>%s' % (old.get('engine'), d.get('engine'))
        if diff:
            evt('cfg', **diff)          # 实时窗口里能看到「谁改了什么」
    return d


def run_cfg_defaults(engine=None):
    eng = engine or load_run_cfg()['engine']
    if eng not in ENGINE_CFG:
        eng = DEFAULT_RUN_ENGINE
    d = {'engine': eng}
    for k, _l, _h, _t, _u in RUN_FIELDS:
        d[k] = _run_dflt(eng, k)
    # 默认值同样要过红线：界面显示的「本通道默认」必须等于真正会生效的值
    cap = int(ENGINE_CHAR_CAP.get(eng, CHAR_CAP_DEFAULT))
    if d['batch_chars'] > cap:
        d['batch_chars'] = cap
    d['char_cap'] = cap
    return d


def save_run_cfg(patch=None, **kw):
    """合并写回 engine_cfg.json。**先校验**：非法就直接拒绝，绝不写半个文件。
    返回 (ok, err, 新配置)。"""
    patch = dict(patch or {})
    patch.update(kw)
    bad = []
    names = [k for k, _l, _h, _t, _u in RUN_FIELDS]
    for k in sorted(patch):
        v = patch[k]
        if k in ('note', 'updated_at', 'by'):
            continue
        if k == 'engine':
            if str(v) not in ENGINE_CFG:
                bad.append('engine=%s（可选 %s）'
                           % (v, '/'.join(sorted(ENGINE_CFG))))
            continue
        if k not in names:
            bad.append('未知字段 %s' % k)
            continue
        lo, hi = [(l, h) for kk, l, h, _t, _u in RUN_FIELDS if kk == k][0]
        try:
            n = int(v)
        except Exception:
            bad.append('%s 不是整数' % k)
            continue
        if n < lo or n > hi:
            bad.append('%s 要在 %d~%d（收到 %d）' % (k, lo, hi, n))
    if bad:
        return (False, '；'.join(bad), load_run_cfg(force=True))
    cur = read_run_cfg_file()
    cur.update(patch)
    cur['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = '%s.%d.new' % (RUN_CFG_PATH, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cur, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, RUN_CFG_PATH)
    except Exception as e:
        return (False, '写配置失败：%r' % e, load_run_cfg(force=True))
    d = load_run_cfg(force=True)
    evt('cfgsave', set=json.dumps(patch, ensure_ascii=False)[:160])
    return (True, run_cfg_cap_note(), d)


def run_cfg_presets():
    return [{'key': k, 'name': n, 'desc': ds, 'values': dict(v)}
            for k, n, ds, v in RUN_PRESETS]


def _cfg_signature(c):
    """会被套用到运行中实例的字段签名（变了就重新套用）。"""
    return tuple([c['engine'], c['updated_at']]
                 + [c[k] for k, _l, _h, _t, _u in RUN_FIELDS])


def rate_gate():
    """全局节流闸：把「发请求的速率」压到配置值以下（两项都为 0 = 不额外限制）。

    gap = max(sleep_ms/1000, 60/max_rpm)，一把锁串行化 —— 于是并发再高，
    实际往外发的请求也被卡在 1/gap 以内。这就是「改发送请求次数」的那道闸。
    这是**额外**的一道：`_global_throttle()` 与单账号 `_throttle()` 照常生效。
    任何异常都吞掉：节流绝不能把翻译搞崩。
    """
    try:
        c = load_run_cfg()
        gap = 0.0
        if c['sleep_ms']:
            gap = max(gap, c['sleep_ms'] / 1000.0)
        if c['max_rpm']:
            gap = max(gap, 60.0 / float(c['max_rpm']))
        if gap <= 0:
            return
        with _RATE['lock']:
            wait = _RATE['last'] + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            _RATE['last'] = time.time()
    except Exception:
        return



# ---------------------------------------------------------------- 调度
_NONWORD = re.compile(r'[\u4e00-\u9fff\u3040-\u30fa\u30fc0-9A-Za-z]')
_PH_NUM = re.compile(r'ZQX\s*(\d+)\s*QXZ')


def _ph_multi(t):
    return collections.Counter(_PH_NUM.findall(t))


def _repair_split(units, outs):
    """翻译结果条目数 > 输入行数时的原位修补（替代盲二分重发）。

    实测病因：引擎偶尔把某行**行尾的标点**切成独立条目——例如 input[4] 的
    「（親睦を…って感じ？）」译文被拆成「…的感觉？」+「）」，于是 16 行进
    17 条，整批触发二分降级。16 行合批时约 40% 的批次会中一次，实测使请求
    数涨到理论值的 3.4 倍。

    修补：把「不含任何实词、长度 ≤4」的碎片条目并回相邻条目；再用
    【占位符多重集逐行比对 + 长度比】校验对齐结果，任一不符即判失败退回二分。
    """
    if len(outs) <= len(units):
        return None
    o = list(outs)
    i = 0
    while len(o) > len(units) and i < len(o):
        s = o[i]
        if len(s) <= 4 and not _NONWORD.search(s):
            if i > 0:
                o[i - 1] += s
                del o[i]
            else:
                o[1] = o[0] + o[1]
                del o[0]
            continue
        i += 1
    if len(o) != len(units):
        return None
    for u, x in zip(units, o):
        if _ph_multi(u) != _ph_multi(x):
            return None
        if x and not (0.15 <= float(len(x)) / max(len(u), 1) <= 3.0):
            return None
    return o


LINE_BASE = 8000          # 行首哨兵编号基址   ZQX(8000+i+1)QXZ
LINE_Q = 8500             # 行尾哨兵编号基址   ZQX(8500+i+1)QXZ
SENT_LO, SENT_HI = 8001, 8699     # 哨兵编号区间（与术语 0..999 / <br> 9000+ 不重叠）
_SENT = re.compile(r'ZQX\s*(\d+)\s*QXZ')


def _is_sent(n):
    return SENT_LO <= n <= SENT_HI


def add_line_sentinels(units):
    """给每行**首尾各**追加一个批内唯一哨兵。

    为什么需要：实测引擎会在句末标点处把**一条输入行的译文切成两条**
    （如「うん。聞いてる…ZQX9001QXZすごくいい子…」→「嗯。」+「听着呢…」），
    之后所有条目整体错位、末尾多出一条——这才是「行数不符」的真因（不是折行、
    不是占位符、不是长度）。

    为什么首尾都要：单侧哨兵实测丢失率 6.7%，丢一个就得把相邻两行合成一组、
    再按长度比近似切（约 10% 的行边界会不精确）。首尾各一个哨兵后，只要
    任意一个存活即可定界，≈0.45% 才会退化成近似切分。
    """
    sent, msets = [], []
    for i, u in enumerate(units):
        p, q = LINE_BASE + i + 1, LINE_Q + i + 1
        sent.append('%s%d%s%s%s%d%s'
                    % (PH_L, p, PH_R, u, PH_L, q, PH_R))
        m = _ph_multi(u)
        m[str(p)] += 1
        m[str(q)] += 1
        msets.append(m)
    return sent, msets


def realign_sentinel(units, msets, outs):
    """按首尾哨兵把输出条目重新分组到输入行。

    锚点式（关键）：不要求每行哨兵都在——**存在的哨兵就是切点**。
    对条目 e 定义 close(e) = max{ j : Q_j∈e } ∪ { j-1 : P_j∈e }，
    即「行尾哨兵 j 出现」或「行首哨兵 j 出现 → 行 j-1 已结束」；
    取 ≥ 当前行的最大值做单调推进，某行哨兵全丢时就与邻行同组、按长度比切开。

    整组用【内容占位符频次】校验（排除哨兵），能挡住错位分配。
    返回 (逐行译文, 近似切分组数)；无法判定返回 (None, 0)。
    """
    n = len(units)
    if n < 2:
        return None, 0
    content = [collections.Counter({k: v for k, v in msets[i].items()
                                    if not _is_sent(int(k))}) for i in range(n)]
    cuts = []
    next_line = 0
    for e, o in enumerate(outs):
        idx = [int(x) for x in _PH_NUM.findall(o)]
        cand = set()
        for v in idx:
            if LINE_Q + 1 <= v <= LINE_Q + n:
                cand.add(v - LINE_Q - 1)              # 行尾哨兵 -> 行 j 结束
            elif LINE_BASE + 1 <= v <= LINE_BASE + n:
                cand.add(v - LINE_BASE - 2)           # 行首哨兵 -> 行 j-1 结束
        cand = [c for c in cand if c >= next_line]
        if cand:
            j = max(cand)
            if j >= n:
                return None, 0
            cuts.append((e, j))
            next_line = j + 1
    if next_line < n and cuts:
        cuts.append((len(outs) - 1, n - 1))
    elif not cuts:
        return None, 0
    groups, prev_e, prev_line = [], -1, -1
    for e, j in cuts:
        if j < prev_line:
            return None, 0
        groups.append((prev_e + 1, e, prev_line + 1, j))
        prev_e, prev_line = e, j
    if not groups or groups[-1][3] != n - 1:
        return None, 0
    res, approx = [], 0
    for a, b, lo, hi in groups:
        buf = ''.join(outs[a:b + 1])
        want = collections.Counter()
        for i in range(lo, hi + 1):
            want += content[i]
        got = collections.Counter({k: v for k, v in _ph_multi(buf).items()
                                   if not _is_sent(int(k))})
        if got != want:
            return None, 0
        if lo == hi:
            res.append(buf)
        else:
            parts = _split_by_ratio(buf, units[lo:hi + 1])
            if parts is None:
                return None, 0
            res.extend(parts)
            approx += 1
    return res, approx


_SPLIT_PUNCT = '，。！？…、；：'


def _split_by_ratio(text, units):
    """把一段合并译文按各源行长度占比切开（仅在哨兵全丢致两行被夹在一组时使用）。

    ⚠️ 切点绝不能落在占位符内部：切出「ZQX8003」+「QXZ…」会让 restore 认不出
    占位符，残留的 ZQX 会被污染闸门整行丢掉。所以先把占位符内部标为禁区，
    再优先吸附到句末标点。
    """
    k = len(units)
    if k < 2 or not text:
        return None
    bad = set()
    for m in _SENT.finditer(text):
        bad.update(range(m.start() + 1, m.end()))
    total = float(sum(len(u) for u in units))
    if total <= 0:
        return None
    L = len(text)
    parts, pos = [], 0
    for i in range(k - 1):
        target = pos + int(round(L * len(units[i]) / total))
        target = max(pos + 1, min(L - (k - 1 - i), target))
        punct = safe = -1
        for cand in range(max(pos + 1, target - 12), min(L, target + 13)):
            if cand in bad:
                continue
            if safe < 0:
                safe = cand
            if text[cand - 1] in _SPLIT_PUNCT:
                punct = cand
                break
        if punct < 0:
            for d in range(13, 61):
                for cand in (target + d, target - d):
                    if pos < cand < L and cand not in bad:
                        safe = cand
                        break
                if safe > 0 and not (max(pos + 1, target - 12) <= safe
                                     < min(L, target + 13)):
                    break
        cut = punct if punct > 0 else safe
        if cut <= 0 or cut >= L:
            return None
        parts.append(text[pos:cut])
        pos = cut
    parts.append(text[pos:])
    if any(len(p) == 0 for p in parts):
        return None
    return parts


class Translator:
    def __init__(self, workers=None, cache=None, verbose=True,
                 engine=DEFAULT_ENGINE):
        cfg = ENGINE_CFG[engine]
        self.engine = engine
        self.workers = workers or cfg['DEF_WORKERS']
        self.max_lines = cfg['MAX_LINES']
        self.max_chars = cfg['MAX_CHARS']
        # 调速：调用方显式给的 workers 优先；否则每波批次重读 engine_cfg.json
        self.auto_workers = workers is None
        self.block_lines = 2000
        self.batch_pause_ms = 0
        self._cfg_sig = None
        self._apply_run_cfg()
        self.cache = cache or Cache()
        self.verbose = verbose
        # 整行送翻（保留 <br>）仅对大模型通道开启：语境完整、质量最好；
        # 标记守恒不符会自动退回片段级（见 translate_block）。
        self.whole_line = engine in ('baidu_llm',)
        self.line_sentinel = engine in ('baidu_llm',)
        self.pairs = load_tm()
        self.prot = build_protect(self.pairs)
        # 术语表指纹：掺进缓存键，术语一改，旧译文自动失效。
        # （踩坑：缓存键原为原文 sha1，改术语表后命中旧条目 -> 译名永远不更新）
        self.term_sig = hashlib.sha1(
            '\x01'.join('%s=%s' % (k, v)
                        for k, v in sorted(self.pairs.items()))
            .encode('utf-8')).hexdigest()[:16]
        self._local = threading.local()
        # 首次构造时做一次账号自检（每进程一次），把余额/服务死号剔出轮询
        global _PROBED
        if engine in ('baidu', 'baidu_llm', 'cht') and not _PROBED:
            _PROBED = True
            if self.verbose:
                print('账号池自检...', flush=True)
            probe_accounts(verbose=self.verbose)
        # 用 Counter：新增统计项不会因漏初始化而 KeyError 崩掉整批
        self.stats = collections.Counter(
            {'api': 0, 'cache': 0, 'tm': 0, 'fail': 0, 'char': 0,
             'polluted': 0, 'markup_lost': 0, 'seg': 0,
             'whole': 0, 'markup_retry': 0, 'aligned': 0,
             'realign': 0, 'realign_approx': 0, 'bisect': 0,
             'sensitive': 0, 'sens_skip': 0, 'sens_manual': 0})
        self.lock = threading.Lock()

    def eng(self):
        if not hasattr(self._local, 'eng'):
            self._local.eng = ENGINES[self.engine]()
        return self._local.eng

    # --- 调速配置：构造时套一次，之后每波批次/每块刷新 ---
    def _apply_run_cfg(self):
        """把 engine_cfg.json 的值落到本实例。

        只有配置里的 engine 与**本实例的引擎**一致才套用：否则
        `Translator(engine='google')` 做 bench 会被大模型通道的批次参数污染。
        """
        c = load_run_cfg()
        # 签名取**全部会被套用的字段**：光比 updated_at 不够——它只到秒，
        # 同一秒内连改两次就认不出来了（mtime 与值都比一遍才稳）。
        self._cfg_sig = _cfg_signature(c)
        if c['engine'] != self.engine:
            return c
        if self.auto_workers:
            self.workers = c['workers']
        self.max_lines = c['batch_lines']
        self.max_chars = c['batch_chars']
        self.block_lines = c['block_lines']
        self.batch_pause_ms = c['batch_pause_ms']
        return c

    def _peek_run_cfg(self):
        """看一眼配置有没有被人改（load_run_cfg 自带 1 秒 TTL，很便宜）。"""
        c = load_run_cfg()
        if _cfg_signature(c) != self._cfg_sig:
            self._apply_run_cfg()
        return c

    # --- 单批：已保护的多行文本 -> 译文列表 ---
    def _batch(self, lines, origs=None):
        """origs: 与 lines 同序的「保护前原文」，仅用于把敏感行登记进人工清单。
        百度 20003 是内容级判定（与账号无关），确诊后永久跳过、留人工翻译。"""
        msets = None
        if self.line_sentinel and len(lines) > 1:
            lines_to_send, msets = add_line_sentinels(lines)
        else:
            lines_to_send = lines
        joined = '\n'.join(lines_to_send)
        try:
            raw = self.eng().call(joined)
        except BatchError as e:
            msg = str(e)
            # 命中敏感词：百度对整批(含任一触发行)直接拒绝。二分拆小，
            # 干净的一半立即成功，只剩真正触发的单行——确诊后登记人工清单。
            if '20003' in msg or '敏感' in msg or 'sensitive' in msg.lower():
                if len(lines) == 1:
                    with self.lock:
                        self.stats['fail'] += 1
                        self.stats['sensitive'] += 1
                    mark_sensitive(origs[0] if origs else lines[0])
                    return [None]
                mid = len(lines) // 2
                return (self._batch(lines[:mid], origs[:mid] if origs else None)
                        + self._batch(lines[mid:],
                                      origs[mid:] if origs else None))
            raise
        out = raw.split('\n')
        with self.lock:
            self.stats['api'] += 1
            self.stats['char'] += len(joined)
        # 用了哨兵就**一定**要重分组：条目数相等也可能是「一行被切、另一行被并」
        # 的巧合，直接按序取用会把译文整段错位。
        if msets is not None:
            fixed, approx = realign_sentinel(lines, msets, out)
            if fixed is not None:
                with self.lock:
                    self.stats['realign' if approx == 0
                               else 'realign_approx'] += 1
                return fixed
        if len(out) != len(lines):
            fixed = None
            if len(out) > len(lines):
                fixed = _repair_split(lines, out)
                if fixed is not None:
                    with self.lock:
                        self.stats['aligned'] += 1
            if fixed is not None:
                return fixed
            if len(lines) == 1:
                # 单行输入却返回多行：LLM 偶尔在译文里插入换行符，合并回单行即可
                # （片段级翻译已把 <br> 切走，单段内本不应有换行；仅当全空才视为真失败）
                if not ''.join(out).strip():
                    raise BatchError('单行返回空')
                return [''.join(out)]
            # 二分降级
            with self.lock:
                self.stats['bisect'] += 1
            mid = len(lines) // 2
            return (self._batch(lines[:mid], origs[:mid] if origs else None)
                    + self._batch(lines[mid:], origs[mid:] if origs else None))
        return out

    def _run_batches(self, units, unit_idx, sink, origs=None):
        """units: 待翻文本列表; unit_idx: 对应下标; sink: 结果写入回调(i, text)
        origs: 与 units 同序的保护前原文（可选；用于敏感行登记人工清单，
               否则清单里只会留下带占位符的内部文本，人工看不懂）
        合批 -> 并发 -> 失败整批作废（不做 zip 截断）"""
        def one(b_idx):
            b, idx, org = b_idx
            try:
                return self._batch(b, org or None)
            except Exception as e:
                return e

        # 边切批边发：一波 = workers 个批次。切批前重读配置，所以
        # **并发与批次大小都能在翻译进行中改**（最迟一波 ≈25 秒生效），
        # 不用重启进程、不用等这一轮跑完。
        total = len(units)
        pos = 0
        while pos < total:
            self._peek_run_cfg()
            W = max(1, int(self.workers))
            ml, mc = max(1, int(self.max_lines)), max(1, int(self.max_chars))
            wave = []
            for _ in range(W):
                if pos >= total:
                    break
                b, bidx, borig, sz = [], [], [], 0
                while pos < total:
                    u = units[pos]
                    if b and (sz + len(u) > mc or len(b) >= ml):
                        break
                    b.append(u)
                    bidx.append(unit_idx[pos])
                    if origs is not None:
                        borig.append(origs[pos])
                    sz += len(u)
                    pos += 1
                wave.append((b, bidx, borig))
            if not wave:
                break
            if W <= 1 or len(wave) == 1:
                outs = []
                for b_idx in wave:
                    b, idx, org = b_idx
                    try:
                        outs.append(self._batch(b, org or None))
                    except Exception as e:
                        outs.append(e)
            else:
                with ThreadPoolExecutor(max_workers=min(W, len(wave))) as ex:
                    outs = list(ex.map(one, wave))
            for (b, idx, org), o in zip(wave, outs):
                self._absorb(o, idx, sink)
            if self.batch_pause_ms and pos < total:
                time.sleep(self.batch_pause_ms / 1000.0)

    def _absorb(self, outs, idx, sink):
        if isinstance(outs, Exception):
            with self.lock:
                self.stats['fail'] += len(idx)
            if self.verbose:
                print('  [批次失败 %d 条] %s' % (len(idx), outs), flush=True)
            for i in idx:
                sink(i, None)
            return
        if outs is None or len(outs) != len(idx):
            with self.lock:
                self.stats['fail'] += len(idx)
            for i in idx:
                sink(i, None)
            return
        for i, o in zip(idx, outs):
            sink(i, o)

    def _lkey(self, s):
        """缓存键：术语指纹 + 文本。术语表变更 -> 键变更 -> 旧条目自然失效"""
        return hashlib.sha1((self.term_sig + '\x00' + s)
                            .encode('utf-8')).hexdigest()

    @staticmethod
    def _raw_key(s):
        """旧版键（不含指纹），仅用于「该行不含术语」时的兼容命中"""
        return hashlib.sha1(s.encode('utf-8')).hexdigest()

    def _term_hit(self, s):
        """该行是否含受保护术语（含则不得用旧键，否则译名会停在改前版本）"""
        return any(jp in s for jp, _ in self.prot)

    def translate_block(self, lines):
        """lines: 原始日文行列表 -> 中文行列表。

        两轮策略：
          ① 整行送翻——把 <br> 等结构标记掩成占位符后整行送引擎，语境完整；
             掩码让换行位置逐字守恒（实测 24/24），质量最好。
          ② 片段级——标记切走再拼回，位置 100% 精确，作为①的兜底。
        ①是否启用由 self.whole_line 控制（仅对大模型通道开启）。"""
        if not lines:
            return []
        result = [None] * len(lines)
        todo = []
        for i, s in enumerate(lines):
            # 已确诊敏感的行：有人工译文就直接用，没有就跳过——不再送百度，
            # 既不白烧配额，也不会每轮重来一次（否则敏感行多的轮次会被拖死）。
            hit, mcn = sens_state(s)
            if hit:
                if mcn:
                    result[i] = mcn
                    with self.lock:
                        self.stats['sens_manual'] += 1
                else:
                    with self.lock:
                        self.stats['sens_skip'] += 1
                continue
            c = self.cache.get(self._lkey(s))
            if c is None and not self._term_hit(s):
                c = self.cache.get(self._raw_key(s))     # 无术语行可用旧键
            if c is not None:
                result[i] = c
                with self.lock:
                    self.stats['cache'] += 1
            else:
                todo.append(i)
        if not todo:
            return result

        if self.whole_line:
            units, idxs, pmap, origs = [], [], {}, []
            frag_first = []
            for i in todo:
                # 含已登记敏感片段的行：整行送百度必被 20003 拒（白烧请求），
                # 直接走片段级 —— 那里能从清单取到译文，整行反而拼得出来。
                if sens_frag_hit(lines[i]):
                    frag_first.append(i)
                    continue
                ps, slots = protect(lines[i], self.prot)
                ps, _ = mask_struct(ps, slots)
                units.append(ps)
                idxs.append(i)
                pmap[i] = slots
                origs.append(lines[i])
            got = {}

            def sink(i, o):
                got[i] = o
            self._run_batches(units, idxs, sink, origs)
            with self.lock:
                self.stats['whole'] += len(units)
            fallback = []
            for i in todo:
                o = got.get(i)
                if o is None:
                    # 已被百度内容审核判掉（本次刚登记）-> 直接留空等人工，
                    # 不再退片段级重发：同一行送回百度只会再吃一次 20003。
                    hit, _m = sens_state(lines[i])
                    if hit:
                        with self.lock:
                            self.stats['sens_skip'] += 1
                        continue
                cn = restore(o, pmap[i], self.pairs) if o is not None else None
                if cn is not None:
                    cn = cleanup_breaks(unmask_struct(cn))
                if cn is None or PH_L in cn or PH_R in cn:
                    fallback.append(i)
                    continue
                a = collections.Counter(STRUCT_RE.findall(lines[i]))
                b = collections.Counter(STRUCT_RE.findall(cn))
                if a != b:                       # 标记被改写/丢失 -> 退片段级
                    with self.lock:
                        self.stats['markup_retry'] += 1
                    fallback.append(i)
                    continue
                result[i] = postfix(cn)
                self.cache.put(self._lkey(lines[i]), result[i])
                evt('ln', jp=evt_snip(lines[i]), cn=evt_snip(result[i]),
                    via='whole')
            todo = fallback + frag_first

        if todo:
            self._translate_segments(lines, todo, result)
        return result

    def _translate_segments(self, lines, todo, result):
        """片段级：标记切走->合批翻译->拼回。标记位置 100% 精确（兜底路径）"""
        seg_units, seg_key = [], []      # 待翻片段文本 / 归属 key(line_idx, seg_pos)
        seg_orig = []                    # 对应片段原文（敏感行登记用，人工要看懂）
        seg_ps_map = {}                  # (line_idx, seg_pos) -> 保护后片段原文
        per_line = {}                    # line_idx -> (marks, slots_list, prot_texts)
        for i in todo:
            s = lines[i]
            c = self.cache.get(self._lkey(s))
            if c is None and not self._term_hit(s):
                c = self.cache.get(self._raw_key(s))     # 无术语行可用旧键
            if c is not None:
                result[i] = c
                with self.lock:
                    self.stats['cache'] += 1
                continue
            texts, marks = split_markup(s)
            prot_texts, slots_list = [], []
            for j, t in enumerate(texts):
                ps, slots = protect(t, self.prot)
                prot_texts.append(ps)
                slots_list.append(slots)
                ck = self._lkey(ps)
                cached = self.cache.get(ck)
                # 跳过被污染的缓存条目（旧版曾把未还原的占位符存进缓存，
                # 复用脏值会永久丢弃该行）——脏值视为未命中，重新送翻。
                if cached is not None and PH_L not in cached and PH_R not in cached:
                    prot_texts[j] = cached      # 直接放译文，无需再送翻
                    with self.lock:
                        self.stats['cache'] += 1
                elif t.strip():
                    # 已确诊的敏感片段：有译文（人工 cn 优先，其次机翻 mt）
                    # 就直接用，别再送百度（送一次拒一次）；没有则留空，
                    # 整行保留日文原文等人工。
                    hit_s, scn = sens_state(t)
                    if hit_s:
                        prot_texts[j] = scn if scn else None
                        with self.lock:
                            if scn:
                                self.stats['sens_manual'] += 1
                            else:
                                self.stats['sens_skip'] += 1
                        continue
                    seg_units.append(ps)
                    seg_key.append((i, j))
                    seg_ps_map[(i, j)] = ps
                    seg_orig.append(t)
            per_line[i] = (marks, slots_list, prot_texts, texts)
        if seg_units:
            seg_out = {}

            def sink(i, o):
                seg_out[i] = o
            self._run_batches(seg_units, seg_key, sink, seg_orig)
            with self.lock:
                self.stats['seg'] += len(seg_units)
            for k in seg_key:
                i, j = k
                o = seg_out.get(k)
                marks, slots_list, prot_texts, texts = per_line[i]
                if o is None:
                    prot_texts[j] = None
                    continue
                cn = restore(o, slots_list[j], self.pairs)
                if PH_L not in cn and PH_R not in cn:   # 污染片段不入库，避免缓存投毒
                    cn = postfix(cn)                    # 后处理必须在写缓存之前
                    self.cache.put(self._lkey(seg_ps_map[k]), cn)
                prot_texts[j] = cn
        for i, (marks, slots_list, prot_texts, texts) in per_line.items():
            if any(p is None for p in prot_texts):
                with self.lock:
                    self.stats['fail'] += 1
                result[i] = None
                continue
            cn = join_markup(prot_texts, marks)
            result[i] = self._finish(lines[i], cn)
            if result[i]:
                evt('ln', jp=evt_snip(lines[i]), cn=evt_snip(result[i]),
                    via='seg')
        return result

    def _finish(self, src, cn):
        """两道判据，任一不满足即丢弃该条（保留原文，绝不静默产出坏成品）：
           1) 占位符残留（引擎改写了占位符且没还原干净）
           2) 游戏标记守恒（拼回法下理论恒成立，仍校验防回归）
        """
        if cn is None:
            return None
        if PH_L in cn or PH_R in cn:
            with self.lock:
                self.stats['fail'] += 1
                self.stats['polluted'] += 1
            if self.verbose:
                print('  [污染丢弃] %s' % cn[:60], flush=True)
            return None
        a = collections.Counter(MARKUP_RE.findall(src))
        b = collections.Counter(MARKUP_RE.findall(cn))
        if a != b:
            with self.lock:
                self.stats['fail'] += 1
                self.stats['markup_lost'] += 1
            if self.verbose:
                print('  [标记丢失] 丢弃一条 (%s -> %s)'
                      % (dict(a), dict(b)), flush=True)
            return None
        cn = postfix(cn)
        if src:
            self.cache.put(self._lkey(src), cn)
        return cn

    def translate(self, lines):
        """对外主入口，分块处理以控制内存与失败半径"""
        out = []
        N = len(lines)
        st = 0
        while st < N:
            self._peek_run_cfg()           # 每块都认最新配置（block_lines 可调小）
            step = max(200, int(self.block_lines or 2000))
            chunk = lines[st:st + step]
            res = self.translate_block(chunk)
            out.extend(res)
            self.cache.save()      # 每块落盘：长任务中途挂掉也能断点续跑
            st += step
            if self.verbose and N > step:
                print('  ... %d/%d' % (min(st, N), N), flush=True)
        return out


# ---------------------------------------------------------------- CLI
def selftest():
    tr = Translator(workers=4)
    samples = [
        'エンヴィリア最強の騎士団『蒼炎騎士団』の騎士見習い。',
        '蒼炎騎士団の団長。',
        'ウロボロスの力を宿す者。',
        'リズベットとアルマの親友。',
        '砂漠地帯で生まれ育った。',
        '光騎士の月に生まれる。',
        'ロギと一緒に旅をする。',
        'アガサの作るシチューが好き。',
        'ズルくない！<br>…私なりに、ワダツミの未来を<br>考えた結果なんだからね',
    ]
    print('=== 引擎样张（含 <br> 行）===')
    out = tr.translate(samples)
    for a, b in zip(samples, out):
        tag = ''
        if a.count('<br>') != (b or '').count('<br>'):
            tag = '  !! 标记丢失'
        print('  %s\n    -> %s%s' % (a[:52], b, tag))
    print('\n=== 负向测试：行数守恒闸门 ===')

    t2 = Translator(workers=1)

    class FixedWrong:
        """无论输入几行，恒定返回 3 行 —— 必须被闸门拦下"""
        def call(self, text):
            return '译\n译\n译'

    t2._local.eng = FixedWrong()
    try:
        r = t2._batch(['あ'])
        print('  [单行] 结果:', r, ' !! 未拦截 —— 危险')
    except BatchError as ex:
        print('  [单行] 闸门拦截 OK:', ex)
    try:
        r = t2._batch(['あ', 'い', 'う', 'え'])
        print('  [4行]  结果:', r, ' !! 未拦截 —— 危险')
    except BatchError as ex:
        print('  [4行]  闸门拦截 OK:', ex)

    class Correct:
        def call(self, text):
            return '\n'.join('译' for _ in text.split('\n'))
    t3 = Translator(workers=1)
    t3._local.eng = Correct()
    ok = t3._batch(['あ', 'い', 'う', 'え'])
    print('  [正常] 返回 %d 行，与输入一致 -> %s'
          % (len(ok), 'OK' if len(ok) == 4 else 'FAIL'))

    print('\n=== 负向测试：污染（引擎改写占位符后残留）===')
    t4 = Translator(workers=1)

    class Pollute:
        def call(self, text):
            return 'P' + text + 'Z'      # 保留占位符且加前后缀
    t4._local.eng = Pollute()
    r = t4.translate_block(['エンヴィリアの剣士。'])
    print('  结果:', r, '->', 'OK(已丢弃)' if r[0] is None else '!! 未拦截 危险')

    print('\n统计:', dict(tr.stats))


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == 'selftest':
        selftest()
    elif args and args[0] == 'bench':
        import glob
        JP = os.path.join(os.path.dirname(BASE), '_extract', '剧情文档',
                          'Loc', 'japanese')

        def has_kana(s):
            return any((0x3040 <= ord(c) <= 0x3096)
                       or (0x30A0 <= ord(c) <= 0x30FA) for c in s)
        pool = []
        for p in sorted(glob.glob(JP + '/*.txt'))[3000:3600]:
            for l in open(p, encoding='utf-8', errors='replace'):
                f = l.rstrip('\n').split('\t')
                t = f[1] if len(f) > 1 else ''
                if t and has_kana(t):
                    pool.append(t)
            if len(pool) >= 1200:
                break
        eng = args[1] if len(args) > 1 else DEFAULT_ENGINE
        w = int(args[2]) if len(args) > 2 else ENGINE_CFG[eng]['DEF_WORKERS']
        tr = Translator(workers=w, engine=eng)
        t0 = time.time()
        res = tr.translate(pool[:1200])
        dt = time.time() - t0
        ok = sum(1 for r in res if r)
        print('engine=%s workers=%d: %d 行 %5.1fs  %.1f 行/秒  成功 %d/%d'
              % (eng, w, len(pool[:1200]), dt, len(pool[:1200]) / dt, ok,
                 len(pool[:1200])))
        print('  13 万行预计 %.2f 小时' % (130000 / (len(pool[:1200]) / dt) / 3600))
        print('  统计:', dict(tr.stats))
        tr.cache.save()
    else:
        print('用法: mt.py selftest | mt.py bench [google|bing] [workers]')
