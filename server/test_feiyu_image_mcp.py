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
        pos, neg, err, _pk = M.compose('white dress girl in garden')
        self.assertIsNone(err)
        self.assertTrue(pos.startswith(M.HEAD))
        self.assertIn('artist:tianliang duohe fangdongye', pos)
        self.assertIn('nsfw', neg)                       # 非 R18 第一保险必须在
        self.assertIn('(black tights:1.35)', neg)

    def test_length_degrade_drops_scene_first(self):
        long_scene = 'extremely detailed ' * 40
        pos, _neg, err, _pk = M.compose('white dress girl', scene=long_scene.strip())
        self.assertIsNone(err)
        self.assertLessEqual(len(pos), M.LIMIT)
        self.assertNotIn('extremely detailed', pos)      # 整段场景被降级删掉

    def test_pos_rechecked(self):
        # subject 过了闸但 outfit 里塞 R18 词 → 拼完复查要拦下
        pos, _neg, err, _pk = M.compose('cute girl', outfit='nude swimsuit')
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
        # 1110: 单测绝不真往服务器回传(fake 图不该上生产媒体目录); 回传映射单测里用桩验
        cls._old_publish = M.PUBLISH
        M.PUBLISH = ''
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
        M.PUBLISH = cls._old_publish
        shutil.rmtree(cls._sandbox, ignore_errors=True)

    def test_publish_rewrites_to_remote_paths(self):
        """回传契约: images 换成服务器 jpg 路径, PC 原图留在 images_pc; 失败不静默。"""
        import types
        calls = []

        class R:
            returncode = 0
            stderr = b''
        old_sub, old_pub = M.subprocess, M.PUBLISH
        M.subprocess = types.SimpleNamespace(run=lambda *a, **k: (calls.append(a), R())[1])
        M.PUBLISH = 'agent@example.test:/tmp/media/gen_image'
        try:
            from PIL import Image
            d = Path(self._sandbox) / 'pub'
            d.mkdir(exist_ok=True)
            p = d / 'a.png'
            Image.new('RGB', (8, 8), (1, 2, 3)).save(p, 'PNG')
            payload = {'images': [str(p)]}
            M._publish_payload(payload)
            self.assertTrue(payload['publish']['ok'], payload['publish'])
            self.assertEqual(payload['images'], ['/tmp/media/gen_image/pub/a.jpg'])
            self.assertEqual(payload['images_pc'], [str(p)])
            self.assertTrue(any('mkdir' in str(c) for c in calls))
        finally:
            M.subprocess, M.PUBLISH = old_sub, old_pub

    def test_fake_gen_moss_engine(self):
        """engine=moss 在 FAKE 模式走通全链(占位图 fym_ 前缀 + 洗图 + engine 标记)。"""
        M._LAST_GEN_TS[0] = 0.0
        r = self.call({'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {
            'name': 'gen_image', 'arguments': {
                'subject': 'coffee shop storefront poster, the sign reads "MOSS COFFEE"',
                'engine': 'moss', 'count': 1}}})
        p = json.loads(r['result']['content'][0]['text'])
        self.assertTrue(p['ok'], p)
        self.assertEqual(p.get('engine'), 'moss')
        self.assertEqual(len(p['images']), 1)
        for path in p['images']:
            self.assertIn('fym_', Path(path).name)
            self.assertFalse(M.has_text_meta(Path(path)))

    def test_bad_engine_rejected(self):
        M._LAST_GEN_TS[0] = 0.0
        r = self.call({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {
            'name': 'gen_image', 'arguments': {'subject': 'white dress girl',
                                               'engine': 'flux'}}})
        p = json.loads(r['result']['content'][0]['text'])
        self.assertFalse(p['ok'])
        self.assertIn('engine', p['reason'])

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

    def test_detailer_detectors_not_crossed(self):
        """1111 修: 模板把两个细节器的检测器接反了(手部专用词压到脸上)。"""
        if not M.TEMPLATE_PNG.exists():
            self.skipTest('模板 PNG 不在本机(其他机器跳过)')
        api = M.build_api('POS', M.NEG_FIXED, 109002, 'detbatch')
        # 176(先跑 dn0.5, 走主正向) = 脸 → face_yolov8m
        self.assertEqual(api['176']['inputs']['bbox_detector'], ['143', 0])
        self.assertEqual(api['143']['inputs']['model_name'], 'bbox/face_yolov8m.pt')
        # 142(后跑, 走手部专用提词 301/302) = 手 → hand_yolov8s
        self.assertEqual(api['142']['inputs']['bbox_detector'], ['177', 0])
        self.assertEqual(api['177']['inputs']['model_name'], 'bbox/hand_yolov8s.pt')

    def test_signature_weighting_and_wlop_down(self):
        """1111 鱼鱼报障: 顶部签名 'DI N WLOP' / 手上铁环 → 名称类提权 + wlop 1→0.7。"""
        self.assertIn('(artist:wlop:0.7)', M.ART)
        self.assertNotIn('(artist:wlop:1)', M.ART)
        for t in ('(signature:1.5)', '(watermark:1.5)', '(artist name:1.4)',
                  '(ring on hand:1.25)', '(metal bracelet:1.2)'):
            self.assertIn(t, M.NEG_FIXED)
        # 画风骨架其余 11 个 tag 不许动
        for t in ('(artist:tianliang duohe fangdongye:0.9)', 'artist:ciloranko',
                  'artist:noyu', 'ningen mame', 'artist:dino', '(artist:fkey:0.8)'):
            self.assertIn(t, M.ART)

    def test_restraint_negatives_and_r18_lora_off(self):
        """1111 第二轮: 签名消了但手铐/脚镣变多 → 点名束缚具 + 尾位 R18 细节 LoRA 归零。"""
        for t in ('(shackles:1.35)', '(handcuffs:1.35)', '(leg irons:1.35)',
                  '(ankle cuffs:1.35)', '(leash:1.3)', '(chains:1.3)'):
            self.assertIn(t, M.NEG_FIXED)
        # 泛词 collar 会打掉正向的 pj shirt with a white lace collar, 不许进负向
        self.assertNotIn('(collar:', M.NEG_FIXED)
        self.assertEqual(M.DETAIL_LORA_STRENGTH, 0.0)
        if not M.TEMPLATE_PNG.exists():
            self.skipTest('模板 PNG 不在本机(其他机器跳过)')
        api = M.build_api('POS', M.NEG_FIXED, 109003, 'loraoffbatch')
        node = api[M.DETAIL_LORA_NODE]['inputs']
        self.assertEqual(node['strength_model'], 0.0)
        self.assertEqual(node['strength_clip'], 0.0)
        # 其余 LoRA 链强度不许被顺手改掉
        self.assertEqual(api['175']['inputs']['strength_model'], 1.0)
        self.assertEqual(api['188']['inputs']['strength_model'], -2.0)
        self.assertEqual(api['189']['inputs']['strength_model'], 1.0)
        self.assertEqual(api['193']['inputs']['strength_model'], -1.0)


