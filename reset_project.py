#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全重置汉化项目，使已生成的坏译文可重翻。
BACKUP-FIRST：先完整备份再改，可回滚。

做了什么：
  1) 备份整个 .tt/ (含坏 cache.json / state.json) 到 .tt.bak_<时间戳>/
  2) 备份所有 *_zh.txt 到 .tt/bak_zh/
  3) 清空 .tt/cache.json（坏译文锁死在这里，必须清）
  4) 重置 .tt/state.json 的 done_files（否则引擎跳过这 32 文件）
  5) 删除 32 个坏 *_zh.txt（让引擎重翻重新生成，否则会读回坏 existing）

回滚：把 .tt.bak_<时间戳>/ 拷回 .tt/ 即可。
"""
import os, json, shutil, sys, time

J = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
TT = os.path.join(J, '.tt')
BAK = os.path.join(J, '.tt.bak_%s' % time.strftime('%Y%m%d_%H%M%S'))


def log(m):
    print('[reset] ' + m)


def main():
    if not os.path.isdir(J):
        log('目录不存在: ' + J)
        return
    zh = sorted(f for f in os.listdir(J) if f.endswith('_zh.txt'))
    log('发现 _zh.txt: %d 个' % len(zh))

    # 1) 备份 .tt 整体
    if os.path.isdir(TT):
        shutil.copytree(TT, BAK)
        log('已备份 .tt -> %s' % BAK)
    else:
        os.makedirs(TT, exist_ok=True)
        log('.tt 不存在，已新建')

    # 2) 备份 _zh.txt
    bak_zh = os.path.join(TT, 'bak_zh')
    os.makedirs(bak_zh, exist_ok=True)
    for f in zh:
        shutil.copy2(os.path.join(J, f), os.path.join(bak_zh, f))
    log('已备份 %d 个 _zh.txt -> %s' % (len(zh), bak_zh))

    # 3) 清空 cache
    cp = os.path.join(TT, 'cache.json')
    json.dump({}, open(cp, 'w', encoding='utf-8'), ensure_ascii=False)
    log('已清空 cache.json (坏译文锁解除)')

    # 4) 重置 state 的 done_files（保留 exhausted 以免重复踩封禁）
    sp = os.path.join(TT, 'state.json')
    state = {}
    if os.path.exists(sp):
        try:
            state = json.load(open(sp, encoding='utf-8'))
        except Exception:
            state = {}
    exhausted = state.get('exhausted', [])
    json.dump({'exhausted': exhausted, 'acc_idx': 0, 'done_files': []},
              open(sp, 'w', encoding='utf-8'), ensure_ascii=False)
    log('已重置 state.json (done_files 清空，保留 exhausted=%d)' % len(exhausted))

    # 5) 删除坏 _zh.txt
    for f in zh:
        os.remove(os.path.join(J, f))
    log('已删除 %d 个坏 _zh.txt（重翻将重新生成）' % len(zh))
    log('完成。现在重启引擎即可重翻这 %d 个文件（记得先确认指令已修复）。' % len(zh))


if __name__ == '__main__':
    main()
