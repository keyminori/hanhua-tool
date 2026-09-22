# -*- coding: utf-8 -*-
"""
剧情对白批量机翻

架构要点（踩过坑才改成这样）：
  - **跨文件批量**：早期版本逐文件翻译，并发被小文件切碎（32 workers 大部分空转，
    实测只有 1.3 行/秒）。现在先全局收集待译行 -> 一次性大池并发 -> 再写回各文件，
    并发度才能填满（实测 21.9 req/s）。
  - 硬约束：
    1. 语音 cue（第3列）取自日文原文，逐行透传，写入后回读校验
    2. 行数守恒、索引顺序不变
    3. 已有中文覆盖优先保留，只填空缺（例外：值恰为本行机翻草稿 mt 时允许覆盖）
    4. 失败行保留日文原文，绝不空值/占位符污染
  - 支持断点续跑：已译行进 mt_cache.json，重复启动不浪费请求。

用法:
  mt_story.py --batch 8000              # 处理约 8000 行后收工（推荐分批跑）
  mt_story.py --all                     # 一次跑完（耗时长，可能中断）
  mt_story.py --file 01_a_2d.txt        # 单文件
"""
import os
import sys
import glob
import time
import re
import argparse
import collections

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import mt                                    # noqa: E402

# 日文源 / 中文产物目录：**由 project.apply() 注入**，这里只留空壳。
JP = ''
CN_DIR = ''

# 列格式：不同游戏导出的表不一样（有的没有语音 cue，有的用 csv）。
# COLS = (索引列, 原文列, cue列)，cue 列填 -1 表示「本项目没有这一列」。
SEP = '\t'
COLS = (0, 1, 2)

# 只想翻某些文件时用的正则（None = 全收）。例如只翻剧情：r'^(qe|eq_|cq_)'。
FILE_RE = None


# 中文间隔号「・」(U+30FB) 落在片假名区间里，但中文译文里大量用它做
# 姓名/名词分隔（「吉克・克劳利」「三杰・最后的一人」）。不排除它的话，
# 这类**已经翻好的行**会被判成日文 -> 永远算「需翻」-> 每轮空转。
_KANA_SKIP = frozenset('\u30fb')


def has_kana(s):
    return any(((0x3040 <= ord(c) <= 0x3096) or (0x30A0 <= ord(c) <= 0x30FA))
               and c not in _KANA_SKIP for c in s)


def is_story(name):
    """这个文件要不要翻。

    原来是《为谁炼金》专属的一串前缀判断（qe/eq_/cq_/_a_2d…），换项目就全错。
    现在默认全收，需要筛文件时由项目配置给一个正则（FILE_RE）。
    """
    if not FILE_RE:
        return True
    try:
        # 忽略大小写：文件名里 QE05_... 和 qe05_... 混着是常态，
        # 让用户为一个过滤规则还要操心大小写太坑了
        return bool(re.search(FILE_RE, name, re.IGNORECASE))   # 大小写不敏感
    except Exception:
        return True


def _replace_retry(tmp, dst, tries=6):
    """os.replace 在 Windows 上偶发 WinError 5（目标被索引/杀软短暂占用）。

    一轮要写回 444 个文件，偶发一次失败就会让整轮作废，所以重试几次再放弃。
    """
    last = None
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return True
        except OSError as e:
            last = e
            time.sleep(0.15 * (i + 1))
    raise last


def read_tsv(path):
    """读一张表 -> [(id, 原文, cue)]。

    列号/分隔符取模块级 COLS / SEP（项目可配），cue 列给 -1 表示没有这一列。
    """
    rows = []
    ic, tc, cc = COLS

    def cell(p, i):
        return p[i] if 0 <= i < len(p) else ''

    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n').rstrip('\r')
            if not line.strip():
                continue
            p = line.split(SEP)
            rows.append([cell(p, ic), cell(p, tc), cell(p, cc)])
    return rows


def collect(files, budget):
    """收集待译行。返回 {file: (rows, prev, need)}, 唯一原文列表, 总需翻数"""
    data = {}
    uniq = []
    seen = set()
    total_need = 0
    for name in files:
        rows = read_tsv(os.path.join(JP, name))
        prev = {}
        dst = os.path.join(CN_DIR, name)
        if os.path.exists(dst):
            prev = {r[0]: r[1] for r in read_tsv(dst)}
        need = 0
        for idx, txt, cue in rows:
            cur = prev.get(idx, '')
            if mt.manual_ov(name, idx):
                continue              # 人工改过稿（工作台/ctl set）-> 永不重译
            if has_kana(txt) and (not cur or has_kana(cur)):
                need += 1
                if txt not in seen:
                    seen.add(txt)
                    uniq.append(txt)
        if need:
            data[name] = (rows, prev, need)
            total_need += need
        if budget and total_need >= budget:
            break
    return data, uniq, total_need


