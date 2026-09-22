# -*- coding: utf-8 -*-
"""隔离行机翻（谷歌 / 必应免费通道）—— 机器初翻底稿，等人工审核。

背景：百度大模型通道的内容审核（20003）会按**字面词表**稳定拒收某些行，
与账号无关，隔离后产物里只剩日文原文。这里改用**别的**翻译通道给这些行
生成一份机器初翻底稿，写进 sensitive.json 的 mt 字段：

    cn  = 人工译文（最高优先级，人工审核后填入）
    mt  = 机器初翻草稿（谷歌 / 必应，供人工校对润色用）

引擎取值顺序 cn > mt —— 产物里至少是中文，人工只需"审校"而不必"从零翻"。

换行守恒（<br>）：
  * 谷歌/腾讯的网页接口会把 <br> 当标签吃掉（实测整句合并成一行），
    但把 <br> 换成 \\n 再送，实测换行位置逐字守恒；
  * ① 整行送翻（保住整句语境）→ 换行数不对就 ② 逐段送翻（个数 100% 精确）。
  与引擎 mt.py 的「整行 / 片段级」两轮策略同思路。

术语：送翻前把 tm.json 的钉死译名掩成占位符，译后换回中文 —— 否则谷歌会把
      タガタメ 翻成「塔格塔姆」、シナイ 之类专名也各翻各的，与全作译名不一致。

古语台词：反派腔整行片假名（ソノ覚悟無キ貴様二…）谷歌直接翻是乱码，识别后先
      降为平假名再送翻；普通台词不受影响（判据见 is_archaic）。

通道（按顺序试，失败自动落到下一个）：
      google  translate.googleapis.com     实测可用，质量最好
      tencent transmart.qq.com/api/imt     国内可直连，实测 <br> 守恒
      bing    edge.microsoft.com/translate/auth
              —— 实测本机 404 / 空 body（网络到不了），只在前面都失败时白试一次

用法：
  python sens_mt.py --stats            看隔离清单与机翻覆盖情况
  python sens_mt.py --mt               给「无人工译文且无机翻」的行补机翻
  python sens_mt.py --mt --force       全部重翻（含已有草稿的行）
  python sens_mt.py --mt --one 原文    只翻一条（调试用）
  python sens_mt.py --mt --dry         只看会翻哪些，不写文件
"""
import io
import json
import os
import re
import sys
import time
import threading
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, '解包', '汉化')
SENSF = os.environ.get('MT_SENS') or os.path.join(D, 'sensitive.json')
TMF = os.path.join(D, 'tm.json')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0 Safari/537.36')
BR_RE = re.compile(r'<br\s*/?>', re.I)
_SENS_LOCK = threading.Lock()
_TERMS = {'pairs': None, 'at': 0.0}

MAX_CHARS = 1800        # 谷歌单次请求约 5000 字符上限，留足余量

# 古语台词（反派腔）整行片假名（ソノ覚悟無キ貴様二…），谷歌直接翻是乱码。
# 识别特征：片假名占假名比 >= 40% 且带片假名副词（ヲ/ハ/ガ/バ）—— 普通台词
# 里 シナイ山 / 火を噴く聖山シナイ 这类专名行比例只有 0.25~0.5 且无片假名副词，
# 不会被误判（误判会把「西奈」变成「しない」）。命中则整行片假名降为平假名再送翻。
KATA_RE = re.compile(r'[\u30a1-\u30f6]')
HIRA_RE = re.compile(r'[\u3041-\u3096]')
KATA_SIG = ('\u30f2', '\u30cf', '\u30ac', '\u30d0')


def kata2hira(t):
    return KATA_RE.sub(lambda m: chr(ord(m.group(0)) - 0x60), t)


def is_archaic(text):
    core = BR_RE.sub('', text)
    k = len(KATA_RE.findall(core))
    h = len(HIRA_RE.findall(core))
    if k < 6 or not (k + h) or k / float(k + h) < 0.4:
        return False
    return any(sg in core for sg in KATA_SIG)


# ------------------------------------------------------------------ 清单读写
def load_sens():
    try:
        with io.open(SENSF, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_sens(d):
    with _SENS_LOCK:
        tmp = '%s.%d.tmp' % (SENSF, threading.get_ident())
        with io.open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, SENSF)


