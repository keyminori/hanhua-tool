# -*- coding: utf-8 -*-
"""《为谁炼金》汉化 · 手动工作台（网页面板 /manual 与 控制台 ctl.py 共用）

提供两件事：
  1) **手动改译文**：按 `文件#行号` 或关键字找到产物里的行，直接改中文并写回
     chinese/；同时登记台账 manual_ov.json —— 引擎轮末整文件写回时以台账为准
     （否则本轮开头的旧快照会把人工译文冲回日文/机翻，见 mt.manual_ov）。
  2) **手动发翻译请求**：任意日文原文，指定通道（谷歌/腾讯/必应/百度大模型/
     百度通用），并排看各通道的译文、耗时、以及 `<br>` 个数是否守恒。

产物格式：`行id \t 译文 \t 语音cue`，行内换行用 `<br>` 表示（不能塞真换行）。
本模块不启动任何进程、不依赖引擎在跑；只读日文源，只写产物与台账。
"""
import io
import os
import re
import sys
import glob
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

D = os.path.join(HERE, '解包', '汉化')
CN = ''                 # 产物目录：由 bind(项目配置) 注入
JP = ''                 # 日文源目录：同上


def bind(cfg):
    """切到某个项目：改稿/查行都在这个项目的目录里进行。"""
    global CN, JP
    if not cfg:
        return None
    CN = cfg.get('out_dir') or ''
    JP = cfg.get('src_dir') or ''
    return cfg
SENSF = os.path.join(D, 'sensitive.json')
MANF = os.path.join(D, 'manual_ov.json')       # 与 mt.MAN_PATH 同一份

import sens_mt                                   # noqa: E402
sens_mt.SENSF = SENSF          # 与面板/引擎用同一份隔离清单

KANA = re.compile(r'[\u3040-\u3096\u30a0-\u30fa]')
FILEID = re.compile(r'^(.+?\.txt)\s*#\s*(\S+)$', re.I)
MAX_HITS = 40000               # 关键字命中上限（防呆查询把内存打满）
LIMIT = 60                     # 默认返回条数


def log(msg):
    pass                        # 面板/控制台各自记日志，这里保持安静


# --------------------------------------------------------------- 小工具
def is_story(name):
    """与 mt_story.is_story 一致：本运行器只跑剧情文本。"""
    x = name.lower()
    return (x.startswith('qe') or x.startswith('eq_') or x.startswith('cq_')
            or (x[:1].isdigit() and '_a_2d' in x) or '_a_2d' in x
            or '_win' in x or '_3d' in x)


def _mtm():
    """引擎模块（台账、百度通道都在它里面）。"""
    return sens_mt._mtmod()


def _eol(line):
    return '\r\n' if line.endswith('\r\n') else '\n'


def sens_keys():
    try:
        d = json.load(io.open(SENSF, encoding='utf-8'))
        return set(d.keys()) if isinstance(d, dict) else set()
    except Exception:
        return set()


def _meta(it, sk=None):
    jp, cn = it.get('jp') or '', it.get('cn') or ''
    it['story'] = is_story(it['file'])
    it['sens'] = jp in (sk if sk is not None else sens_keys())
    it['kana'] = bool(cn) and bool(KANA.search(cn))
    it['done'] = bool(cn) and not it['kana']
    return it


# --------------------------------------------------------------- 读源/产物
def jp_line(name, lid):
    """该行的日文原文（产物已是中文时用来对照）。"""
    try:
        for l in io.open(os.path.join(JP, name), encoding='utf-8',
                         errors='replace'):
            r = l.rstrip('\r\n').split('\t')
            if r and r[0] == lid:
                return r[1] if len(r) > 1 else ''
    except Exception:
        pass
    return ''


def cn_line(name, lid):
    """该行的当前译文（产物）。"""
    try:
        for l in io.open(os.path.join(CN, name), encoding='utf-8',
                         errors='replace'):
            r = l.rstrip('\r\n').split('\t')
            if r and r[0] == lid:
                return r[1] if len(r) > 1 else ''
    except Exception:
        pass
    return ''


def _one(name, lid):
    it = {'file': name, 'id': lid, 'jp': jp_line(name, lid),
          'cn': cn_line(name, lid)}
    if not it['jp'] and not it['cn']:
        return None
    it['ov'] = _mtm().manual_get(name, lid)
    return _meta(it)


