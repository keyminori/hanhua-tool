# -*- coding: utf-8 -*-
"""工作区（汉化项目）管理 —— 让这套工具能同时伺候好几个项目。

设计要点
--------
工具本体（代码）和项目数据（目录、术语、状态文件）**彻底分开**：

    <工具目录>/
        core/  ui/  README.md  汉化工具.exe
    <数据目录>/                      默认 = 工具目录/projects，不可写时退回 LOCALAPPDATA
        _common/
            accounts.json           百度账号（跨项目共享，别每个项目配一遍）
            current.json            记住上次打开的是哪个项目
        <项目名>/
            project.json            项目配置：源目录 / 产物目录 / 文件匹配 / 引擎参数
            glossary.json           术语表（日文 -> 固定中文译法）
            tm.json                 引擎实际读的术语表（由 glossary 生成，别手改）
            sensitive.json          敏感隔离句
            manual_ov.json          人工改稿台账
            engine_cfg.json         调速配置
            mt_cache.json           翻译缓存

切换项目 = 换一份 project.json，然后**重绑引擎里的路径常量**（见 apply()）。
引擎用模块级常量（TM_PATH / SENS_PATH / ...），Python 函数里引用的是全局名，
所以只要改 globals 字典就能整体搬家，不用改引擎内部一行代码。

project.json 字段
-----------------
    name        项目名（也是目录名）
    src_dir     日文源目录（TSV：行号<TAB>原文<TAB>语音cue）
    out_dir     中文产物目录（自动创建）
    pattern     源文件匹配，默认 *.txt（子目录也扫：*.txt 会递归）
    recursive   是否递归子目录，默认 False
    cols        [id列, 原文列, cue列]，默认 [0, 1, 2]
    sep         列分隔符，默认 TAB
    note        备注
    created_at / updated_at
"""
import os
import sys
import io
import json
import time
import threading

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 路径定位
def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '.wtest')
        with open(p, 'w') as f:
            f.write('1')
        os.remove(p)
        return True
    except Exception:
        return False


def _has_data(d):
    """d/projects 里有没有真东西（至少一个项目目录）"""
    p = os.path.join(d, 'projects')
    if not os.path.isdir(p):
        return False
    try:
        # 只看有没有「真项目」目录；光有一个 _common（建目录时顺手建的）不算
        return any(n != '_common' and os.path.isdir(os.path.join(p, n))
                   for n in os.listdir(p))
    except Exception:
        return False


def _pick_exe_dir(d):
    """exe 可能被人放在 dist/ 之类的子目录里（打包时那儿也生成过一份），
    那样它会盯着 dist/projects 这个空壳，表现为「项目列表是空的」——静默降级。
    所以：同层没数据、上一层有数据，就用上一层。"""
    if not getattr(sys, 'frozen', False):
        return d
    up = os.path.dirname(d)
    if up and up != d and not _has_data(d) and _has_data(up):
        return up
    return d


if getattr(sys, 'frozen', False):          # PyInstaller 打包后
    EXE_DIR = _pick_exe_dir(os.path.dirname(os.path.abspath(sys.executable)))
    BUNDLE = getattr(sys, '_MEIPASS', EXE_DIR)
else:
    BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    EXE_DIR = BUNDLE

ROOT = BUNDLE


def data_dir():
    """项目数据放哪：工具目录能写就放工具目录（方便整体拷贝备份），
    否则（比如装进 Program Files）退回用户目录。"""
    d = os.path.join(EXE_DIR, 'projects')
    if _writable(d):
        return d
    alt = os.path.join(os.environ.get('LOCALAPPDATA') or
                       os.path.expanduser('~'), 'HanhuaTool', 'projects')
    os.makedirs(alt, exist_ok=True)
    return alt


PROJECTS_DIR = data_dir()
COMMON_DIR = os.path.join(PROJECTS_DIR, '_common')
CUR_FILE = os.path.join(COMMON_DIR, 'current.json')

DEFAULT_COLS = [0, 1, 2]


def _ensure():
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    os.makedirs(COMMON_DIR, exist_ok=True)


