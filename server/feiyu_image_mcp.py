#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feiyu-image-mcp —— 肥鱼生图模块 (PC 侧, 2026-11-10 M2/M3)

她在 QQ 说一句话 → 云上大脑翻译成 danbooru tag → 调本模块的 MCP 工具 →
PC 用生图工作盘已验证的画风基线(coco 12 artist tag, 1108 定案)出**非 R18** 图 →
过洗图硬闸(剥元数据) → 返回洗净图路径 → 桥 (qq-say.mjs --image) 发图。

协议: 照抄 BWF MCP(18081) 的 streamable-HTTP JSON-RPC, 见 docs/肥鱼生图模块_设计稿_1110.md 第五节。
监听: 127.0.0.1:18082 (只本机; 云上经 remote-proxy.py 17853→18082 Tailscale 中继)。

红线(全硬编码, 无开关):
  1. 非 R18 双层闸 —— 负向焊死 nsfw/suggestive + 正向 R18 词黑名单拒单
  2. 真人声优/偶像名字形象一律拒单(real_person_names.txt + 指示词)
  3. 洗图硬闸 —— raw/ 原图带提示词永不出目录, MCP 返回的路径只有洗净版
  4. 直播推流中禁止自动拉起 ComfyUI(宁败单, 不打架)
  5. 不动 DSH 插件层(本模块纯 PC 侧)

用法:
  python -B feiyu_image_mcp.py            # 正式(真渲染 M4 接入, 当前 gen_image 返回未接入)
  python -B feiyu_image_mcp.py --fake     # 联调: 不碰 ComfyUI, 出占位 PNG 走完整洗图链路
  python -B test_feiyu_image_mcp.py       # 单测(洗图三断言/拒单闸/拼词/MCP 冒烟)

基线出处(一字不改, 改前先对 G:\\生图工作盘\\xufufu_pipeline\\):
  ART   = probe_coco_linglan_1108.ART           (12 artist tag, 266 字)
  HEAD  = gen_suzuran_beauty_1110.HEAD          (solo:1.25 + 1girl)
  NEG   = 1109 定案串剥 censored 段 + TAIL_NEG(nsfw/suggestive 保留) + 黑丝组
