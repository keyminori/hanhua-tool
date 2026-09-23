# -*- coding: utf-8 -*-
"""修复存量坏行：<br> 数量不符 / 低质(无中文/残留假名/截断)。
逐行用引擎的 _retry_single（补强指令 + 换账号）重翻，格式保真写回 chinese/，
成功后覆盖缓存。用法: python repair_bad_lines.py
"""
import os, re, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine

JAP = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
CHN = r'J:\tagatame\解包\_extract\剧情文档\Loc\chinese'
TT  = r'J:\tagatame\翻译工具\data\tagatame\.tt'

kana = re.compile(r'[ぁ-んァ-ヶ]')
han  = re.compile(r'[一-鿿]')


def is_bad(jp, tr):
    """返回坏因描述；None=良好。判据与引擎闸门一致(双向br+注音豁免)。"""
    if not tr or not han.search(tr):
        return '无中文'
    t3 = re.sub(r'（[ぁ-んァ-ヶ]+）', '', tr)
    if kana.search(t3):
        return '残留假名'
    if jp.count('<br>') != tr.count('<br>'):
        return 'br不符 %d→%d' % (jp.count('<br>'), tr.count('<br>'))
    return None


def split_term(line):
    if line.endswith('\r\n'):
        return line[:-2], '\r\n'
    if line.endswith('\n'):
        return line[:-1], '\n'
    return line, ''


def main():
    eng = engine.Engine(
        proj_dir=JAP, out_dir=CHN,
        accounts_path=r'J:\tagatame\解包\汉化\accounts.json',
        glossary_path=r'J:\tagatame\解包\_extract\剧情文档\Loc\glossary.json',
        metadir=TT, rps=1.0, batch=5, context_on=True)
    eng.cache = json.load(open(os.path.join(TT, 'cache.json'), encoding='utf-8'))

    todo = []   # (fn, lineno, jp, old_tr, reason)
    for fn in sorted(os.listdir(CHN)):
        if not fn.endswith('.txt'):
            continue
        jps = open(os.path.join(JAP, fn), encoding='utf-8', newline='').readlines()
        trs = open(os.path.join(CHN, fn), encoding='utf-8', newline='').readlines()
        if len(jps) != len(trs):
            print('!! 行数不一致，跳过', fn)
            continue
        for i, (a, b) in enumerate(zip(jps, trs)):
            ca, cb = split_term(a)[0].split('\t'), split_term(b)[0].split('\t')
            if len(ca) < 2 or len(cb) < 2 or not kana.search(ca[1]):
                continue
            reason = is_bad(ca[1], cb[1])
            if reason:
                todo.append((fn, i, ca[1], cb[1], reason))
    print('待修复行数:', len(todo))
    for fn, i, jp, tr, r in todo:
        print('  %s#%d [%s] %r -> %r' % (fn, i, r, jp[:30], tr[:24]))

    fixed, failed = 0, []
    for fn, i, jp, old_tr, reason in todo:
        tr = eng._retry_single(jp)
        if tr is None:
            failed.append((fn, i, jp, reason))
            eng.emit('error', '修复失败(留人工): %s#%d %s' % (fn, i, jp[:30]))
            continue
        # 格式保真写回
        path = os.path.join(CHN, fn)
        with open(path, encoding='utf-8', newline='') as f:
            lines = f.readlines()
        body, term = split_term(lines[i])
        cols = body.split('\t')
        cols[1] = tr
        lines[i] = '\t'.join(cols) + term
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
        eng.cache[jp] = tr            # 覆盖缓存（可能存在旧坏译文）
        fixed += 1
        print('✓ %s#%d: %r -> %r' % (fn, i, jp[:28], tr[:36]))

    eng._save_cache()
    print('\n修复 %d / %d，失败 %d' % (fixed, len(todo), len(failed)))
    for x in failed:
        print('  FAILED', x)

    # 复扫验证
    print('\n=== 复扫 ===')
    remain = 0
    for fn in sorted(os.listdir(CHN)):
        if not fn.endswith('.txt'):
            continue
        jps = open(os.path.join(JAP, fn), encoding='utf-8', newline='').readlines()
        trs = open(os.path.join(CHN, fn), encoding='utf-8', newline='').readlines()
        for i, (a, b) in enumerate(zip(jps, trs)):
            ca, cb = split_term(a)[0].split('\t'), split_term(b)[0].split('\t')
            if len(ca) < 2 or len(cb) < 2 or not kana.search(ca[1]):
                continue
            r = is_bad(ca[1], cb[1])
            if r:
                remain += 1
                print('  残留 %s#%d [%s] %r' % (fn, i, r, ca[1][:30]))
    print('残留坏行:', remain)


if __name__ == '__main__':
    main()
