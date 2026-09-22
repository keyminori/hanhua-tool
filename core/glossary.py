# -*- coding: utf-8 -*-
"""术语表：把「某个词必须翻成某个样子」钉死，并保证全项目统一。

为什么单独做一层
----------------
机翻最烦的不是翻错，是**同一个名字前后翻得不一样**（蕾布娜/雷布娜/蕾芙娜）。
术语表的做法是：送翻之前把日文专名换成占位符（ZQX###QXZ），翻完再换回中文 ——
引擎根本没机会自由发挥。所以术语表是**硬约束**，不是"建议"。

坑（本项目踩过，写在这免得再踩）
--------------------------------
1. **改了术语，已经翻完的行不会自动跟着变**。译文一旦落盘，判重逻辑认为
   "已有中文"就保留。所以改术语必须**回溯**（retro）：把受影响的行的译文
   退回日文原文，让引擎下一轮重翻。本模块的 retro() 就干这个。
2. 缓存键里带"术语指纹"，术语变了旧缓存自然失效 —— 所以回溯**不用动缓存**，
   也就不用为了改术语去停引擎（停止引擎代价大，且违反"缓存整文件回写"的禁忌）。

数据落在 <项目目录>/glossary.json：
    {"日文": {"cn": "中文", "note": "备注", "on": true, "at": "时间"}}
引擎实际读的是同目录的 tm.json（由 build_tm() 生成，**不要手改**）。
"""
import os
import io
import json
import time
import threading

_LOCK = threading.RLock()

# 内置示例：新建项目时给一份能看懂的样板（用户会整份替换掉）
SAMPLE = {
    "アルケミスト": {"cn": "炼金术师", "note": "职业名", "on": True},
    "タガタメ": {"cn": "为谁炼金", "note": "作品名", "on": True},
}


def path(name=None):
    import project
    d = project.workspace(name)
    return os.path.join(d, 'glossary.json') if d else None