"""
import io
import json
import os
import re
import shutil
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 18082
# ── 路径(生图工作盘侧) ──────────────────────────────────────
WORKSPACE = Path(r'G:\生图工作盘')
OUT_BASE = WORKSPACE / 'ComfyUI_output' / '肥鱼OS'
COMFY_DIR = Path(r'G:\ComfyUI')
COMFY_URL = 'http://127.0.0.1:8188'
SEED_BASE = 109000            # 台账顺延新段
SEED_USED_FILE = HERE / 'seed_used.json'

# ── 直播互锁(M3, feiyu-os 1110 拍板): 进程名含「哔哩哔哩」或 livehime 即推流中, ──
# 宁保守(开着就算); **不要**用 live_host /healthz 的 obs.streaming 当判据
# (推流走直播姬不走 OBS, 实测房间在播时 obs.streaming=false, 反过来也不可靠)。
LIVE_LOCK_FILES = []                             # 预留状态文件通道, 暂无
LIVE_PROCS = ['哔哩哔哩', 'livehime']
COMFY_START_TIMEOUT = 300                        # 拉起后探活等待上限(秒)
# ── 显存驱动生命周期(用户 1110 指令): ──────────────────────
#   探测剩余显存; 不够 = 判定宿主在跑大型游戏 → 不拉起生图任务;
#   ComfyUI 已在线 = 有生图任务也直接投(插队); 空闲且显存够 → 自己拉起服务;
#   本单出完且队列排空 → 自动卸载模型(/free)把显存还给游戏。
VRAM_MIN_GB = 9.0                                # SDXL 全链常驻约 8.1GB(AGENTS 实测) + 工作余量

LIMIT = 750                                      # 正向硬闸(美图线定案)
STYLE_ENUM = ('coco_linglan_1108',)              # 对她不开放自由文本; 内部预留
MAX_COUNT = 4
MIN_SEND_INTERVAL = 3.0                          # os 约定: QQ 连发间隔≥3s(风控); 模块侧对 gen_image 调用节流
_LAST_GEN_TS = [0.0]                             # [可变] 上次成功出单时刻

# ── 角色预设(尾位 LoRA 原位换名 = 零改线) ─────────────────────────────
# 模板 LoRA 链: 175 Expressive_H → 188 contrast → 189 brightness → 193 saturation
#   → 192 realistic_cum → **900 角色槽**(3/KSampler、176脸、142手 全吃它的输出)。
# 命中关键词就把 900 原位换 lora_name+权重 —— 下游全部自动继承(1103 教训:
# 重连 LoRA 必须"原位换"或显式点名, 否则 FaceDetailer 拿旧模型会把脸换成另一个人)。
# 触发词/权重/外观锚出处: 《Ds娘的日常》复刻档案(ref01 全对版), 权重 0.9 沿用已验证配方。
TAIL_LORA_NODE = '900'
# 大肥鱼(DS娘)两套预设**分开接线分开画**(用户 1110 拍板: "女仆和q版应该分开",
# 二次纠正: "Q版要专门用Q版lora生成, 女仆是有星星尾巴的那个"):
#   fish_q    = Q版二头身本体 —— Q版大肥鱼ds_whalemaid(触发 ds_whalemaid, 47 张数据集)
#   fish_maid = 女仆原版      —— 女仆大肥鱼DeepseekChan_illust(触发 deepseekchan,
#                                153 张全角色数据集: blue_hair/whale_fin_ears/gradient_hair,
#                                星星尾巴)。⚠ ds_whalemaid.safetensors 与 Q版是同物异名拷贝
#                                (metadata output_name/尺寸/tag 全同), 别当女仆版用(1110 实锤)。
# 触发词/权重出处《Ds娘的日常》复刻档案, 0.9 起步。
CHARACTER_PRESETS = {
    'fish_q': {
        'match': ('q版', 'chibi', '二头身'),
        'lora': 'Q版大肥鱼ds_whalemaid.safetensors',
        'strength': 0.9,
        'trigger': 'ds_whalemaid',
        # 锚对齐用户给的标准像(1110): 圆头二头身/波浪长发/围裙鲸鱼徽章/白蕾丝短袜
        'anchors': ('(chibi:1.35), (two-heads-tall proportion:1.3), whale fin ears, '
                    'ahoge, (long wavy hair:1.15), dark blue hair, blue gradient hair tips, '
                    '(big whale tail:1.3), maid dress, (whale print apron:1.15), blue bow, '
                    'blue bows in hair, big blue eyes, white frilled ankle socks, '
                    'mary janes, '),
    },
    'fish_maid': {
        'match': ('女仆', 'maid version', 'maid ver'),
        'lora': '女仆大肥鱼DeepseekChan_illust.safetensors',
        'strength': 0.9,
        'trigger': 'deepseekchan',
        # 锚对齐标准像: 黑白女仆装+白色过膝袜+星月纹大鲸尾(星星尾巴是她的硬识别点)
        'anchors': ('whale fin ears, ahoge, (long wavy hair:1.15), dark blue hair, '
                    'blue gradient hair tips, (big whale tail with star pattern:1.3), '
                    'maid dress with white apron, white thighhighs, black bow, '
                    'big blue eyes, '),
    },
}
DEFAULT_PRESET = 'fish_q'                        # 光说"肥鱼/ds娘"= 她本体(Q版)
DEFAULT_PRESET_KEYWORDS = ('肥鱼', '鱼娘', 'ds娘', 'whalemaid', 'whale maid',
                           'whale girl', 'deepseek whale', 'ds-chan')


def match_preset(text):
    t = (text or '').lower()
    for key, p in CHARACTER_PRESETS.items():
        if any(m.lower() in t for m in p['match']):
            return key, p
    if any(m.lower() in t for m in DEFAULT_PRESET_KEYWORDS):
        return DEFAULT_PRESET, CHARACTER_PRESETS[DEFAULT_PRESET]
    return None, None

# ── 画风基线(钉死) ────────────────────────────────────────
HEAD = '(solo:1.25), 1girl, '
ART = ('(artist:tianliang duohe fangdongye:0.9), artist:ciloranko, artist:noyu, '
       'noyu23386566,ningen mame, (artist:sho:0.8264462809917354), '
       '(sho lwlw:0.9090909090909091),(artist:kedama_milk:0.5),(bee_\\(deadflow\\):0.7),'
       'artist:dino,(artist:fkey:0.8),(artist:wlop:1),year 2023,')

# 1109 定案串: BASE_NEG(剥 censored 段) + TAIL_NEG。⚠ 与做爱批的差别就在
# `nsfw, suggestive` **保留** —— 这是非 R18 的第一道保险(美图批 66/66 验证过)。
NEG_BASE = ('negative_hand, NegfeetV2, bad anatomy,blurry,(worst quality:1.8),low quality,'
            'hands bad,face bad,(normal quality:1.3),bad hands,[extra fingers],'
            'mutated hands and fingers,extra legs,extra arms,duplicate,cropped,text,jpeg,'
            'artifacts,signature,watermark,username,blurry,artist name,trademark,title,'
            'multiple view,Reference sheet,long body,multiple breasts,mutated,bad anatomy,'
            'disfigured,bad proportions,bad feet,ugly,text font ui,missing limb,monochrome,'
            'bad hands,worst quality, low quality, bad quality, multiple views, 4koma, comic, '
            'jpeg artifacts, monochrome, sepia, greyscale, flat color, pale color, muted color, '
            'low contrast, bad anatomy, picture frame, english text, logo, patreon username, '
            'web address,(worst quality, low quality:1.2), bad feet,score_4,score_3,score_2,'
            'score_1,too many fingers,Blurred eyes,')
NEG_TAIL = ('nsfw, suggestive, multiple girls, 2girls, 3girls, crowd, extra characters, '
            'duplicate, ')
# 1110 用户"不喜欢黑丝" —— 模块级沿用(压腿部紧身单品)
NEG_TIGHTS = ('(black tights:1.35), (pantyhose:1.3), (black leggings:1.3), (fishnet:1.3), '
              '(thigh straps:1.2)')
NEG_FIXED = NEG_BASE + NEG_TAIL + NEG_TIGHTS

# ── 拒单闸 ────────────────────────────────────────────────
# R18 词黑名单(命中即拒)。英文按词边界, 中文按整词(不写裸"裸"字——会误杀"裸足")。
R18_WORDS_EN = [
    'sex', 'vaginal', 'vagina', 'pussy', 'penis', 'dick', 'cock', 'cum', 'creampie',
    'nipple', 'areola', 'nude', 'nudity', 'naked', 'topless', 'bottomless', 'uncensored',
    'bdsm', 'bondage', 'shibari', 'masturbation', 'dildo', 'vibrator', 'hentai', 'nsfw',
    'erotic', 'orgasm', 'ahegao', 'penetrat', 'anal', 'blowjob', 'fellatio', 'paizuri',
    'handjob', 'titfuck', 'cumshot', 'bukakke', 'bukkake', 'r18', 'xxx', 'porn',
    'spread pussy', 'spread legs nude', 'breasts out', 'exposed breasts', 'nudify',
]
R18_WORDS_ZH = [
    '做爱', '性爱', '性交', '插入', '内射', '射精', '精液', '阴道', '阴蒂', '阴唇',
    '阴茎', '肉棒', '口交', '乳交', '足交', '手淫', '自慰', '潮吹', '后入', '骑乘位',
    '全裸', '裸体', '裸露', '露点', '露乳', '奶子', '淫', '色情', '涩图', '车图',
    '黄色图', '18禁', '十八禁', '成人内容', ' nsfw', 'r18', '安慰器', '跳蛋',
]
# 真人红线: 指示词(命中即拒) + 名单文件(一行一个名字, 中/日/罗马字, os 侧可维护)
REAL_HINT_ZH = ['真人', '本人', '声优', '偶像', '爱豆', '歌手', '演员', '主播',
                '中之人', '明星', '写真', '证件照', '实拍', '照片级真人']
REAL_HINT_EN = ['real person', 'idol', 'voice actor', 'seiyuu', 'celebrity', 'photo of me']
REAL_NAMES_FILE = HERE / 'real_person_names.txt'


def _load_real_names():
    try:
        names = []
        for ln in REAL_NAMES_FILE.read_text(encoding='utf-8').splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                names.append(ln.lower())
        return names
    except OSError:
        return []


REAL_NAMES = _load_real_names()


def _norm(s):
    return (s or '').lower().replace('\uff2f\uff32\uff11\uff18', 'r18')


def _en_word_hit(text, word):
    """英文按词边界(比子串少误杀: e.g. `sex` 不吃 `essex`)。"""
    return re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', text) is not None


def check_request(subject, outfit='', scene=''):
    """拒单双闸 + 真人红线。通过返回 None, 否则返回原因字符串。"""
    blob = _norm(' '.join([subject, outfit, scene]))
    for w in R18_WORDS_ZH:
        if w in blob:
            return '拒单: 请求含 R18 词「%s」。本模块只出非 R18 图。' % w.strip()
    for w in R18_WORDS_EN:
        if _en_word_hit(blob, w):
            return '拒单: 请求含 R18 词「%s」。本模块只出非 R18 图。' % w
    for w in REAL_NAMES:
        if w in blob:
            return ('拒单: 真人声优/偶像/公众人物(%s)的名字和形象一律不画。'
                    '可以试试画成原创角色或已有版权角色。' % w)
    for w in REAL_HINT_ZH + REAL_HINT_EN:
        if w in blob:
            return ('拒单: 请求含真人指向词「%s」。真人声优/偶像/公众人物一律不画, '
                    '换成原创角色或动漫角色即可。' % w)
    return None


def compose(subject, outfit='', scene='', preset_key=None):
    """拼正向(照美图线 build(): HEAD+ART+outfit+subject+', '+scene) 与固定负向。

    命中角色预设时, 触发词+外观锚插在 ART 之后(角色锚位), subject 仍是核心。
    长度降级梯只砍场景再砍服装(基线定案: 超长删场景, 不加加权); subject 是核心永不砍,
    砍完仍超限返回 None。双保险: 拼完再过一遍 R18 词检查(防上游漏网)。
    """
    subject = ' '.join((subject or '').split())
    outfit = ' '.join((outfit or '').split())
    scene = ' '.join((scene or '').split())
    _key, preset = match_preset(subject + ' ' + outfit)
    if preset_key and preset_key in CHARACTER_PRESETS:
        _key, preset = preset_key, CHARACTER_PRESETS[preset_key]
    char_block = ('' + preset['trigger'] + ', ' + preset['anchors']) if preset else ''
    core = (HEAD + ART + char_block
            + (outfit + ' ' if outfit else '') + subject).strip()
    core = re.sub(r'\s*,\s*', ', ', core).rstrip(', ')
    if len(core) > LIMIT:
        return None, None, '描述过长(%d 字, 上限 %d): 请精简 subject' % (len(core), LIMIT), None
    pos = (core + (', ' + scene if scene else '')).rstrip(', ')
    if len(pos) > LIMIT:
        pos = core                                   # 降级: 整段场景删掉
    bad = check_request(pos)
    if bad:
        return None, None, bad, None
    return pos, NEG_FIXED, None, (_key if preset else None)


# ══ 洗图硬闸(M2 先行) ═══════════════════════════════════════
# 逻辑照抄 G:\生图工作盘\洗图工具\strip_meta.py(像素级无损重存, 已实战)。
# raw/ 原图带 ComfyUI 的 tEXt prompt(完整节点图=明文), 永不出目录。

PNG_KEEP = {b'IHDR', b'PLTE', b'IDAT', b'IEND', b'tRNS', b'iCCP', b'sRGB', b'gAMA'}


def _png_chunks(data):
    pos, n = 8, len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > n:
            raise ValueError('PNG 块越界, 文件可能损坏')
        yield ctype, data[pos:end]
        pos = end
        if ctype == b'IEND':
            break


def strip_png_bytes(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('不是 PNG')
    kept = []
    for ctype, raw in _png_chunks(data):
        if ctype in PNG_KEEP:
            kept.append(raw)
    if not kept or kept[0][4:8] != b'IHDR':
        raise ValueError('PNG 缺 IHDR')
    return b'\x89PNG\r\n\x1a\n' + b''.join(kept)


def strip_jpeg_bytes(d):
    if d[:2] != b'\xff\xd8':
        raise ValueError('不是 JPEG')
    out = bytearray(b'\xff\xd8')
    i, n = 2, len(d)
    while i < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1] if i + 1 < n else 0
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            out += b'\xff\xd9'
            break
        (seglen,) = struct.unpack('>H', d[i + 2:i + 4])
        seg = d[i:i + 2 + seglen]
        if marker == 0xDA:                        # SOS 之后是熵编码数据, 原样搬到 EOI
            out += seg
            i += 2 + seglen
            j = i
            while j + 1 < n:
                if d[j] == 0xFF and d[j + 1] == 0xD9:
                    out += d[i:j + 2]
                    return bytes(out)
                j += 1
            out += d[i:]
            return bytes(out)
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):   # APPn/COM 丢
            out += seg
        i += 2 + seglen
    if out[-2:] != b'\xff\xd9':
        out += b'\xff\xd9'
    return bytes(out)


def pixels_equal(a_path, b_path):
    from PIL import Image, ImageChops
    with Image.open(a_path) as ia, Image.open(b_path) as ib:
        if ia.size != ib.size:
            return False, -1
        diff = ImageChops.difference(ia.convert('RGB'), ib.convert('RGB'))
        box = diff.getbbox()
        if box is None:
            return True, 0
        return False, max(diff.getextrema()[c][1] for c in range(3))


def has_text_meta(path):
    from PIL import Image
    try:
        with Image.open(path) as im:
            if im.format == 'JPEG':
                return bool(im.info.get('exif') or im.info.get('comment'))
            return bool(im.text)
    except Exception:
        return True                                # 读不了的当有残留, 宁严勿宽


def strip_gate(raw_dir, out_dir):
    """raw_dir → out_dir 逐张剥元数据。返回 (成功数, 失败明细[])。

    硬闸语义: 失败的图**不会出现在 out_dir**(不出目录); 像素校验不过的也拦下。
    """
    from PIL import Image
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, fails = 0, []
    for p in sorted(Path(raw_dir).iterdir()):
        ext = p.suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
            continue
        tmp = out_dir / (p.stem + '.washing')
        try:
            data = p.read_bytes()
            if ext == '.png':
                (tmp).write_bytes(strip_png_bytes(data))
            elif ext in ('.jpg', '.jpeg'):
                (tmp).write_bytes(strip_jpeg_bytes(data))
            else:
                with Image.open(p) as im:          # webp: PIL 重存(剥容器块)
                    im.save(tmp, 'WEBP', lossless=True)
            same, maxdiff = pixels_equal(p, tmp)
            if not same:
                tmp.unlink(missing_ok=True)
                fails.append((p.name, '像素校验失败 maxdiff=%s' % maxdiff))
                continue
            if has_text_meta(tmp):
                tmp.unlink(missing_ok=True)
                fails.append((p.name, '洗后仍有内嵌文本'))
                continue
            final = out_dir / p.name
            tmp.replace(final)
            ok += 1
        except Exception as e:                     # noqa
            tmp.unlink(missing_ok=True)
            fails.append((p.name, str(e)))
    return ok, fails


# ══ ComfyUI 生命周期(M3, 只写不跑) ═══════════════════════════
def comfy_probe(timeout=3):
    try:
        import urllib.request
        with urllib.request.urlopen(COMFY_URL + '/system_stats', timeout=timeout):
            return True
    except Exception:
        return False


def is_live_now():
    """直播互锁判据(os 1110 拍板): 进程名含「哔哩哔哩」或 livehime = 推流中, 宁保守。

    tasklist 输出显式按 gbk 解(中文进程名; utf-8 兜底), 不依赖控制台 locale。
    """
    for f in LIVE_LOCK_FILES:
        if f.exists():
            return True
    try:
        import subprocess
        r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                           capture_output=True, timeout=15)
        raw = r.stdout
        out = raw.decode('gbk', errors='ignore')
        if all(ch.isascii() for ch in out[:200]):
            out = raw.decode('utf-8', errors='ignore')
        out = out.lower()
        for proc in LIVE_PROCS:
            if proc.lower() in out:
                return True
    except Exception:
        pass
    return False


def vram_free_gb():
    """剩余显存(GiB); nvidia-smi 探不到返回 None(调用方按"不能确认余量"处理)。"""
    try:
        import subprocess
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.free',
             '--format=csv,noheader,nounits'],
            capture_output=True, timeout=10)
        mb = int(r.stdout.decode('utf-8', 'ignore').strip().splitlines()[0])
        return mb / 1024.0
    except Exception:
        return None


def comfy_queue_count():
    """8188 当前在跑+排队任务数; 服务器不在返回 None。"""
    try:
        import urllib.request
        with urllib.request.urlopen(COMFY_URL + '/queue', timeout=5) as r:
            q = json.loads(r.read().decode('utf-8'))
        return len(q.get('queue_running') or []) + len(q.get('queue_pending') or [])
    except Exception:
        return None


def comfy_free():
    """自动卸载模型归还显存(/free, 核心新API; 旧核 404 时只能重启释放, 如实记账)。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            COMFY_URL + '/free',
            data=json.dumps({'unload_models': True, 'free_memory': True}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode('utf-8', 'ignore')[:100]
        _ledger({'tool': 'comfy_free', 'ok': True, 'resp': body})
        return True
    except Exception as e:                         # noqa
        _ledger({'tool': 'comfy_free', 'ok': False, 'err': str(e)[:120]})
        return False


_COMFY_PROC = None


def ensure_comfy():
    """探活 → 拉起(带 --disable-comfy-compiler, DETACHED 脱离本进程树)。

    双重闸(缺一不可):
      直播中(哔哩哔哩/livehime 进程)绝不拉起 —— 显存打架打崩直播帧率;
      显存剩余 < VRAM_MIN_GB 视为宿主在跑大型游戏, 绝不拉起。
    返回 (True, 'ok') 或 (False, 原因)。
    """
    global _COMFY_PROC
    if comfy_probe():
        return True, 'ok'
    if is_live_now():
        return False, '直播推流中, 禁止自动拉起 ComfyUI(避免显存打架); 请稍后再试'
    free = vram_free_gb()
    if free is None:
        return False, '无法确认显存余量(nvidia-smi 不可用), 保守起见不拉起'
    if free < VRAM_MIN_GB:
        return False, ('显存剩余 %.1f GiB(<%.1f), 判定宿主在跑大型游戏/重载, '
                       '不拉起生图服务; 请关游戏或稍后再试' % (free, VRAM_MIN_GB))
    log = (LOG / ('comfy8188_%s.log' % time.strftime('%m%d_%H%M%S'))).open('ab')
    try:
        import subprocess
        _COMFY_PROC = subprocess.Popen(
            ['python', '-B', 'main.py', '--listen', '127.0.0.1', '--port', '8188',
             '--force-fp16', '--disable-api-nodes', '--disable-comfy-compiler',
             '--output-directory', 'G:/生图工作盘/ComfyUI_output'],
            cwd=str(COMFY_DIR), stdout=log, stderr=subprocess.STDOUT,
            creationflags=0x00000008 | 0x00000200)   # DETACHED | NEW_PROCESS_GROUP
    except Exception as e:                         # noqa
        log.close()
        return False, 'ComfyUI 拉起失败: %s' % e
    deadline = time.time() + COMFY_START_TIMEOUT
    while time.time() < deadline:
        if comfy_probe():
            return True, '拉起成功(%.0fs)' % (COMFY_START_TIMEOUT - (deadline - time.time()))
        time.sleep(5)
    return False, 'ComfyUI 启动超时(%ds)' % COMFY_START_TIMEOUT


# ══ 渲染(真通道) ═════════════════════════════════════════════
def _next_seed(count):
    used = 0
    try:
        used = json.loads(SEED_USED_FILE.read_text(encoding='utf-8')).get('last', 0)
    except Exception:
        used = SEED_BASE
    start = max(used + 1, SEED_BASE)
    SEED_USED_FILE.write_text(json.dumps({'last': start + count - 1}), encoding='utf-8')
    return start


# 模板源 = 美图66 批成品 PNG 内嵌节点图(同 coco 全链; ⚠不要用 R18 批当源——
# 它的负向已拆 nsfw)。接线照 gen_suzuran_beauty_1110.make_api 通用化:
#   165/153 换文本, 17 dn0.55, 301/302 手部细节器新词, 142←301/302, 77←142;
#   脸细节器 176 在模板里已引用 165/153, 换文本自动继承, 不用重接。
TEMPLATE_PNG = Path(r'G:\生图工作盘\ComfyUI_output\CunnyFunky\铃兰_美图66_1109'
                    r'\szb_001_875000_00001_.png')
SEED_NODES = ('3', '17', '142', '176')
CLIENT_ID = 'feiyu-image-mcp'
RENDER_TIMEOUT = 600                              # 单张轮询上限(秒)
HAND_POS = (', perfect hands, detailed fingers, five fingers on each hand, '
            'natural hand shape')
HAND_NEG = (', fused fingers, too many fingers, missing fingers, extra digits, '
            'malformed hands, (toes:1.2), (feet instead of hands:1.2)')


def _post(path, payload, timeout=30):
    import urllib.request
    req = urllib.request.Request(
        COMFY_URL + path,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def build_api(pos, neg, seed, batch_name, preset=None):
    """从模板 PNG 载入节点图并按美图档接线。命中角色预设时尾位 LoRA(node 900)原位换名。"""
    from PIL import Image
    with Image.open(TEMPLATE_PNG) as im:
        api = json.loads(im.text['prompt'])
    api['165']['inputs']['text'] = pos
    api['153']['inputs']['text'] = neg
    api['17']['inputs']['denoise'] = 0.55          # 美图档(比插入拍多给重画)
    if preset:
        api[TAIL_LORA_NODE]['inputs']['lora_name'] = preset['lora']
        api[TAIL_LORA_NODE]['inputs']['strength_model'] = preset['strength']
        api[TAIL_LORA_NODE]['inputs']['strength_clip'] = preset['strength']
    api['301'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': pos + HAND_POS,
                             'clip': list(api['165']['inputs']['clip'])}}
    api['302'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': neg + HAND_NEG,
                             'clip': list(api['153']['inputs']['clip'])}}
    api['142']['inputs']['positive'] = ['301', 0]
    api['142']['inputs']['negative'] = ['302', 0]
    api['142']['inputs']['denoise'] = 0.55
    api['142']['inputs']['cycle'] = 1
    api['77']['inputs']['images'] = ['142', 0]
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/%s/fyi_%d' % (batch_name, seed)
    for n in SEED_NODES:
        api[n]['inputs']['seed'] = seed
    return api