# ------------------------------------------------------------------ 术语掩码
def load_terms():
    """tm.json -> [(日文, 中文)]，长的优先（先替换长词，避免被短词切碎）。"""
    if _TERMS['pairs'] is not None and time.time() - _TERMS['at'] < 60:
        return _TERMS['pairs']
    pairs = []
    try:
        with io.open(TMF, encoding='utf-8') as f:
            d = json.load(f)
        for k, v in (d.get('term') or {}).items():
            if k and v and k != v:
                pairs.append((k, v))
        for k, v in (d.get('name') or {}).items():
            cn = (v or {}).get('cn') if isinstance(v, dict) else v
            if k and cn and k != cn:
                pairs.append((k, cn))
    except Exception:
        pass
    pairs = sorted(set(pairs), key=lambda x: -len(x[0]))
    _TERMS['pairs'] = pairs
    _TERMS['at'] = time.time()
    return pairs


def mask_terms(text, pairs=None):
    """把术语换成 ZQX{n}QXZ 占位符；返回 (掩码文本, [(占位符, 中文)])"""
    pairs = load_terms() if pairs is None else pairs
    slots = []
    out = text
    for jp, cn in pairs:
        if jp in out:
            ph = 'ZQX%dQXZ' % len(slots)
            out = out.replace(jp, ph)
            slots.append((ph, cn))
    return out, slots


def unmask(text, slots):
    for ph, cn in slots:
        text = text.replace(ph, cn)
    return text


# ------------------------------------------------------------------ 通道
def _get(url, headers=None, data=None, timeout=20):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def tr_google(text, timeout=20):
    q = urllib.parse.quote(text)
    url = ('https://translate.googleapis.com/translate_a/single'
           '?client=gtx&sl=ja&tl=zh-CN&dt=t&q=' + q)
    d = json.loads(_get(url, timeout=timeout))
    return ''.join(seg[0] for seg in d[0] if seg and seg[0])


def tr_bing(text, timeout=20):
    tok = _get('https://edge.microsoft.com/translate/auth',
               timeout=timeout).strip()
    url = ('https://api-edge.cognitive.microsofttranslator.com/translate'
           '?api-version=3.0&from=ja&to=zh-Hans')
    body = json.dumps([{'Text': text}]).encode('utf-8')
    d = json.loads(_get(url, headers={'Content-Type': 'application/json',
                                      'Authorization': 'Bearer ' + tok},
                        data=body, timeout=timeout))
    return d[0]['translations'][0]['text']


def tr_tencent(text, timeout=20):
    """腾讯交互翻译（transmart.qq.com 网页版接口，无需 key）。

    国内可直连，实测 <br> 位置守恒，作为谷歌之后的第二通道。
    """
    body = json.dumps({
        'header': {'fn': 'auto_translation',
                   'client_key': 'browser-chrome-122.0.0.0-%d'
                                 % int(time.time() * 1000),
                   'session': '', 'user': ''},
        'type': 'plain', 'model_category': 'normal',
        'source': {'lang': 'ja', 'text_list': [text]},
        'target': {'lang': 'zh'}}).encode('utf-8')
    d = json.loads(_get('https://transmart.qq.com/api/imt',
                        headers={'Content-Type': 'application/json',
                                 'Referer': 'https://transmart.qq.com/zh-CN/index',
                                 'Origin': 'https://transmart.qq.com'},
                        data=body, timeout=timeout))
    hd = d.get('header') or {}
    if hd.get('ret_code') != 'succ':
        raise RuntimeError('腾讯返回异常 %s' % hd)
    out = d.get('auto_translation') or []
    if not out or not str(out[0]).strip():
        raise RuntimeError('腾讯返回空')
    return out[0]


# 通道顺序：谷歌 -> 腾讯 -> 必应。必应在本机网络实测不通
# （edge auth 404 / ttranslatev3 空 body），只在前面都失败时白试一次。
VENDORS = (('google', tr_google), ('tencent', tr_tencent), ('bing', tr_bing))

# 通道中文名（界面/日志用）
VN_LABEL = {'google': '谷歌', 'tencent': '腾讯', 'bing': '必应',
            'baidu_llm': '百度大模型', 'baidu': '百度通用'}
# 用户可选渠道：auto = 按 VENDORS 顺序自动降级；其余 = 只走该通道
VENDOR_CHOICES = (('auto', '自动（谷歌 → 腾讯 → 必应）'),
                  ('google', '谷歌'), ('tencent', '腾讯'), ('bing', '必应'),
                  ('baidu_llm', '百度大模型（质量好·慢·耗额度）'),
                  ('baidu', '百度通用'))

