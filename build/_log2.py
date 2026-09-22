# -*- coding: utf-8 -*-
import io

note = '''
### GitHub 自动发版搭建（20:49-21:10）
- 仓库 github.com/keyminori/hanhua-tool（master，已有 v1.0.0），gh 已登录 keyminori
- 新增 release.py 一键发版：add/commit -> 取最新 tag bump 版本 -> push + tag；--local 走本机打包直发
- 新增 .github/workflows/release.yml：push v* tag -> Actions windows-latest 自动 PyInstaller 打包建 Release
- ⚠ 推送含 workflow 文件的提交被拒：gh 令牌缺 workflow scope；已拆分推送（源码已上，workflow 本地待授权）
  激活方法：gh auth refresh -h github.com -s workflow（浏览器输码）后 git add .github && git commit && git push
- ⚠ GitHub Release 附件名会把非 ASCII 全剥掉：纯中文名 -> default.exe（报 already_exists）。
  附件固定用英文稳定名 hanhua-tool.exe，Release 标题/说明仍中文
- v1.0.1 已发：https://github.com/keyminori/hanhua-tool/releases/tag/v1.0.1（含按钮修复版 exe）
- git/gh 的中文参数经本机坏 bash 会乱码，凡是传中文参数一律用 python subprocess/urllib
- git.exe 实际位置：PortableGit versions/1.2.0/cmd/git.exe；gh 在 Program Files/GitHub CLI
'''
io.open(r'J:\tagatame\.workbuddy\memory\2026-09-22.md', 'a', encoding='utf-8').write(note)

memp = r'J:\tagatame\.workbuddy\memory\MEMORY.md'
m = io.open(memp, encoding='utf-8', newline='').read()
line = '\n## GitHub 发版\n- hanhua 仓库发版 = `python release.py "说明"`（tag 自动 bump 推送）；附件名必须 ASCII（GitHub 剥中文）-> 固定 hanhua-tool.exe\n- Actions 云端构建待激活：令牌缺 workflow scope，跑 `gh auth refresh -h github.com -s workflow` 后提交 .github/ 即可\n'
if 'GitHub 发版' not in m:
    io.open(memp, 'a', encoding='utf-8', newline='').write(line)
print('logged')