def load(name=None):
    p = path(name)
    if not p or not os.path.isfile(p):
        return {}
    try:
        d = json.loads(io.open(p, encoding='utf-8').read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(data, name=None):
    p = path(name)
    if not p:
        return (False, '未选择项目')
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return (True, '')


def items(name=None):
    """给界面用：[{jp, cn, note, on, at}]（按日文排序）"""
    d = load(name)
    out = []
    for jp, v in d.items():
        v = v if isinstance(v, dict) else {'cn': str(v)}
        out.append({'jp': jp, 'cn': v.get('cn') or '',
                    'note': v.get('note') or '',
                    'on': v.get('on', True) is not False,
                    'at': v.get('at') or ''})
    out.sort(key=lambda x: x['jp'])
    return out


def set_term(jp, cn, note='', on=True, name=None):
    """新增/改一条。返回 (ok, msg, 是否影响已翻内容)。"""
    jp = (jp or '').strip()
    cn = (cn or '').strip()
    if not jp:
        return (False, '日文原词不能为空', False)
    d = load(name)
    old = d.get(jp, {})
    old_cn = (old.get('cn') or '') if isinstance(old, dict) else str(old or '')
    d[jp] = {'cn': cn, 'note': note, 'on': on is not False,
             'at': time.strftime('%Y-%m-%d %H:%M:%S')}
    ok, msg = save(d, name)
    if ok:
        build_tm(name)
    return (ok, msg, bool(old_cn) and old_cn != cn)


def remove(jp, name=None):
    d = load(name)
    if jp not in d:
        return (False, '没有这条术语')
    old = d.pop(jp)
    ok, msg = save(d, name)
    if ok:
        build_tm(name)
    old_cn = (old.get('cn') or '') if isinstance(old, dict) else str(old or '')
    return (True, '', bool(old_cn))


def build_tm(name=None):
    """术语表 -> 引擎读的 tm.json（只写 term 一节，其余分节留空）。"""
    import project
    d = project.workspace(name)
    if not d:
        return None
    pairs = {}
    for jp, v in load(name).items():
        v = v if isinstance(v, dict) else {'cn': str(v), 'on': True}
        if v.get('on', True) is not False and (v.get('cn') or '').strip():
            pairs[jp] = v['cn'].strip()
    tm = {'term': pairs, '_generated': time.strftime('%Y-%m-%d %H:%M:%S'),
          '_source': 'glossary.json（别手改这个文件，改术语表即可）'}
    p = os.path.join(d, 'tm.json')
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(tm, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return len(pairs)


# ---------------------------------------------------------------- 导入导出
def export_rows(name=None):
    return [[i['jp'], i['cn'], i['note'], '1' if i['on'] else '0']
            for i in items(name)]


def import_rows(rows, name=None, replace=True):
    """rows: [[jp, cn, note, on]] —— 从 CSV/表格粘来的术语批量导入。"""
    d = {} if replace else load(name)
    n = 0
    for r in rows:
        if len(r) < 2:
            continue
        jp, cn = (r[0] or '').strip(), (r[1] or '').strip()
        if not jp or not cn:
            continue
        d[jp] = {'cn': cn,
                 'note': (r[2] if len(r) > 2 else '') or '',
                 'on': (str(r[3]).strip() not in ('0', 'false', 'False')
                        if len(r) > 3 else True),
                 'at': time.strftime('%Y-%m-%d %H:%M:%S')}
        n += 1
    ok, msg = save(d, name)
    if ok:
        build_tm(name)
    return (ok, msg, n)


# ---------------------------------------------------------------- 回溯
def retro(jps, scope='source', name=None):
    """改了术语之后，把受影响的行的译文**退回日文原文**，让引擎下轮重翻。

    scope='source'      日文原文里含该术语的行（彻底统一，默认）
    scope='translated'  只动「译文里还写着旧译名」的行（影响面小）

    返回 {'files': n, 'lines': n, 'removed': [旧译名...]}
    """
    import project
    import mt_story as S
    cfg = project.get(name) if name else project.current()
    if not cfg or not cfg.get('src_dir') or not cfg.get('out_dir'):
        return {'ok': False, 'err': '项目没配好源目录/产物目录',
                'files': 0, 'lines': 0, 'removed': []}
    jps = [j for j in (jps or []) if j]
    if not jps:
        return {'ok': True, 'err': '', 'files': 0, 'lines': 0, 'removed': []}

    S.JP = cfg['src_dir']
    S.CN_DIR = cfg['out_dir']
    src, dst_root = cfg['src_dir'], cfg['out_dir']
    ic, tc, cc = S.COLS = tuple(int(x) for x in (cfg.get('cols') or [0, 1, 2]))
    S.SEP = cfg.get('sep') or '\t'

    stat = {'ok': True, 'err': '', 'files': 0, 'lines': 0, 'removed': []}
    for rel in project.source_files(cfg):
        try:
            rows = S.read_tsv(os.path.join(src, rel))
        except Exception:
            continue
        hit = []
        for idx, txt, cue in rows:
            if scope == 'source':
                if any(j in txt for j in jps):
                    hit.append(idx)
            else:
                pass
        dst = os.path.join(dst_root, rel)
        cur = {}
        if os.path.isfile(dst):
            try:
                cur = {r[ic]: (r[tc] if len(r) > tc else '')
                       for r in S.read_tsv(dst)}
            except Exception:
                cur = {}
        if scope == 'translated':
            hit = [idx for idx, txt, cue in rows
                   if any(j in (cur.get(idx) or '') for j in
                          stat['removed'] or [])]
        if not hit:
            continue
        hit = set(hit)
        changed = 0
        for idx, txt, cue in rows:
            if idx in hit and (cur.get(idx) or '') != txt:
                cur[idx] = txt                # 退回原文
                changed += 1
        # 全量重写（保持行序与 cue）
        out = []
        for idx, txt, cue in rows:
            v = cur.get(idx, txt)
            out.append(S.SEP.join((idx, v, cue)) if cc >= 0
                       else S.SEP.join((idx, v)))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.tmp'
        with io.open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
        S._replace_retry(tmp, dst)
        if changed:
            stat['files'] += 1
            stat['lines'] += changed
    return stat


def retro_old_cns(old_cns, name=None):
    """按**旧译名**回溯：产物里凡是写了旧译名的行，退回原文重翻。"""
    import project
    import mt_story as S
    cfg = project.get(name) if name else project.current()
    if not cfg:
        return {'ok': False, 'err': '未选择项目', 'files': 0, 'lines': 0}
    cns = [c for c in (old_cns or []) if c]
    if not cns:
        return {'ok': True, 'err': '', 'files': 0, 'lines': 0}
    S.SEP = cfg.get('sep') or '\t'
    ic, tc, cc = S.COLS = tuple(int(x) for x in (cfg.get('cols') or [0, 1, 2]))
    files = lines = 0
    for rel in project.source_files(cfg):
        sp = os.path.join(cfg['src_dir'], rel)
        dp = os.path.join(cfg['out_dir'], rel)
        if not os.path.isfile(sp) or not os.path.isfile(dp):
            continue
        try:
            srows = S.read_tsv(sp)
            drows = S.read_tsv(dp)
        except Exception:
            continue
        changed = 0
        for i, r in enumerate(drows):
            v = r[tc] if len(r) > tc else ''
            if v and any(c in v for c in cns):
                drows[i] = [r[ic] if len(r) > ic else str(i),
                            srows[i][tc] if i < len(srows) and len(srows[i]) > tc else '',
                            r[cc] if cc >= 0 and len(r) > cc else '']
                changed += 1
        if changed:
            out = [S.SEP.join(x[:2] if cc < 0 else x[:3]) for x in drows]
            tmp = dp + '.tmp'
            with io.open(tmp, 'w', encoding='utf-8') as f:
                f.write('\n'.join(out) + '\n')
            S._replace_retry(tmp, dp)
            files += 1
            lines += changed
    return {'ok': True, 'err': '', 'files': files, 'lines': lines}
