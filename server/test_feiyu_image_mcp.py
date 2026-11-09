#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feiyu-image-mcp 单测 (M2 硬闸先行, stdlib unittest, 不碰 ComfyUI)

跑法: python -B test_feiyu_image_mcp.py
覆盖:
  T1 洗图硬闸-PNG: 带 tEXt prompt 的 PNG 过闸后无内嵌文本 + 像素零差 + prompt 字面不存在
  T2 洗图硬闸-JPEG: 带 EXIF/注释的 JPEG 同上
  T3 拒单双闸: R18 英/中 + 真人名单 + 真人指示词 全拒; 正常请求放行
  T4 拼词: HEAD+ART 头在, ≤750 降级删场景, 拼完正向再过闸(防漏网)
  T5 MCP 冒烟: initialize → tools/list → tools/call gen_image(--fake 全链含洗图) → status
"""
import json
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from zlib import crc32

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import feiyu_image_mcp as M  # noqa


def _make_png_with_meta(path, prompt_text):
    """手工构造带 tEXt 的 PNG(PngInfo 同款, 自拼以防 PIL 行为差异)。"""
    from PIL import Image, PngImagePlugin
    im = Image.new('RGB', (64, 64), (200, 120, 90))
    meta = PngImagePlugin.PngInfo()
    meta.add_text('prompt', prompt_text)
    meta.add_text('workflow', '{"node": "165"}')
    im.save(path, 'PNG', pnginfo=meta)


def _make_jpeg_with_meta(path):
    from PIL import Image
    im = Image.new('RGB', (64, 64), (90, 200, 120))
    exif = Image.Exif()
    exif[0x010F] = 'FAKE-MAKE'                  # Make 标签
    im.save(path, 'JPEG', exif=exif.tobytes(), comment=b'fake comment')


class T1StripPng(unittest.TestCase):
    def test_png_prompt_stripped_and_pixels_equal(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            raw, out = td / 'raw', td / 'out'
            raw.mkdir()
            txt = json.dumps({'165': {'inputs': {'text': 'SECRET PROMPT 白裙女孩'}}})
            _make_png_with_meta(raw / 'a.png', txt)
            n_ok, fails = M.strip_gate(raw, out)
            self.assertEqual(n_ok, 1, 'fails=%s' % fails)
            self.assertEqual(fails, [])
            washed = out / 'a.png'
            self.assertTrue(washed.exists())
            # 断言1: PIL 读不到内嵌文本
            self.assertFalse(M.has_text_meta(washed))
            # 断言2: 像素零差
            same, maxdiff = M.pixels_equal(raw / 'a.png', washed)
            self.assertTrue(same, 'maxdiff=%s' % maxdiff)
            # 断言3: prompt 字面在文件字节里不存在
            data = washed.read_bytes()
            self.assertNotIn(b'SECRET PROMPT', data)
            self.assertNotIn(b'165', data)


class T2StripJpeg(unittest.TestCase):
    def test_jpeg_exif_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            raw, out = td / 'raw', td / 'out'
            raw.mkdir()
            _make_jpeg_with_meta(raw / 'b.jpg')
            n_ok, fails = M.strip_gate(raw, out)
            self.assertEqual(n_ok, 1, 'fails=%s' % fails)
            washed = out / 'b.jpg'
            self.assertFalse(M.has_text_meta(washed))
            same, maxdiff = M.pixels_equal(raw / 'b.jpg', washed)
            self.assertTrue(same, 'maxdiff=%s' % maxdiff)
            self.assertNotIn(b'FAKE-MAKE', washed.read_bytes())
            self.assertNotIn(b'fake comment', washed.read_bytes())


class T3Gates(unittest.TestCase):
    def test_r18_en_reject(self):
        for s in ('nude girl on beach', 'topless anime', 'ahegao face',
                  'sex with boy', 'nsfw illustration'):
            self.assertIn('拒单', M.check_request(s) or '')

    def test_r18_zh_reject(self):
        for s in ('画一张全裸的女孩', '穿着睡衣做爱', '涩图来一张', 'R18 帮我画',
                  '裸体写生', '自慰场景'):
            self.assertIn('拒单', M.check_request(s) or '')

    def test_real_person_reject(self):
        for s in ('花泽香菜的图', '画一张 sakura ayane', '我的偶像 in dress',
                  '声优姐姐', '写真照片风'):
            self.assertIn('拒单', M.check_request(s) or '')

    def test_sfw_pass(self):
        self.assertIsNone(M.check_request('white dress girl in garden'))
        self.assertIsNone(M.check_request('狐耳女孩白裙在花园'))
        # 裸足/裸脚是 SFW, 不能被"裸"字误杀
        self.assertIsNone(M.check_request('barefoot girl, bare legs'))
        # essex 不吃 sex 词边界
        self.assertIsNone(M.check_request('essex class ship girl'))


class T4Compose(unittest.TestCase):
    def test_head_and_art_present(self):
        pos, neg, err = M.compose('white dress girl in garden')
        self.assertIsNone(err)
        self.assertTrue(pos.startswith(M.HEAD))
        self.assertIn('artist:tianliang duohe fangdongye', pos)
        self.assertIn('nsfw', neg)                       # 非 R18 第一保险必须在
        self.assertIn('(black tights:1.35)', neg)

    def test_length_degrade_drops_scene_first(self):
        long_scene = 'extremely detailed ' * 40
        pos, _neg, err = M.compose('white dress girl', scene=long_scene.strip())
        self.assertIsNone(err)
        self.assertLessEqual(len(pos), M.LIMIT)
        self.assertNotIn('extremely detailed', pos)      # 整段场景被降级删掉

    def test_pos_rechecked(self):
        # subject 过了闸但 outfit 里塞 R18 词 → 拼完复查要拦下
        pos, _neg, err = M.compose('cute girl', outfit='nude swimsuit')
        self.assertIsNone(pos)
        self.assertIn('拒单', err)


class T5McpSmoke(unittest.TestCase):
    srv = None
    thd = None
    port = None

    @classmethod
    def setUpClass(cls):
        M.FAKE = True
        # 沙盒: 假图/种子记账全落临时目录, 不污染真实产出盘
        cls._sandbox = tempfile.mkdtemp(prefix='fyimg_test_')
        cls._old_out, cls._old_seed = M.OUT_BASE, M.SEED_USED_FILE
        M.OUT_BASE = Path(cls._sandbox) / 'out'
        M.SEED_USED_FILE = Path(cls._sandbox) / 'seed_used.json'
        srv = ThreadingHTTPServer(('127.0.0.1', 0), M.McpHandler)
        cls.srv = srv
        cls.port = srv.server_address[1]
        cls.thd = threading.Thread(target=srv.serve_forever, daemon=True)
        cls.thd.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        M.OUT_BASE, M.SEED_USED_FILE = cls._old_out, cls._old_seed
        shutil.rmtree(cls._sandbox, ignore_errors=True)

    def call(self, body, session=True):
        hdr = {'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream'}
        if session and getattr(self, 'sid', None):
            hdr['mcp-session-id'] = self.sid
        req = urllib.request.Request('http://127.0.0.1:%d/mcp' % self.port,
                                     data=json.dumps(body).encode('utf-8'),
                                     headers=hdr)
        resp = urllib.request.urlopen(req, timeout=10)
        sid = resp.headers.get('mcp-session-id')
        if sid:
            self.sid = sid
        raw = resp.read().decode('utf-8')
        if not raw.strip():
            return None                            # notifications 无响应体
        if raw.startswith('event:'):
            raw = raw.split('data:', 1)[1].strip()
        return json.loads(raw)

    def test_full_handshake_and_fake_gen(self):
        init = self.call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                          'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                     'clientInfo': {'name': 't', 'version': '1'}}})
        self.assertEqual(init['result']['serverInfo']['name'], 'feiyu-image-mcp')
        self.call({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        tools = self.call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        names = [t['name'] for t in tools['result']['tools']]
        self.assertEqual(names, ['gen_image', 'gen_image_status'])

        # gen_image 假图全链: 闸 → 拼 → 占位 PNG → 洗图 → 返回洗净路径
        M._LAST_GEN_TS[0] = 0.0                # 清节流(测试内连发)
        r = self.call({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'gen_image',
            'arguments': {'subject': 'white dress girl in garden, silver hair',
                          'count': 2}}})
        payload = json.loads(r['result']['content'][0]['text'])
        self.assertTrue(payload['ok'], payload)
        self.assertTrue(payload['fake'])
        self.assertEqual(len(payload['images']), 2)
        for p in payload['images']:
            f = Path(p)
            self.assertTrue(f.exists(), p)
            self.assertFalse(M.has_text_meta(f))         # 发出去的图必须已剥净
            self.assertNotIn(b'FAKE PROMPT', f.read_bytes())

        # 拒单走 MCP 也能拿到原因
        M._LAST_GEN_TS[0] = 0.0
        r2 = self.call({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
            'name': 'gen_image', 'arguments': {'subject': 'nude girl'}}})
        p2 = json.loads(r2['result']['content'][0]['text'])
        self.assertFalse(p2['ok'])
        self.assertIn('拒单', p2['reason'])

        # status
        r3 = self.call({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {
            'name': 'gen_image_status', 'arguments': {}}})
        p3 = json.loads(r3['result']['content'][0]['text'])
        self.assertTrue(p3['ok'])
        self.assertTrue(p3['fake_mode'])
        self.assertIsInstance(p3['today_batches'], list)


class T6BuildApi(unittest.TestCase):
    """真渲染接线单测: 只读模板 PNG 改节点, 不碰 GPU/服务器。"""

    def test_wiring(self):
        if not M.TEMPLATE_PNG.exists():
            self.skipTest('模板 PNG 不在本机(其他机器跳过)')
        api = M.build_api('TEST POS 白裙', M.NEG_FIXED, 109001, 'testbatch')
        self.assertEqual(api['165']['inputs']['text'], 'TEST POS 白裙')
        self.assertEqual(api['153']['inputs']['text'], M.NEG_FIXED)
        self.assertEqual(api['17']['inputs']['denoise'], 0.55)
        self.assertEqual(api['142']['inputs']['positive'], ['301', 0])
        self.assertEqual(api['142']['inputs']['negative'], ['302', 0])
        self.assertEqual(api['142']['inputs']['denoise'], 0.55)
        self.assertEqual(api['77']['inputs']['images'], ['142', 0])
        self.assertEqual(api['77']['inputs']['filename_prefix'],
                         '肥鱼OS/testbatch/fyi_109001')
        self.assertIn('perfect hands', api['301']['inputs']['text'])
        self.assertIn('fused fingers', api['302']['inputs']['text'])
        for n in M.SEED_NODES:
            self.assertEqual(api[n]['inputs']['seed'], 109001, n)
        # 脸细节器 176 仍在图中且引用完整(模板自带, 换文本自动继承)
        self.assertIn('176', api)
        # 负向 nsfw 保险必须在(非 R18 第一道)
        self.assertIn('nsfw', api['153']['inputs']['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
