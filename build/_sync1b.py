# -*- coding: utf-8 -*-
"""同步第 1 步（续）：glossary.py 与本作的 tm_manual.json 对接 + manual.py。"""
import io
import os
import py_compile

NEW = r'J:\hanhua\core'
OLD = r'J:\tagatame\解包\汉化'
ROOT = r'J:\tagatame'
JP_SRC = 'J:/tagatame/解包/_extract/剧情文档/Loc/japanese'


def w(p, s):
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    py_compile.compile(p, doraise=True)
    print('  wrote', p, len(s))


# ---------------------------------------------------------------- glossary.py
t = io.open(os.path.join(NEW, 'glossary.py'), encoding='utf-8').read()

old1 = """def path(name=None):
    import project
    d = project.workspace(name)
    return os.path.join(d, 'glossary.json') if d else None"""
new1 = """def path(name=None):
    \"\"\"术语表文件 = 本作原来的人工钉死表 tm_manual.json。

    fix_tm.py 认的就是它（A/B/C 清洗不动它），所以术语真源不变，
    这里只是给它套一层增删改 + 回溯的界面。
    \"\"\"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'tm_manual.json')"""
assert t.count(old1) == 1, 'glossary 锚点1'
t = t.replace(old1, new1)

old2 = """def load(name=None):
    p = path(name)
    if not p or not os.path.isfile(p):
        return {}
    try:
        d = json.loads(io.open(p, encoding='utf-8').read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}"""
new2 = """def load(name=None):
    \"\"\"读术语表。tm_manual.json 分 name（人名）/ term（普通术语）两节，
    界面管的是 term 节；name 节由 name_dict 那套脚本维护，原样保留。\"\"\"
    p = path(name)
    if not p or not os.path.isfile(p):
        return {}
    try:
        d = json.loads(io.open(p, encoding='utf-8').read())
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return d['term'] if isinstance(d.get('term'), dict) else d"""
assert t.count(old2) == 1, 'glossary 锚点2'
t = t.replace(old2, new2)

old3 = """def save(data, name=None):
    p = path(name)
    if not p:
        return (False, '未选择项目')
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return (True, '')"""
new3 = """def save(data, name=None):
    \"\"\"写回 tm_manual.json 的 term 节，name 节原样保留（先写临时文件再原子替换）。\"\"\"
    p = path(name)
    if not p:
        return (False, '未选择项目')
    try:
        whole = json.loads(io.open(p, encoding='utf-8').read())
    except Exception:
        whole = {}
    if not isinstance(whole, dict):
        whole = {}
    if not isinstance(whole.get('name'), dict):
        whole['name'] = {}
    whole['term'] = data
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(whole, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return (True, '')"""
assert t.count(old3) == 1, 'glossary 锚点3'
t = t.replace(old3, new3)

i = t.index('def build_tm(name=None):')
j = t.index('# ---------------------------------------------------------------- 导入导出')
new4 = '''def build_tm(name=None):
    """术语表 -> 引擎读的 tm.json（**合并写**，不是整份重写）。

    tm.json 里还有 name（人名）节、以及 term 节中 fix_tm.py 自动生成的条目，
    整份重写会把它们清空。所以只把人工表里的译法覆盖上去。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tm.json')
    pairs = {}
    for jp, v in load(name).items():
        v = v if isinstance(v, dict) else {'cn': str(v), 'on': True}
        if v.get('on', True) is not False and (v.get('cn') or '').strip():
            pairs[jp] = v['cn'].strip()
    try:
        tm = json.loads(io.open(p, encoding='utf-8').read())
    except Exception:
        tm = {}
    if not isinstance(tm, dict):
        tm = {}
    term = tm.get('term')
    term = term if isinstance(term, dict) else {}
    term.update(pairs)                     # 人工表优先，自动条目保留
    tm['term'] = term
    tm['_term_from_manual'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(tm, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return len(pairs)


'''
t = t[:i] + new4 + t[j:]
w(os.path.join(OLD, 'glossary.py'), t)
print('4. glossary.py <- 新版 + 读写 tm_manual.json + 合并写 tm.json')

# ---------------------------------------------------------------- manual.py
t = io.open(os.path.join(NEW, 'manual.py'), encoding='utf-8').read()
old5 = """CN = ''                 # 产物目录：由 bind(项目配置) 注入
JP = ''                 # 日文源目录：同上"""
new5 = ("""# 默认就是本作的目录（单独跑 manual.py 时不用先 bind）
CN = os.path.join(r'%s', 'chinese')
JP = '%s'""" % (OLD.replace('\\', '\\\\'), JP_SRC))
assert t.count(old5) == 1, 'manual 锚点'
t = t.replace(old5, new5)
w(os.path.join(ROOT, 'manual.py'), t)
print('5. manual.py <- 新版（bind）+ 本作默认目录')
