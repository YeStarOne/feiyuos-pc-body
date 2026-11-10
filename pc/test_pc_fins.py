# -*- coding: utf-8 -*-
"""pc_fins 单测：python -B test_pc_fins.py

覆盖：命令执行/退出码/超时杀树/灾难命令拒绝/编码/读写往返/offset 分页/
      路径 deny（.ssh）/docx-xlsx 提取/目录列举/审计落盘/b64。
全部落在 tempfile 沙盒，不碰真实文档。
"""
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import pc_fins as H

TD = tempfile.mkdtemp(prefix='ffins_')


class ExecTests(unittest.TestCase):
    def test_echo_and_exit(self):
        r = H.exec_cmd('echo hello-hands')
        self.assertTrue(r['ok'], r)
        self.assertIn('hello-hands', r['stdout'].lower())
        self.assertEqual(r['exit'], 0)

    def test_exit_code_propagates(self):
        r = H.exec_cmd('exit /b 3')
        self.assertFalse(r['ok'])
        self.assertEqual(r['exit'], 3)

    def test_stderr_separated(self):
        r = H.exec_cmd('echo oops 1>&2')
        self.assertIn('oops', r['stderr'].lower())
        self.assertEqual(r['stdout'].strip(), '')

    def test_gbk_output_decodes(self):
        r = H.exec_cmd('echo 中文回显')
        self.assertIn('中文回显', r['stdout'])

    def test_timeout_kills_tree(self):
        t0 = time.time()
        r = H.exec_cmd('ping -n 10 127.0.0.1', timeout=1)
        self.assertTrue(r['timed_out'], r)
        self.assertLess(time.time() - t0, 8)
        self.assertLess(r['dur_ms'], 8000)

    def test_deny_format(self):
        r = H.exec_cmd('format d: /q')
        self.assertEqual(r['error'], 'denied')
        self.assertEqual(r['exit'] if 'exit' in r else None, None)

    def test_deny_diskpart_case_insensitive(self):
        self.assertEqual(H.exec_cmd('DISKPART /s x.txt')['error'], 'denied')

    def test_cwd_applies(self):
        r = H.exec_cmd('cd', cwd=TD)
        self.assertIn(TD.lower(), r['stdout'].lower())

    def test_bad_cwd(self):
        r = H.exec_cmd('echo x', cwd=str(Path(TD) / 'nope-nope'))
        self.assertEqual(r['error'], 'bad-cwd')


