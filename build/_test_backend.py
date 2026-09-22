# -*- coding: utf-8 -*-
"""后端实测：项目创建/保存 + 术语表新增/导入/读取（用临时项目，测完删）。"""
import sys
import os
import shutil

sys.path.insert(0, r'J:\hanhua\core')
os.makedirs(r'J:\tmp_src_test', exist_ok=True)
with open(r'J:\tmp_src_test\01.txt', 'w', encoding='utf-8') as f:
    f.write('1\tこんにちは\tcue1\n2\tアルケミスト\t\n')

import project
import glossary

NAME = 'zz_测试临时'

# 1) 建项目
ok, msg, cfg = project.create(NAME, r'J:\tmp_src_test', '')
print('create:', ok, msg)
project.set_current(NAME)

# 2) update 保存设置
ok2, msg2, cfg2 = project.update(NAME, {'src_dir': r'J:\tmp_src_test',
                                        'pattern': '*.txt'})
print('update:', ok2, msg2, cfg2.get('src_dir'))

# 3) 术语：手动加一条
ok3, msg3, changed = glossary.set_term('こんにちは', '你好', '测试')
print('set_term:', ok3, msg3, 'glossary.json =',
      os.path.isfile(os.path.join(project.workspace(NAME), 'glossary.json')))

# 4) 术语：导入覆盖
ok4, msg4, n = glossary.import_rows([['アルケミスト', '炼金术师', '职业名', '1']],
                                    replace=True)
print('import:', ok4, msg4, n)
print('items:', glossary.items())
print('tm.json:', open(os.path.join(project.workspace(NAME), 'tm.json'),
                      encoding='utf-8').read()[:200])

# 5) 清理
project.set_current('')
shutil.rmtree(r'J:\tmp_src_test', ignore_errors=True)
project.delete(NAME)
print('cleaned, projects =',
      [p['name'] for p in project.list_projects()])
