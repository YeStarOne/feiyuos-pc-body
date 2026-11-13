#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feiyu-image-mcp —— 肥鱼生图模块 (PC 侧, 2026-11-10 M2/M3)

她在 QQ 说一句话 → 云上大脑翻译成 danbooru tag → 调本模块的 MCP 工具 →
PC 用生图工作盘已验证的画风基线(coco 12 artist tag, 1108 定案)出**非 R18** 图 →
过洗图硬闸(剥元数据) → 返回洗净图路径 → 桥 (qq-say.mjs --image) 发图。

协议: 照抄 BWF MCP(18081) 的 streamable-HTTP JSON-RPC, 见 docs/肥鱼生图模块_设计稿_1110.md 第五节。
监听: 127.0.0.1:18082 (只本机; 云上经 os 侧 -R 47853→127.0.0.1:18082 隧道)。

双引擎(1113): gen_image 加 engine 参数 ——
  "sdxl"(默认) = coco 画风基线, 走 8188, 角色预设/LoRA 全在这条线;
  "moss"       = MOSS-Image 文字/版面引擎, 走 8190 (实例归属生图工作盘主线,
                 G:\ComfyUI_moss, 1110 部署; 逐字文字/海报/多语种/漫画分格)。
                 moss 无 LoRA 生态: 预设不生效, 提示词剥 SDXL 加权语法, 出图前与
                 8188 显存互锁(两线满载挤爆 16G), 8190 空载 0 显存可随时拉起。

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
import shlex
import shutil
import socket
import struct
import subprocess
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

# ── MOSS 引擎(1113 接入; 实例是生图工作盘主线的, 只调 HTTP API + 只读, 不改他家文件) ──
MOSS_URL = 'http://127.0.0.1:8190'
MOSS_DIR = Path(r'G:\ComfyUI_moss\ComfyUI')          # v0.44.0 源码实例, venv 在上一级
MOSS_OUT = MOSS_DIR / 'output'
MOSS_PREFIX_ROOT = 'feiyuOS_moss'                    # SaveImage 子目录前缀(在他家 output 里隔离)
MOSS_NEG = ('nsfw, worst quality, bad anatomy, watermark, jpeg artifact, '
            'multiple girls, two girls, crowd, extra characters, duplicate, '
            'logo, username, artist name')   # 官方四件套+业务负向, 全平词(契约 §四.5)
MOSS_W, MOSS_H = 1024, 1024                          # v1 固定 1M px(官方推荐档, ~60s/Mpx)
MOSS_LIMIT = 1500                                    # MOSS 吃长词(官方 ~4K token), 只设滥用上限
MOSS_RENDER_TIMEOUT = 600                            # 扩写 85s + 采样 ~120s + 首张加载
MOSS_START_TIMEOUT = 180                             # owner 定案: 起服务等待 ≤180s(他家启动器自检同口径)
MOSS_VRAM_MIN_GB = 8.5                               # MOSS 出图峰值 ~7.7G(1110 部署实测) + 余量
QWEN_URL = 'http://127.0.0.1:8189'                   # 千问线; 哪天常驻与 MOSS 同档大显存, 进互锁(owner 1113 提醒)
# 扩写指令 = _moss_boundary3.gen_expansion 的实证版(平铺键 + 指令写进 prompt;
# 官方模板的 system_prompt 接线在本实例没验证过, 不冒险), 逐字文字保护加进去。
MOSS_ENH_INSTR = (
    'Rewrite the following text as one detailed English text-to-image prompt for MOSS-Image. '
    'Describe subject, appearance, clothing, pose, setting, materials, colors, lighting, '
    'framing and camera. Any wording the user wants displayed in the image must be kept '
    'verbatim, in its original language, inside double quotes. Output only the prompt '
    'itself, no explanation, no options, no markdown, no quotes.\n\nText: ')

# ── 图回传(1110 夜; 她那边只读得到服务器本地文件, PC 的 G:\ 路径她读不到) ──
#   出图后: PNG → JPEG(q95, 4:4:4, 与 G:\生图工作盘\洗图工具 同档) → scp 到服务器媒体目录。
#   目标目录必须落在 dsh-onebot-qq 的 mediaDir(~/.dsh/media/onebot) 之内, 否则发送栅栏拒收。
#   置空 FEIYU_IMAGE_PUBLISH='' 可关掉回传(退回只给 PC 路径的旧行为)。
PUBLISH = os.environ.get('FEIYU_IMAGE_PUBLISH',
                         'agent@60.204.236.128:/home/agent/.dsh/media/onebot/gen_image')
