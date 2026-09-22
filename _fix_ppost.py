# -*- coding: utf-8 -*-
"""修复 ppost 被误嵌进 dpup click 回调的作用域 bug（v2，兼容混合换行符）。
症状：newProjOk/glImport/saveProj 等调用 ppost 时 ReferenceError，
     且 .catch 尚未挂上 -> 界面永远停在「创建中…」。
修法：把 ppost 函数体从 dpup 回调中移出，提升到顶层绑定区之前。
"""
import io
import re
import sys

# 函数体（行间用 \r?\n 柔性匹配；本区域实际是 LF）
PPOST_RE = re.compile(
    r"function ppost\(url, body\)\{\r?\n"
    r"  return fetch\(url, \{method:'POST',\r?\n"
    r"      headers:\{'Content-Type':'application/json'\},\r?\n"
    r"      body:JSON\.stringify\(body\)\}\)\r?\n"
    r"    \.catch\(function\(e\)\{\r?\n"
    r"      /\* 网络断/服务端崩也给出 JSON，UI 永不静默卡住 \*/\r?\n"
    r"      return \{ json: function\(\)\{ return \{ok:false, err:'请求失败：' \+ e\}; \} \};\r?\n"
    r"    \}\);\r?\n"
    r"\}"
)
ANCHOR = "document.getElementById('projrel').addEventListener('click', loadProj);"
BLOCK = (
    "function ppost(url, body){\n"
    "  return fetch(url, {method:'POST',\n"
    "      headers:{'Content-Type':'application/json'},\n"
    "      body:JSON.stringify(body)})\n"
    "    .catch(function(e){\n"
    "      /* 网络断/服务端崩也给出 JSON，UI 永不静默卡住 */\n"
    "      return { json: function(){ return {ok:false, err:'请求失败：' + e}; } };\n"
    "    });\n"
    "}\n"
)


def fix(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        src = f.read()

    m = list(PPOST_RE.finditer(src))
    if len(m) != 1:
        print("[SKIP] %s : ppost 块匹配 %d 处（期望 1）" % (path, len(m)))
        return False
    m = m[0]

    if src.count(ANCHOR) != 1:
        print("[FAIL] %s : 绑定锚点异常" % path)
        return False

    # 1) 摘出 ppost 块（连同其后紧跟的一个换行符，若紧贴 '}' 后面就是换行）
    removed = m.group(0)
    start, end = m.span()
    if src[end:end + 1] in ("\n", "\r\n") or src[end:end + 2] == "\r\n":
        end2 = end + (2 if src[end:end + 2] == "\r\n" else 1)
    else:
        end2 = end
    src = src[:start] + src[end2:]

    # 2) 插到顶层绑定区锚点之前
    idx = src.index(ANCHOR)
    src = src[:idx] + BLOCK + src[idx:]

    # 3) 自检
    assert src.count("function ppost") == 1, "ppost 定义数异常"
    assert src.count(ANCHOR) == 1, "锚点数异常"
    idx_ppost = src.index("function ppost")
    idx_dpup = src.index("getElementById('dpup')")
    assert idx_ppost < idx_dpup, "ppost 仍在 dpup 之后（可能仍嵌套）"
    # ppost 到 dpup 之间不该再出现 dpup 回调特征（cur.lastIndexOf）
    seg = src[idx_ppost:idx_dpup]
    assert "cur.lastIndexOf" not in seg, "ppost 疑似仍嵌在 dpup 回调内"

    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    print("[OK] %s : ppost 已提升到顶层" % path)
    return True


if __name__ == "__main__":
    ok1 = fix(r"J:/hanhua/ui/page.html")
    ok2 = fix(r"J:/tagatame/manual_page.html")
    sys.exit(0 if (ok1 or ok2) else 1)
