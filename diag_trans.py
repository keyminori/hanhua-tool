#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用【修复后的指令】真实翻译几行日文，验证：
  - "日式RPG游戏台词"腔是否消失
  - 是否还漏翻（译文仍含日文假名）
输出到独立 /tmp/diag，绝不污染项目 .tt 与 _zh。
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine

J = r'J:\tagatame\解包\_extract\剧情文档\Loc\japanese'
ACC = r'J:\tagatame\解包\汉化\accounts.json'
GLO = r'J:\tagatame\解包\_extract\剧情文档\Loc\glossary.json'
OUT = r'/tmp/diag'

e = Engine(proj_dir=J, accounts_path=ACC, glossary_path=GLO,
           out_dir=OUT, rps=1, batch=5, dryrun=False, context_on=True)

# 抽 07_a_2d.txt 与 02_a_2d.txt 里真实日文行
targets = ['07_a_2d.txt', '02_a_2d.txt']
samples = []
for fn in targets:
    for body, term, raw in e.read_pairs(os.path.join(J, fn)):
        if body == '':
            continue
        c = body.split('\t')
        if len(c) >= 2 and re.search(r'[ぁ-んァ-ヶ]', c[1]):
            samples.append(c[1])
        if len(samples) >= 8:
            break
    if len(samples) >= 8:
        break

print('=== 修复后指令真实翻译诊断（仅 %d 行，独立缓存）===' % len(samples))
HIRA = re.compile(r'[ぁ-ん]')
for jp in samples:
    try:
        tr = e.llm_batch([jp])[0]
    except Exception as ex:
        tr = '!!ERR %r' % ex
    leak = '⚠漏翻' if HIRA.search(tr) else '✓'
    print('%s JP: %s' % (leak, jp))
    print('   TR: %s' % tr)
    print('-' * 50)
