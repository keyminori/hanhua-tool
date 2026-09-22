# -*- coding: utf-8 -*-
"""汉化工具 · 统一入口（双击 exe 就是跑这个）

    hanhua.py                控制台菜单（第一次用先走这个）
    hanhua.py gui            开网页工作台（默认 http://127.0.0.1:8777）
    hanhua.py gui --detach   工作台放后台跑，命令行立刻返回
    hanhua.py run            前台跑翻译引擎（Ctrl+C 停）
    hanhua.py run --detach   引擎放后台跑
    hanhua.py status         看一眼状态与进度
    hanhua.py project ...    项目管理（见下）
    hanhua.py term ...       术语表管理
    hanhua.py acc ...        百度账号（停用/新增/体检）
    hanhua.py cfg ...        调速（并发/批次/频率）

project 子命令
    project list                       列出所有项目
    project new <名字> <日文源目录> [产物目录]
    project switch <名字>
    project set <字段> <值>             字段：src_dir/out_dir/pattern/recursive/cols/sep
    project del <名字>

term 子命令
    term list
    term set <日文> <中文> [备注]
    term del <日文>
    term retro                         把受影响的行退回原文，下轮用新译名重翻
"""
import os
import sys
import io
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, 'frozen', False):        # 打包后：代码在临时目录，数据在 exe 旁边
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = HERE
CORE = os.path.join(HERE, 'core')
UI = os.path.join(HERE, 'ui')
for p in (CORE, UI, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

NO_WINDOW = 0x08000000
DETACHED = 0x00000008
PYEXE = sys.executable

# 下面这些模块在运行时是动态 import 的（_mod()），PyInstaller 静态分析看不见，
# 不打进包里 exe 一跑就 ModuleNotFoundError。所以显式 import 一遍。
# 用 try 包着：源码运行时目录万一不全也不至于起不来。
try:
    import project
    import glossary
    import mt
    import mt_story
    import manual
    import sens_mt
    import runner
    import server
except Exception:
    pass

# frozen 时 project 会往上找真数据目录（exe 放在 dist/ 里也不会盯空壳）
try:
    if getattr(sys, 'frozen', False) and 'project' in globals():
        APP_DIR = project.EXE_DIR
except Exception:
    pass


def _mod(name):
    __import__(name)
    return sys.modules[name]


# ---------------------------------------------------------------- 项目管理
def cmd_project(args):
    project = _mod('project')
    project.apply_all()          # 绑上目录与「只翻哪些文件」过滤，进度口径才和 status 一致
    if not args or args[0] == 'list':
        lst = project.list_projects()
        cur = project.current_name()
        if not lst:
            print('还没有项目。建一个：hanhua.py project new 游戏名 J:\\游戏\\日文目录')
            return
        print('%-16s %-8s %s' % ('项目', '当前', '日文源目录'))
        for p in lst:
            print('%-16s %-8s %s' % (p['name'],
                                     '←' if p['name'] == cur else '',
                                     p.get('src_dir') or '(未设置)'))
        st = project.stat()
        if st.get('ok'):
            print('\n当前项目进度：%d/%d 行（%.2f%%），文件 %d/%d'
                  % (st['done'], st['lines'], st['pct'],
                     st['done_files'], st['files']))
        return
    a = args[0]
    if a == 'new':
        if len(args) < 3:
            print('用法：hanhua.py project new <名字> <日文源目录> [产物目录]')
            return
        ok, msg, cfg = project.create(args[1], args[2],
                                      args[3] if len(args) > 3 else '')
        print(('已创建：%s' % cfg['name']) if ok else ('失败：%s' % msg))
        if ok:
            project.set_current(cfg['name'])
    elif a == 'switch':
        if len(args) < 2:
            print('用法：hanhua.py project switch <名字>')
            return
        cfg = project.apply_all(args[1])
        print(('已切换到 %s' % args[1]) if cfg else '没有这个项目')
    elif a == 'set':
        if len(args) < 3:
            print('用法：hanhua.py project set <字段> <值>')
            return
        k, v = args[1], args[2]
        if k == 'recursive':
            v = v.lower() in ('1', 'true', 'yes', 'on')
        elif k == 'cols':
            v = [int(x) for x in v.split(',')]
        elif k == 'sep':
            v = '\t' if v.lower() in ('\\t', 'tab') else v
        ok, msg, _ = project.update(project.current_name() or '', {k: v})
        print('已保存' if ok else ('失败：%s' % msg))
    elif a == 'del':
        if len(args) < 2:
            print('用法：hanhua.py project del <名字>')
            return
        ok, msg, _ = project.delete(args[1])
        print(msg or '已删除')
    else:
        print('未知子命令：%s' % a)


# ---------------------------------------------------------------- 术语表
def cmd_term(args):
    glossary = _mod('glossary')
    project = _mod('project')
    project.apply_all()
    if not args or args[0] == 'list':
        it = glossary.items()
        if not it:
            print('术语表是空的。加一条：hanhua.py term set 日文 中文')
            return
        for i in it:
            print('%-24s -> %-16s %s%s'
                  % (i['jp'], i['cn'], i['note'], '' if i['on'] else ' （停用）'))
        print('\n共 %d 条' % len(it))
        return
    a = args[0]
    if a == 'set':
        if len(args) < 3:
            print('用法：hanhua.py term set <日文> <中文> [备注]')
            return
        ok, msg, changed = glossary.set_term(
            args[1], args[2], args[3] if len(args) > 3 else '')
        print('已保存（之前翻过的行需要 retro）' if ok and changed
              else ('已保存' if ok else ('失败：%s' % msg)))
    elif a == 'del':
        if len(args) < 2:
            print('用法：hanhua.py term del <日文>')
            return
        ok, msg, _ = glossary.remove(args[1])
        print('已删除' if ok else ('失败：%s' % msg))
    elif a == 'retro':
        jps = args[1:] or [i['jp'] for i in glossary.items()]
        r = glossary.retro(jps)
        print('已退回 %d 行 / %d 个文件，引擎下一轮会用新译名重翻'
              % (r.get('lines', 0), r.get('files', 0)))
    else:
        print('未知子命令：%s' % a)


# ---------------------------------------------------------------- 状态
def cmd_status(_args=None):
    project = _mod('project')
    cfg = project.apply_all()
    if not cfg:
        print('还没有项目。先建一个：hanhua.py project new 游戏名 <日文源目录>')
        return
    st = project.stat()
    print('项目：%s（%s）' % (cfg['name'], '当前' if True else ''))
    print('  日文源：%s' % (cfg.get('src_dir') or '（未设置）'))
    print('  产物：  %s' % (cfg.get('out_dir') or '（未设置）'))
    if st.get('ok'):
        print('  进度：  %d/%d 行（%.2f%%）· 文件 %d/%d'
              % (st['done'], st['lines'], st['pct'],
                 st['done_files'], st['files']))
    else:
        print('  进度：  读不出来（%s）' % st.get('err'))
    try:
        glossary = _mod('glossary')
        print('  术语：  %d 条' % len(glossary.items()))
    except Exception:
        pass


# ---------------------------------------------------------------- 启停
def _spawn(args, detach):
    if detach:
        subprocess.Popen([PYEXE] + args,
                         creationflags=DETACHED | NO_WINDOW,
                         close_fds=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return '已在后台启动'
    return subprocess.run([PYEXE] + args).returncode


def _respawn(sub):
    """后台模式：用「自己」再开一个进程（打包后 sys.executable 就是 exe）。"""
    me = [sys.executable]
    if not getattr(sys, 'frozen', False):
        me.append(os.path.abspath(__file__))
    me.append(sub)
    subprocess.Popen(me, creationflags=DETACHED | NO_WINDOW,
                     close_fds=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def cmd_gui(args):
    detach = '--detach' in args
    if not detach:
        # 打包后磁盘上没有 server.py，必须进程内调，不能再 subprocess 跑脚本
        server = _mod('server')
        return server.main()
    _respawn('gui')
    # 端口可能因为被占用而后移，等它把实际端口写出来再报
    import time
    pf = os.path.join(APP_DIR, '_port.txt')
    for _i in range(40):
        time.sleep(0.25)
        try:
            v = io.open(pf, encoding='utf-8').read().strip()
            if v:
                print('网页工作台已在后台启动： http://127.0.0.1:%s' % v)
                print('  工作台 /manual · 实时窗口 /live · 总览 /')
                return
        except Exception:
            pass
    print('已在后台启动（端口还没写出来，稍等几秒再访问）')


def cmd_run(args):
    detach = '--detach' in args
    if detach:
        _respawn('run')
        print('翻译引擎已在后台启动（日志见 项目目录/_run.log）')
        return
    print('翻译引擎开始跑（Ctrl+C 停止）')
    runner = _mod('runner')
    return runner.main()


def cmd_acc(args):
    mt = _mod('mt')
    _mod('project').apply_all()
    if not args or args[0] == 'list':
        d = mt.acc_list()
        for p in d['pools']:
            print('%s：可用 %d / 共 %d' % (p['name'], p['avail'], p['n']))
            for i in p['items']:
                if not i.get('off'):
                    print('   %s %s' % (i['appid'][-4:], i.get('note') or ''))
        return
    a = args[0]
    if a in ('off', 'on') and len(args) >= 3:
        ok = mt.acc_disable(args[1], args[2], a == 'off')
        print('已%s %s·%s' % ('停用' if a == 'off' else '启用', args[1], args[2][-4:])
              if ok else '没找到这个账号')
    elif a == 'test' and len(args) >= 3:
        r = mt.acc_test(args[1], args[2])
        print(('通过 %dms' % r['ms']) if r.get('ok') else ('失败：%s' % r.get('err')))
    elif a == 'add' and len(args) >= 3:
        ok, msg = mt.acc_add(args[1], args[2],
                             args[3] if len(args) > 3 else 'llm')
        print('已添加' if ok else ('失败：%s' % msg))
    else:
        print('用法：hanhua.py acc [list|off|on|test|add] ...')


def cmd_cfg(args):
    mt = _mod('mt')
    _mod('project').apply_all()
    c = mt.load_run_cfg(force=True)
    if not args or args[0] == 'view':
        print('通道 %s · 并发 %d · 单请求 %d 行/%d 字符（红线 %d）· 间隔 %dms · 上限 %s'
              % (c['engine'], c['workers'], c['batch_lines'], c['batch_chars'],
                 c.get('char_cap', 0), c['sleep_ms'],
                 ('%d 次/分' % c['max_rpm']) if c['max_rpm'] else '不限'))
        return
    ok, msg, d = mt.save_run_cfg({args[0]: args[1]})
    print(('已保存 %s=%s' % (args[0], args[1])) if ok else ('没保存：%s' % msg))
    if ok and msg:
        print('⚠ %s' % msg)


# ---------------------------------------------------------------- 控制台菜单
def console():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print('=' * 56)
        print('  汉化工具 · 控制台')
        print('=' * 56)
        try:
            cmd_status()
        except Exception as e:
            print('  （状态读不出来：%r）' % e)
        print('-' * 56)
        print('  1  开网页工作台（推荐，所有功能都在里面）')
        print('  2  开始翻译（前台跑，Ctrl+C 停）')
        print('  3  开始翻译（后台跑，关掉窗口也继续）')
        print('  4  项目管理（新建 / 切换 / 设目录）')
        print('  5  术语表（固定译法）')
        print('  6  百度账号（停用 / 新增 / 体检）')
        print('  7  调速（并发 / 批次 / 频率）')
        print('  0  退出')
        print('-' * 56)
        c = input('请选择：').strip()
        if c == '1':
            cmd_gui([])
        elif c == '2':
            cmd_run([])
        elif c == '3':
            print(cmd_run(['--detach']))
            input('已在后台启动，回车继续…')
        elif c == '4':
            print('\n'.join('  %s' % p['name']
                            for p in _mod('project').list_projects()) or '  （没有项目）')
            name = input('切换/新建项目名（直接回车跳过）：').strip()
            if name:
                project = _mod('project')
                if not project.get(name):
                    src = input('  日文源目录：').strip()
                    out = input('  产物目录（留空用默认）：').strip()
                    ok, msg, _ = project.create(name, src, out)
                    print('  %s' % ('已创建' if ok else msg))
                print('  切到 %s' % name if project.apply_all(name) else '  失败')
                sd = input('  日文源目录（留空不改）：').strip()
                if sd:
                    project.update(name, {'src_dir': sd})
                od = input('  产物目录（留空不改）：').strip()
                if od:
                    project.update(name, {'out_dir': od})
                project.apply_all(name)
            input('回车继续…')
        elif c == '5':
            cmd_term(['list'])
            jp = input('\n日文原词（留空返回）：').strip()
            if jp:
                cn = input('固定译法：').strip()
                if cn:
                    cmd_term(['set', jp, cn])
            input('回车继续…')
        elif c == '6':
            cmd_acc(['list'])
            input('回车继续…')
        elif c == '7':
            cmd_cfg(['view'])
            k = input('\n要改的字段（workers/batch_chars/engine…，留空返回）：').strip()
            if k:
                cmd_cfg([k, input('新值：').strip()])
            input('回车继续…')
        elif c == '0':
            return


def cmd_build(_args=None):
    """打包成 exe（需要 PyInstaller：pip install pyinstaller）"""
    import shutil
    try:
        import PyInstaller
    except ImportError:
        print('没装 PyInstaller。先装：pip install pyinstaller')
        return
    # 路径全部用绝对：相对路径会被 workpath/specpath 带偏，报找不到文件
    # 不用 --clean：它会整批删 build/_work（几十个文件），触发删除保护把打包拦下来
    # 游戏图标：assets/game.ico 存在就带上（后续打出的 exe 都有图标）
    _ico = os.path.join(HERE, 'assets', 'game.ico')
    _icon_args = ['--icon', _ico] if os.path.isfile(_ico) else []

    spec = ['--noconfirm', '--onefile', '--console',
            '--name', '汉化工具',
            *_icon_args,
            '--paths', CORE, '--paths', UI,
            '--add-data', os.path.join(UI, 'page.html') + os.pathsep + 'ui',
            '--distpath', os.path.join(HERE, 'dist'),
            '--workpath', os.path.join(HERE, 'build', '_work'),
            '--specpath', os.path.join(HERE, 'build'),
            os.path.join(HERE, 'hanhua.py')]
    print('打包中（要几分钟）：pyinstaller ' + ' '.join(spec))
    r = subprocess.run([sys.executable, '-m', 'PyInstaller'] + spec)
    if r.returncode != 0:
        print('打包失败（退出码 %d）' % r.returncode)
        return
    import glob as _g
    exes = _g.glob(os.path.join(HERE, 'dist', '*.exe'))
    if not exes:
        print('打包完没找到 exe')
        return
    src = exes[0]
    # 挪到根目录：数据目录 = exe 所在目录/projects，放根目录才能和源码版共用一份
    dst = os.path.join(HERE, os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dst):
        import shutil
        shutil.copy2(src, dst)
    print('')
    print('打包好了：%s' % dst)
    print('  双击它就能用；想拷到别的电脑，把 exe 单独拷走即可')
    print('  （项目数据会建在 exe 旁边，默认 %s）'
          % os.path.join(os.path.dirname(dst), 'projects'))


def main():
    a = sys.argv[1:]
    if not a:
        console()
        return
    c = a[0]
    rest = a[1:]
    table = {'gui': cmd_gui, 'run': cmd_run, 'status': cmd_status,
             'project': cmd_project, 'term': cmd_term, 'acc': cmd_acc,
             'cfg': cmd_cfg, 'build': cmd_build,
             'console': lambda _x: console()}
    fn = table.get(c)
    if fn is None:
        print(__doc__)
        return
    fn(rest)


if __name__ == '__main__':
    main()