# ---- 额外通道：只在**显式点名**时使用，不进 auto 自动降级链 ----------------
# 百度两条走 解包/汉化/mt.py 的账号池。惰性加载：不点名就永不 import，
# 面板启动开销不受影响。
_MTM = [None]


def _mtmod():
    """加载引擎模块（只为拿账号池）。失败就抛，由调用方落到错误提示里。"""
    if _MTM[0] is None:
        m = sys.modules.get('mt')      # 引擎进程里已按 'mt' 导入 -> 必须复用，
        if m is None:                  # 否则台账会有两个内存副本，改一处不生效
            import importlib.util
            p = os.path.join(D, 'mt.py')
            spec = importlib.util.spec_from_file_location('tagatame_mt', p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        m._EVT_ON = False      # 绝不写引擎事件流（两进程同改一个文件会互相截尾）
        _MTM[0] = m
    return _MTM[0]


def tr_baidu_llm(text, timeout=90):
    """百度大模型翻译。重试压到 2 次：手动请求宁可快点报错，别把界面卡住。"""
    return _mtmod().ENGINES['baidu_llm']().call(text, retry=2)


def tr_baidu(text, timeout=90):
    """百度通用翻译（账号池）。"""
    return _mtmod().ENGINES['baidu']().call(text, retry=3)


VENDORS_ALL = VENDORS + (('baidu_llm', tr_baidu_llm), ('baidu', tr_baidu))


# ------------------------------------------------- 引擎调速（面板/控制台共用）
# 不自己实现读写：直接透传引擎模块的函数 —— 校验、区间夹取、原子落盘只有一份，
# 面板与控制台不会各改各的。
def run_cfg():
    return _mtmod().load_run_cfg(force=True)


def run_cfg_save(patch):
    return _mtmod().save_run_cfg(patch)


def run_cfg_defaults(engine=None):
    return _mtmod().run_cfg_defaults(engine)


def run_cfg_presets():
    return _mtmod().run_cfg_presets()


def run_cfg_meta(engine=None):
    """给界面用的元信息：字段区间、可选引擎、预设、当前引擎的默认值。"""
    m = _mtmod()
    eng = engine or m.load_run_cfg()['engine']
    return {'fields': [{'key': k, 'lo': l, 'hi': h, 'label': t, 'unit': u}
                       for k, l, h, t, u in m.RUN_FIELDS],
            'engines': [e for e in ('baidu_llm', 'baidu', 'cht', 'google', 'bing')
                        if e in m.ENGINE_CFG],
            'default_engine': m.DEFAULT_RUN_ENGINE,
            'defaults': m.run_cfg_defaults(eng),
            'char_cap': m.char_cap(eng),
            'presets': m.run_cfg_presets(),
            'path': m.RUN_CFG_PATH}


# ------------------------------------------------- 百度账号（面板/控制台共用）
# 同样只做透传：停用名单与后加账号只有 accounts.json 一份，两个进程都读它，
# 引擎侧 mtime + 1 秒 TTL 热重载 —— 面板里点完停用，引擎 1 秒内就摘掉那个号。
def acc_list():
    return _mtmod().acc_list()


def acc_disable(kind, appid, on=True):
    return _mtmod().acc_disable(kind, appid, on)


def acc_add(appid, key, pool='llm', note=''):
    return _mtmod().acc_add(appid, key, pool, note)


def acc_remove(appid, pool=None):
    return _mtmod().acc_remove(appid, pool)


def acc_test(kind, appid):
    """单账号体检：发 20 行真实日文（约 700 字符）看能不能干活。

    不用「こんにちは」：5 字符的请求 234ms 就回来，而真实载荷会 25 秒整超时 ——
    短请求根本不进模型，测出来全绿却零预测力。
    """
    return _mtmod().acc_test(kind, appid)


# ------------------------------------------------------------------ 换行守恒
def split_segs(text):
    """按 <br> 切段，丢掉空段（相邻 <br> 会切出空串）。"""
    return [s.strip() for s in BR_RE.split(text) if s.strip()]


def tidy(out):
    """译文 -> 段落列表：统一换行符、去行首行尾空白、丢空行。"""
    out = (out or '').replace('\r\n', '\n').replace('\r', '\n')
    return [p.strip(' \t\u3000') for p in out.split('\n') if p.strip()]


def draft(text, log=None, vendor=''):
    """给一条日文原文生成机翻草稿。

    vendor 为空/auto 时按 VENDORS 顺序自动降级；指定渠道时**只走那一条**，
    失败就把原因写进 mt_note（便于人工对比不同渠道的译文差异）。

    返回 {'mt','mt_src','mt_at','mt_note'}；全部通道都失败时返回
    {'mt':'', 'mt_note':失败原因}（调用方据此记账，不要当成功）。"""
    text = (text or '').strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    masked, slots = mask_terms(text)
    if is_archaic(masked):                 # 古语片假名行：先降为平假名再送翻
        masked = kata2hira(masked)
        if log:
            log('    识别为古语片假名行 -> 假名化后送翻')
    segs = split_segs(masked)
    nbr = len(BR_RE.findall(masked))
    errs = []
    vt = (vendor or '').strip().lower()
    if vt in ('', 'auto'):
        vt = ''
    # 显式点名时在 VENDORS_ALL 里找（含百度两条）；auto/空 = 只走 VENDORS
    chain = tuple(x for x in VENDORS_ALL if x[0] == vt) if vt else VENDORS
    if vt and not chain:                   # 名字写错：退回自动，但记一笔
        errs.append('未知通道 %s，已按自动模式重试' % vendor)
        vt = ''
        chain = VENDORS
    for name, fn in chain:
        # ① 整行送翻：<br> -> \n，保住整句语境（实测换行位置守恒）
        flat = BR_RE.sub('\n', masked)
        got = None
        for attempt in (1, 2):
            try:
                got = fn(flat)
                break
            except Exception as e:
                errs.append('%s 整行: %s' % (name, e))
                if log:
                    log('    通道 %s 整行第 %d 次失败：%s' % (name, attempt, e))
                time.sleep(0.8)
        if got is not None:
            parts = tidy(got)
            if parts and len(parts) == max(len(segs), 1):
                out = '<br>'.join(unmask(p, slots) for p in parts)
                return {'mt': out, 'mt_src': name, 'mt_at': int(time.time()),
                        'mt_note': ''}
            errs.append('%s 整行换行数 %d != %d' % (name, len(parts), nbr))
            if log:
                log('    通道 %s：换行数 %d != %d，退逐段'
                    % (name, len(parts), nbr))
        # ② 逐段送翻：个数 100% 精确，作为整行的兜底
        outs, ok = [], True
        for s in segs:
            try:
                outs.append(unmask(fn(s), slots).strip())
            except Exception as e:
                errs.append('%s 逐段: %s' % (name, e))
                if log:
                    log('    通道 %s 逐段失败：%s' % (name, e))
                ok = False
                break
            time.sleep(0.25)
        if ok and outs and all(outs):
            return {'mt': '<br>'.join(outs), 'mt_src': name,
                    'mt_at': int(time.time()), 'mt_note': ''}
    head = ('通道 %s 失败：' % VN_LABEL.get(vt, vt)) if vt else '全部通道失败：'
    return {'mt': '', 'mt_src': '', 'mt_at': 0,
            'mt_note': (head + ' / '.join(errs[-3:]))[:200]}


PROBE_TEXT = '\u3053\u308c\u306f\u2026\uff01<br>\u30b7\u30ca\u30a4\u5c71\u304c\u3001\u307e\u305f\u5674\u706b\u3057\u3066\u3044\u308b\u2026!?'


def probe(timeout=8, log=None):
    """逐个测通道可用性（界面「测通道」按钮用）。

    每通道只送一次、不重试；返回 [{'name','label','ok','ms','out','err'}]。"""
    res = []
    for name, fn in VENDORS:
        t0 = time.time()
        rec = {'name': name, 'label': VN_LABEL.get(name, name), 'ok': False,
               'ms': 0, 'out': '', 'err': ''}
        try:
            got = fn(BR_RE.sub('\n', PROBE_TEXT), timeout=timeout)
            rec['out'] = (got or '').replace('\n', ' / ').strip()
            rec['ok'] = bool(rec['out'])
            if not rec['ok']:
                rec['err'] = '返回空'
        except Exception as e:
            rec['err'] = '%s: %s' % (type(e).__name__, e)
        rec['ms'] = int((time.time() - t0) * 1000)
        if log:
            log('  通道 %-8s %s %5dms  %s'
                % (name, 'OK  ' if rec['ok'] else 'FAIL', rec['ms'],
                   rec['out'][:34] if rec['ok'] else rec['err'][:44]))
        res.append(rec)
    return res


def translate_pending(limit=0, force=False, only='', log=None, dry=False,
                      vendor=''):
    """给隔离清单里「无人工译文」的行补机翻草稿。

    force=True 时连已有草稿的行也重翻；only 指定则只翻该原文。
    vendor 指定渠道（google/tencent/bing），空 = 自动降级。
    dry=True 只统计不改文件。返回 {'todo','done','fail','skip','msgs'}"""
    log = log or (lambda *a: None)
    d = load_sens()
    res = {'todo': 0, 'done': 0, 'fail': 0, 'skip': 0, 'msgs': [],
           'vendor': (vendor or 'auto')}
    if not d:
        return res
    todo = []
    for jp, v in d.items():
        v = v if isinstance(v, dict) else {}
        if only and jp != only:
            continue
        if (v.get('cn') or '').strip():
            continue                       # 已人工审核，机翻不再插手
        if (v.get('mt') or '').strip() and not force:
            continue
        todo.append(jp)
    todo.sort(key=lambda k: (d[k].get('first', 0), k))
    if limit:
        todo = todo[:limit]
    res['todo'] = len(todo)
    if dry or not todo:
        return res
    changed = False
    for i, jp in enumerate(todo, 1):
        got = draft(jp, log=log, vendor=vendor)
        if got is None:
            res['skip'] += 1
            continue
        v = d[jp] if isinstance(d[jp], dict) else {}
        if not (got.get('mt') or '').strip():
            v['mt_note'] = got.get('mt_note') or '机翻失败'
            d[jp] = v
            changed = True
            res['fail'] += 1
            log('  [%d/%d] 失败：%s | %s'
                % (i, len(todo), jp[:26], v['mt_note'][:60]))
        else:
            v.update(got)
            d[jp] = v
            changed = True
            res['done'] += 1
            log('  [%d/%d] %s -> %s'
                % (i, len(todo), jp[:26], got['mt'].replace('<br>', ' / ')[:46]))
        res['msgs'].append((jp, v.get('mt') or '', v.get('mt_note') or ''))
        if i < len(todo):
            time.sleep(0.35)               # 免费通道，温和一点
    if changed:
        save_sens(d)
    return res


# ------------------------------------------------------------------ 命令行
def _stats():
    d = load_sens()
    if not d:
        print('清单为空：%s' % SENSF)
        return
    rev = sum(1 for v in d.values() if (v or {}).get('cn', '').strip())
    mt = sum(1 for v in d.values()
             if not (v or {}).get('cn', '').strip()
             and (v or {}).get('mt', '').strip())
    none = len(d) - rev - mt
    print('清单文件 : %s' % SENSF)
    print('总条数   : %d' % len(d))
    print('已人工审 : %d' % rev)
    print('机翻待审 : %d' % mt)
    print('无译文   : %d' % none)
    vc = {}
    for v in d.values():
        s = (v or {}).get('mt_src') or ''
        if s:
            vc[s] = vc.get(s, 0) + 1
    if vc:
        print('通道分布 : %s' % ' / '.join('%s %d' % kv for kv in vc.items()))


def main():
    args = sys.argv[1:]
    if '--stats' in args or not args:
        _stats()
        return
    if '--probe' in args:
        print('通道体检（每条只送一次、不重试）…')
        probe(log=lambda x: (print(x), sys.stdout.flush()))
        return
    if '--mt' in args:
        limit = 0
        if '--limit' in args:
            i = args.index('--limit')
            limit = int(args[i + 1]) if i + 1 < len(args) else 0
        only = ''
        if '--one' in args:
            i = args.index('--one')
            only = args[i + 1] if i + 1 < len(args) else ''
        vendor = ''
        if '--vendor' in args:
            i = args.index('--vendor')
            vendor = args[i + 1] if i + 1 < len(args) else ''
            if vendor == 'auto':
                vendor = ''
        print('开始机翻（渠道：%s）…'
              % (VN_LABEL.get(vendor, vendor) if vendor else '自动降级'))
        t0 = time.time()
        r = translate_pending(limit=limit, force='--force' in args, only=only,
                              log=lambda s: (print(s), sys.stdout.flush()),
                              dry='--dry' in args, vendor=vendor)
        print('完成：待翻 %d / 成功 %d / 失败 %d / 跳过 %d，用时 %.1fs'
              % (r['todo'], r['done'], r['fail'], r['skip'], time.time() - t0))
        print('机翻底稿写入 sensitive.json 的 mt 字段；')
        print('面板 http://127.0.0.1:8777/live 底部可直接核对并保存人工译文。')
        return
    print('未识别的参数：%s' % ' '.join(args))
    print('可用：--stats / --probe / --mt [--force] [--limit N] [--one 原文]'
          ' [--vendor google|tencent|bing|auto] [--dry]')


if __name__ == '__main__':
    main()
