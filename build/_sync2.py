# -*- coding: utf-8 -*-
"""同步第 2 步：面板（progress_server.py）与页面换成新版，回填本作布局与任务名。"""
import io
import os
import shutil
import py_compile

NEW = r'J:\hanhua\ui'
ROOT = r'J:\tagatame'
OLD = r'J:\tagatame\解包\汉化'


def w(p, s):
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    py_compile.compile(p, doraise=True)
    print('  wrote', p, len(s))


# ---------------------------------------------------------------- 面板
t = io.open(os.path.join(NEW, 'server.py'), encoding='utf-8').read()

old1 = """# ---- 目录：工具本体（代码）与项目数据分开 ----
_FROZEN = getattr(sys, 'frozen', False)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 源码根"""
new1 = """# ---- 目录：工具本体（代码）与项目数据分开 ----
_FROZEN = getattr(sys, 'frozen', False)
# 本作布局：面板在 J:\\tagatame，引擎模块（mt / project / glossary）都在 解包\\汉化
BASE = os.path.dirname(os.path.abspath(__file__))
MANUAL_DIR = BASE"""
assert t.count(old1) == 1, 'server 锚点1'
t = t.replace(old1, new1)

old2 = """CODE_ROOT = (getattr(sys, '_MEIPASS', BASE) if _FROZEN else BASE)
CORE = os.path.join(CODE_ROOT, 'core')
if not os.path.isdir(CORE):            # 源码形态下兜底
    CORE = os.path.join(BASE, 'core')"""
new2 = """CODE_ROOT = (getattr(sys, '_MEIPASS', BASE) if _FROZEN else BASE)
CORE = os.path.join(BASE, '\\u89e3\\u5305', '\\u6c49\\u5316')      # 引擎模块目录
if not os.path.isdir(CORE):
    CORE = BASE"""
assert t.count(old2) == 1, 'server 锚点2'
t = t.replace(old2, new2)

old3 = """TASK_ENGINE = 'HanhuaTranslate'        # 翻译引擎（core/runner.py）
TASK_PANEL = 'HanhuaProgress'          # 本面板"""
new3 = """TASK_ENGINE = 'TagatameTranslate'      # 翻译引擎（run_tagatame.py）
TASK_PANEL = 'TagatameProgress'        # 本面板"""
assert t.count(old3) == 1, 'server 锚点3'
t = t.replace(old3, new3)

old4 = """MANF_FILE = os.path.join(CODE_ROOT, 'ui', 'page.html')
if not os.path.isfile(MANF_FILE):                 # 再兜一次源码形态
    MANF_FILE = os.path.join(BASE, 'ui', 'page.html')"""
new4 = """MANF_FILE = os.path.join(BASE, 'manual_page.html')"""
assert t.count(old4) == 1, 'server 锚点4'
t = t.replace(old4, new4)

old5 = """    if project is None:
        return None
    cfg = project.apply_all(name)      # mt / mt_story / manual 一起切"""
new5 = """    if project is None:
        return None
    try:                               # 本作第一次用时自动登记成项目
        project.ensure_default()
    except Exception:
        pass
    cfg = project.apply_all(name)      # mt / mt_story / manual 一起切"""
assert t.count(old5) == 1, 'server 锚点5'
t = t.replace(old5, new5)

w(os.path.join(ROOT, 'progress_server.py'), t)
print('6. progress_server.py <- 新版（项目卡/术语卡）+ 本作路径与任务名')

# ---------------------------------------------------------------- 页面
shutil.copy2(os.path.join(NEW, 'page.html'), os.path.join(ROOT, 'manual_page.html'))
print('7. manual_page.html <- 新版（多出 ⓪项目 ①术语表 两张卡）')
print('   size', os.path.getsize(os.path.join(ROOT, 'manual_page.html')))
