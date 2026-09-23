#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
审计已生成的 _zh.txt 与 .tt/cache.json，量化两类损坏：
  1) 漏翻译：译文列仍含日文假名（去除伪标记后），说明没翻出来/被留原文
  2) 日式RPG腔：译文含典型配音腔/文言腔/中二腔词，或模型把假名注音塞进括号
只读不写，安全。
用法：python audit_translations.py [japanese_dir]
"""
import os, re, sys, json

J = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
if len(sys.argv) > 1:
    J = sys.argv[1]

MASK_RE = re.compile(r'Z(\d+)Q\1Z')
# 腔调信号（强）
TONE_STRONG = ['日式RPG游戏台词', '吾', '汝', '殿下', '阁下', '在下', '本王', 'ござる',
               'である', 'であるぞ', 'じゃ', 'じゃの', '～だ', '～じゃ']
# 括号里塞日文注音，如 蜥蜴（とかげ）
PAREN_KANA = re.compile(r'（[ぁ-んァ-ヶ]{1,8}）')
KANA = re.compile(r'[ぁ-んァ-ヶ]')
HIRA = re.compile(r'[ぁ-ん]')


def strip_mask(s):
    return MASK_RE.sub('', s)


def audit_file(zp, sp):
    """返回 (总行, 漏翻行, 腔调行, 坏样本[(jp,tr)])。sp 为源文件可选。"""
    total = leak = tone = 0
    bad = []
    try:
        with open(zp, encoding='utf-8', newline='') as f:
            lines = f.read().split('\n')
    except Exception:
        return 0, 0, 0, []
    # 对齐源日文（若源文件存在）
    src_jp = {}
    if sp and os.path.exists(sp):
        try:
            with open(sp, encoding='utf-8', newline='') as f:
                for ln in f.read().split('\n'):
                    c = ln.split('\t')
                    if len(c) >= 2:
                        src_jp[c[0]] = c[1]
        except Exception:
            pass
    for ln in lines:
        if ln == '':
            continue
        c = ln.split('\t')
        if len(c) < 2:
            continue
        tr = c[1]
        if tr == '':
            continue
        total += 1
        t2 = strip_mask(tr)
        # 漏翻：去除伪标记后仍含平假名（平假名几乎必然需翻译）
        if HIRA.search(t2):
            leak += 1
            bad.append(('LEAK', src_jp.get(c[0], ''), tr))
            continue
        # 腔调
        hit = any(w in tr for w in TONE_STRONG) or bool(PAREN_KANA.search(tr))
        if hit:
            tone += 1
            bad.append(('TONE', src_jp.get(c[0], ''), tr))
    return total, leak, tone, bad


def main():
    zh_files = sorted(f for f in os.listdir(J) if f.endswith('_zh.txt'))
    print('=' * 60)
    print('审计目录: %s' % J)
    print('_zh.txt 文件数: %d' % len(zh_files))
    print('=' * 60)
    grand_total = grand_leak = grand_tone = 0
    per_file = []
    all_bad = []
    for zf in zh_files:
        zp = os.path.join(J, zf)
        sp = os.path.join(J, zf[:-7] + '.txt')  # 去 _zh 得源
        t, lk, tn, bad = audit_file(zp, sp)
        grand_total += t
        grand_leak += lk
        grand_tone += tn
        if lk or tn:
            per_file.append((zf, t, lk, tn))
            all_bad.extend(bad)
    per_file.sort(key=lambda x: -(x[2] + x[3]))
    print('\n--- 按损坏严重程度排序（文件 / 总行 / 漏翻 / 腔调）---')
    for zf, t, lk, tn in per_file[:15]:
        print('  %-26s 行=%3d 漏翻=%3d 腔调=%3d' % (zf, t, lk, tn))
    print('\n--- 汇总 ---')
    print('已生成译文行总数: %d' % grand_total)
    print('漏翻译(仍含平假名)行: %d (%.1f%%)' % (grand_leak, 100.0 * grand_leak / max(1, grand_total)))
    print('日式RPG腔行:         %d (%.1f%%)' % (grand_tone, 100.0 * grand_tone / max(1, grand_total)))
    print('任一损坏行:           %d (%.1f%%)' % (len(all_bad), 100.0 * len(all_bad) / max(1, grand_total)))

    # 缓存体检
    cache_path = os.path.join(J, '.tt', 'cache.json')
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding='utf-8'))
            bad_cache = 0
            for k, v in cache.items():
                if isinstance(v, str) and HIRA.search(strip_mask(v)) and v != k:
                    bad_cache += 1
            print('\n--- .tt/cache.json ---')
            print('缓存条目: %d  其中坏缓存(值仍含平假名且≠键): %d' % (len(cache), bad_cache))
        except Exception as e:
            print('缓存读取失败:', e)

    print('\n--- Top 20 坏样本 (类型 / 原文 / 译文) ---')
    for kind, jp, tr in all_bad[:20]:
        print('[%s] 原:%s' % (kind, jp[:50]))
        print('      译:%s' % (tr[:80]))


if __name__ == '__main__':
    main()