def _wait_file(prompt_id, seed, batch_dir, timeout=RENDER_TIMEOUT):
    """轮询 /history 直到 77 节点出文件并落盘, 返回文件 Path 或 None。"""
    import urllib.request
    deadline = time.time() + timeout
    want = batch_dir / ('fyi_%d_*.png' % seed)
    while time.time() < deadline:
        time.sleep(4)
        try:
            with urllib.request.urlopen(COMFY_URL + '/history/' + prompt_id,
                                        timeout=10) as r:
                hist = json.loads(r.read().decode('utf-8'))
        except Exception:
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        outs = (entry.get('outputs') or {}).get('77') or {}
        if outs.get('images'):
            hits = sorted(batch_dir.glob('fyi_%d_*.png' % seed))
            if hits:
                return hits[0]
        # history 有记录但文件没落地 → 继续等(极端情况超时由 deadline 兜底)
        _ = want                    # noqa: 占位保持意图可读
    return None


def render_batch(pos, neg, batch_dir, seeds, preset=None):
    """真渲染: 逐 seed 提交+轮询落盘 → 原图挪 raw/ → 洗图硬闸 → 返回洗净路径。

    出错语义: 任一 seed 失败即返回 ok:false(已成的图仍在 batch_dir/raw/ 不外传)。
    """
    batch_dir = Path(batch_dir)
    raw_dir = batch_dir / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for s in seeds:
        api = build_api(pos, neg, s, batch_dir.name, preset=preset)
        try:
            r = _post('/prompt', {'prompt': api, 'client_id': CLIENT_ID})
        except Exception as e:                     # noqa
            return {'ok': False,
                    'reason': '提交失败(seed %d): %s; 看 server/logs/comfy8188_*.log'
                              % (s, str(e)[:160])}
        pid = r.get('prompt_id')
        if not pid:
            return {'ok': False,
                    'reason': '提交无 prompt_id: %s' % json.dumps(r, ensure_ascii=False)[:200]}
        f = _wait_file(pid, s, batch_dir)
        if f is None:
            return {'ok': False,
                    'reason': 'seed %d 渲染超时(%ds); ComfyUI 队列/日志自查' % (s, RENDER_TIMEOUT)}
        dst = raw_dir / f.name
        shutil.move(str(f), str(dst))
        moved.append(dst.name)
    n_ok, fails = strip_gate(raw_dir, batch_dir)
    images = [str(batch_dir / p.name) for p in sorted(batch_dir.glob('*.png'))]
    return {'ok': n_ok > 0, 'images': images, 'batch_dir': str(batch_dir),
            'seeds': seeds, 'washed': n_ok,
            'washed_fail': [x[0] for x in fails]}