SSH_BIN = os.environ.get('FEIYU_IMAGE_SSH', r'D:\Program Files\Git\usr\bin\ssh.exe')
SCP_BIN = os.environ.get('FEIYU_IMAGE_SCP', r'D:\Program Files\Git\usr\bin\scp.exe')

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
# 1111 第二轮: 模板尾位(193 之后)那条细节 LoRA(`realistic_cum_v1`)是 R18 配饰/体液的
# 强先验。非 R18 线本来就不该挂它 —— 强度归零(等于摘掉, 但保留节点不动接线),
# 需要恢复旧观感时把 DETAIL_LORA_STRENGTH 调回 0.8。
DETAIL_LORA_NODE = '192'
DETAIL_LORA_STRENGTH = 0.0
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
                    '(her whale tail attached to her lower back:1.3), '
                    'maid dress, (whale print apron:1.15), blue bow, '
                    'blue bows in hair, big blue eyes, white frilled ankle socks, '
                    'mary janes, '),
        # 1111: 数据集自带"伴生Q版小人"构图 → 单人物声明 + 压第二个人
        'single': ('(exactly one girl in the frame:1.35), (solo:1.2), '),
        'neg_extra': ('(two girls:1.45), (second girl:1.45), (another girl:1.4), '
                      '(small chibi girl in the background:1.5), (tiny girl:1.45), '
                      '(miniature figure:1.45), (plushie:1.4), (stuffed toy:1.35), '
                      '(mascot character:1.4), (multiple heads:1.45), (split face:1.4), '
                      '(floating whale:1.35), (whale in background:1.35), '
                      # 1111: 109032 把鲸画成"头顶顶着一只大鲸" —— 数据集伴生鲸的另一种形态
                      '(whale above her head:1.5), (whale hat:1.45), (whale on her head:1.45), '
                      '(giant whale:1.35), (whale beside her:1.4), '
                      # 1111: 109037 左下角还蹲着个极小的米色小人 —— 伴生人的最小形态
                      '(tiny figure in the corner:1.5), (small figure on the floor:1.45), '
                      '(doll on the floor:1.45)'),
        'pos_suffix': ('(facing the viewer:1.3), (looking at the viewer:1.25), '
                       '(front view:1.2)'),
    },
    'fish_maid': {
        'match': ('女仆', 'maid version', 'maid ver'),
        'lora': '女仆大肥鱼DeepseekChan_illust.safetensors',
        # 1110 探针7 定档(g0/g1 同 seed 对照): sm1.0 + 176 脸重画压 0.3 —— 基线的
        # 12 artist tag 画风层 + 176dn0.5 会把 LoRA 本脸洗成"风格脸"(用户实锤"脸不像");
        # 克隆是 seed 彩票(109030 全克隆, 109031/109032 干净), 换 seed 修, 不靠负向。
        'strength': 1.0,
        'face_dn': 0.3,
        'trigger': 'deepseekchan',
        # 锚对齐标准像: 少女体型(用户 1110 实锤"体型不像, 应该是少女体型") + 黑白女仆
        # +白色过膝袜+星月纹大鲸尾。该 LoRA 数据集自带"伴鲸"构图, 尾巴必须写成
        # "从她腰后长出来"并配专属负向压背景鲸(见 neg_extra)。
               'anchors': ('(petite:1.3), (young girl:1.2), (small breasts:1.15), '
                           '(round face:1.3), (cute:1.15), '
                           'whale fin ears, ahoge, (long wavy hair:1.15), dark blue hair, '
                           'blue gradient hair tips, '
                           '(her whale tail attached to her lower back:1.3), '
                           '(starry navy whale tail:1.15), '
                           'maid dress with white apron, white thighhighs, black bow, '
                           'big blue eyes, '),
        'neg_extra': ('(floating whale:1.3), (whale in background:1.3), '
                      '(giant whale:1.2), (whale beside her:1.25), '
                      '(two girls:1.45), (second girl:1.45), (another girl:1.4), '
                      '(small chibi girl in the background:1.45), (tiny girl:1.4), '
                      '(multiple heads:1.45), (two heads:1.4)'),
        'single': ('(exactly one girl in the frame:1.3), '),
        # 1111 修正: 原来全批硬压 from behind/back view/rear view, 但对"回望"这种体裁
        # 是过度抑制 —— 女仆 LoRA 自带转身先验, 压死背面后模型只能画成"侧后回望半转身"
        # (109028/109029 两张全中)。背面机位交给 subject 明确写, 这里只留"别背着观众"。
        'pos_suffix': ('(facing the viewer:1.3), (looking at the viewer:1.25), '
                       '(front view:1.2)'),
    },
}
DEFAULT_PRESET = 'fish_q'                        # 光说"肥鱼/ds娘"= 她本体(Q版)
DEFAULT_PRESET_KEYWORDS = ('肥鱼', '鱼娘', 'ds娘', 'whalemaid', 'whale maid',
                           'whale girl', 'fish girl', 'deepseek whale', 'ds-chan')


def match_preset(text):
    t = (text or '').lower()
    has_fish = any(m.lower() in t for m in DEFAULT_PRESET_KEYWORDS)
    for key, p in CHARACTER_PRESETS.items():
        # 版本词必须与鱼娘身份词同时在场: 防「女仆装的铃兰」「chibi 可莉」
        # 这类别人的请求被误路由到大肥鱼 LoRA(身份词只认 DEFAULT_PRESET_KEYWORDS)
        if has_fish and any(m.lower() in t for m in p['match']):
            return key, p
    if has_fish:
        return DEFAULT_PRESET, CHARACTER_PRESETS[DEFAULT_PRESET]
    return None, None

# ── 画风基线(钉死) ────────────────────────────────────────
HEAD = '(solo:1.25), 1girl, '
ART = ('(artist:tianliang duohe fangdongye:0.9), artist:ciloranko, artist:noyu, '
       'noyu23386566,ningen mame, (artist:sho:0.8264462809917354), '
       '(sho lwlw:0.9090909090909091),(artist:kedama_milk:0.5),(bee_\\(deadflow\\):0.7),'
       'artist:dino,(artist:fkey:0.8),(artist:wlop:0.7),year 2023,')