def write_back(data, got):
    """把译文写回各文件，并做行数/cue 闸门校验"""
    stat = collections.Counter()
    _ic, _tc, cc = COLS
    for name, (rows, prev, need) in data.items():
        out = []
        for idx, txt, cue in rows:
            val = prev.get(idx, '')
            ov = mt.manual_ov(name, idx)
            # 人工改稿台账（工作台/ctl set）最高优先级：collect 拿到的 prev 可能
            # 是改稿前的旧快照，不查台账就会把人工译文冲回日文/机翻。
            # val 恰好是本行登记的机翻草稿(mt) 时，视为「还没正式译文」：
            # 允许被本次译文盖掉，否则草稿会永久钉在产物里（见 mt.sens_draft）
            if ov:
                val = ov
            elif val and not has_kana(val) and val != mt.sens_draft(txt):
                pass                       # 已有中文，保留
            elif txt in got:
                val = got[txt]
            else:
                val = txt                  # 失败 -> 保留日文
            out.append(SEP.join((idx, val, cue)) if cc >= 0
                       else SEP.join((idx, val)))
        dst = os.path.join(CN_DIR, name)
        tmp = dst + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
        _replace_retry(tmp, dst)
        # 闸门：回读校验行数 + 索引 + cue
        back = read_tsv(dst)
        if len(back) != len(rows):
            stat['bad_len'] += 1
            print('  [行数异常] %s %d != %d' % (name, len(back), len(rows)),
                  flush=True)
            continue
        bad = False
        for (i1, _, c1), (i2, _, c2) in zip(rows, back):
            if i1 != i2 or c1 != c2:
                stat['bad_cue'] += 1
                print('  [cue串位] %s @%s' % (name, i1), flush=True)
                bad = True
                break
        if not bad:
            stat['ok'] += 1
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少个文件')
    ap.add_argument('--batch', type=int, default=0, help='本轮最多翻译多少行')
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('--workers', type=int, default=0,
                    help='并发线程数（0=按引擎取默认值）')
    ap.add_argument('--engine', default='baidu_llm',
                    choices=list(mt.ENGINE_CFG.keys()))
    ap.add_argument('--no-probe', action='store_true', help='跳过账号池自检')
    ap.add_argument('--file', default=None)
    ap.add_argument('--all', action='store_true')
    args = ap.parse_args()

    if args.no_probe:
        mt._PROBED = True

    os.makedirs(CN_DIR, exist_ok=True)
    if args.file:
        files = [args.file]
    else:
        files = [os.path.basename(p) for p in glob.glob(JP + '/*.txt')
                 if is_story(os.path.basename(p))]
        files.sort()
        files = files[args.start:]
        if args.limit:
            files = files[:args.limit]

    budget = 0 if args.all else (args.batch or 8000)
    print('扫描待译行 (本轮上限 %s 行)...' % (budget or '全部'), flush=True)
    data, uniq, total_need = collect(files, budget)
    if not uniq:
        print('没有待译内容，收工。')
        return
    print('待译文件 %d 个，需翻 %d 行，其中唯一 %d 行'
          % (len(data), total_need, len(uniq)), flush=True)

    tr = mt.Translator(workers=args.workers or None, engine=args.engine)
    t0 = time.time()
    res = tr.translate(uniq)
    dt = time.time() - t0

    got = {}
    for a, b in zip(uniq, res):
        if b and b.strip() and 'QXZ' not in b and 'ZQX' not in b:
            got[a] = b

    stat = write_back(data, got)
    tr.cache.save()

    print()
    print('机翻 %d/%d 条唯一文本，用时 %.1fs  -> %.2f 行/秒'
          % (len(got), len(uniq), dt, len(uniq) / max(dt, 0.1)))
    print('写回文件 %d 个（校验通过 %d）' % (len(data), stat['ok']))
    st = dict(tr.stats)
    print('引擎统计:', st)
    if st.get('whole'):
        print('整行送翻 %d 行，其中标记不符退回片段级 %d 行 (%.1f%%)'
              % (st['whole'], st.get('markup_retry', 0),
                 100.0 * st.get('markup_retry', 0) / st['whole']))
    print('闸门: 行数异常=%d  cue串位=%d' % (stat['bad_len'], stat['bad_cue']))


if __name__ == '__main__':
    main()