def _fake_png(path, hue_seed):
    """联调占位图: 纯色 PNG(颜色由 subject 哈希决定), 带假 tEXt prompt → 走真洗图链。"""
    from PIL import Image, PngImagePlugin
    h = int(hue_seed.encode('utf-8').hex()[-6:], 16) if hue_seed else 0x123456
    im = Image.new('RGB', (256, 384), (h >> 16 & 255, h >> 8 & 255, h & 255))
    meta = PngImagePlugin.PngInfo()
    meta.add_text('prompt', '{"165": {"inputs": {"text": "FAKE PROMPT 应被硬闸剥掉"}}}')
    meta.add_text('workflow', '{"fake": true}')
    im.save(path, 'PNG', pnginfo=meta)


def _slug(s, n=14):
    s = re.sub(r'[\\/:*?"<>|\s]+', '', (s or 'img'))[:n]
    return s or 'img'


# ══ MCP 工具面 ═════════════════════════════════════════════
LEDGER = HERE / 'ledger.jsonl'
LOG = HERE / 'logs'
FAKE = False                                    # --fake 联调开关


def _ledger(evt):
    evt = dict(evt, ts=time.strftime('%Y-%m-%d %H:%M:%S'))
    try:
        with LEDGER.open('a', encoding='utf-8') as f:
            f.write(json.dumps(evt, ensure_ascii=False) + '\n')
    except OSError:
        pass


