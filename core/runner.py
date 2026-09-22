# -*- coding: utf-8 -*-
"""剧情翻译独立运行器（供 Windows 计划任务拉起）。

为什么要这个文件：
  之前的全量任务由 Agent 会话的后台通道拉起，进程是会话的子进程——会话一结束
  就被系统回收，表现是「后台卡死」：日志停在中途、python 进程消失、chinese/ 里
  一个文件都没更新（因为 --all 只在全部跑完才写回）。
  放在纯 ASCII 路径（J:\\tagatame\\）是为了让计划任务能拉起它而不必在命令行
  里传中文路径（J:\\tagatame\\解包\\汉化 含中文，schtasks/cmd 传参会乱码）。

策略：每轮最多 20000 行，译完**立刻写回 chinese/**，然后进入下一轮。
  - 进度可见：每轮结束 chinese/ 都会新增已译文件
  - 抗中断：缓存每 2000 行落盘一次，重启自动续跑，最多丢一轮未译部分
  - 完成判定：collect() 找不到待译行即收工
"""
import os
import sys
import glob
import time
import io
import traceback

# 工作目录由项目配置决定（不能写死某个游戏的路径）：
#   python runner.py                跑“当前项目”
#   python runner.py --project 项目名
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 工具根
CORE = os.path.join(D, 'core')
sys.path.insert(0, CORE)
os.chdir(CORE)

_PROJ = None
for i, a in enumerate(sys.argv[1:]):
    if a in ('--project', '-p') and i + 2 <= len(sys.argv):
        _PROJ = sys.argv[i + 2]
LOG = None
log = None              # main() 里按项目目录打开（每个项目一份日志）

ROUND = 20000          # 每轮行数上限（兜底值；实际以 engine_cfg.json 的
                       # round_lines 为准，每轮开始时重读一次）


def out(msg):
    if log is None:
        print(msg)
        return
    log.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    log.flush()


def main():
    import mt
    import mt_story as S
    import project

    global log
    PCFG = project.apply_all(_PROJ)          # 源目录 / 产物目录 / 状态文件全部就位
    if not PCFG:
        print('没有可用的项目：先在工具里新建一个项目（选好日文源目录与产物目录）')
        return
    LOG = os.path.join(project.workspace(PCFG['name']), '_run.log')
    log = io.open(LOG, 'a', encoding='utf-8', buffering=1)

    out('=' * 60)
    out('汉化引擎启动 pid=%d · 项目 %s' % (os.getpid(), PCFG['name']))
    out('日文源：%s' % PCFG.get('src_dir'))
    out('产物：  %s' % PCFG.get('out_dir'))
    n = 0
    total_done = 0
    repaired = False
    t_start = time.time()
    while True:
        n += 1
        # 每轮认一次最新调速配置：改「每轮行数 / 引擎」下一轮生效；
        # 并发与批次改动由引擎自己热重载（最迟一波批次 ≈25 秒）。
        try:
            cfg = mt.load_run_cfg(force=True)
        except Exception:
            cfg = {'engine': 'baidu_llm', 'workers': 6, 'batch_lines': 40,
                   'batch_chars': 3000, 'block_lines': 2000,
                   'round_lines': ROUND, 'sleep_ms': 0, 'batch_pause_ms': 0,
                   'max_rpm': 0}
        round_lines = max(100, int(cfg.get('round_lines') or ROUND))
        eng = cfg.get('engine') or 'baidu_llm'
        files = [f for f in project.source_files(PCFG) if S.is_story(f)]
        files.sort()
        data, uniq, total_need = S.collect(files, round_lines)
        if not uniq:
            # 全部译完 -> 做一次术语回溯修补：把「术语改动前译出」的旧行退回待译
            # （规则取自 tm_manual.json）。命中则再跑一轮，形成自愈闭环。
            if not repaired:
                repaired = True
                try:
                    import glossary
                    _cns = [v.get('cn') for v in glossary.load().values()
                            if isinstance(v, dict) and v.get('cn')]
                    r = glossary.retro_old_cns(_cns)
                    h, nf = r.get('lines', 0), r.get('files', 0)
                    out('术语回溯修补：命中 %d 行 / %d 文件' % (h, nf))
                    if h:
                        continue
                except Exception:
                    out('术语回溯修补失败：\n' + traceback.format_exc())
            out('第 %d 轮：已无待译内容 -> 全部完成。' % n)
            break
        out('第 %d 轮：%d 个文件 / 需翻 %d 行 / 唯一 %d 行'
            % (n, len(data), total_need, len(uniq)))
        out('  调速：引擎 %s · 并发 %d · 单请求 %d 行/%d 字符 · 每块 %d 行 · '
            '间隔 %dms · 上限 %s'
            % (eng, cfg['workers'], cfg['batch_lines'], cfg['batch_chars'],
               cfg['block_lines'], cfg['sleep_ms'],
               ('%d 次/分' % cfg['max_rpm']) if cfg['max_rpm'] else '不限'))
        tr = None
        got = {}
        st = {}
        req0 = mt.req_count()
        t0 = time.time()
        try:
            tr = mt.Translator(engine=eng)
            res = tr.translate(uniq)
            for a, b in zip(uniq, res):
                if b and b.strip() and 'QXZ' not in b and 'ZQX' not in b:
                    got[a] = b
            stat = S.write_back(data, got)
            tr.cache.save()
        except KeyboardInterrupt:
            raise
        except Exception:
            # 一轮两万行、上千个文件，任何一处异常都不该让整个任务退出：
            # 踩过（2026-09-22 01:04）—— 别的进程正读 mt_cache.json 时
            # os.replace 抛 WinError 5，引擎直接退出、停摆 8 分钟没人发现。
            out('!! 第 %d 轮出错（跳过本轮，20 秒后重试）：\n%s'
                % (n, traceback.format_exc()))
            try:
                if tr is not None:
                    tr.cache.save()
            except Exception:
                pass
            time.sleep(20)
            continue
        total_done += len(got)
        st = dict(tr.stats)
        _dt = max(0.01, time.time() - t0)
        out('第 %d 轮完成：译出 %d/%d，写回 %d 文件(校验通过 %d)，用时 %.1fs'
            % (n, len(got), len(uniq), len(data), stat['ok'], _dt))
        try:
            _rc = mt.req_count() - req0
            _c = mt.load_run_cfg()
            out('  本轮发出请求 %d 次（%.2f 请求/行，%.2f 请求/秒）；当前调速：'
                '并发 %d · 单请求 %d 行 · 间隔 %dms · 上限 %s'
                % (_rc, _rc / float(max(1, len(uniq))), _rc / _dt,
                   _c['workers'], _c['batch_lines'], _c['sleep_ms'],
                   ('%d 次/分' % _c['max_rpm']) if _c['max_rpm'] else '不限'))
        except Exception:
            pass
        out('  引擎统计 %s' % st)
        try:
            _s = mt.load_sensitive()
            _pend = sum(1 for v in _s.values()
                        if not (v.get('cn') or '').strip())
            if _s:
                out('  敏感隔离：累计 %d 条（待人工 %d 条）-> sensitive.json'
                    % (len(_s), _pend))
        except Exception:
            pass
        if stat['bad_len'] or stat['bad_cue']:
            out('  !! 闸门异常 行数=%d cue=%d' % (stat['bad_len'], stat['bad_cue']))
    out('全部结束：累计译出 %d 条，总用时 %.1f 小时'
        % (total_done, (time.time() - t_start) / 3600.0))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log.write(traceback.format_exc())
        log.flush()
        raise
    finally:
        out('run_tagatame 退出')
        log.close()