class T7FishPreset(unittest.TestCase):
    """大肥鱼两套预设分开: Q版(chibi+Q LoRA) / 女仆(原版 LoRA), 鲸尾都在锚里。"""

    def test_match_q_keywords(self):
        for s in ('q版大肥鱼', 'chibi fish girl', '二头身鱼娘', '肥鱼在打游戏',
                  'ds娘 sitting', 'whale maid girl', 'whalemaid mascot'):
            key, p = M.match_preset(s)
            self.assertEqual(key, 'fish_q', s)
            self.assertEqual(p['trigger'], 'ds_whalemaid')

    def test_match_maid_keywords(self):
        for s in ('大肥鱼女仆版', '女仆 fish girl', 'maid version whale maid',
                  '女仆版的你 whale maid'):
            key, p = M.match_preset(s)
            self.assertEqual(key, 'fish_maid', s)
            self.assertEqual(p['trigger'], 'deepseekchan')

    def test_no_cross_character_misroute(self):
        # 别人的请求带版本词也不准路由到鱼 LoRA(身份词必须在场)
        for s in ('女仆装的铃兰', 'chibi 可莉', 'maid version standing',
                  'maid dress girl in garden', '二头身的纳西妲'):
            key, _p = M.match_preset(s)
            self.assertIsNone(key, s)

    def test_no_match_keeps_tail(self):
        self.assertIsNone(M.match_preset('white dress girl in garden')[0])

    def test_compose_injects_trigger_and_anchors(self):
        pos, neg, err, pk = M.compose('whale maid girl eating snacks at her desk')
        self.assertIsNone(err)
        self.assertEqual(pk, 'fish_q')
        self.assertIn('ds_whalemaid', pos)
        self.assertIn('(her whale tail attached to her lower back:1.3)', pos)  # 鲸尾硬识别点
        self.assertIn('(chibi:1.35)', pos)
        self.assertLessEqual(len(pos), M.LIMIT)

    def test_build_api_swaps_tail_lora(self):
        if not M.TEMPLATE_PNG.exists():
            self.skipTest('模板 PNG 不在本机')
        _k, p = M.match_preset('q版大肥鱼')
        api = M.build_api('POS', M.NEG_FIXED, 109010, 'fishbatch', preset=p)
        tail = api[M.TAIL_LORA_NODE]['inputs']
        self.assertEqual(tail['lora_name'], 'Q版大肥鱼ds_whalemaid.safetensors')
        self.assertEqual(tail['strength_model'], 0.9)
        self.assertEqual(api['3']['inputs']['model'], [M.TAIL_LORA_NODE, 0])
        self.assertEqual(api['176']['inputs']['model'], [M.TAIL_LORA_NODE, 0])
        # 女仆版换的是另一个文件(DeepseekChan 星星尾巴女仆; ds_whalemaid 是 Q版拷贝别用)
        _k2, p2 = M.match_preset('女仆版 大肥鱼')
        api2 = M.build_api('POS', M.NEG_FIXED, 109011, 'maidbatch', preset=p2)
        self.assertEqual(api2[M.TAIL_LORA_NODE]['inputs']['lora_name'],
                         '女仆大肥鱼DeepseekChan_illust.safetensors')
        # 不换时保持模板原样(rossi)
        api3 = M.build_api('POS', M.NEG_FIXED, 109012, 'plainbatch')
        self.assertIn('rossi', api3[M.TAIL_LORA_NODE]['inputs']['lora_name'])