def tool_gen_image(args):
    gap = time.time() - _LAST_GEN_TS[0]
    if _LAST_GEN_TS[0] and gap < MIN_SEND_INTERVAL:
        return {'ok': False,
                'reason': 'QQ 发图节流: 距上一单 %.1fs(需≥%ds), 请稍候再试' % (gap, MIN_SEND_INTERVAL)}
    subject = args.get('subject') or ''
    outfit = args.get('outfit') or ''
    scene = args.get('scene') or ''
    try:
        count = int(args.get('count') or 2)
    except (TypeError, ValueError):
        count = 2
    count = max(1, min(MAX_COUNT, count))
    seed = args.get('seed')
    if not isinstance(subject, str) or len(subject.strip()) < 2:
        return {'ok': False, 'reason': 'subject 太短: 用英文 danbooru tag 描述画什么'
                                       '(角色+动作/构图), 例如 "silver hair girl in white '
                                       'dress reading a book, library"'}

    why = check_request(subject, outfit, scene)
    if why:
        _ledger({'tool': 'gen_image', 'ok': False, 'reject': why})
        return {'ok': False, 'reason': why}

    pos, neg, err, preset_key = compose(subject, outfit, scene)
    if err:
        return {'ok': False, 'reason': err}
    _preset = CHARACTER_PRESETS.get(preset_key) if preset_key else None

    # seed 分配与批次目录建在**全闸通过之后**(1110 夜教训: 拒单调用白烧 seed 段
    # 且会留空目录; seed 段要和真实成图一一对应, 台账按 seed 审计)。
    def _alloc():
        s = int(seed) if seed is not None else _next_seed(count)
        bd = OUT_BASE / ('%s_%s' % (datetime.now().strftime('%m%d'), _slug(subject)))
        (bd / 'raw').mkdir(parents=True, exist_ok=True)
        return [s + i for i in range(count)], bd

    if FAKE:
        seeds, batch_dir = _alloc()
        raw_dir = batch_dir / 'raw'
        for s in seeds:
            _fake_png(raw_dir / ('fyi_%d_%06d.png' % (s, s)), subject + str(s))
        n_ok, fails = strip_gate(raw_dir, batch_dir)
        images = [str(batch_dir / p.name) for p in sorted(batch_dir.iterdir())
                  if p.suffix.lower() == '.png']
        payload = {'ok': n_ok > 0, 'fake': True, 'images': images, 'batch_dir': str(batch_dir),
                   'pos_len': len(pos), 'washed': n_ok, 'washed_fail': [f[0] for f in fails]}
        _ledger({'tool': 'gen_image', 'ok': payload['ok'], 'fake': True,
                 'batch_dir': str(batch_dir), 'washed': n_ok})
        return payload

    # 显存驱动生命周期(用户 1110 指令):
    #   ComfyUI 在线 = 直接投(插队 —— 队列 FIFO, 有没有别的生图任务都照投);
    #   离线 = 由 ensure_comfy 把双闸(直播中/显存不足=在跑大游戏)全过一遍才拉起;
    #   本单出完且队列排空 = 自动卸载模型把显存还给游戏(队列还有别人就不卸)。
    was_online = comfy_probe()
    if not was_online:
        ok_comfy, why = ensure_comfy()
        if not ok_comfy:
            _ledger({'tool': 'gen_image', 'ok': False, 'reject': why})
            return {'ok': False, 'reason': why}
    seeds, batch_dir = _alloc()
    try:
        payload = render_batch(pos, neg, batch_dir, seeds, preset=_preset)
        q = comfy_queue_count()
        if q == 0:
            comfy_free()
        elif q is not None:
            payload['note'] = '队列仍有 %d 个任务, 本次不卸载模型' % q
    except NotImplementedError:
        return {'ok': False, 'reason': '真渲染通道 M4 接入; 当前联调请让 os 侧用 --fake 模式'}
    _ledger({'tool': 'gen_image', 'ok': payload.get('ok', False),
             'batch_dir': str(batch_dir)})
    return payload