# 1111 鱼鱼报障(顶部 "DI N WLOP" 签名 / 手上铁环): 这两样是 ART 里 wlop·dino 的
# 强相关先验(Danbooru 里 artist tag ≈ 图上有署名), 无权重负向打不过。wlop 1→0.7 是
# 收窄后的第一刀(仍压不住才考虑摘), 画风骨架其余 11 个 tag 一字未动。

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
# 1111 署名/配饰提权(与上面 wlop 0.7 配套): 名称类无权重时被 artist 串压住, 提权才见效;
# 铁环/手链单独点名 —— 只在手上出现的是配饰, 不是构图问题, 别去改构图词。
NEG_SIGN = ('(signature:1.5), (watermark:1.5), (artist name:1.4), (logo:1.4), (text:1.4), '
            '(english text:1.3), (signature on hand:1.35), (ring on hand:1.25), '
            '(metal bracelet:1.2), (ornate hand accessories:1.15), ')
# 1111 第二轮(签名已消, 手铐/脚镣反增): 提权把"金属配饰"这一类挤到别处了 —— 但只要画面里
# 出现了束缚具, 就说明这个方向还没封死。点名到具体物件, 不写泛词(写 collar 会打掉
# 正向的 pj shirt with a white lace collar)。
NEG_RESTRAIN = ('(shackles:1.35), (handcuffs:1.35), (leg irons:1.35), (ankle cuffs:1.35), '
                '(chains:1.3), (leash:1.3), (restraints:1.25), (bindings:1.25), '
                '(ball and chain:1.3), ')
NEG_FIXED = NEG_BASE + NEG_SIGN + NEG_RESTRAIN + NEG_TAIL + NEG_TIGHTS

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
    # 1111 bugfix: 该 LoRA 数据集全是"本体+伴生Q版小人"双人构图(数据源下载图里的
    # 画中画), 所以 LoRA 一直在召唤第二个女仆 —— 成年版出双头克隆, Q版出背景幽灵小人。
    # NEG_TAIL 已有 2girls/extras, 但打不过 LoRA 的构图先验(5/5 全中), 必须给足权重。
    char_block = ('' + preset['trigger'] + ', ' + preset['anchors']) if preset else ''
    if preset and preset.get('single'):
        char_block += preset['single']
    core = (HEAD + ART + char_block
            + (outfit + ' ' if outfit else '') + subject).strip()
    core = re.sub(r'\s*,\s*', ', ', core).rstrip(', ')
    if len(core) > LIMIT:
        # 1111: 预设自带的加成块(character anchors / single)不是用户输入, 顶破闸时先还原
        # 成"字符锚 + 用户 body"再判 —— 用户该精简的是他自己写的那部分。全身拍 M2 正式批
        # 是 758 字, 这一段就该让闸放行而不是拒单。
        body = (outfit + ' ' if outfit else '') + subject
        bare = re.sub(r'\s*,\s*', ', ', (HEAD + ART + body)).strip().rstrip(', ')
        if len(bare) > LIMIT:
            return None, None, ('描述过长(%d 字, 上限 %d): 请精简 subject'
                                % (len(bare), LIMIT)), None
    pos = (core + (', ' + scene if scene else '')).rstrip(', ')
    if len(pos) > LIMIT:
        pos = core                                   # 降级: 整段场景删掉
    bad = check_request(pos)
    if bad:
        return None, None, bad, None
    return pos, NEG_FIXED, None, (_key if preset else None)


# ══ MOSS 引擎(1113, engine='moss') ═══════════════════════════
def moss_compose(subject, outfit='', scene=''):
    """MOSS 正向: 自然语言/tag 都吃, 但 SDXL 加权语法无效(1110 边界实测) → 剥权重。

    不加 HEAD/ART(那是 SDXL 画风链, 与 MOSS 无关); 大肥鱼 LoRA 预设不适用
    (MOSS 无 LoRA 生态)。中文原样保留(MOSS 双语)。返回 (pos, note, err)。
    """
    parts = []
    for seg in (subject, outfit, scene):
        seg = ' '.join((seg or '').split())
        if seg:
            parts.append(seg)
    text = ', '.join(parts)
    text = re.sub(r'\\([()\[\]])', r'\1', text)          # danbooru 转义括号还原
    prev = None
    while prev != text:                                   # (x:1.3) / ((x:1.3)) → x
        prev = text
        text = re.sub(r'\(([^():]{1,160}):\s*\d+(?:\.\d+)?\s*\)', r'\1', text)
    text = re.sub(r'\s*,\s*', ', ', text).strip().rstrip(', ')
    if len(text) > MOSS_LIMIT:
        return None, None, ('描述过长(%d 字, MOSS 上限 %d): 请精简 subject'
                            % (len(text), MOSS_LIMIT))
    note = None
    low = text.lower()
    notes = []
    if (('hand' in low or 'finger' in low)
            and ('close-up' in low or 'closeup' in low or 'macro' in low)):
        notes.append('MOSS 手部特写是硬伤(1110 实测六指), 手部近景建议改用默认引擎 sdxl')
    # 契约 §四.4: 单页要渲染的正文 >~150 字 → 拒单(中段崩叠字), 建议拆块或转 sdxl。
    # 判据 = 引号里的"要逐字画进图"的文字总长。
    quoted = re.findall(r'"([^"]+)"', text) + re.findall(r'“([^”]+)”', text)
    qtotal = sum(len(q) for q in quoted)
    if qtotal > 150:
        return None, None, ('要渲染进图里的文字共 %d 字, 超 MOSS 单页可靠上限(~150 字, '
                            '中段会崩成叠字); 请拆块或改用 sdxl 引擎' % qtotal)
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    if not quoted and cjk > 200:
        notes.append('提示词含 %d 个中文且没有引号: 未加引号的文字 MOSS 会自己编假字, '
                     '要显示的文字请放进引号' % cjk)
    note = '; '.join(notes) or None
    return text, note, None