class T8MossEngine(unittest.TestCase):
    """MOSS 引擎(1113): 权重剥离 / 扁平 sampling_mode 键 / 手部警示 / 官方负面平词。"""

    def test_compose_strips_sdxl_weights(self):
        pos, _note, err = M.moss_compose('(white dress:1.3), ((long hair:1.2))',
                                         'red shoes', 'night garden')
        self.assertIsNone(err)
        self.assertNotIn(':1.3', pos)
        self.assertNotIn(':1.2', pos)
        self.assertIn('white dress', pos)
        self.assertIn('long hair', pos)
        self.assertIn('red shoes', pos)
        self.assertIn('night garden', pos)
        # 中文/引号逐字保留(MOSS 双语, 引号内文字会被逐字画进图)
        pos2, _n2, err2 = M.moss_compose('sign reads "肥鱼咖啡"', '', '')
        self.assertIsNone(err2)
        self.assertIn('肥鱼咖啡', pos2)

    def test_compose_hand_closeup_note(self):
        _pos, note, _err = M.moss_compose('close-up of her hand holding a teacup', '', '')
        self.assertIsNotNone(note)
        self.assertIn('sdxl', note)
        _p2, note2, _e2 = M.moss_compose('a coffee shop at night, sign reading "OPEN"', '', '')
        self.assertIsNone(note2)

    def test_compose_over_limit(self):
        _p, _n, err = M.moss_compose(('word ' * 500).strip(), '', '')
        self.assertIn('描述过长', err)

    def test_build_api_flat_sampling_keys(self):
        """TextGenerate 必须**平铺** sampling_mode.* 键(嵌套能过校验但执行报错, 1110 实锤)。"""
        api = M.build_moss_api('POS', M.MOSS_NEG, 109100, 'feiyuOS_moss/b1/fym_109100',
                               enhance=True)
        self.assertEqual(api['4']['inputs']['text'], ['10', 0])      # 正向来自扩写器
        self.assertEqual(api['10']['class_type'], 'TextGenerate')
        tg = api['10']['inputs']
        self.assertEqual(tg['sampling_mode'], 'on')
        for k in ('sampling_mode.temperature', 'sampling_mode.top_k',
                  'sampling_mode.top_p', 'sampling_mode.min_p',
                  'sampling_mode.repetition_penalty', 'sampling_mode.seed'):
            self.assertIn(k, tg, k)
        self.assertEqual(tg['sampling_mode.seed'], 109100)
        self.assertEqual(tg['max_length'], 1024)
        self.assertEqual(api['7']['inputs']['positive'], ['4', 0])
        self.assertEqual(api['7']['inputs']['cfg'], 2.5)
        self.assertEqual(api['7']['inputs']['sampler_name'], 'er_sde')
        self.assertEqual(api['2']['inputs']['type'], 'moss_image')   # CLIPLoader type 铁律
        self.assertEqual(api['9']['inputs']['filename_prefix'],
                         'feiyuOS_moss/b1/fym_109100')
        # enhance=False: 直连原文, 无 TextGenerate 节点
        api2 = M.build_moss_api('POS RAW', M.MOSS_NEG, 109101, 'p', enhance=False)
        self.assertEqual(api2['4']['inputs']['text'], 'POS RAW')
        self.assertNotIn('10', api2)
        self.assertEqual(api['5']['inputs']['text'], M.MOSS_NEG)

    def test_build_api_default_no_enhancer(self):
        """契约 §五.3: TextGenerate 本契约不用 → 默认直连, 显式 enhance=true 才开。"""
        api = M.build_moss_api('POS', M.MOSS_NEG, 1, 'p')
        self.assertEqual(api['4']['inputs']['text'], 'POS')
        self.assertNotIn('10', api)

    def test_compose_quoted_text_over_150_rejected(self):
        long_cn = '一二三四五六七八九十' * 16          # 160 字 → 拒
        _p, _n, err = M.moss_compose('poster with text "%s"' % long_cn, '', '')
        self.assertIn('拆块', err)
        short_cn = '一二三四五六七八九十' * 14          # 140 字 → 放行
        p, _n, e = M.moss_compose('poster with text "%s"' % short_cn, '', '')
        self.assertIsNone(e)

    def test_moss_neg_is_official_plain_words(self):
        self.assertIn('nsfw', M.MOSS_NEG)
        self.assertIn('multiple girls', M.MOSS_NEG)   # 契约 §四.5 业务负向平词追加
        self.assertNotIn(':1.', M.MOSS_NEG)           # MOSS 加权语法无效, 必须平词

    def test_free_tolerates_empty_body(self):
        """owner 验收反馈 A: ComfyUI /free 回 **200 + 空 body** → json.loads('') 必炸,
        动作其实生效了却记 ok:false(ledger 里那条 moss_free 假失败)。空体按 {} 处理。"""
        class R:
            def read(self):
                return b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        old_url, old_led = urllib.request.urlopen, M.LEDGER
        tmp = tempfile.mkdtemp(prefix='fyimg_ledger_')
        led = Path(tmp) / 'ledger_empty.jsonl'
        M.LEDGER = led
        urllib.request.urlopen = lambda req, timeout=None: R()
        try:
            self.assertEqual(M._post('/free', {}, base='http://127.0.0.1:9'), {})
            self.assertTrue(M.moss_free())
        finally:
            urllib.request.urlopen, M.LEDGER = old_url, old_led
        recs = [json.loads(l) for l in led.read_text(encoding='utf-8').splitlines() if l.strip()]
        self.assertTrue(recs and recs[-1]['ok'] is True, recs)   # 不再是假失败
        shutil.rmtree(tmp, ignore_errors=True)

    def test_vram_gate_frees_idle_moss_residue(self):
        """owner 验收反馈 B: 8190 跑完图模型常驻 ~12.3G(**queue=0 也占着**) → 同一实例的
        第二单被自己的驻留显存误判成"宿主在跑大型游戏"拒掉。修后 8190 空载先 /free 再测。"""
        import types

        def _stub(free_seq, moss_q, freed):
            return {
                'comfy_queue_count': lambda: 0,
                '_queue_count': lambda url, timeout=5: 0,
                'vram_free_gb': lambda: free_seq.pop(0) if free_seq else 12.0,
                'comfy_probe': lambda timeout=3: False,
                'moss_probe': lambda timeout=3: True,
                'moss_queue_count': lambda: moss_q,
                'moss_free': lambda: (freed.append(1), True)[1],
                'time': types.SimpleNamespace(sleep=lambda s: None),
            }

        def run(stubs):
            old = {k: getattr(M, k) for k in stubs}
            for k, v in stubs.items():
                setattr(M, k, v)
            try:
                return M.moss_vram_gate()
            finally:
                for k, v in old.items():
                    setattr(M, k, v)

        # ① 8190 空载驻模(3.4G) → 先卸自己的残留(回 12G) → 放行
        freed = []
        self.assertIsNone(run(_stub([3.4, 12.0], 0, freed)))
        self.assertEqual(len(freed), 1, '空载时应先卸 8190 自己的驻留模型')
        # ② 8190 队列忙(别人在跑) → 不许动它, 照旧拒单(不动别人的负载)
        freed2 = []
        self.assertIn('大型游戏', run(_stub([3.4, 3.4, 3.4], 2, freed2)) or '')
        self.assertEqual(freed2, [], '8190 队列忙时不该去 /free 它的模型')


if __name__ == '__main__':
    unittest.main(verbosity=2)
