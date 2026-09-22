# -*- coding: utf-8 -*-
r"""一键推送发版：提交改动 -> 打版本 tag -> 推到 GitHub -> Actions 自动打包并建 Release。

用法（在 J:\hanhua 目录下）：
    python release.py "本次改了什么"            # 默认 patch 版本号 +1（v1.0.0 -> v1.0.1）
    python release.py "说明" --minor            # 次版本 +1（v1.0.x -> v1.1.0）
    python release.py "说明" --major            # 主版本 +1（v1.x.x -> v2.0.0）
    python release.py "说明" --local            # 不走云端，本机打包并建 Release（要装 PyInstaller + gh）

发版流程（默认，全自动）：
    1. git add -A + commit（没有改动就跳过）
    2. 取最新 tag，按参数 bump 版本，打新 tag
    3. push master 和 tag 到 origin
    4. GitHub Actions 检测到 v* tag，自动在 windows 环境打包 汉化工具.exe 并创建 Release
       进度看：https://github.com/<owner>/<repo>/actions
"""
import re
import subprocess
import sys

HERE = r"J:\hanhua"
GIT_CANDS = [r"C:\Users\keymi\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"]
GH_CANDS = [r"C:\Program Files\GitHub CLI\gh.exe"]


def exe(name, cands):
    import shutil
    p = shutil.which(name)
    if p:
        return p
    for c in cands:
        import os
        if os.path.isfile(c):
            return c
    raise SystemExit("找不到 %s，请先安装并加入 PATH" % name)


def run(argv, **kw):
    r = subprocess.run(argv, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        raise SystemExit("命令失败：%s\n%s%s" % (" ".join(argv), r.stdout, r.stderr))
    return r.stdout.strip()


def main():
    args = sys.argv[1:]
    level = "patch"
    local = False
    for f in ("--major", "--minor", "--local"):
        if f in args:
            args.remove(f)
            if f == "--major":
                level = "major"
            elif f == "--minor":
                level = "minor"
            else:
                local = True
    note = args[0] if args else "常规更新"

    git = exe("git", GIT_CANDS)
    gh = exe("gh", GH_CANDS)

    remote = run([git, "remote", "get-url", "origin"])
    repo = re.sub(r"^https://github\.com/|\.git$", "", remote.strip())

    # 1) 提交现有改动（没改动就跳过）
    run([git, "add", "-A"])
    st = run([git, "status", "--porcelain"])
    if st:
        run([git, "commit", "-m", "发版：%s" % note])
        print("已提交改动")
    else:
        print("工作区干净，跳过提交")

    # 2) 版本号
    tags = run([git, "tag", "-l", "v*", "--sort=-v:refname"]).splitlines()
    latest = tags[0] if tags else "v0.0.0"
    a, b, c = (int(x) for x in latest.lstrip("v").split("."))
    if level == "major":
        a, b, c = a + 1, 0, 0
    elif level == "minor":
        b, c = b + 1, 0
    else:
        c += 1
    tag = "v%d.%d.%d" % (a, b, c)
    branch = run([git, "rev-parse", "--abbrev-ref", "HEAD"])

    # 3) 推送
    run([git, "push", "origin", branch])
    if local:
        run([sys.executable, "hanhua.py", "build"])
        import os
        import shutil
        src = os.path.join(HERE, "dist", "汉化工具.exe")
        dst = os.path.join(HERE, "hanhua-tool.exe")
        shutil.copy2(src, dst)  # GitHub 会剥掉附件名里的中文，固定用英文稳定名
        run([gh, "release", "create", tag, dst,
             "--title", "汉化工具 通用版 " + tag,
             "--notes", note + "\n\n附件 hanhua-tool.exe 即「汉化工具」，下载后双击即用。"])
        os.remove(dst)
        print("本地发版完成：%s" % tag)
    else:
        tracked = run([git, "ls-files", ".github/workflows/release.yml"])
        run([git, "tag", tag, "-m", note])
        run([git, "push", "origin", tag])
        if tracked:
            print("已推送 %s（%s 分支 + tag），GitHub Actions 正在自动打包发版" % (tag, branch))
            print("进度：https://github.com/%s/actions" % repo)
        else:
            print("已推送 %s（%s 分支 + tag）" % (tag, branch))
            print("⚠ 云端自动构建尚未激活：.github/workflows/release.yml 因令牌缺")
            print("  workflow 权限还没推上去。先跑一次授权，再把它提交推送：")
            print("    gh auth refresh -h github.com -s workflow")
            print("  或本次直接用本机打包发版：python release.py \"%s\" --local" % note)
        print("Release 页：https://github.com/%s/releases" % repo)


if __name__ == "__main__":
    main()
