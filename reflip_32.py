#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仅重翻此前生成的 32 个坏 _zh 对应的源文件（从 .tt/bak_zh 反推文件名）。
使用【已修复+加固】的引擎：修正日式RPG腔指令 + 空回/过短译文单条重试。
输出回 japanese/，写入 .tt/cache.json（好译文）。跑完自动自检漏翻/腔调。
"""
import os, re, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, MASK_RE

J = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
ACC = r'J:\tagatame\解包\汉化\accounts.json'
GLO = r'J:\tagatame\解包\_extract\剧情文档\Loc\glossary.json'
BAK_ZH = os.path.join(J, '.tt', 'bak_zh')

HIRA = re.compile(r'[ぁ-ん]')
TONE = ['日式RPG游戏台词', '吾', '汝', '殿下', '本王', 'ござる']
PAREN = re.compile(r'（[ぁ-んァ-ヶ]{1,8}）')


def main():
    fns = sorted(f[:-7] + '.txt' for f in os.listdir(BAK_ZH) if f.endswith('_zh.txt'))
    print('待重翻文件数: %d' % len(fns))
    e = Engine(proj_dir=J, accounts_path=ACC, glossary_path=GLO,
               out_dir=J, rps=2, batch=5, dryrun=False, context_on=True)

    # 让事件打印出来，便于观察
    orig = e.emit
    def emit(level, msg):
        if level in ('error', 'warn'):
            print('[%s] %s' % (level, msg))
        orig(level, msg)
    e.emit = emit

    for fn in fns:
        try:
            e.process_file(fn)
            print('✓ %s' % fn)
        except StopIteration:
            print('!! 58003 终止: %s' % fn)
            break
        except Exception as ex:
            print('!! 异常 %s: %r' % (fn, ex))
    e._save_cache()
    e._save_state()

    # ---- 自检 ----
    print('\n===== 自检（重翻后） =====')
    total = leak = tone = 0
    for fn in fns:
        zp = os.path.join(J, fn[:-4] + '_zh.txt')
        if not os.path.exists(zp):
            print('  缺失:', zp); continue
        for ln in open(zp, encoding='utf-8', newline='').read().split('\n'):
            if ln == '':
                continue
            c = ln.split('\t')
            if len(c) < 2 or c[1] == '':
                continue
            total += 1
            t2 = MASK_RE.sub('', c[1])
            if HIRA.search(t2):
                leak += 1
            if any(w in c[1] for w in TONE) or PAREN.search(c[1]):
                tone += 1
    print('译文行总数: %d' % total)
    print('漏翻(含平假名): %d (%.1f%%)' % (leak, 100.0 * leak / max(1, total)))
    print('日式RPG腔:     %d (%.1f%%)' % (tone, 100.0 * tone / max(1, total)))


if __name__ == '__main__':
    main()