# --------------------------------------------------------------- 查找
def _scan(dirpath, q, hits, field, cap=MAX_HITS):
    """在 dirpath 的 txt 里找 q（只看第 2 列），结果并进 hits。"""
    for p in glob.glob(os.path.join(dirpath, '*.txt')):
        try:
            s = io.open(p, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        if q not in s or '\t' not in s:
            continue
        name = os.path.basename(p)
        for l in s.split('\n'):
            if q not in l:
                continue
            r = l.rstrip('\r').split('\t')
            if len(r) < 2 or q not in r[1]:
                continue
            k = name + '#' + r[0]
            it = hits.get(k)
            if it is None:
                if len(hits) >= cap:
                    return
                it = hits[k] = {'file': name, 'id': r[0], 'jp': '', 'cn': ''}
            it[field] = r[1]


def _fill(hits, dirpath, field):
    """给命中的行补齐另一侧（产物有原文没有 = 从日文源取，反之亦然）。"""
    want = {}
    for k, it in hits.items():
        if not it[field]:
            want.setdefault(it['file'], {})[it['id']] = it
    for name, idx in want.items():
        p = os.path.join(dirpath, name)
        if not os.path.exists(p):
            continue
        try:
            for l in io.open(p, encoding='utf-8', errors='replace'):
                r = l.rstrip('\r\n').split('\t')
                if len(r) > 1 and r[0] in idx:
                    idx[r[0]][field] = r[1]
        except Exception:
            pass


def find(q, limit=LIMIT, scope='all'):
    """查行。

    q = `文件#行号`（精确）或任意关键字（同时搜日文源与中文产物，两侧对齐）。
    scope = all | undone（未译）| done（已译）| sens（敏感隔离行）| ov（改过稿）
    """
    t0 = time.time()
    q = (q or '').strip()
    out = {'q': q, 'items': [], 'total': 0, 'trunc': False, 'scope': scope,
           'limit': limit, 'ms': 0}
    if not q:
        return out
    m = FILEID.match(q)
    if m:                                   # ① 文件#行号：直接定位
        it = _one(os.path.basename(m.group(1)), m.group(2))
        if it:
            out['items'] = [it]
            out['total'] = 1
        else:
            out['err'] = '没找到 %s' % q
        out['ms'] = int((time.time() - t0) * 1000)
        return out
    hits = {}                               # ② 关键字：两侧都搜，按 文件#行 对齐
    _scan(JP, q, hits, 'jp')
    _scan(CN, q, hits, 'cn')
    _fill(hits, JP, 'jp')
    _fill(hits, CN, 'cn')
    sk = sens_keys()
    ovset = set(_mtm().manual_load(force=True).keys())
    items = []
    for k, it in hits.items():
        it['ov'] = k in ovset
        _meta(it, sk)
        if scope == 'undone' and it['done']:
            continue
        if scope == 'done' and not it['done']:
            continue
        if scope == 'sens' and not it['sens']:
            continue
        if scope == 'ov' and not it['ov']:
            continue
        items.append(it)
    items.sort(key=lambda x: (x['file'], x['id']))
    out['total'] = len(items)
    out['trunc'] = len(items) > limit
    out['items'] = items[:limit]
    out['files'] = len(set(x['file'] for x in items))
    out['ms'] = int((time.time() - t0) * 1000)
    return out


# --------------------------------------------------------------- 改译文
def set_line(name, lid, cn, register=True):
    """把某行译文写回 chinese/ 产物，并登记台账（引擎写回不再覆盖它）。"""
    name = os.path.basename((name or '').strip())
    lid = (lid or '').strip()
    cn = (cn or '').strip()
    if not name or not lid:
        return {'ok': False, 'err': '缺少 文件 / 行号'}
    if not cn:
        return {'ok': False, 'err': '译文不能为空（要还原请点「还原」）'}
    if any(c in cn for c in ('\n', '\r', '\t')):
        return {'ok': False, 'err': '不能有真换行/制表符 —— 行内换行请写 <br>'}
    path = os.path.join(CN, name)
    created = False
    if not os.path.exists(path):
        # 该文件还没轮到翻译（产物还没生成）：照日文源建一份「全行日文」的副本，
        # 结构与引擎产出完全一致（行id 	 译文 	 cue），再改我们的那一行。
        # 引擎之后处理这个文件时按 prev 取值，会原样保留我们改过的行。
        src = os.path.join(JP, name)
        if not os.path.exists(src):
            return {'ok': False, 'err': '产物和日文源里都没有 %s' % name}
        try:
            body = io.open(src, encoding='utf-8', newline='').read()
            tmp0 = '%s.%d.new' % (path, os.getpid())
            io.open(tmp0, 'w', encoding='utf-8', newline='').write(body)
            os.replace(tmp0, path)
            created = True
        except Exception as e:
            return {'ok': False, 'err': '建产物副本失败：%r' % e}
    old = None
    wrote = False
    for _ in range(4):                      # 乐观并发：期间被引擎写回就重来
        m0 = os.path.getmtime(path)
        try:
            lines = io.open(path, encoding='utf-8', newline='').readlines()
        except Exception as e:
            return {'ok': False, 'err': '读产物失败：%r' % e}
        i, r = None, None
        for i2, l in enumerate(lines):
            r2 = l.rstrip('\r\n').split('\t')
            if r2 and r2[0] == lid:
                i, r = i2, r2
                break
        if i is None:
            return {'ok': False, 'err': '%s 里没有行 %s' % (name, lid)}
        old = r[1] if len(r) > 1 else ''
        if old != cn:
            r[1:2] = [cn]
            lines[i] = '\t'.join(r) + _eol(lines[i])
            if os.path.getmtime(path) != m0:
                continue                    # 有人（引擎轮末）动过 -> 丢掉本次改写重来
            tmp = '%s.%d.tmp' % (path, os.getpid())
            try:
                with io.open(tmp, 'w', encoding='utf-8', newline='') as f:
                    f.writelines(lines)
                os.replace(tmp, path)
                wrote = True
            except Exception as e:
                return {'ok': False, 'err': '写产物失败：%r' % e}
        break
    else:
        return {'ok': False, 'err': '产物正被引擎写回，请再试一次'}
    if register:
        _mtm().manual_set(name, lid, cn, old=old or '')
    return {'ok': True, 'file': name, 'id': lid, 'old': old or '', 'cn': cn,
            'wrote': wrote, 'created': created, 'jp': jp_line(name, lid),
            'kana': bool(KANA.search(cn))}


def unset_line(name, lid):
    """撤销改稿：删台账，并把产物还原成改稿前的值（有备份时）。"""
    name = os.path.basename((name or '').strip())
    lid = (lid or '').strip()
    v = _mtm().manual_get(name, lid)
    old = (v.get('old') or '').strip() if isinstance(v, dict) else ''
    _mtm().manual_set(name, lid, '')
    if not old:
        return {'ok': True, 'file': name, 'id': lid, 'restored': '',
                'note': '已撤销台账（没有旧值备份，产物未改动）'}
    if old == cn_line(name, lid):
        return {'ok': True, 'file': name, 'id': lid, 'restored': old,
                'wrote': False, 'note': '已撤销台账（产物已经是旧值）'}
    r = set_line(name, lid, old, register=False)
    r['restored'] = old
    r['note'] = '已还原为改稿前的值' if r.get('ok') else '台账已撤销，但产物还原失败'
    return r


def ov_list(limit=60):
    """最近改稿（按时间倒序）。"""
    d = _mtm().manual_load(force=True)
    rows = []
    for k, v in d.items():
        if not isinstance(v, dict):
            v = {'cn': v} if isinstance(v, str) else {}
        f, _, i = k.rpartition('#')
        rows.append({'file': f, 'id': i, 'cn': (v.get('cn') or ''),
                     'old': (v.get('old') or ''), 'at': int(v.get('at', 0)),
                     'n': int(v.get('n', 0) or 0)})
    rows.sort(key=lambda x: -x['at'])
    return {'items': rows[:limit], 'total': len(rows)}


# --------------------------------------------------------------- 手动翻译
def tr(text, vendors=('google', 'tencent')):
    """按指定通道各发一次请求，返回译文/耗时/失败原因与 <br> 守恒情况。"""
    text = (text or '').strip()
    if not text:
        return {'ok': False, 'err': '没有要翻译的文本'}
    if isinstance(vendors, str):
        vendors = [vendors]
    known = dict((c[0], c[1]) for c in sens_mt.VENDOR_CHOICES)
    want = [str(v).strip().lower() for v in (vendors or []) if str(v or '').strip()]
    if 'all' in want:                      # 全部通道（对比用）
        want = [c[0] for c in sens_mt.VENDOR_CHOICES if c[0] != 'auto']
    want = [v for v in want if v in known and v != 'auto']
    if not want:
        want = ['google', 'tencent']
    nbr = len(sens_mt.BR_RE.findall(text))
    items = []
    for v in want:
        t0 = time.time()
        try:
            d = sens_mt.draft(text, vendor=v) or {}
            out = (d.get('mt') or '').strip()
            items.append({'vendor': v, 'label': sens_mt.VN_LABEL.get(v, v),
                          'ok': bool(out), 'ms': int((time.time() - t0) * 1000),
                          'out': out,
                          'err': '' if out else (d.get('mt_note') or '未返回译文'),
                          'nbr_in': nbr,
                          'nbr_out': len(sens_mt.BR_RE.findall(out))})
        except Exception as e:
            items.append({'vendor': v, 'label': sens_mt.VN_LABEL.get(v, v),
                          'ok': False, 'ms': int((time.time() - t0) * 1000),
                          'out': '', 'err': '%s: %s' % (type(e).__name__, e),
                          'nbr_in': nbr, 'nbr_out': 0})
    return {'ok': True, 'items': items, 'br': nbr, 'text': text}


def stats():
    """工作台概览：产物规模、改稿条数、可搜范围。"""
    try:
        nf = len(glob.glob(os.path.join(CN, '*.txt')))
    except Exception:
        nf = 0
    o = ov_list(1)
    return {'files': nf, 'ov': o['total'], 'cn': CN, 'jp': JP,
            'vendors': [list(c) for c in sens_mt.VENDOR_CHOICES]}