def tool_gen_image_status(_args):
    today = datetime.now().strftime('%m%d')
    batches = []
    if OUT_BASE.exists():
        for d in sorted(OUT_BASE.iterdir()):
            if d.is_dir() and d.name.startswith(today):
                washed = len([p for p in d.iterdir() if p.suffix.lower() == '.png'])
                raw = len(list((d / 'raw').glob('*.png'))) if (d / 'raw').exists() else 0
                batches.append({'dir': d.name, 'washed': washed, 'raw': raw})
    free = vram_free_gb()
    return {'ok': True, 'fake_mode': FAKE, 'comfy_online': comfy_probe(),
            'comfy_queue': comfy_queue_count(), 'live_now': is_live_now(),
            'vram_free_gb': round(free, 1) if free is not None else None,
            'vram_min_gb': VRAM_MIN_GB,
            'style': STYLE_ENUM[0], 'today_batches': batches,
            'out_base': str(OUT_BASE),
            'note': '图已全部过洗图硬闸(无内嵌提示词), 可直接外传'}


TOOLS = [
    {'name': 'gen_image',
     'description': '画一张(非 R18)动漫插画。固定使用团队已验证的画风基线(不可换风格)。'
                    '参数全部用英文 danbooru tag(由你翻译, 不收中文); subject 必填'
                    '(角色+动作/构图), outfit/scene 可选, count 1~4 默认 2。'
                    '画大肥鱼(DS娘)时: 要 Q 版二头身就在 subject 里写 "chibi", '
                    '要女仆原版就写 "maid version", 什么都不写默认 Q 版。'
                    '红线: R18 词与真人声优/偶像名字一律拒单。'
                    '返回 JSON: ok/reason/images(洗净版绝对路径, 可直接发图)/batch_dir。',
     'inputSchema': {'type': 'object', 'properties': {
         'subject': {'type': 'string', 'description': '画什么(英文 danbooru tag: 角色+动作/构图)'},
         'outfit': {'type': 'string', 'description': '服装描述(英文 tag, 可省略)'},
         'scene': {'type': 'string', 'description': '场景/光线(英文 tag, 可省略)'},
         'count': {'type': 'integer', 'description': '出几张 1~4, 默认 2'},
         'seed': {'type': 'integer', 'description': '种子(复现用, 可省略)'},
     }, 'required': ['subject']}},
    {'name': 'gen_image_status',
     'description': '查询生图模块状态: ComfyUI 在线与否/是否直播互锁/今日批次与张数/当前风格档。'
                    '出图前想确认"画到哪了"或"能不能画"时调用。',
     'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
]


def dispatch(name, args):
    if name == 'gen_image':
        payload = tool_gen_image(args or {})
        if payload.get('ok'):
            _LAST_GEN_TS[0] = time.time()
        return payload
    if name == 'gen_image_status':
        return tool_gen_image_status(args or {})
    return {'ok': False, 'reason': '未知工具 %s' % name}


# ══ MCP streamable-HTTP 骨架(照 BWF 18081 协议) ═══════════════
SESSIONS = {}


class McpHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):                # 静默, 日志走 ledger
        pass

    def do_GET(self):
        self.send_error(405, 'use POST /mcp')

    def do_POST(self):
        if self.path.rstrip('/') != '/mcp':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            self.send_error(400)
            return
        method = req.get('method')
        rid = req.get('id')
        sid = self.headers.get('mcp-session-id')
        if method == 'initialize':
            sid = uuid.uuid4().hex
            SESSIONS[sid] = True
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'feiyu-image-mcp', 'version': '0.2.0'}}
        elif method == 'notifications/initialized':
            self._send(202, b'')                   # 无响应体
            return
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            params = req.get('params') or {}
            name = params.get('name')
            try:
                payload = dispatch(name, params.get('arguments') or {})
                text = json.dumps(payload, ensure_ascii=False)
                result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
            except Exception as e:                 # noqa
                result = {'content': [{'type': 'text', 'text': '内部错误: %s' % e}],
                          'isError': True}
        elif rid is not None:
            result = None
            self._json({'jsonrpc': '2.0', 'id': rid,
                        'error': {'code': -32601, 'message': 'method not found: %s' % method}},
                       sid)
            return
        else:
            return
        self._json({'jsonrpc': '2.0', 'id': rid, 'result': result}, sid)

    def _json(self, obj, sid):
        acc = self.headers.get('Accept') or ''
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        if 'text/event-stream' in acc:
            payload = (b'event: message\ndata: ' + body + b'\n\n')
            ctype = 'text/event-stream'
        else:
            payload = body
            ctype = 'application/json'
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(payload)))
            if sid:
                self.send_header('mcp-session-id', sid)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, code, payload):
        try:
            self.send_response(code)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


_LOCK = HERE / 'feiyu_image_mcp.lock'


def main(argv=None):
    """单实例判重 = 端口绑定本身(WinError 10048 即已有实例)。

    ⚠ 不要用 os.kill(pid, 0) 做 Windows 探活 —— 它在 Windows 语义是
    TerminateProcess(撞 PID 会误杀真进程), 1110 夜实测踩坑。
    锁文件只写当前 PID 供看门狗读, 不参与判重。
    """
    global FAKE
    argv = sys.argv[1:] if argv is None else argv
    FAKE = '--fake' in argv
    LOG.mkdir(exist_ok=True)
    OUT_BASE.mkdir(parents=True, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', PORT), McpHandler)
    except OSError as e:
        print('18082 绑定失败(已有实例在跑?): %s' % e, flush=True)
        return 1
    _LOCK.write_text(str(os.getpid()), encoding='utf-8')
    print('feiyu-image-mcp %s 监听 127.0.0.1:%d (pid=%d)'
          % ('联调' if FAKE else '正式', PORT, os.getpid()), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LOCK.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