class FileTests(unittest.TestCase):
    def test_roundtrip_utf8_and_offset(self):
        p = Path(TD) / 'a.md'
        text = '第一行\n第二行\n' + 'x' * 100
        H.write_file(str(p), content=text)
        r = H.read_file(str(p))
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['kind'], 'text')
        self.assertEqual(r['chars'], len(text))
        r2 = H.read_file(str(p), offset=4, max_chars=3)
        self.assertEqual(r2['text'], text[4:7])
        self.assertEqual(r2['next_offset'], 7)
        self.assertTrue(r2['truncated'])

    def test_gbk_fallback(self):
        p = Path(TD) / 'gbk.txt'
        raw = '中文GBK内容'.encode('gbk')
        H.write_file(str(p), b64=base64.b64encode(raw).decode())
        r = H.read_file(str(p))
        self.assertEqual(r['encoding'], 'gbk')
        self.assertIn('中文GBK内容', r['text'])

    def test_append_and_create_dirs(self):
        p = Path(TD) / 'deep' / 'er' / 'b.txt'
        H.write_file(str(p), content='a')
        H.write_file(str(p), content='b', append=True)
        self.assertEqual(p.read_text(encoding='utf-8'), 'ab')
        self.assertFalse(H.write_file(str(p), content='c')['created'])

    def test_deny_ssh_key(self):
        d = Path(TD) / '.ssh'
        d.mkdir(exist_ok=True)
        k = d / 'id_rsa'
        k.write_text('PRIVATE', encoding='utf-8')
        r = H.read_file(str(k))
        self.assertFalse(r['ok'])
        self.assertIn('禁读', r['error'])
        self.assertFalse(H.write_file(str(k), content='x')['ok'])   # 写也禁

    def test_deny_write_windows_dir(self):
        r = H.write_file('C:\\Windows\\evil.txt', content='x')
        self.assertFalse(r['ok'])
        self.assertIn('禁写', r['error'])

    def test_no_such_file(self):
        self.assertEqual(H.read_file(str(Path(TD) / 'nope.md'))['error'], 'no-such-file')

    def test_b64_roundtrip_binary(self):
        p = Path(TD) / 'blob.bin'
        p.write_bytes(bytes(range(256)))
        r = H.read_file(str(p), as_b64=True)
        self.assertEqual(base64.b64decode(r['b64']), bytes(range(256)))
        self.assertEqual(r['kind'], 'base64')

    def test_binary_default_rejected(self):
        p = Path(TD) / 'blob.bin'
        p.write_bytes(b'\x00\x01')
        self.assertEqual(H.read_file(str(p))['error'], 'unknown-type')

    def test_docx(self):
        import docx
        p = Path(TD) / 'd.docx'
        doc = docx.Document()
        doc.add_heading('标题一', level=1)
        doc.add_paragraph('正文段落')
        t = doc.add_table(rows=1, cols=2)
        t.rows[0].cells[0].text = '甲'
        t.rows[0].cells[1].text = '乙'
        doc.save(str(p))
        r = H.read_file(str(p))
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['kind'], 'docx')
        self.assertIn('标题一', r['text'])
        self.assertIn('正文段落', r['text'])
        self.assertIn('甲 | 乙', r['text'])

    def test_xlsx(self):
        import openpyxl
        p = Path(TD) / 'x.xlsx'
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '台账'
        ws.append(['日期', '金额'])
        ws.append(['11-10', 12.5])
        wb.save(str(p))
        r = H.read_file(str(p))
        self.assertEqual(r['kind'], 'xlsx')
        self.assertIn('sheet: 台账', r['text'])
        self.assertIn('11-10 | 12.5', r['text'])

    def test_pdf_unsupported_hint(self):
        p = Path(TD) / 'x.pdf'
        p.write_bytes(b'%PDF-1.4')
        r = H.read_file(str(p))
        self.assertEqual(r['error'], 'unsupported-type')
        self.assertIn('b64=1', r['hint'])


class ListTests(unittest.TestCase):
    def test_drives(self):
        r = H.list_dir('')
        self.assertTrue(r['ok'])
        self.assertTrue(any(d.startswith('C:') for d in r['roots']))

    def test_listing_and_filter(self):
        d = Path(TD) / 'lst'
        (d / 'sub').mkdir(parents=True, exist_ok=True)
        (d / 'a.md').write_text('x', encoding='utf-8')
        (d / 'b.docx').write_text('x', encoding='utf-8')
        r = H.list_dir(str(d))
        self.assertEqual(r['count'], 3)
        self.assertEqual(r['items'][0]['type'], 'dir')      # 目录在前
        r2 = H.list_dir(str(d), ext='docx')
        # ext 只筛文件；目录始终保留（否则没法往下翻）
        self.assertEqual([i['name'] for i in r2['items'] if i['type'] == 'file'], ['b.docx'])
        self.assertIn('sub', [i['name'] for i in r2['items']])
        r3 = H.list_dir(str(d), q='a.m')
        self.assertEqual([i['name'] for i in r3['items']], ['a.md'])

    def test_stat(self):
        p = Path(TD) / 'a.md'
        p.write_text('abc', encoding='utf-8')
        r = H.stat_path(str(p))
        self.assertEqual(r['size'], 3)
        self.assertEqual(r['type'], 'file')


class GuardTests(unittest.TestCase):
    def test_off_flag_disables_everything(self):
        H.OFF_FLAG.write_text('off', encoding='utf-8')
        try:
            self.assertFalse(H.enabled())
            self.assertEqual(H.exec_cmd('echo x')['error'], 'fins-off')
            self.assertEqual(H.read_file(str(Path(TD) / 'a.md'))['error'], 'fins-off')
        finally:
            H.OFF_FLAG.unlink()

    def test_audit_written(self):
        H.exec_cmd('echo audited')
        rows = H.recent_audit(5)
        self.assertTrue(any(r['action'] == 'exec' and 'audited' in r['detail'] for r in rows), rows)

    def test_status(self):
        s = H.status()
        self.assertTrue(s['ok'])
        self.assertIn('max_timeout_s', s['limits'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
