# -*- coding: utf-8 -*-
"""同步第 1 步：把新版核心模块搬进旧工具，并回填本作默认值 / 适配路径。

原则：
  * 状态文件（tm.json / mt_cache.json / sensitive.json / manual_ov.json /
    accounts.json / engine_cfg.json）**一律留在 J:\\tagatame\\解包\\汉化**，
    靠 project 的 data_dir 指向它，缓存和进度一行都不动。
  * 术语真源仍是 tm_manual.json（fix_tm.py 认它），glossary 只做 UI 层。
"""
import io
import os
import shutil
import py_compile

NEW = r'J:\hanhua\core'
OLD = r'J:\tagatame\解包\汉化'
ROOT = r'J:\tagatame'

JP_SRC = 'J:/tagatame/解包/_extract/剧情文档/Loc/japanese'
FILE_RE = r'^(qe|eq_|cq_)|_a_2d|_win|_3d'


def w(p, s):
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    py_compile.compile(p, doraise=True)
    print('  wrote', p, len(s))


# ---------------------------------------------------------------- 1. mt.py
shutil.copy2(os.path.join(NEW, 'mt.py'), os.path.join(OLD, 'mt.py'))
py_compile.compile(os.path.join(OLD, 'mt.py'), doraise=True)
print('1. mt.py <- 新版（多出 set_home / 运行时取缓存路径）')

# ---------------------------------------------------------------- 2. mt_story.py
t = io.open(os.path.join(NEW, 'mt_story.py'), encoding='utf-8').read()
old = """# 日文源 / 中文产物目录：**由 project.apply() 注入**，这里只留空壳。
JP = ''
CN_DIR = ''"""
new = ("""# 日文源 / 中文产物目录：**通常由 project.apply() 注入**（面板/引擎启动时会绑）。
# 这里填本作的默认值，单独跑 mt_story.py 时行为与改造前完全一致。
JP = '%s'
CN_DIR = os.path.join(BASE, 'chinese')""" % JP_SRC)
assert t.count(old) == 1, 'mt_story 锚点1'
t = t.replace(old, new)

old2 = """# 只想翻某些文件时用的正则（None = 全收）。例如只翻剧情：r'^(qe|eq_|cq_)'。
FILE_RE = None"""
new2 = ("""# 只想翻某些文件时用的正则（None = 全收）。
# 本作默认：只翻剧情文件（qe/eq_/cq_ 前缀、_a_2d、_win、_3d），跳过 UI 与道具名。
# 这就是改造前 is_story() 里那串前缀判断的等价写法，匹配时忽略大小写。
FILE_RE = r'%s'""" % FILE_RE)
assert t.count(old2) == 1, 'mt_story 锚点2'
t = t.replace(old2, new2)
w(os.path.join(OLD, 'mt_story.py'), t)
print('2. mt_story.py <- 新版 + 本作默认目录/文件过滤')

# ---------------------------------------------------------------- 3. project.py
t = io.open(os.path.join(NEW, 'project.py'), encoding='utf-8').read()

old3 = """if getattr(sys, 'frozen', False):          # PyInstaller 打包后
    EXE_DIR = _pick_exe_dir(os.path.dirname(os.path.abspath(sys.executable)))
    BUNDLE = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    EXE_DIR = BUNDLE"""
new3 = """if getattr(sys, 'frozen', False):          # PyInstaller 打包后
    EXE_DIR = _pick_exe_dir(os.path.dirname(os.path.abspath(sys.executable)))
    BUNDLE = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    EXE_DIR = BUNDLE

# 本作的历史数据目录：术语 / 缓存 / 敏感句 / 台账 / 账号全都在这，
# 「为谁炼金」项目的 data_dir 指向它 —— 不让同步搬家动到任何一个状态文件。
LEGACY_HOME = os.path.dirname(os.path.abspath(__file__))"""
assert t.count(old3) == 1, 'project 锚点1'
t = t.replace(old3, new3)

old4 = """def accounts_path():
    _ensure()
    return os.path.join(COMMON_DIR, 'accounts.json')"""
new4 = """def accounts_path():
    \"\"\"百度账号清单：沿用本作原来的 解包/汉化/accounts.json（停机名单在里面）。\"\"\"
    return os.path.join(LEGACY_HOME, 'accounts.json')"""
assert t.count(old4) == 1, 'project 锚点2'
t = t.replace(old4, new4)

old5 = """def workspace(name=None):
    \"\"\"项目状态文件目录（术语/敏感/台账/调速/缓存都在这）。\"\"\"
    n = name or current_name()
    if not n:
        return None
    d = _pdir(n)
    os.makedirs(d, exist_ok=True)
    return d"""
new5 = """def workspace(name=None):
    \"\"\"项目状态文件目录（术语/敏感/台账/调速/缓存都在这）。

    项目配置里给了 data_dir 就用它（本作的 data_dir = 解包/汉化，
    这样同步改造不会把缓存和术语搬走），否则用默认的项目目录。
    \"\"\"
    n = name or current_name()
    if not n:
        return None
    cfg = get(n) or {}
    d = cfg.get('data_dir') or _pdir(n)
    os.makedirs(d, exist_ok=True)
    return d"""
assert t.count(old5) == 1, 'project 锚点3'
t = t.replace(old5, new5)