# ---------------------------------------------------------------- 账号（全局）
def accounts_path():
    _ensure()
    return os.path.join(COMMON_DIR, 'accounts.json')


# ---------------------------------------------------------------- 项目读写
def _pdir(name):
    return os.path.join(PROJECTS_DIR, str(name))


def _pfile(name):
    return os.path.join(_pdir(name), 'project.json')


def skeleton(name, src_dir='', out_dir='', **kw):
    d = {
        'name': str(name),
        'src_dir': src_dir,
        'out_dir': out_dir,
        'pattern': kw.get('pattern') or '*.txt',
        'file_re': kw.get('file_re') or '',   # 只翻匹配这个正则的文件；留空 = 全收
        'recursive': bool(kw.get('recursive', False)),
        'cols': list(kw.get('cols') or DEFAULT_COLS),
        'sep': kw.get('sep') or '\t',
        'note': kw.get('note') or '',
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'updated_at': '',
    }
    d.update({k: v for k, v in kw.items() if k not in d})
    return d


def slug(s):
    """项目名 -> 安全的目录名（中文保留，去掉路径非法字符）。"""
    bad = '\\/:*?"<>|'
    out = ''.join(('-' if c in bad or ord(c) < 32 else c) for c in str(s)).strip()
    return out or 'project'


def list_projects():
    """列出所有项目（带一句状态摘要）。"""
    _ensure()
    out = []
    for n in sorted(os.listdir(PROJECTS_DIR)):
        if n.startswith('_') or n.startswith('.'):
            continue
        f = _pfile(n)
        if not os.path.isfile(f):
            continue
        try:
            d = json.loads(io.open(f, encoding='utf-8').read())
        except Exception:
            continue
        d['_dir'] = _pdir(n)
        d['_stat'] = stat(n)
        out.append(d)
    return out


def get(name):
    if not name:
        return None
    f = _pfile(name)
    if not os.path.isfile(f):
        return None
    try:
        d = json.loads(io.open(f, encoding='utf-8').read())
    except Exception:
        return None
    d['_dir'] = _pdir(name)
    return d


def create(name, src_dir='', out_dir='', **kw):
    """新建项目。out_dir 没给就放在项目目录下 output/。"""
    _ensure()
    n = slug(name)
    if get(n):
        return (False, '已经有同名项目：%s' % n, None)
    d = _pdir(n)
    os.makedirs(d, exist_ok=True)
    if not out_dir:
        out_dir = os.path.join(d, 'output')
    cfg = skeleton(n, src_dir, out_dir, **kw)
    os.makedirs(out_dir, exist_ok=True)
    _save(n, cfg)
    return (True, '', cfg)


