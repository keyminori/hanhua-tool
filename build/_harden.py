# -*- coding: utf-8 -*-
"""加固：1) ppost 网络失败也返回 JSON（UI 不再卡住）
2) do_POST 全块 try/except（服务端异常必回 JSON）
3) pickdir 改 tkinter 优先 + PowerShell 兜底
4) 页面头部加版本号（辨新旧页面）"""
import io, re, py_compile, os

PAGES = [r'J:\hanhua\ui\page.html', r'J:\tagatame\manual_page.html']
SERVERS = [r'J:\hanhua\ui\server.py', r'J:\tagatame\progress_server.py']
VER = 'v1.0.4'

# ---------- 页面 ----------
PP_OLD = re.compile(
    r"function ppost\(url, body\)\{\s*"
    r"return fetch\(url, \{method:'POST',\s*"
    r"headers:\{'Content-Type':'application/json'\},\s*"
    r"body:JSON\.stringify\(body\)\}\);\s*"
    r"\}")
PP_NEW = (
"function ppost(url, body){\n"
"  return fetch(url, {method:'POST',\n"
"      headers:{'Content-Type':'application/json'},\n"
"      body:JSON.stringify(body)})\n"
"    .catch(function(e){\n"
"      /* 网络断/服务端崩也给出 JSON，UI 永不静默卡住 */\n"
"      return { json: function(){ return {ok:false, err:'请求失败：' + e}; } };\n"
"    });\n"
"}")

VER_OLD = '<a class="lnk" href="/">← 进度总览</a>'
VER_NEW = ('<span class="tag">工作台 ' + VER + '</span> ' + VER_OLD)

for p in PAGES:
    s = io.open(p, encoding='utf-8', newline='').read()
    assert len(PP_OLD.findall(s)) == 1, p + ' ppost 锚点'
    s = PP_OLD.sub(lambda m: PP_NEW, s, count=1)
    assert s.count(VER_OLD) == 1, p + ' 版本锚点'
    s = s.replace(VER_OLD, VER_NEW, 1)
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    print('page hardened:', p)

# ---------- 服务端 ----------
PICK_NEW = '''def pickdir():
    """弹 Windows 原生目录选择框：优先进程内 tkinter（exe 自带），
    不可用则 subprocess 调 PowerShell FolderBrowserDialog（无控制台窗口）"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        d = filedialog.askdirectory(parent=root, title='选择目录',
                                    mustexist=False) or ''
        root.destroy()
        if d and os.path.isdir(d):
            return {'ok': True, 'dir': os.path.abspath(d)}
        return {'ok': False, 'err': '未选择目录'}
    except Exception:
        pass
    import subprocess
    cmd = ('powershell', '-NoProfile', '-STA', '-NoLogo', '-Command',
           "Add-Type -AssemblyName System.Windows.Forms;"
           "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
           "$f.ShowNewFolderButton=$true;"
           "if($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
           "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
           "[Console]::Out.Write($f.SelectedPath)}")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        out = (r.stdout or b'').decode('utf-8', 'replace').strip()
        if r.returncode == 0 and out and os.path.isdir(out):
            return {'ok': True, 'dir': out}
        return {'ok': False, 'err': '未选择目录'}
    except Exception as e:
        return {'ok': False, 'err': str(e)[:200]}


'''

for p in SERVERS:
    s = io.open(p, encoding='utf-8', newline='').read()
    eol = '\r\n' if '\r\n' in s else '\n'
    # 1) 整体替换 pickdir 函数
    m = re.search(r'def pickdir\(\):[\s\S]*?def bind_project\(', s)
    assert m, p + ' pickdir 块'
    s = s[:m.start()] + PICK_NEW.replace('\n', eol) + 'def bind_project(' + s[m.end():]
    # 2) do_POST 分发包 try/except
    i = s.find('def do_POST')
    j = s.find("            req = {}", i)
    k = s.find("        if p.startswith('/api/ctl')", j)
    end = s.find("        self._send(json.dumps(res,", k)
    assert i >= 0 and j >= 0 and k >= 0 and end > k, p + ' do_POST 结构'
    block = s[k:end]
    indented = '\n'.join(('    ' + ln if ln.strip() else ln)
                         for ln in block.split(eol))
    wrapped = ('        try:' + eol + indented + eol +
               "        except Exception as e:" + eol +
               "            res = {'ok': False, 'err': "
               "'服务器内部错误：' + str(e)[:200]}" + eol)
    s = s[:k] + wrapped + s[end:]
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    py_compile.compile(p, doraise=True)
    print('server hardened + compiled:', p)
print('ALL OK')