# 默认项目：第一次跑自动建「为谁炼金」
t += '''

# ---------------------------------------------------------------- 本作默认项目
def ensure_default():
    """第一次用时把本作登记成默认项目（路径照搬改造前写死的那几个）。"""
    _ensure()
    if list_projects():
        return current_name()
    ok, msg, cfg = create('为谁炼金', JP_DEFAULT, os.path.join(LEGACY_HOME, 'chinese'),
                          file_re=FILE_RE_DEFAULT, data_dir=LEGACY_HOME,
                          note='本作主项目（源/产物目录与改造前一致）')
    if ok:
        set_current(cfg['name'])
        return cfg['name']
    return current_name()


JP_DEFAULT = '%s'
FILE_RE_DEFAULT = r'%s'
''' % (JP_SRC, FILE_RE)
w(os.path.join(OLD, 'project.py'), t)
print('3. project.py <- 新版 + data_dir 支持 + 默认项目')

# ---------------------------------------------------------------- 4. glossary.py
t = io.open(os.path.join(NEW, 'glossary.py'), encoding='utf-8').read()

old6 = """def path(name=None):
    \"\"\"术语表文件（每个项目一份）。\"\"\"
    import project
    d = project.workspace(name)
    return os.path.join(d if d else '.', 'glossary.json')"""
new6 = """def path(name=None):
    \"\"\"术语表文件 = 本作原来的人工钉死表 tm_manual.json。

    fix_tm.py 认的就是这个文件（A/B/C 清洗不动它），所以术语真源不变，
    这里只是给它套一层增删改和回溯的界面。
    \"\"\"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'tm_manual.json')"""
assert t.count(old6) == 1, 'glossary 锚点1'
t = t.replace(old6, new6)

# load：tm_manual.json 是 {name:..., term:...} 两节，界面只管 term 节
old7 = """def load(name=None):
    \"\"\"读术语表。\"\"\"
    p = path(name)
    if not os.path.isfile(p):
        return {}
    try:
        return json.loads(io.open(p, encoding='utf-8').read())
    except Exception:
        return {}"""
new7 = """def load(name=None):
    \"\"\"读术语表。tm_manual.json 分 name（人名）/ term（普通术语）两节，
    界面管的是 term 节；name 节由 name_dict 那套脚本维护，原样保留。\"\"\"
    p = path(name)
    if not os.path.isfile(p):
        return {}
    try:
        d = json.loads(io.open(p, encoding='utf-8').read())
    except Exception:
        return {}
    if isinstance(d, dict) and isinstance(d.get('term'), dict):
        return d['term']
    return d"""
assert t.count(old7) == 1, 'glossary 锚点2'
t = t.replace(old7, new7)

old8 = """def save(data, name=None):
    \"\"\"写术语表（先写临时文件再原子替换，避免写一半被读到）。\"\"\"
    p = path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return True"""
new8 = """def save(data, name=None):
    \"\"\"写回 tm_manual.json 的 term 节，name 节原样保留（先写临时文件再原子替换）。\"\"\"
    p = path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        whole = json.loads(io.open(p, encoding='utf-8').read())
        if not isinstance(whole, dict):
            whole = {}
    except Exception:
        whole = {}
    if not isinstance(whole.get('name'), dict):
        whole['name'] = whole.get('name') if isinstance(whole.get('name'), dict) else {}
    whole['term'] = data
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(whole, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return True"""
assert t.count(old8) == 1, 'glossary 锚点3'
t = t.replace(old8, new8)

# build_tm：合并模式，绝不整份覆盖（tm.json 还有 name 节和 fix_tm 生成的自动条目）
old9 = t[t.index('def build_tm(name=None):'):t.index('# ---------------------------------------------------------------- 导入导出')]
new9 = '''def build_tm(name=None):
    """术语表 -> 引擎读的 tm.json。

    **合并写**：tm.json 里的 name（人名）节、以及 term 节中 fix_tm.py 自动生成的
    条目都要留着，只把人工表里的译法覆盖上去。整份重写会把人名表清空。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tm.json')
    pairs = {}
    for jp, v in load(name).items():
        v = v if isinstance(v, dict) else {'cn': str(v), 'on': True}
        if v.get('on', True) is not False and (v.get('cn') or '').strip():
            pairs[jp] = v['cn'].strip()
    try:
        tm = json.loads(io.open(p, encoding='utf-8').read())
        if not isinstance(tm, dict):
            tm = {}
    except Exception:
        tm = {}
    term = tm.get('term')
    if not isinstance(term, dict):
        term = {}
    term.update(pairs)                     # 人工表优先，自动条目保留
    tm['term'] = term
    tm['_term_from_manual'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = '%s.%d.new' % (p, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(tm, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return len(pairs)


'''
t = t.replace(old9, new9)
w(os.path.join(OLD, 'glossary.py'), t)
print('4. glossary.py <- 新版 + 读写 tm_manual.json + 合并写 tm.json')

# ---------------------------------------------------------------- 5. manual.py
t = io.open(os.path.join(NEW, 'manual.py'), encoding='utf-8').read()
old10 = """CN = ''                 # 产物目录：由 bind(项目配置) 注入
JP = ''                 # 日文源目录：同上"""
new10 = ("""# 默认就是本作的目录（单独跑 manual.py 时不用先 bind）
CN = os.path.join(r'%s', 'chinese')
JP = '%s'""" % (OLD.replace('\\', '\\\\'), JP_SRC))
assert t.count(old10) == 1, 'manual 锚点'
t = t.replace(old10, new10)
w(os.path.join(ROOT, 'manual.py'), t)
print('5. manual.py <- 新版（bind）+ 本作默认目录')