def update(name, patch):
    d = get(name)
    if not d:
        return (False, '没有这个项目：%s' % name, None)
    for k in ('_dir', '_stat'):
        d.pop(k, None)
    for k, v in (patch or {}).items():
        if k in ('name',):
            continue
        d[k] = v
    d['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if d.get('out_dir'):
        try:
            os.makedirs(d['out_dir'], exist_ok=True)
        except Exception:
            pass
    _save(name, d)
    return (True, '', d)


def _save(name, d):
    _ensure()
    os.makedirs(_pdir(name), exist_ok=True)
    f = _pfile(name)
    tmp = '%s.%d.new' % (f, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, f)


def delete(name):
    import shutil
    d = _pdir(name)
    if not os.path.isdir(d):
        return (False, '没有这个项目：%s' % name)
    shutil.rmtree(d, ignore_errors=True)
    return (True, '已删除项目 %s（产物目录 %s 保留）'
            % (name, (get(name) or {}).get('out_dir', '')), None)


# ---------------------------------------------------------------- 当前项目
def current_name():
    try:
        d = json.loads(io.open(CUR_FILE, encoding='utf-8').read())
        return d.get('current') or ''
    except Exception:
        return ''


def set_current(name):
    _ensure()
    tmp = '%s.%d.new' % (CUR_FILE, os.getpid())
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'current': name,
                            'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                           ensure_ascii=False))
    os.replace(tmp, CUR_FILE)
    return name


def current():
    """当前项目；没有就取第一个；一个都没有返回 None。"""
    n = current_name()
    d = get(n) if n else None
    if d:
        return d
    lst = list_projects()
    if lst:
        set_current(lst[0]['name'])
        return lst[0]
    return None


def workspace(name=None):
    """项目状态文件目录（术语/敏感/台账/调速/缓存都在这）。"""
    n = name or current_name()
    if not n:
        return None
    d = _pdir(n)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 统计
def source_files(cfg):
    """按配置列出日文源文件（相对 src_dir 的文件名）。"""
    src = (cfg or {}).get('src_dir') or ''
    if not src or not os.path.isdir(src):
        return []
    pat = (cfg.get('pattern') or '*.txt')
    import fnmatch
    out = []
    if cfg.get('recursive'):
        for root, _ds, fs in os.walk(src):
            for f in fs:
                if fnmatch.fnmatch(f, pat):
                    p = os.path.join(root, f)
                    out.append(os.path.relpath(p, src).replace('\\', '/'))
    else:
        for f in sorted(os.listdir(src)):
            if fnmatch.fnmatch(f, pat) and os.path.isfile(os.path.join(src, f)):
                out.append(f)
    return out


def stat(name=None):
    """项目进度：源文件数 / 产物行数 / 待翻行数。"""
    cfg = get(name) if name else current()
    if not cfg:
        return {'files': 0, 'done_files': 0, 'lines': 0, 'done': 0,
                'left': 0, 'pct': 0.0, 'ok': False,
                'err': '未选择项目'}
    try:
        import mt_story
        files = [f for f in source_files(cfg) if mt_story.is_story(f)]
        lines = done = 0
        done_files = 0
        for rel in files:
            try:
                rows = mt_story.read_tsv(os.path.join(cfg['src_dir'], rel))
            except Exception:
                continue
            lines += len(rows)
            dst = os.path.join(cfg['out_dir'], rel)
            if os.path.isfile(dst):
                done_files += 1
                try:
                    for r in mt_story.read_tsv(dst):
                        v = r[1] if len(r) > 1 else ''
                        if v and not mt_story.has_kana(v):
                            done += 1
                except Exception:
                    pass
        pct = (100.0 * done / lines) if lines else 0.0
        return {'files': len(files), 'done_files': done_files,
                'lines': lines, 'done': done, 'left': max(0, lines - done),
                'pct': round(pct, 2), 'ok': True, 'err': ''}
    except Exception as e:
        return {'files': 0, 'done_files': 0, 'lines': 0, 'done': 0,
                'left': 0, 'pct': 0.0, 'ok': False, 'err': str(e)[:120]}


# ---------------------------------------------------------------- 绑到引擎
def apply(mt=None, story=None, name=None):
    """把某个项目的路径绑进引擎模块（mt / mt_story）。

    引擎里那些路径是模块级常量，函数体内引用的是全局名，所以改 globals
    就能整体搬家 —— 不用动引擎内部逻辑。返回生效的项目配置。
    """
    cfg = get(name) if name else current()
    if not cfg:
        return None
    home = workspace(cfg['name'])
    if mt is not None:
        mt.set_home(home)
    if story is not None:
        story.JP = cfg.get('src_dir') or ''
        story.CN_DIR = cfg.get('out_dir') or ''
        try:
            os.makedirs(story.CN_DIR, exist_ok=True)
        except Exception:
            pass
        cols = cfg.get('cols') or DEFAULT_COLS
        story.COLS = tuple(int(x) for x in cols)
        story.SEP = cfg.get('sep') or '\t'
        # 只翻文件名匹配这个正则的文件；留空 = 全收
        story.FILE_RE = (cfg.get('file_re') or '').strip() or None
    return cfg


def apply_all(name=None):
    """把 mt / mt_story / manual 一起切到某个项目。"""
    import mt as _mt
    import mt_story as _st
    cfg = apply(_mt, _st, name)
    try:
        import manual as _mn
        _mn.bind(cfg)
    except Exception:
        pass
    return cfg
