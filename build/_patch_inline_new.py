# -*- coding: utf-8 -*-
"""新建项目从 prompt 弹窗改成内联表单（自动适配 CRLF/LF）。"""
import io
import os

HTML_ANCHOR = """      <button class="warn" id="projdel">删除项目</button>
      <span class="msg" id="projmsg"></span>
    </div>"""
HTML_NEW = HTML_ANCHOR + """
    <div class="frow" id="projnewf" style="display:none;margin-top:6px">
      <label>项目名 <input id="projnname" style="width:180px" placeholder="例如：某游戏名"></label>
      <button class="pri" id="projnok">✓ 创建（用上面填的源/产物目录）</button>
      <button id="projnno">取消</button>
    </div>"""

JS_OLD = """function newProj(){
  var n = prompt('新项目叫什么名字？（建议用游戏名，会成为目录名）');
  if(!n) return;
  var s = prompt('日文源目录（待翻的表所在目录）', '');
  if(s === null) return;
  post({action:'create', name:n, src_dir:s, out_dir:''})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.ok){ projMsg('没建成：' + (d.err || ''), 'bad'); return; }
      projMsg('已新建并切换到 ' + n + '，记得再填产物目录', 'ok');
      loadProj(); loadGl();
    });
}"""
JS_NEW = """function newProj(){
  /* 内联表单开关（不用弹窗） */
  var f = document.getElementById('projnewf');
  var show = (f.style.display === 'none');
  f.style.display = show ? '' : 'none';
  if(show){
    document.getElementById('projnname').value = '';
    document.getElementById('projnname').focus();
    projMsg('填项目名后点「创建」，会用上面填的源/产物目录', 'warn');
  }
}
function newProjOk(){
  var n = document.getElementById('projnname').value.trim();
  if(!n){ projMsg('先填项目名', 'bad'); return; }
  var sepv = document.getElementById('projsep').value;
  var cols = (document.getElementById('projcols').value || '0,1,2')
             .split(',').map(function(x){ return parseInt(x, 10); });
  projMsg('创建中…');
  post({action:'create', name:n,
        src_dir: document.getElementById('projsrc').value.trim(),
        out_dir: document.getElementById('projout').value.trim(),
        pattern: document.getElementById('projpat').value.trim() || '*.txt',
        recursive: document.getElementById('projrec').checked,
        sep: (sepv === '\\t' || sepv.toLowerCase() === 'tab') ? '\\t' : sepv,
        cols: cols})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.ok){ projMsg('没建成：' + (d.err || ''), 'bad'); return; }
      document.getElementById('projnewf').style.display = 'none';
      projMsg('已新建并切换到 ' + n, 'ok');
      loadProj(); loadGl(); loadCfg(); loadAcc(); loadLed();
    })
    .catch(function(e){ projMsg('创建失败：' + e, 'bad'); });
}"""

BIND_ANCHOR = "document.getElementById('projdel').addEventListener('click', delProj);"
BIND_NEW = BIND_ANCHOR + """
document.getElementById('projnok').addEventListener('click', newProjOk);
document.getElementById('projnno').addEventListener('click', function(){
  document.getElementById('projnewf').style.display = 'none';
});"""

FILES = [r'J:\hanhua\ui\page.html', r'J:\tagatame\manual_page.html']

for p in FILES:
    raw = io.open(p, encoding='utf-8', newline='').read()
    eol = '\r\n' if '\r\n' in raw else '\n'

    def fix(t):
        return t.replace('\n', eol)

    s = raw
    for label, a in (('HTML', HTML_ANCHOR), ('JS', JS_OLD), ('BIND', BIND_ANCHOR)):
        n = s.count(fix(a))
        assert n == 1, '%s: %s 锚点出现 %d 次' % (p, label, n)
    assert 'projnewf' not in s, p + ' 已改过'
    s = s.replace(fix(HTML_ANCHOR), fix(HTML_NEW))
    s = s.replace(fix(JS_OLD), fix(JS_NEW))
    s = s.replace(fix(BIND_ANCHOR), fix(BIND_NEW))
    bak = p + '.bak-bind2'
    if not os.path.exists(bak):
        io.open(bak, 'w', encoding='utf-8', newline='').write(raw)
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    print('patched:', p, '(eol=%s)' % repr(eol))

# 复核绑定完整性
import re
for p in FILES:
    s = io.open(p, encoding='utf-8', newline='').read()
    ids = set(re.findall(r'id="(\w+)"', s))
    bound = set(re.findall(r"getElementById\(['\"](\w+)['\"]\)", s))
    un = sorted(i for i in ids - bound if not i.endswith('msg'))
    print(os.path.basename(p), '仍未绑定:', un if un else '无')
