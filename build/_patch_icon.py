# -*- coding: utf-8 -*-
"""hanhua.py cmd_build：assets/game.ico 存在时自动加 --icon。"""
import io
import os

P = r'J:\hanhua\hanhua.py'
s = io.open(P, encoding='utf-8', newline='').read()
eol = '\r\n' if '\r\n' in s else '\n'

anchor = "    spec = ['--noconfirm', '--onefile', '--console',"
assert s.count(anchor) == 1, s.count(anchor)

inject = (
    "    # 游戏图标：assets/game.ico 存在就带上（后续打出的 exe 都有图标）" + eol +
    "    _ico = os.path.join(HERE, 'assets', 'game.ico')" + eol +
    "    _icon_args = ['--icon', _ico] if os.path.isfile(_ico) else []" + eol +
    eol +
    anchor)
s = s.replace(anchor, inject)

anchor2 = ("            '--name', '汉化工具'," + eol +
           "            '--paths', CORE, '--paths', UI,")
assert s.count(anchor2) == 1, s.count(anchor2)
s = s.replace(anchor2,
              "            '--name', '汉化工具'," + eol +
              "            *_icon_args," + eol +
              "            '--paths', CORE, '--paths', UI,")

bak = P + '.bak-icon'
if not os.path.exists(bak):
    io.open(bak, 'w', encoding='utf-8', newline='').write(
        io.open(P, encoding='utf-8', newline='').read())
io.open(P, 'w', encoding='utf-8', newline='').write(s)
import ast
ast.parse(s)
print('hanhua.py patched + syntax OK')
