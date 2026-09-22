# -*- coding: utf-8 -*-
"""按前端真实载荷做边界实测：创建（空 out_dir / 空 sep / 中文名）+ 术语导入/回溯"""
import time, urllib.request, json, shutil, os

time.sleep(2.5)
port = open(r'J:\hanhua\_port.txt').read().strip()
BASE = 'http://127.0.0.1:' + port

def post(path, obj, timeout=20):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(obj).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return '200', r.read().decode('utf-8', 'replace')[:220]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:220]
    except Exception as e:
        return 'EXC', repr(e)[:220]

cases = [
    # (说明, 路径, 载荷)
    ('创建: 空产物目录+空sep+中文名', '/api/project',
     {'action': 'create', 'name': '测试项目',
      'src_dir': r'J:\hanhua', 'out_dir': '', 'pattern': '*.txt',
      'recursive': False, 'sep': '', 'cols': [0, 1, 2]}),
    ('创建: TAB 字面量', '/api/project',
     {'action': 'create', 'name': 'tab测试',
      'src_dir': r'J:\hanhua', 'out_dir': '', 'pattern': '*.txt',
      'recursive': False, 'sep': '\\t', 'cols': [0, 1, 2]}),
    ('创建: 换行分隔符(前端value可能带真实TAB)', '/api/project',
     {'action': 'create', 'name': 'realtab',
      'src_dir': r'J:\hanhua', 'out_dir': '', 'pattern': '*.txt',
      'recursive': False, 'sep': '\t', 'cols': [0, 1, 2]}),
]
for label, path, payload in cases:
    st, body = post(path, payload)
    print('[%s] %s -> %s %s' % (st, label, st, body))

# 术语导入：完全模拟前端（含表头行）
rows = [['日文', '中文', '备注', '启用'],
        ['テストJP', '测试CN', '角色', 'on'],
        ['アルケミスト', '炼金术师', '', 'on']]
st, body = post('/api/glossary', {'action': 'import', 'rows': rows, 'replace': True})
print('[导入(覆盖)] ->', st, body)
st, body = post('/api/glossary', {'action': 'set', 'jp': '手动加一条', 'cn': '手动CN', 'note': 'n'})
print('[手动加] ->', st, body)
st, body = post('/api/glossary', {'action': 'retro', 'scope': 'all'}, timeout=60)
print('[回溯] ->', st, body)
g = urllib.request.urlopen(BASE + '/api/glossary', timeout=10).read().decode('utf-8')
d = json.loads(g)
print('[列表] n =', d.get('n'), '| 项目 =', d.get('project'))

# 清理测试项目
for name in ('测试项目', 'tab测试', 'realtab'):
    post('/api/project', {'action': 'delete', 'name': name})
    p = r'J:\hanhua\projects' + '\\' + name
    shutil.rmtree(p, ignore_errors=True)
print('cleaned')
