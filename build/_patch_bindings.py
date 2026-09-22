# -*- coding: utf-8 -*-
"""给 page.html 项目卡/目录选择器/术语刷新按钮补上事件绑定（两个文件同补）。"""
import io
import os

BIND = r'''
/* ---- 项目卡 / 目录选择器 / 术语刷新 事件绑定 ---- */
document.getElementById('projrel').addEventListener('click', loadProj);
document.getElementById('projsw').addEventListener('click', switchProj);
document.getElementById('projnew').addEventListener('click', newProj);
document.getElementById('projdel').addEventListener('click', delProj);
document.getElementById('projsave').addEventListener('click', saveProj);
document.getElementById('projbs').addEventListener('click', function(){ browse('projsrc'); });
document.getElementById('projbo').addEventListener('click', function(){ browse('projout'); });
document.getElementById('glrel').addEventListener('click', loadGl);
document.getElementById('dpup').addEventListener('click', function(){
  var cur = document.getElementById('dpdir').textContent.trim();
  if(!cur || cur.indexOf('我的电脑') >= 0) return;
  if(cur.length <= 3 && cur.slice(-1) === '\\'){ dpBrowse(''); return; }
  var i = cur.lastIndexOf('\\', cur.length - 2);
  dpBrowse(i > 0 ? cur.slice(0, i + 1) : '');
});
document.getElementById('dpclose').addEventListener('click', function(){
  document.getElementById('dirpick').style.display = 'none';
});
/* 下拉换项目时，把该项目的配置填进输入框（还没保存，只预览） */
document.getElementById('projsel').addEventListener('change', function(){
  fetch('/api/project', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.ok) return;
      var v = document.getElementById('projsel').value;
      var c = (d.list || []).filter(function(p){ return p.name === v; })[0] || {};
      document.getElementById('projsrc').value = c.src_dir || '';
      document.getElementById('projout').value = c.out_dir || '';
      document.getElementById('projpat').value = c.pattern || '*.txt';
      document.getElementById('projrec').checked = !!c.recursive;
      document.getElementById('projsep').value =
        (c.sep === '\t') ? '\\t' : (c.sep || '\\t');
      document.getElementById('projcols').value = (c.cols || [0,1,2]).join(',');
    });
});
'''

MARK = "loadProj(); loadGl(); loadStat(); loadLed(); loadCfg(); loadAcc();"
FILES = [r'J:\hanhua\ui\page.html', r'J:\tagatame\manual_page.html']

for p in FILES:
    s = io.open(p, encoding='utf-8', newline='').read()  # 保留原始换行符
    n = s.count(MARK)
    assert n == 1, '%s: 初始化行出现 %d 次' % (p, n)
    assert "projrel').addEventListener" not in s, p + ' 已有绑定，跳过'
    s = s.replace(MARK, BIND + '\n' + MARK)
    # 备份
    bak = p + '.bak-bind'
    if not os.path.exists(bak):
        io.open(bak, 'w', encoding='utf-8', newline='').write(
            io.open(p, encoding='utf-8', newline='').read())
    io.open(p, 'w', encoding='utf-8', newline='').write(s)
    print('patched:', p)

# 复核：所有非 msg 元素 id 都应有绑定
import re
for p in FILES:
    s = io.open(p, encoding='utf-8', newline='').read()
    ids = set(re.findall(r'id="(\w+)"', s))
    bound = set(re.findall(r"getElementById\(['\"](\w+)['\"]\)", s))
    un = sorted(i for i in ids - bound if not i.endswith('msg'))
    print(os.path.basename(os.path.dirname(p)) + '/' + os.path.basename(p),
          '仍未绑定:', un if un else '无')