def build_moss_api(pos, neg, seed, prefix, enhance=False):
    """MOSS t2i 扁平 9 节点图(照 G:\\ComfyUI_moss\\_test_moss.py, 本实例已验证)。

    enhance=True 追加 TextGenerate 扩写(opt-in; 契约 §五.3 本契约默认不用) ——
    必须**平铺** sampling_mode.* 键(照 _moss_boundary3.gen_expansion; 嵌套对象能过
    /prompt 校验但执行报 missing 1 required positional argument, 1110 实锤)。
    """
    g = {
        '1': {'class_type': 'UNETLoader',
              'inputs': {'unet_name': 'moss_image_sft_int8_convrot.safetensors',
                         'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader',
              'inputs': {'clip_name': 'qwen3.5_9b_w4a8_gptq.safetensors',
                         'type': 'moss_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader',
              'inputs': {'vae_name': 'flux2-vae_small.safetensors'}},
        '5': {'class_type': 'CLIPTextEncode', 'inputs': {'text': neg, 'clip': ['2', 0]}},
        '6': {'class_type': 'EmptyFlux2LatentImage',
              'inputs': {'width': MOSS_W, 'height': MOSS_H, 'batch_size': 1}},
        '7': {'class_type': 'KSampler',
              'inputs': {'model': ['1', 0], 'seed': seed, 'steps': 25, 'cfg': 2.5,
                         'sampler_name': 'er_sde', 'scheduler': 'simple',
                         'positive': ['4', 0], 'negative': ['5', 0],
                         'denoise': 1.0, 'latent_image': ['6', 0]}},
        '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['3', 0]}},
        '9': {'class_type': 'SaveImage',
              'inputs': {'images': ['8', 0], 'filename_prefix': prefix}},
    }
    if enhance:
        g['10'] = {'class_type': 'TextGenerate', 'inputs': {
            'clip': ['2', 0], 'prompt': MOSS_ENH_INSTR + pos, 'max_length': 1024,
            'use_default_template': True, 'thinking': False,
            'sampling_mode': 'on',
            'sampling_mode.temperature': 0.7, 'sampling_mode.top_k': 64,
            'sampling_mode.top_p': 0.95, 'sampling_mode.min_p': 0.05,
            'sampling_mode.repetition_penalty': 1.05, 'sampling_mode.seed': seed}}
        g['4'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': ['10', 0], 'clip': ['2', 0]}}
    else:
        g['4'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': pos, 'clip': ['2', 0]}}
    return g


def moss_probe(timeout=3):
    try:
        import urllib.request
        with urllib.request.urlopen(MOSS_URL + '/system_stats', timeout=timeout):
            return True
    except Exception:                              # noqa
        return False


def moss_queue_count():
    """8190 在跑+排队任务数; 服务器不在返回 None。"""
    return _queue_count(MOSS_URL)


def moss_free():
    """卸 8190 模型归还显存。**别信部署文档的"空载 0 显存"** —— 1113 实测: 没跑过图的实例
    0 显存, 但**跑过图的实例模型常驻 ~12.3G 不会自卸**(owner 验收: /free 后 vram_free 回落
    14.53G)。所以每批跑完 /queue 空必须调这个, 否则下一单会被自己的驻留显存挡在闸外。
    """
    try:
        _post('/free', {'unload_models': True, 'free_memory': True}, base=MOSS_URL)
        _ledger({'tool': 'moss_free', 'ok': True})
        return True
    except Exception as e:                         # noqa
        _ledger({'tool': 'moss_free', 'ok': False, 'err': str(e)[:120]})
        return False


_MOSS_PROC = None


def ensure_moss():
    """探活 → 按需拉起 8190(照他家 start_8190.bat 正文, DETACHED 脱离本进程树)。

    与 8188 的差别: 8190 **空载 0 显存**(模型动态 staged), 起服务不抢卡,
    所以不做直播闸/显存闸 —— 出图时的显存互锁单独走 moss_vram_gate()。
    """
    global _MOSS_PROC
    if moss_probe():
        return True, 'ok'
    # 1113 owner 定案: 启动器归他家 —— start_8190_headless.cmd(幂等+自检)优先;
    # 他落盘前回退本模块直启(命令与他家 bat 逐字一致, 双方等价, 他 ACK 过)。
    headless = MOSS_DIR.parent / 'start_8190_headless.cmd'
    log = (LOG / ('comfy8190_%s.log' % time.strftime('%m%d_%H%M%S'))).open('ab')
    try:
        if headless.exists():
            _MOSS_PROC = subprocess.Popen(
                ['cmd', '/c', str(headless)], stdout=log, stderr=subprocess.STDOUT,
                creationflags=0x00000008 | 0x00000200)
        else:
            py = MOSS_DIR.parent / 'venv' / 'Scripts' / 'python.exe'
            if not py.exists():
                log.close()
                return False, 'MOSS venv 不存在(%s): 实例未部署或被移动' % py
            _MOSS_PROC = subprocess.Popen(
                [str(py), '-B', '-s', 'main.py',
                 '--listen', '127.0.0.1', '--port', '8190',
                 '--disable-comfy-compiler', '--disable-api-nodes',
                 '--preview-method', 'none'],
                cwd=str(MOSS_DIR), stdout=log, stderr=subprocess.STDOUT,
                creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
    except Exception as e:                         # noqa
        log.close()
        return False, 'MOSS 8190 拉起失败: %s' % e
    deadline = time.time() + MOSS_START_TIMEOUT
    while time.time() < deadline:
        if moss_probe():
            return True, '拉起成功(%.0fs)' % (MOSS_START_TIMEOUT - (deadline - time.time()))
        time.sleep(5)
    return False, 'MOSS 8190 启动超时(%ds); 看 server/logs/comfy8190_*.log' % MOSS_START_TIMEOUT


def moss_vram_gate():
    """显存互锁: MOSS(~7.7G) 与 8188 SDXL(常驻 ~8.1G) 不能同时满载(1110 部署 §六)。

    8188/8189 队列忙 → 拒单; 空闲但模型驻着(8188 SDXL **或 8190 自己**) → 先 /free 卸掉再测;
    卸完仍不够 = 宿主在跑大型游戏 → 拒单。都没起 = 无冲突, 放行。
    """
    q = comfy_queue_count()
    if q:
        return ('8188 SDXL 队列还有 %d 个任务, 两线满载会挤爆 16G 显存; '
                '等队列排空再试 MOSS' % q)
    q8189 = _queue_count(QWEN_URL, timeout=3)
    if q8189:
        return ('8189 千问线队列还有 %d 个任务, 与 MOSS 同档大显存互锁; '
                '等队列排空再试' % q8189)
    free = vram_free_gb()
    if free is not None and free < MOSS_VRAM_MIN_GB:
        if comfy_probe():
            comfy_free()                           # 8188 空闲但 SDXL 还驻着 → 先卸
        # 1113 owner 反馈 B: 8190 自己跑完图模型常驻 ~12.3G(**queue=0 也占着**),
        # 不先卸就会让下面那句判据误报"宿主在跑大型游戏" ——
        # 同一实例的第二单必然被自己挡住。空载时先把它自己的残留卸掉再测。
        if moss_probe() and not moss_queue_count():
            moss_free()
        time.sleep(3)
        free = vram_free_gb()
    if free is not None and free < MOSS_VRAM_MIN_GB:
        return ('显存剩余 %.1f GiB(<%.1f, MOSS 出图峰值约 7.7G), 判定宿主在跑大型游戏; '
                '请关游戏或稍后再试' % (free, MOSS_VRAM_MIN_GB))
    return None


def _wait_moss_file(prompt_id, seed, batch_name, timeout=MOSS_RENDER_TIMEOUT):
    """轮询 8190 /history, 等 feiyuOS_moss/<批次>/fym_<seed>_*.png 落盘; 出错/超时 None。"""
    import urllib.request
    root = MOSS_OUT / MOSS_PREFIX_ROOT / batch_name
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            with urllib.request.urlopen(MOSS_URL + '/history/' + prompt_id,
                                        timeout=10) as r:
                hist = json.loads(r.read().decode('utf-8'))
        except Exception:                          # noqa
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        if (entry.get('status') or {}).get('status_str') == 'error':
            return None
        if root.exists():
            hits = sorted(root.glob('fym_%d_*.png' % seed))
            if hits:
                return hits[-1]
    return None


def render_moss_batch(pos, neg, batch_dir, seeds, enhance=True):
    """MOSS 渲染: 逐 seed 提交+轮询 → **拷贝**到本模块 raw/ → 洗图硬闸 → 洗净路径。

    拷贝不挪: 他家 output 目录只增不改(1113 约定不动 G:\\ComfyUI_moss 内容)。
    """
    batch_dir = Path(batch_dir)
    raw_dir = batch_dir / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    for s in seeds:
        api = build_moss_api(pos, neg, s,
                             '%s/%s/fym_%d' % (MOSS_PREFIX_ROOT, batch_dir.name, s),
                             enhance=enhance)
        try:
            r = _post('/prompt', {'prompt': api, 'client_id': CLIENT_ID}, base=MOSS_URL)
        except Exception as e:                     # noqa
            return {'ok': False,
                    'reason': 'MOSS 提交失败(seed %d): %s; 看 server/logs/comfy8190_*.log'
                              % (s, str(e)[:160])}
        pid = r.get('prompt_id')
        if not pid:
            return {'ok': False,
                    'reason': 'MOSS 提交无 prompt_id: %s'
                              % json.dumps(r, ensure_ascii=False)[:200]}
        f = _wait_moss_file(pid, s, batch_dir.name)
        if f is None:
            return {'ok': False,
                    'reason': 'seed %d MOSS 渲染超时/出错(%ds); 8190 /history 自查'
                              % (s, MOSS_RENDER_TIMEOUT)}
        shutil.copyfile(str(f), str(raw_dir / f.name))
    n_ok, fails = strip_gate(raw_dir, batch_dir)
    images = []
    for s in seeds:
        images.extend(str(p) for p in sorted(batch_dir.glob('fym_%d_*.png' % s)))
    return {'ok': n_ok > 0, 'engine': 'moss', 'images': images,
            'batch_dir': str(batch_dir), 'seeds': seeds, 'washed': n_ok,
            'washed_fail': [x[0] for x in fails]}


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
        # 瞬时锁重试一次(1110 实锤: 杀毒扫新落盘文件短暂持锁 → 首轮 PermissionError,
        # 同文件复检即过; 不重试会静默少一张且 ledger 无从追查)
        reason = None
        for attempt in (0, 1):
            reason = None
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
                    reason = '像素校验失败 maxdiff=%s' % maxdiff
                elif has_text_meta(tmp):
                    reason = '洗后仍有内嵌文本'
            except Exception as e:                     # noqa
                reason = str(e)
            if reason is None:
                tmp.replace(out_dir / p.name)
                ok += 1
                break
            tmp.unlink(missing_ok=True)
            if attempt == 0:
                time.sleep(0.5)
        if reason is not None:
            fails.append((p.name, reason))
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


def _queue_count(url, timeout=5):
    """/queue 的 running+pending 总数; 服务器不在返回 None(8188/8189/8190 共用)。"""
    try:
        import urllib.request
        with urllib.request.urlopen(url + '/queue', timeout=timeout) as r:
            q = json.loads(r.read().decode('utf-8'))
        return len(q.get('queue_running') or []) + len(q.get('queue_pending') or [])
    except Exception:                              # noqa
        return None


def comfy_queue_count():
    """8188 当前在跑+排队任务数; 服务器不在返回 None。"""
    return _queue_count(COMFY_URL)


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
    if free < VRAM_MIN_GB and moss_probe() and _queue_count(MOSS_URL) == 0:
        moss_free()          # MOSS 跑完残留 12.3G 常驻(契约 §二), 还给显存再判一次
        time.sleep(3)
        free = vram_free_gb()
    if free < VRAM_MIN_GB:
        return False, ('显存剩余 %.1f GiB(<%.1f), 判定宿主在跑大型游戏, '
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


def _post(path, payload, timeout=30, base=None):
    import urllib.request
    req = urllib.request.Request(
        (base or COMFY_URL) + path,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'ignore').strip()
    # 1113 owner 反馈 A: ComfyUI /free 回 **200 + 空 body**, json.loads('') 抛
    # JSONDecodeError → 动作其实生效了却记 ok:false(ledger 里那条 moss_free 假失败)。
    # 空体一律按 {} 处理。
    return json.loads(body) if body else {}


def build_api(pos, neg, seed, batch_name, preset=None):
    """从模板 PNG 载入节点图并按美图档接线。命中角色预设时尾位 LoRA(node 900)原位换名。"""
    from PIL import Image
    with Image.open(TEMPLATE_PNG) as im:
        api = json.loads(im.text['prompt'])
    # 1111 bugfix: 旧写法 `pos + pos_suffix` 没有分隔符 → 拼出
    # `white background(facing the viewer:1.3)`, 整串构图词被 CLIP 当成乱码, 机位全部失效。
    _suf = preset.get('pos_suffix') if preset else None
    api['165']['inputs']['text'] = (pos + ', ' + _suf) if _suf else pos
    # 1111 bugfix: neg_extra 是 tuple, 旧写法 neg + tuple 直接 TypeError(只在 preset 带
    # neg_extra 时才炸 —— fish_maid 全批 500 就是死在这行, 5 张一张没出)。
    _nx = preset.get('neg_extra') if preset else None
    neg_use = (neg + ', ' + ', '.join(_nx)) if _nx else neg
    api['153']['inputs']['text'] = neg_use
    api['17']['inputs']['denoise'] = (preset.get('dn17') or 0.55) if preset else 0.55
    if preset:
        api[TAIL_LORA_NODE]['inputs']['lora_name'] = preset['lora']
        api[TAIL_LORA_NODE]['inputs']['strength_model'] = preset['strength']
        api[TAIL_LORA_NODE]['inputs']['strength_clip'] = preset['strength']
        if preset.get('face_dn'):                  # LoRA 角色: 压脸重画保脸型(探针定档后写死)
            api['176']['inputs']['denoise'] = preset['face_dn']
    api['301'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': api['165']['inputs']['text'] + HAND_POS,
                             'clip': list(api['165']['inputs']['clip'])}}
    api['302'] = {'class_type': 'CLIPTextEncode',
                  'inputs': {'text': neg + HAND_NEG,
                             'clip': list(api['153']['inputs']['clip'])}}
    api['142']['inputs']['positive'] = ['301', 0]
    api['142']['inputs']['negative'] = ['302', 0]
    api['142']['inputs']['denoise'] = 0.55
    api['142']['inputs']['cycle'] = 1
    # 1111 修接线: 生产模板把两个细节器的检测器接反了 —— 176(先跑, dn0.5) 接的是
    # 手部检测器、142(拿手部专用提词 301/302) 接的是人脸检测器。结果手部专用负向
    # (fused fingers / toes / feet instead of hands)全落在脸的裁片上, 手一次也没拿到。
    # 176=脸(face_yolov8m/143), 142=手(hand_yolov8s/177)。
    api['176']['inputs']['bbox_detector'] = ['143', 0]
    api['142']['inputs']['bbox_detector'] = ['177', 0]
    # 1111 第二轮: 尾位 R18 细节 LoRA 归零(非 R18 线不挂它; 恢复旧观感 = 改回 0.8)
    api[DETAIL_LORA_NODE]['inputs']['strength_model'] = DETAIL_LORA_STRENGTH
    api[DETAIL_LORA_NODE]['inputs']['strength_clip'] = DETAIL_LORA_STRENGTH
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
    # 只返回**本单 seed**的洗净图(批次目录会累积多次调用, 全量排序会发错图, 1110 实锤)
    images = []
    for s in seeds:
        images.extend(str(p) for p in sorted(batch_dir.glob('fyi_%d_*.png' % s)))
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


def _to_jpg(png, quality=95):
    """PNG → JPEG(q95 / 4:4:4 / optimize), 与 G:\\生图工作盘\\洗图工具 同档, 体积约 1/3~1/5。"""
    from PIL import Image
    dst = Path(png).with_suffix('.jpg')
    with Image.open(png) as im:
        im.convert('RGB').save(dst, 'JPEG', quality=quality, subsampling=0, optimize=True)
    return dst


def _publish_payload(payload):
    """把洗净图转 JPEG 并推到服务器媒体目录, 让**她那边**读得到(qq_send_image 只认服务器本地路径)。

    成功: payload['images'] = 服务器侧路径(可直接发给 QQ), 原 PC 路径留 images_pc。
    失败: 保留原路径 + publish.ok=False + 错误原因 —— 绝不静默假装成功。
    """
    images = payload.get('images') or []
    if not images:
        payload['publish'] = {'ok': False, 'err': 'no images'}
        return payload
    target, _, remote_root = (PUBLISH or '').partition(':')
    if not (target and remote_root.startswith('/')):
        payload['publish'] = {'ok': False, 'skipped': True,
                              'err': "FEIYU_IMAGE_PUBLISH 未配置(或置空), 只返回 PC 路径"}
        return payload
    batch = Path(images[0]).parent.name
    remote_dir = remote_root.rstrip('/') + '/' + batch
    try:
        up_dir = Path(images[0]).parent / 'upload'
        up_dir.mkdir(exist_ok=True)
        jpgs = []
        for p in images:
            jpg = _to_jpg(p)
            dst = up_dir / jpg.name
            shutil.move(str(jpg), str(dst))
            jpgs.append(dst)
        opt = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=8']
        r = subprocess.run([SSH_BIN, '-n'] + opt + [target, 'mkdir -p ' + shlex.quote(remote_dir)],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError('ssh mkdir rc=%d %s' % (r.returncode, r.stderr.decode('utf-8', 'ignore')[:150]))
        for f in jpgs:
            r = subprocess.run([SCP_BIN, '-q'] + opt + [str(f), '%s:%s/' % (target, remote_dir)],
                               capture_output=True, timeout=90)
            if r.returncode != 0:
                raise RuntimeError('scp %s rc=%d %s' % (f.name, r.returncode,
                                                        r.stderr.decode('utf-8', 'ignore')[:150]))
        payload['images_pc'] = list(images)
        payload['images'] = [remote_dir + '/' + f.name for f in jpgs]
        payload['publish'] = {'ok': True, 'dir': remote_dir, 'n': len(jpgs),
                              'bytes': int(sum(f.stat().st_size for f in jpgs)),
                              'note': 'jpg 上传版; 原 PNG 在 PC images_pc'}
    except Exception as e:                                  # noqa: BLE001
        payload['publish'] = {'ok': False, 'err': str(e)[:220]}
    return payload


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


def _tool_gen_image_moss(subject, outfit, scene, count, seed, args):
    """engine=moss 分支: 拒单闸已在 tool_gen_image 过过 → NL 正向 → 8190 生命周期。"""
    # 契约 §五.3: TextGenerate 本契约不用 → 默认关; 显式 enhance=true 才走扩写(+85s)。
    enhance = str(args.get('enhance', 'false')).lower() in ('1', 'true', 'yes', 'on')
    pos, note, err = moss_compose(subject, outfit, scene)
    if err:
        return {'ok': False, 'reason': err}

    def _alloc():
        s = int(seed) if seed is not None else _next_seed(count)
        bd = OUT_BASE / ('%s_%s' % (datetime.now().strftime('%m%d'), _slug(subject)))
        (bd / 'raw').mkdir(parents=True, exist_ok=True)
        return [s + i for i in range(count)], bd

    if FAKE:
        seeds, batch_dir = _alloc()
        raw_dir = batch_dir / 'raw'
        for s in seeds:
            _fake_png(raw_dir / ('fym_%d_%06d.png' % (s, s)), subject + str(s))
        n_ok, fails = strip_gate(raw_dir, batch_dir)
        payload = {'ok': n_ok > 0, 'fake': True, 'engine': 'moss',
                   'images': [str(p) for p in sorted(batch_dir.glob('fym_*.png'))],
                   'batch_dir': str(batch_dir), 'washed': n_ok,
                   'washed_fail': [f[0] for f in fails]}
        if note:
            payload['note'] = note
        _publish_payload(payload)                  # 联调也走一遍回传, 不烧显卡就能验链路
        _ledger({'tool': 'gen_image', 'engine': 'moss', 'fake': True,
                 'ok': payload['ok'], 'washed': n_ok})
        return payload

    ok_moss, why = ensure_moss()
    if not ok_moss:
        _ledger({'tool': 'gen_image', 'engine': 'moss', 'ok': False, 'reject': why})
        return {'ok': False, 'reason': why}
    gate = moss_vram_gate()
    if gate:
        _ledger({'tool': 'gen_image', 'engine': 'moss', 'ok': False, 'reject': gate})
        return {'ok': False, 'reason': gate}
    seeds, batch_dir = _alloc()
    payload = render_moss_batch(pos, MOSS_NEG, batch_dir, seeds, enhance=enhance)
    # 8190 跑完模型常驻 12.3G 不会自卸(契约 §二)。⚠ /queue 清空有滞后: 刚落盘就查
    # 常常还剩 running(E2E-2 实锤 12026MiB 残留) → 带重试收尾, 24s 内清空才 free。
    for _ in range(3):
        if moss_queue_count() == 0:
            moss_free()                            # 兜底卸模, 失败只记账不报错
            break
        time.sleep(8)
    if note:
        payload['note'] = note
    _publish_payload(payload)
    _ledger({'tool': 'gen_image', 'engine': 'moss', 'ok': payload.get('ok', False),
             'batch_dir': str(batch_dir), 'seeds': seeds, 'enhance': enhance,
             'washed': payload.get('washed'), 'washed_fail': payload.get('washed_fail'),
             'publish': payload.get('publish')})
    return payload


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

    engine = (args.get('engine') or 'sdxl').strip().lower()
    if engine not in ('sdxl', 'moss'):
        return {'ok': False, 'reason': "engine 只支持 'sdxl' / 'moss'"}
    if engine == 'moss':
        return _tool_gen_image_moss(subject, outfit, scene, count, seed, args)

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
        images = [str(batch_dir / p.name) for p in sorted(batch_dir.glob('fyi_*.png'))]
        payload = {'ok': n_ok > 0, 'fake': True, 'images': images, 'batch_dir': str(batch_dir),
                   'pos_len': len(pos), 'washed': n_ok, 'washed_fail': [f[0] for f in fails]}
        _publish_payload(payload)                  # 联调也走一遍回传, 不烧显卡就能验链路
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
    _publish_payload(payload)                      # 出完即回传(jpg)到服务器, 她那边才发得出去
    _ledger({'tool': 'gen_image', 'ok': payload.get('ok', False),
             'batch_dir': str(batch_dir), 'seeds': seeds,
             'washed': payload.get('washed'),
             'washed_fail': payload.get('washed_fail'),
             'publish': payload.get('publish')})
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
            'moss_online': moss_probe(), 'moss_queue': moss_queue_count(),
            'vram_free_gb': round(free, 1) if free is not None else None,
            'vram_min_gb': VRAM_MIN_GB,
            'style': STYLE_ENUM[0], 'today_batches': batches,
            'out_base': str(OUT_BASE),
            'note': '图已全部过洗图硬闸(无内嵌提示词), 可直接外传'}


TOOLS = [
    {'name': 'gen_image',
     'description': '画图(非 R18), 双引擎。固定画风基线(不可换风格)。'
                    '红线: R18 词与真人声优/偶像名字一律拒单。'
                    'engine 默认 "sdxl" = coco 画风基线: 画角色立绘/普通插画/大肥鱼(DS娘), '
                    '参数用英文 danbooru tag; subject 必填(角色+动作/构图), outfit/scene 可选; '
                    '画大肥鱼时 subject 必须带 "whale maid"(丢了路由不到她的 LoRA), '
                    '要 Q 版写 "chibi", 要女仆原版写 "maid version", 都不写默认 Q 版。'
                    'engine "moss" = MOSS-Image 文字引擎: 适合带文字的海报/菜单/招牌/贺卡'
                    '(图里的字会逐字画对)与精确排版; 不适合手部特写、超过约150字的整页文字。'
                    'moss 时 subject 直接写英文自然语言或 tag(服务端拼 NL, 不用学新语法), '
                    '要显示的文字放进引号逐字写(单页引号正文>150字拒单); '
                    '不带角色 LoRA(大肥鱼预设只对 sdxl 生效); '
                    'enhance=true(默认)先自动扩写提示词, 多花约 85 秒。'
                    '返回 JSON: ok/reason/images(洗净版绝对路径, 可直接发图)/batch_dir。',
     'inputSchema': {'type': 'object', 'properties': {
         'subject': {'type': 'string', 'description': '画什么(英文; sdxl 用 danbooru tag, moss 用自然语言/tag)'},
         'outfit': {'type': 'string', 'description': '服装描述(英文, 可省略)'},
         'scene': {'type': 'string', 'description': '场景/光线(英文, 可省略)'},
         'engine': {'type': 'string', 'enum': ['sdxl', 'moss'],
                    'description': '出图引擎, 默认 sdxl。带逐字文字的海报/菜单/招牌用 moss'},
         'enhance': {'type': 'boolean',
                     'description': '仅 engine=moss 有效: TE 扩写提示词(约 +85s); 默认关, '
                                    '短词/构图随机时建议显式开'},
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
            # 1111: os 侧每次 callTool 都重新 initialize(每单一个新 session), 这个 dict 不设上限
            # 就是无界增长。session 不参与鉴权(仅作握手凭据), 保留最近 4096 个足够。
            while len(SESSIONS) > 4096:
                SESSIONS.pop(next(iter(SESSIONS)), None)
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'feiyu-image-mcp', 'version': '0.3.1'}}
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
