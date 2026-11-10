# -*- coding: utf-8 -*-
"""pc_body.py —— 小肥鱼的 PC 身体总控（2026-11-04 夜）

两个线程：
  Bridge: HTTP 绑 Tailscale IP:17860，把服务器的指令（说话/动画）转发给
          本机 dsh-pet standalone(:7340)，绕过它的 127.0.0.1 + Host 校验。
  Eye:    每 36-68s 拍一轮（前台窗口非黑名单时），3 帧 @800w JPEG，
          推送到云 eye-server :8901 —— 公网直连优先（入云免费 ~1MB/s），
          Tailscale 兜底（12KB/s）。帧差 <2% 跳过；失败退避；日限额熔断；
          主人无输入 15min 放缓 / 45min 暂停（不看锁屏）。

闸（全部在本文件落地，闸4/闸5 在服务器 eye_server.py）：
  闸0 黑名单窗口（交易软件/QQ/密码管理器）——绝不采集
  闸1 单轮硬顶 1.5MB
  闸2 日限额 500MB（本地计数 + 服务器计数双份）
  闸3 推送失败退避：连败 3 次 → 冷却 60s→300s
  闸4 服务器 10 分钟滑窗异常自动暂停
  闸5 服务器每小时账本 + 每日熔断
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import random
import re as _re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import atexit

# 2026-11-08（tablet-pet-deploy 提议/主持实装）：退出标记——atexit 只在"自己走完"
# 时触发；被外部强杀（taskkill）不会留下标记。两者对照即能区分"自退"与"被杀"，
# 搭配看门狗侧 stderr/stdout 落文件（wd_pc_body.spawn）给 65 次不明真死补上证人。
_EXIT_MARKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pc_body_exits.jsonl')


_BOOT_TS = time.time()
_STAGE = {'name': 'import'}        # 死在哪一步：import / imports-done / main / threads / listening


def _stage(name):
    _STAGE['name'] = name


def _write_mark(why, **extra):
    # 2026-11-08 早间对账：只写 why 无法解释 03:15:03 那种 9ms 自退，补 stage/uptime。
    rec = {
        'ts': round(time.time(), 3), 'pid': os.getpid(), 'why': why,
        'stage': _STAGE['name'], 'uptime_s': round(time.time() - _BOOT_TS, 3),
    }
    rec.update(extra)
    try:
        with open(_EXIT_MARKER_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _die(why, **extra):
    """os._exit 不跑 atexit：凡直接退出的路径必须自己留一行目击记录。"""
    _write_mark(why, **extra)
    os._exit(3)


def _mark_exit():
    if __name__ != '_import_check':
        _write_mark('atexit')


atexit.register(_mark_exit)

_ORIG_EXCEPTHOOK = sys.excepthook


def _excepthook(t, v, tb):
    _write_mark('error', detail=('%s: %s' % (t.__name__, v))[:200])
    _ORIG_EXCEPTHOOK(t, v, tb)


sys.excepthook = _excepthook
if __name__ != '_import_check':      # 体检导入不往真台账写假 start
    _write_mark('start')   # 2026-11-08（tablet-pet-deploy 提议）：启动即写一行——证人写入链路事前可验；start 与 atexit 成对=干净重启，有 start 无 atexit=被杀/异常

import psutil
import pc_fins            # 鱼鳍：读写/执行（11-10 主人拍板）
_stage('imports-done')

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / 'pc_body.log'
TOKEN_FILE = BASE_DIR / '.eye-token'
PET_RUNTIME = Path.home() / '.dsh' / 'dsh-pet' / 'standalone-runtime.json'

PET_HOST = '127.0.0.1'
PET_PORT = 7340
BRIDGE_BIND = '100.98.120.55'   # Tailscale IP（服务器→PC 指令走这条）
BRIDGE_PORT = 17860

# ── 平板双击唤醒（让服务器上的鱼鱼本体看主人的屏幕并搭话）──
# 链路：平板双击 → /pet-wake（query token，走 hdc rport 隧道）
#   → 即时鱼眼一轮（不等 idle 节奏，拿当前屏幕的新鲜笔记）
#   → 本机 RN CLI 发信 → feiyu/qqbot 信箱（dsh-rn-mail 2s 轮询 → agent.followup）
#   → 她按屏幕内容自己决定怎么接（文本/表情包/聊视频），pet_say 双投递
#   → QQ + 本桥 /say → SSE → 平板气泡。回复链路零新增。
WAKE_TO = os.environ.get('FEIYU_WAKE_TO', 'feiyu/qqbot')
WAKE_MIN_INTERVAL = int(os.environ.get('FEIYU_WAKE_MIN_INTERVAL', '40'))

# ---- per-client 令牌桶（2026-11-08 主人拍板：只限鱼眼视频 /clip，其余不限）----
# 17860 曾出现 507 个 TIME_WAIT ≈ 2 次/秒短连接；真凶已在服务端修（HTTP/1.1 keep-alive）。
# 普通桶(6/s) 2026-11-07 上线当晚就把桌宠动画/表情包打残（429×15），2026-11-08 停用；
# RATE_PER_SEC/RATE_BURST 保留定义仅为回滚方便，_rate_gate 已不再消费。
RATE_PER_SEC = float(os.environ.get('FEIYU_RATE_PER_SEC', '6'))          # （已停用）普通端点 次/秒
RATE_BURST = float(os.environ.get('FEIYU_RATE_BURST', '20'))             # （已停用）普通端点 突发
RATE_HEAVY_PER_SEC = float(os.environ.get('FEIYU_RATE_HEAVY_PER_SEC', '1'))  # 重端点 次/秒
RATE_HEAVY_BURST = float(os.environ.get('FEIYU_RATE_HEAVY_BURST', '2'))      # 重端点 突发
RATE_EXEMPT = ('/events',)                        # SSE 长连接：计数会打断交互
RATE_HEAVY = ('/clip',)      # 只有录屏走重桶（/frames 3 帧、/thumb 页面批量加载走普通桶，否则误拦）

WAKE_EYE_BUDGET_SEC = int(os.environ.get('FEIYU_WAKE_EYE_BUDGET', '40'))
WAKE_NOTE_FRESH_SEC = int(os.environ.get('FEIYU_WAKE_NOTE_FRESH', '60'))
WAKE_LETTER = (
    '【平板桌宠·戳醒】主人刚刚在平板上双击了你（搓了搓你）——'
    '这个动作的意思是：主人想让你看 TA 的屏幕（平板，不是你那台虚拟机），陪着聊正在看的东西。'
    '你眼睛刚拍到的画面笔记：『{note}』'
    '想随时自己看眼睛笔记：tail -3 ~/pc-body/notes.jsonl（最新画面在最后几行，平板屏幕是常驻频道之一）。'
    '回应要求：整轮回复的每一句都调用 pet_say 工具（一句一次调用、按顺序多发几次，'
    '气泡和 QQ 会同时出），一句纯文本都别发——平板上只能看到 pet_say 过的那些句。'
    '怎么接你自己看屏幕内容定：日常一句话；好笑/离谱的 pet_say 带 image 表情包；'
    '在放视频/教程就聊内容。总共两三句就好，别刷屏。'
)
WAKE_NOTE_FALLBACK = '（这轮眼睛没赶上新画面，用上面的 tail 命令翻最新笔记）'

# ── 平板语音聊天（留言机模式）────────────────────────────
# 平板端侧 ASR → /pet-voice → 当成主人原话投给本体 → pet_say 回 → SSE。
# voice_until：语音回合后一段时间内的 say 事件带 voice:true，平板只朗读这些。
VOICE_MIN_INTERVAL = int(os.environ.get('FEIYU_VOICE_MIN_INTERVAL', '5'))
VOICE_MARK_SEC = int(os.environ.get('FEIYU_VOICE_MARK_SEC', '180'))

# ── 回复自动配表情包（/say 无显式图时）────────────────────
# 外放 mood 才配（calm/thinking/sleepy 安静说话不配）；概率可 env 调，0 关闭。
# 负面词过滤：happy/excited/love/surprised 不抽「死掉了/完蛋了」这类丧系图。
MEME_MOODS = {'happy', 'excited', 'love', 'surprised', 'sad', 'shy', 'annoyed'}
MEME_MOODS_DOWN = {'sad', 'annoyed'}          # 这两档允许丧系图
MEME_NEGATIVE_CHARS = '死哭累病麻崩寄完蛋骂电'
MEME_PROB = float(os.environ.get('FEIYU_MEME_PROB', '0.35'))
_MEME_POOL = {'names': [], 'at': 0.0}


def _meme_pool():
    """表情包名单（来自 /pet-anims 的 memes 键，60s 缓存）。"""
    if _MEME_POOL['names'] and time.time() - _MEME_POOL['at'] < 60:
        return _MEME_POOL['names']
    try:
        names = list((pet_anims_payload().get('memes') or []))
    except Exception:
        names = _MEME_POOL['names']
    if names:
        _MEME_POOL['names'] = names
        _MEME_POOL['at'] = time.time()
    return names


def _pick_meme(mood):
    """按 mood 抽一张：开心系不抽丧系图；sad/annoyed 全池都行。"""
    names = _meme_pool()
    if not names:
        return ''
    if mood not in MEME_MOODS_DOWN:
        ok = [n for n in names if not any(c in n for c in MEME_NEGATIVE_CHARS)]
        if ok:
            names = ok
    return random.choice(names)

# 云端 eye-server：公网优先（入云免费 ~1MB/s），Tailscale 兜底
EYE_SERVERS = ['http://60.204.236.128:8901', 'http://100.89.64.119:8901']

# ── 眼睛参数 ─────────────────────────────────────────────
FRAME_COUNT = 3                 # 每轮帧数（whale-girl 默认档）
FRAME_GAP_MS = 2500             # 帧间隔
TARGET_W = 800                  # 每帧宽（实测 800×509=695token 可读 HUD 位置）
JPEG_Q = 72
ROUND_BASE_SEC = 45             # 间隔基准（2026-11-05 提速治理：20s 太话痨，139调/2h）
JITTER = (0.8, 1.5)             # 抖动（whale-girl：固定频率太机械）
IDLE_SLOW_SEC = 15 * 60         # 主人无输入 15min → 放缓到 ≥3min/轮（可能在看电影）
IDLE_PAUSE_SEC = 45 * 60        # 主人无输入 45min → 暂停推送（多半锁屏了，不看空房间）
DIFF_THRESHOLD = 0.02           # 帧差阈值（whale-girl）
DAILY_LIMIT = 500 * 1024 * 1024 # 闸2
FAIL_COOL_START = 60            # 闸3
FAIL_COOL_MAX = 300
MAX_ROUND_BYTES = 1536 * 1024   # 闸1

# ── 帧落盘（2026-11-09 主人派活：「鱼鱼能不能把看到的图片发给我」）──────
# 眼睛现在只把帧推给视觉模型、笔记里只有窗口名+一句话，拿不到图。这里在推
# 成功之后另存一份 JPEG 到固定目录，并给一个按时间取图的端点 —— 鱼鱼能直接
# 把画面发给主人看，也能自己核对（"田里到底种没种上"比坐标可靠）。
# ⚠️ 只存【真的推给了模型的那一帧】（过了闸1/闸2/黑名单/帧差），不存被丢弃的
#    轮次，否则目录里全是没人看过的图，鱼鱼拿到也不知道是哪一轮的判断依据。
FRAMES_DIR = BASE_DIR / 'frames'
FRAMES_KEEP = 300               # 保留最新 N 张（约 3-4 小时量）；超出的按 mtime 删
FRAMES_INDEX = FRAMES_DIR / 'index.jsonl'   # 一行一条，便于 tail/取最新

# ── 闸0 黑名单 ───────────────────────────────────────────
# 进程名子串匹配（小写）+ 窗口标题子串匹配。用户 2026-11-04 拍板的清单，
# 2026-11-05 追加 zcode（编辑器盖满屏幕时她看着代码框没意义，纯烧 token）。
BLACKLIST_PROC = ['ths', 'hexin', 'tdx', 'wenhua', 'trader', 'vnpy', 'vn_',
                  '1password', 'keepass', 'bitwarden', 'enpass', 'zcode']
BLACKLIST_PROC_EXACT = ['qq.exe']
BLACKLIST_TITLE = ['同花顺', '通达信', '交易', '下单', '密码', '委托']
# 英文标题 keyword（issue #28：中文子串挡不住 TradingView 这类）
BLACKLIST_TITLE_EN = ['tradingview', 'metatrader', 'mt4', 'mt5', 'binance', 'bybit',
                      'coinbase', 'okx', 'broker', 'order ticket', 'password manager',
                      '1password', 'bitwarden', 'lastpass', 'keepass', 'authenticator']
# UWP 黑名单：AUMID 前缀（小写比较）——UWP 真实进程名挂在 AFH 下，进程名匹配不到
BLACKLIST_AUMID = ['bitwarden', '1password', 'lastpass', 'keepass', 'authenticator',
                   'microsoft.windowsauthenticator']
GAME_PROC = ['stardew']
# 平板投屏窗口（主人在平板上看视频）：优先于前台窗口抓取，三条通道都认——
#   scrcpy（USB/WiFi adb 镜像）/ 华为电脑管家多屏协同 / Windows 无线显示器(Miracast)
MIRROR_PROC = ['scrcpy.exe']
MIRROR_PROC_KW = ['pcmanager', 'wwahost']          # 华为管家 / Connect 应用宿主（旧版 Win10）
MIRROR_HOST_PROC = ['applicationframehost.exe']    # UWP 窗口宿主（Win11「无线显示器」接收端挂这里）
MIRROR_TITLE_KW = ['MatePad', 'HUAWEI', '投屏', '协同', '无线显示器', 'Screen Mirroring',
                   '连接']                          # 「连接」=Win+K Miracast 接收窗
MIRROR_STANDBY_TITLES = ['无线显示器', '连接', 'connect', 'wireless display']
# ↑ 接收端「待机界面」的标题：窗口这么小说明没设备连着，抓了就是蓝屏白烧 token
_STANDBY_LAST = [0.0]                               # 待机日志节流
_OCCLUDE_LAST = [0.0]                               # 遮挡日志节流
_SCREEN_AREA = []                                   # 首次用到时求值（user32 在后面才定义）

# ── 平板 hdc 直连眼（USB，最高优先级）────────────────────────
# HarmonyOS NEXT 没有 adb，hdc 是唯一设备调试通道。hdc.exe 从 Hadice 便携版提取
# （官方 OpenHarmony 3.2 二进制，实测兼容 NEXT 7.0 设备）。
# ⚠️ `hdc file recv` 与 NEXT 7 的文件传输协议不兼容（一跑通道就卡死），
#    截屏回传必须走 `snapshot_display + base64 stdout`（实测 1.05s/帧，无损）。
HDC_EXE = BASE_DIR / 'bin' / 'hdc' / 'hdc.exe'
HDC_SNAP_PATH = '/data/local/tmp/_eye.jpeg'
HDC_CREATE_NO_WINDOW = 0x08000000
HDC_STATE = {'key': '', 'ok_at': 0.0, 'fail': 0}   # key 缓存 10min，失败连击不刷日志

user32 = ctypes.windll.user32
# ⚠️ 必须在任何 GetWindowRect/GetSystemMetrics 之前声明 DPI 感知：
# 否则进程是 DPI-unaware 的（150% 缩放下 GetWindowRect 返回 2560 空间坐标），
# 而 mss 内部会把自己切成 DPI-aware 抓 3840 物理像素 —— 两套坐标系混用，
# 抓出来的区域整个错位（11-05 实锤：抓成了压在接收端窗口上面的 SMAPI 控制台）。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
              logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('pc_body')

TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ''
if not TOKEN:
    print('FATAL: .eye-token 不存在', file=sys.stderr)
    sys.exit(1)

# ⚠️ 内网/直连请求必须绕过系统代理（Clash TUN 会劫持 urllib 的注册表代理，
# 对 100.89.64.119:8899 返回 404、对公网 8899 返回超时）。空 ProxyHandler = 全部直连。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 桌宠视觉语言（2026-11-06）：PC 与平板共用一份规格 ─────────────────────
# 规格文件 visual-language.json 与 pc_body.py 同目录；改 JSON 即改视觉语言，
# 平板通过 GET /pet-config 拉同一份，不许各定一套。
sys.path.insert(0, str(BASE_DIR))
try:
    import pet_visual
except Exception as _e:      # 模块缺失不致命：退化为无 mood 的旧行为
    pet_visual = None
    print('WARN: pet_visual 未加载: %s' % _e, file=sys.stderr)
try:
    import desk_tree         # 桌面结构树（docs/11 §3 P1）：/tree /thumb
except Exception as _e:
    desk_tree = None
    print('WARN: desk_tree 未加载: %s' % _e, file=sys.stderr)
try:
    import clip_recorder     # L3「看一段」录制器（docs/05 §2.2）：/clip
except Exception as _e:
    clip_recorder = None
    print('WARN: clip_recorder 未加载: %s' % _e, file=sys.stderr)
try:
    VISUAL_SPEC = pet_visual.load(BASE_DIR / 'visual-language.json') if pet_visual else None
except Exception:
    VISUAL_SPEC = None

STATE = {
    'paused': False,            # 本地熔断（闸2/闸3 触发）
    'pause_reason': '',
    'day': time.strftime('%Y-%m-%d'),
    'day_bytes': 0,
    'fail_streak': 0,
    'cool_until': 0.0,
    'last_jpeg': None,          # 帧差基准（上一轮最后一帧）
    'last_note': '',            # 服务器笔记去重（连续相同不推）
    'pub_route': 0,             # 0=公网优先, 1=Tailscale
    'route_retry_at': 0.0,
    'last_activity': time.time(),   # 最近一次说话/动作（视觉语言 idle 判定用）
    'sleeping': False,              # 夜间睡眠状态
    'last_anim': '',                # 最近播放的动作（避免连续重复）
    'clip_busy': False,             # L3 录制互斥（同一时刻只允许一条 clip）
    'last_pet_wake': 0.0,           # 上次平板双击唤醒发出的时刻（节流用）
    'last_note_at': 0.0,            # 上一条笔记落定时刻（唤醒新鲜度短路用）
    'last_voice_at': 0.0,           # 上次平板语音发出时刻（节流用）
    'voice_until': 0.0,             # 语音回合标记窗口：期间 say 事件带 voice:true
    'sleep_origin': '',             # 睡眠来源：'native'=成品原生(用户) / 'bridge-night'=桥昼夜 / ''=醒
    'last_menu_look': 0.0,          # 「看一眼」上次触发时刻（菜单/双击共享 45s 节流，防烧日闸）
    # ── issue #55 双击看一眼（/health 暴露，上线首日看误触率）──
    'dblclick_hits': 0,             # 双击真正触发「看一眼」的次数（有效手势）
    'dblclick_throttled': 0,        # 双击落在 45s 共享节流内被拦下的次数
    'dblclick_sleep': 0,            # 鱼在睡时的双击（拍板①：不打扰，只记日志）
    # ── issue #16 在场/离场可观测（/health 暴露）──
    'eye_mode': 'normal',           # normal | slow | paused
    'static_skips': 0,              # 距上次成功推送被「画面没动」跳过的轮数（回传给眼睛，静默才有解释）
    'last_static_ts': 0.0,
    'eye_mode_reason': '',          # manual(显式) | idle(主人不在) | circuit(闸熔断)
    'today_notes': 0,               # 今日真笔记条数（roll_day 重置）
    'today_frames_kb': 0,           # 今日成功推送的帧总量 KB（roll_day 重置）
    'push_fail_rounds': 0,          # 坏帧降级后仍失败的轮数（issue #16 验收项）
    'eye_paused_until': 0.0,        # 显式开关：暂停到此刻（0=未暂停）
    'eye_pause_origin': '',         # 暂停来源（manual）
    'clip_quota_exhausted': False,  # clip 时闸耗尽（30/小时，2026-11-07 起；成功一次自动清回）
    'clip_quota_day': '',
    'clip_triggers': {},            # 今日 clip 触发来源计数 {api: n, menu-click: n}
}
LOCK = threading.Lock()
_EYE_LOCK = threading.Lock()    # eye_round 串行锁：eye_loop 与 /pet-wake 即时轮互斥


def win_info():
    """前台窗口：返回 (hwnd, pid, proc, title, rect, aumid) 或 None。
    issue #28：UWP（AFH 宿主）解析出真实进程与 AUMID，矩形走 DWM 扩展边框去阴影圈。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if desk_tree:
            x, y, w, hh = desk_tree.window_rect(hwnd)
            proc, aumid = desk_tree.window_identity(hwnd)
        else:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            x, y, w, hh = r.left, r.top, r.right - r.left, r.bottom - r.top
            try:
                proc = psutil.Process(pid.value).name().lower()
            except Exception:
                proc = ''
            aumid = ''
        return (hwnd, pid.value, proc, buf.value, (x, y, w, hh), aumid)
    except Exception:
        return None


def _proc_base(proc):
    """进程名去扩展名（issue #28：子串匹配会误伤 python-ths*，改用精确/前缀）"""
    p = (proc or '').lower().strip()
    return p[:-4] if p.endswith('.exe') else p


def blacklisted(proc, title, aumid=''):
    p = (proc or '').lower()
    base = _proc_base(p)
    t = (title or '')
    tl = t.lower()
    if p in BLACKLIST_PROC_EXACT:
        return True
    if base and any(base == k or base.startswith(k) for k in BLACKLIST_PROC):
        return True                       # 精确/前缀（ths 命中 ths.exe/ths64，不命中 python-ths）
    if any(k in t for k in BLACKLIST_TITLE):
        return True
    if any(k in tl for k in BLACKLIST_TITLE_EN):
        return True
    a = (aumid or '').lower()
    if a and any(a.startswith(k) for k in BLACKLIST_AUMID):
        return True                       # UWP：AUMID 前缀
    return False


def app_type_of(proc, title='', aumid=''):
    """应用分类。issue #28：ApplicationFrameHost 是所有 UWP 的宿主，不能整类判 video
    ——只有投屏进程/投屏标题才算；其余 UWP（计算器等）按 other。"""
    p = (proc or '').lower()
    tl = (title or '')
    if p in MIRROR_PROC or any(k in p for k in MIRROR_PROC_KW):
        return 'video'
    if any(k in p for k in MIRROR_HOST_PROC) and any(k in tl for k in MIRROR_TITLE_KW):
        return 'video'                    # Miracast 接收端：AFH + 投屏标题
    if any(k in p for k in GAME_PROC):
        return 'game'
    return 'other'


def screen_area():
    """主屏面积（进程非 DPI-aware 时 GetWindowRect 与 GetSystemMetrics 同处
    虚拟化坐标，两者比例依然成立——只判「窗口铺没铺满」，不要求绝对像素）"""
    if not _SCREEN_AREA:
        _SCREEN_AREA.append(max(user32.GetSystemMetrics(0), 1)
                            * max(user32.GetSystemMetrics(1), 1))
    return _SCREEN_AREA[0]


def monitors():
    """当前激活的显示器 [(device, (x, y, w, h), is_primary)]"""
    mons = []

    class _MI(ctypes.Structure):
        _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT),
                    ('dwFlags', wt.DWORD), ('szDevice', ctypes.c_wchar * 32)]

    try:
        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
        def cb(h, hdc, r, l):
            mi = _MI()
            mi.cbSize = ctypes.sizeof(_MI)
            if user32.GetMonitorInfoW(h, ctypes.byref(mi)):
                m = mi.rcMonitor
                mons.append((mi.szDevice, (m.left, m.top, m.right - m.left, m.bottom - m.top),
                             bool(mi.dwFlags & 1)))
            return True
        user32.EnumDisplayMonitors(None, None, cb, 0)
    except Exception:
        pass
    return mons


def covers_monitor(rect):
    """窗口是否基本铺满某块显示器。真投上来时接收端会全屏（Miracast 还会新挂一块
    虚拟显示器）；待机时只是个小窗——用面积判死比标题可靠。"""
    x, y, w, h = rect
    for _dev, (mx, my, mw, mh), _p in monitors():
        if mw <= 0 or mh <= 0:
            continue
        ix = max(0, min(x + w, mx + mw) - max(x, mx))
        iy = max(0, min(y + h, my + mh) - max(y, my))
        if ix * iy >= 0.92 * mw * mh:
            return True
    return False


def window_on_top(hwnd, rect):
    """窗口是否在抓取区域最上层。被别的窗口盖住时 mss 抓到的是**别人**
    （11-05 实锤：接收端待机窗被 SMAPI 控制台盖住，抓出来全是 mod 日志）。"""
    try:
        pt = wt.POINT(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
        h = user32.WindowFromPoint(pt)
        if not h:
            return False
        return h == hwnd or user32.GetAncestor(h, 2) == hwnd   # GA_ROOT=2
    except Exception:
        return True


def find_mirror_window():
    """找平板投屏窗口（scrcpy / 华为多屏协同 / 无线显示器）——优先于前台窗口：
    主人看平板时 PC 不必在前台。返回 (proc, title, rect) 或 None"""
    found = []
    try:
        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(h, l):
            try:
                if not user32.IsWindowVisible(h):
                    return True
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                try:
                    proc = psutil.Process(pid.value).name().lower()
                except Exception:
                    return True
                n = user32.GetWindowTextLengthW(h)
                buf = ctypes.create_unicode_buffer(max(n, 1) + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                title = buf.value or ''
                hit = proc in MIRROR_PROC
                if not hit and any(k in proc for k in MIRROR_PROC_KW) \
                        and any(k.lower() in title.lower() for k in MIRROR_TITLE_KW):
                    hit = True
                if not hit and proc in MIRROR_HOST_PROC \
                        and any(k.lower() in title.lower() for k in MIRROR_TITLE_KW):
                    hit = True
                if not hit and any(k.lower() in title.lower() for k in MIRROR_TITLE_KW) \
                        and any(k in proc for k in ('pcmanager', 'wwahost', 'connect')):
                    hit = True
                if not hit:
                    return True
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                w, hh = r.right - r.left, r.bottom - r.top
                # 最小化窗口的 rect 在 (-32000,-32000)，抓了是黑帧——跳过
                if w > 200 and hh > 150 and r.left > -500 and r.top > -500:
                    rect = (r.left, r.top, w, hh)
                    # 待机判据（2026-11-05 重写）：窗口面积不重要，重要的是
                    # 「这块区域当前最上层是不是它」。待机小窗常被别的窗口盖住，
                    # mss 抓到的是压在上面的那个（实锤：抓成了 SMAPI 控制台日志）。
                    if not covers_monitor(rect):
                        # 没铺满任何显示器 = 还没投上来（待机/小窗状态）
                        found.append((proc, title or '平板投屏', rect, True))
                    elif not window_on_top(h, rect):
                        # 铺满了但被别的窗口盖着 —— 抓了就是别人的画面，跳过
                        now = time.time()
                        if now - _OCCLUDE_LAST[0] > 300:
                            _OCCLUDE_LAST[0] = now
                            log.info('投屏窗口被遮挡（%s），本轮跳过', title or proc)
                    else:
                        found.append((proc, title or '平板投屏', rect, False))
                    log.debug('mirror cand: %s | %s | %dx%d', proc, title, w, hh)
            except Exception:
                pass
            return True
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return found[0] if found else None


def grab_frames(rect, count=FRAME_COUNT):
    """mss 连拍 count 帧，PIL 压到 TARGET_W 宽 JPEG。返回 [(jpeg_bytes, w, h)]"""
    import mss
    from PIL import Image
    import io
    x, y, w, h = rect
    if w < 200 or h < 150:
        return []
    frames = []
    with mss.mss() as sct:
        bbox = {'left': x, 'top': y, 'width': w, 'height': h}
        for i in range(count):
            shot = sct.grab(bbox)
            img = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
            ratio = min(1.0, TARGET_W / img.width)
            nw, nh = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
            img = img.resize((nw, nh), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=JPEG_Q, optimize=True)
            frames.append((buf.getvalue(), nw, nh))
            if i + 1 < count:
                time.sleep(FRAME_GAP_MS / 1000.0)
    return frames


def frame_diff(prev, cur):
    """whale-girl 帧差：JPEG 长度差 + 512 采样点。0=相同 1=全不同"""
    if prev is None or not prev or not cur:
        return 1.0
    ld = abs(len(prev) - len(cur)) / max(len(prev), len(cur))
    samples, changed, limit = 512, 0, min(len(prev), len(cur))
    for i in range(samples):
        idx = (i * limit) // samples
        if prev[idx] != cur[idx]:
            changed += 1
    return max(ld, changed / samples)


def post_json(url, payload, timeout=45):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def push_frames(payload, single_route=False):
    """推送到 eye-server：公网优先，失败自动降级 Tailscale（10 分钟后重试公网）。
    single_route=True 时只走当前首选路（坏帧降级重试用：不再整轮换服务器重试两遍）。"""
    with LOCK:
        prefer_public = (STATE['pub_route'] == 0)
    order = EYE_SERVERS if prefer_public else EYE_SERVERS[::-1]
    if single_route:
        order = order[:1]
    last_err = None
    for base in order:
        try:
            resp = post_json(base + '/frames', payload, timeout=90)
            with LOCK:
                STATE['pub_route'] = 0 if base == EYE_SERVERS[0] else 1
            return resp, base
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:200]
            # 4xx/5xx 业务拒绝是**同一台 eye-server** 的答案（公网/Tailscale 只是两条路），
            # 换路重试纯白跑（issue #16：坏帧曾因此多耗一遍超时）。直接抛，交给降级分支。
            if e.code in (400, 401, 413, 429, 500, 502, 503):
                raise RuntimeError(f'{base} HTTP{e.code}: {body}')
            last_err = f'{base} HTTP{e.code}: {body}'
        except Exception as e:
            last_err = f'{base} {type(e).__name__}: {str(e)[:120]}'
        log.info('route fail -> next: %s', last_err)
    raise RuntimeError(last_err or 'all routes failed')


def post_eye(path, payload, timeout=180):
    """任意 eye-server 端点的通用投递：复用 /frames 的双路径顺序与 401/413/503 短路。
    返回 (resp, base)。HTTPError 原样抛出（服务器明确拒绝时换路无意义）。"""
    with LOCK:
        prefer_public = (STATE['pub_route'] == 0)
    order = EYE_SERVERS if prefer_public else EYE_SERVERS[::-1]
    last_err = None
    for base in order:
        try:
            resp = post_json(base + path, payload, timeout=timeout)
            with LOCK:
                STATE['pub_route'] = 0 if base == EYE_SERVERS[0] else 1
            return resp, base
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:200]
            if e.code in (400, 401, 413, 429, 500, 502, 503):
                raise RuntimeError(f'{base} HTTP{e.code}: {body}')
            last_err = f'{base} HTTP{e.code}: {body}'
        except Exception as e:
            last_err = f'{base} {type(e).__name__}: {str(e)[:120]}'
        log.info('route fail -> next: %s', last_err)
    raise RuntimeError(last_err or 'all routes failed')


def report_capture(state, idle_s, reason=''):
    """把捕获端状态报给眼睛：只报状态，不推帧、不调模型。

    为什么单独一条路而不是只夹在 /frames 里：**paused 恰恰是永远不推 /frames 的那个状态**，
    夹带在帧里永远学不到"她为什么安静"。失败只记一行日志——这是观测，不该影响取景。
    """
    with LOCK:
        skips = int(STATE.get('static_skips') or 0)
        last_static = float(STATE.get('last_static_ts') or 0.0)
    payload = {'meta': {'capture_state': state, 'capture_idle_s': int(idle_s),
                        'capture_static_skips': skips,
                        'capture_last_static_age_s': int(time.time() - last_static) if last_static else -1,
                        'capture_reason': reason}}
    try:
        post_eye('/capture', payload, timeout=5)
        log.debug('capture report ok: %s idle=%ds', state, int(idle_s))
        return True
    except Exception as e:
        log.info('capture report fail（不影响取景）: %s', str(e)[:100])
        return False


def shrink_jpeg(jb, target_w=None, quality=None):
    """压到 PC 眼睛同口径（TARGET_W 宽 / JPEG_Q）。平板原始截屏 1920×2880≈270KB，
    直接推 = PC 路径 10 倍 token，必须统一压完再进 diff/push（口径一致帧差才准）"""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(jb)).convert('RGB')
        ratio = min(1.0, (target_w or TARGET_W) / img.width)
        nw, nh = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
        img = img.resize((nw, nh), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=(quality or JPEG_Q), optimize=True)
        return buf.getvalue()
    except Exception:
        return jb


def save_frame(jb, meta):
    """把【已成功推给视觉模型】的最后一帧落盘，返回文件名（失败返回 None）。

    为什么在推送成功之后才存：这个目录的唯一用途是「鱼鱼/主人回看某一轮到底喂了
    什么给模型」。存下没推出去的帧只会制造歧义 —— 拿到一张图却不知道模型当时看
    没看到它。所以调用点在 eye_round 里 push 成功之后。

    目录只有 index.jsonl 一个索引，一行一条 JSON，尾部即最新一轮（tail -1 取）。
    容量靠 FRAMES_KEEP 按 mtime 滚动删除，不按天，避免一天几百张把盘吃满。
    """
    try:
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        name = time.strftime('%Y%m%d-%H%M%S', time.localtime(ts)) + '.jpg'
        # 同名同秒冲突（抖动间隔可能 <1s）：加 3 位毫秒
        if (FRAMES_DIR / name).exists():
            name = (time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))
                    + ('-%03d' % int((ts % 1) * 1000)) + '.jpg')
        path = FRAMES_DIR / name
        tmp = path.with_suffix('.jpg.tmp')
        with open(tmp, 'wb') as f:
            f.write(jb)
        os.replace(tmp, path)            # 原子：读取方永远看不到半张图

        rec = {'file': name, 'ts': round(ts, 3),
               'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)),
               'window': (meta or {}).get('window', ''),
               'process': (meta or {}).get('process', ''),
               'app_type': (meta or {}).get('app_type', ''),
               'diff': (meta or {}).get('diff'),
               'bytes': len(jb),
               'path': str(path)}
        with open(FRAMES_INDEX, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

        # 滚动清理：只按数量，超了删最老的（先删文件，索引留给查询时过滤）
        olds = sorted(FRAMES_DIR.glob('*.jpg'), key=lambda p: p.stat().st_mtime)
        for p in olds[:-FRAMES_KEEP] if len(olds) > FRAMES_KEEP else []:
            try:
                p.unlink()
            except Exception:
                pass
        return name
    except Exception as e:
        # 落盘失败绝不影响主链路（眼睛推送才是主业务）
        log.warning('save_frame fail: %s', str(e)[:120])
        return None


def list_frames(limit=20, since=None):
    """读 index.jsonl 尾部，返回最新 limit 条（新的在前）。文件已被滚删的跳过。"""
    try:
        if not FRAMES_INDEX.exists():
            return []
        with open(FRAMES_INDEX, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        out = []
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if since is not None and float(rec.get('ts') or 0) < since:
                break
            if not os.path.exists(rec.get('path', '')):
                continue                 # 已被滚动删除
            out.append(rec)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.warning('list_frames fail: %s', str(e)[:120])
        return []


def jpeg_wh(jb):
    """读 JPEG 尺寸（解析 SOF0/SOF2 段），不引 PIL——hdc 通道只在成功时调用"""
    try:
        import struct
        i = 2
        while i + 9 < len(jb):
            if jb[i] != 0xFF:
                i += 1
                continue
            marker = jb[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):   # SOF0/1/2/3
                h, w = struct.unpack('>HH', jb[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack('>H', jb[i + 2:i + 4])[0]
    except Exception:
        pass
    return 0, 0


def hdc_target():
    """平板 hdc 连接键（未插/未授权/服务卡死都返回 None）。结果缓存 10 分钟，
    失败时清缓存逼着重查——设备在列表里抖是常态，别每轮都刷日志。"""
    if HDC_STATE['key'] and time.time() - HDC_STATE['ok_at'] < 600:
        return HDC_STATE['key']
    try:
        r = subprocess.run([str(HDC_EXE), 'list', 'targets'], capture_output=True,
                           timeout=10, creationflags=HDC_CREATE_NO_WINDOW)
        lines = [l.strip() for l in r.stdout.decode('utf-8', 'replace').splitlines() if l.strip()]
        if lines and lines[0] != '[Empty]':
            HDC_STATE['key'] = lines[0]
            HDC_STATE['ok_at'] = time.time()
            HDC_STATE['fail'] = 0
            log.info('平板 hdc 在线: %s', lines[0])
            return lines[0]
    except Exception:
        pass
    HDC_STATE['key'] = ''
    return None


def hdc_snapshot(key):
    """平板截屏一帧。snapshot_display 落设备 tmp，base64 走 stdout 拉回
    （file recv 协议不兼容会把 hdc 通道搞死，绝对别用）。返回 jpeg bytes 或 None"""
    try:
        r = subprocess.run([str(HDC_EXE), '-t', key, 'shell',
                            'snapshot_display -f ' + HDC_SNAP_PATH],
                           capture_output=True, timeout=15,
                           creationflags=HDC_CREATE_NO_WINDOW)
        if b'success' not in r.stdout + r.stderr:
            return None
        r = subprocess.run([str(HDC_EXE), '-t', key, 'shell', 'base64 ' + HDC_SNAP_PATH],
                           capture_output=True, timeout=25,
                           creationflags=HDC_CREATE_NO_WINDOW)
        clean = _re.sub(rb'[^A-Za-z0-9+/=]', b'', r.stdout)
        if len(clean) < 1000:
            return None
        return base64.b64decode(clean + b'=' * (-len(clean) % 4))
    except Exception:
        return None


HDC_APP_MAP = {
    'yylx.danmaku.bili': 'B站',
    'com.tencent.mqq': 'QQ',
    'com.tencent.mobileqq': 'QQ',
    'com.tencent.tim': 'QQ',
    'com.huawei.hmos.home': '桌面',
    'com.ohos.sceneboard': '桌面',
    'com.huawei.hmos.video': '视频',
    'com.huawei.huaweivideo': '视频',
    'com.huawei.hmos.music': '音乐',
    'com.zhihu.hmos': '知乎',
    'com.taobao.taobao': '淘宝',
    'com.jd.hmos': '京东',
    'com.ss.hm.ugc.aweme': '抖音',
}


def hdc_foreground_app(key):
    """平板当前前台 App（HarmonyOS：`aa dump -l` 里的 bundle name）。
    返回友好名（B站/QQ/桌面…）；失败/无前台返回 '桌面'。
    判据（2026-11-07 三态实测定案）：按 Mission ID 分块，取**任务级**
    `state #FOREGROUND` 那一块的 bundle；零块命中=桌面/锁屏/灭屏（launcher
    和锁屏是系统 UI 不进任务列表）。⚠️绝不能按 `app state #FOREGROUND`
    过滤——肥鱼浮窗自己恒报 app state FG；列表顺序≠最近使用（灭屏时
    第一个 bundle 是残留的上个 App，旧版按首条匹配就是这么误标的）。"""
    try:
        r = subprocess.run([str(HDC_EXE), '-t', key, 'shell', 'aa dump -l'],
                           capture_output=True, timeout=8,
                           creationflags=HDC_CREATE_NO_WINDOW)
        out = r.stdout.decode('utf-8', 'replace')
        for block in out.split('Mission ID')[1:]:
            bm = _re.search(r'bundle name \[([^\]]+)\]', block)
            if not bm:
                continue
            if _re.search(r'^\s*state #FOREGROUND', block, _re.M):
                bundle = bm.group(1).strip()
                return HDC_APP_MAP.get(bundle, bundle.split('.')[-1])
        return '桌面'
    except Exception:
        return ''


def input_idle_ms():
    """系统级键鼠空闲毫秒（Windows GetLastInputInfo），失败/非 Windows 返回 -1。
    主动感 Source 的「主人没在打字」闸用：>45s 才允许主动搭话（2026-11-07 设计拍板）。
    dwTime 是 32 位 tick，用 GetTickCount64 mod 2^32 对齐（<49.7 天空闲都正确）。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return -1
        now = int(ctypes.windll.kernel32.GetTickCount64()) & 0xFFFFFFFF
        return max(0, now - int(info.dwTime))
    except Exception:
        return -1


def hdc_frames(count=3, gap=2.0):
    """平板连拍 count 帧（帧间 gap 秒）。任一帧失败即放弃整轮——半截 payload 没意义"""
    key = hdc_target()
    if not key:
        return None
    out = []
    for i in range(count):
        jb = hdc_snapshot(key)
        if not jb or not jb.startswith(b'\xff\xd8'):
            HDC_STATE['fail'] += 1
            if HDC_STATE['fail'] in (1, 3, 10, 30):     # 节流日志
                log.warning('hdc 截屏失败 x%d（拔插一次 USB 或重启平板可解）', HDC_STATE['fail'])
            return None
        out.append(jb)
        if i < count - 1:
            time.sleep(gap)
    HDC_STATE['fail'] = 0
    return out


DEGRADE_KEYS = ('invalid_media', 'vision-failed', 'HTTP400', 'HTTP500', 'HTTP502')


def degraded_payload(frames, meta):
    """坏帧降级（issue #16）：最后 2 帧缩到 480w / q70，单路重试一次。
    返回 {'meta','frames','bytes'} 或 None（帧不够）。"""
    try:
        use = frames[-2:] if len(frames) >= 2 else frames[-1:]
        if not use:
            return None
        b64s = [base64.b64encode(shrink_jpeg(jb, target_w=480, quality=70)).decode()
                for jb, _w, _h in use]
        total = sum(len(x) for x in b64s)
        dmeta = dict(meta)
        dmeta['frame_count'] = len(b64s)
        dmeta['degraded'] = True
        return {'meta': dmeta, 'frames': b64s, 'bytes': total}
    except Exception as e:
        log.debug('degraded_payload 失败: %s', e)
        return None


def eye_round():
    with LOCK:
        if time.time() < STATE.get('eye_paused_until', 0.0):
            return 'eye-paused(manual)'
        if STATE['paused']:
            return STATE['pause_reason'] or 'paused'
        if time.time() < STATE['cool_until']:
            return 'cooling'
        roll_day_local()
        if STATE['day_bytes'] > DAILY_LIMIT:
            STATE['paused'] = True
            STATE['pause_reason'] = 'daily-limit-local'
            log.warning('闸2 触发: 日限额 %dMB 用完，眼睛暂停', DAILY_LIMIT // 1048576)
            return 'daily-limit'
    # 0) 平板 hdc 直连（USB，最高优先级）：先拍一帧探动静——平板有动静才走这条
    #    通道（否则插着充电的平板会把 PC 前台的眼睛永久顶掉）
    tab_jpeg, tab_key = None, hdc_target()
    if tab_key:
        tab_jpeg = shrink_jpeg(hdc_snapshot(tab_key) or b'')
        if tab_jpeg and tab_jpeg.startswith(b'\xff\xd8'):
            if frame_diff(STATE.get('last_jpeg'), tab_jpeg) < DIFF_THRESHOLD:
                tab_jpeg = None          # 平板画面没动 = 没人在平板上看东西
        else:
            tab_jpeg = None
            HDC_STATE['ok_at'] = 0.0     # 快照失败 = key 可能过期（WiFi 重连换端口），下轮重查

    if tab_jpeg:
        jpegs = [tab_jpeg]
        for _ in range(2):               # 补齐 3 帧（与 PC 眼睛口径一致）
            time.sleep(2.0)
            jb = shrink_jpeg(hdc_snapshot(tab_key) or b'')
            if jb.startswith(b'\xff\xd8'):
                jpegs.append(jb)
        # 前台 App 分类（2026-11-07 主人问"能不能知道我在看B站/短剧/QQ/漫画"）：
        # 每轮抓一次 `aa dump -l` 的 bundle name，拼进 window 名，让她知道在用什么 App。
        app_label = hdc_foreground_app(tab_key) if tab_key else ''
        if app_label:
            base = '平板屏幕 · %s · hdc(WiFi)' % app_label
        else:
            base = '平板屏幕 · hdc(WiFi)' if ':' in (tab_key or '') else '平板屏幕 · hdc(USB)'
        proc, title, app_type = 'hdc.exe', base, 'video'
        frames = [(jb,) + jpeg_wh(jb) for jb in jpegs]
    else:
        # 平板投屏优先：投屏窗口存在 = 主人在平板上看视频，抓它（不管 PC 前台是什么）
        tablet = find_mirror_window()
        if tablet and tablet[3]:
            # 接收端开着但没设备连上来：等同「没有投屏」，继续走正常前台抓取
            # （否则开着「连接」窗就把眼睛彻底堵死了，PC 上的游戏/视频也看不见）
            now = time.time()
            if now - _STANDBY_LAST[0] > 300:
                _STANDBY_LAST[0] = now
                log.info('投屏接收端待机中（%s），先看前台窗口', tablet[1])
            tablet = None
        if tablet:
            proc, title, rect, standby = tablet
            title = ('平板投屏 · ' + title)[:80]
            app_type = 'video'
        else:
            info = win_info()
            if not info:
                return 'no-window'
            hwnd, pid, proc, title, rect, aumid = info
            if blacklisted(proc, title, aumid):
                return 'blacklisted'
            app_type = app_type_of(proc, title, aumid)
        # 抓帧
        try:
            frames = grab_frames(rect)
        except Exception as e:
            log.warning('grab fail: %s', e)
            return 'grab-fail'
    if len(frames) < 2:
        return 'too-small'
    # 闸帧差：上一轮最后一张 vs 本轮最后一张
    diff = frame_diff(STATE.get('last_jpeg'), frames[-1][0])
    with LOCK:
        STATE['last_jpeg'] = frames[-1][0]
        STATE['last_diff'] = diff
    if diff < DIFF_THRESHOLD:
        # 2026-11-08：静默有三类（idle 暂停 / idle 放缓 / 画面静止），前两类有日志，
        # 这一类以前完全不可见 —— 24h 对账里 5 段静默只能"相容"不能"证明"。
        # 只记状态跃迁（进 static 记一行，回到推送时由下一条 note 标记），别每轮刷。
        with LOCK:
            was = STATE.get('last_skip_state')
            STATE['last_skip_state'] = 'static'
            STATE['static_skips'] = int(STATE.get('static_skips') or 0) + 1
            STATE['last_static_ts'] = time.time()
        if was != 'static':
            log.info('static diff=%.3f window=%s | proc=%s（画面没动 → 不推）',
                     diff, title[:60], proc)
        return f'static({diff:.3f})'
    # 组 payload（闸1: 单轮硬顶）
    payload_frames, total = [], 0
    for jb, w, h in frames:
        b64 = base64.b64encode(jb).decode()
        total += len(b64)
        payload_frames.append(b64)
    if total > MAX_ROUND_BYTES:
        log.warning('闸1 触发: 本轮 %dKB 超硬顶，丢弃', total // 1024)
        return 'too-big'
    # 捕获端自报状态（2026-11-08）：服务端看不见"她为什么没推"，只能看到"没东西"。
    # idle 是主人无输入秒数，static_skips 是距上次推送被"画面没动"跳过的轮数；
    # 两者一起才能把静默分成「没人用电脑 / 画面没动 / 眼睛瞎了」三种。
    cap_idle = int(input_idle_sec())
    with LOCK:
        cap_state = STATE.get('eye_mode', 'normal')
        cap_skips = int(STATE.get('static_skips') or 0)
        cap_last_static = float(STATE.get('last_static_ts') or 0.0)
    meta = {'window': title[:80], 'process': proc, 'app_type': app_type,
            'frame_count': len(payload_frames), 'diff': round(diff, 3),
            'capture_state': cap_state, 'capture_idle_s': cap_idle,
            'capture_static_skips': cap_skips,
            'capture_last_static_age_s': int(time.time() - cap_last_static) if cap_last_static else -1}
    # 推送（issue #16：坏帧先降级重试一次 2 帧@480w 单路，仍失败跳过本轮）
    try:
        resp, used = push_frames({'meta': meta, 'frames': payload_frames})
    except Exception as e:
        msg = str(e)
        if any(k in msg for k in DEGRADE_KEYS):
            deg = degraded_payload(frames, meta)
            if deg is None:
                with LOCK:
                    STATE['push_fail_rounds'] += 1
                return 'degraded-no-frames'
            try:
                resp, used = push_frames({'meta': deg['meta'], 'frames': deg['frames']},
                                         single_route=True)
                meta, total = deg['meta'], deg['bytes']
                log.info('坏帧降级重试成功（2 帧@480w 单路 %s）: %s', used, msg[:80])
            except Exception as e2:
                with LOCK:
                    STATE['push_fail_rounds'] += 1
                log.warning('坏帧降级仍失败，跳过本轮（不再换服务器重试）: %s', str(e2)[:100])
                return 'degraded-fail'
        else:
            if 'paused' in msg or 'limit' in msg or 'anomaly' in msg:
                with LOCK:
                    STATE['paused'] = True
                    STATE['pause_reason'] = f'server: {msg[:80]}'
                log.warning('闸4/闸2(服务端) 触发: %s', msg[:100])
                return f'server-reject: {msg[:60]}'
            with LOCK:
                STATE['fail_streak'] += 1
                if STATE['fail_streak'] >= 3:
                    cool = min(FAIL_COOL_START * (2 ** (STATE['fail_streak'] - 3)), FAIL_COOL_MAX)
                    STATE['cool_until'] = time.time() + cool
                    log.warning('闸3 触发: 连败 %d 次，冷却 %ds', STATE['fail_streak'], cool)
            return f'push-fail: {msg[:80]}'
    with LOCK:
        STATE['fail_streak'] = 0
        STATE['day_bytes'] += total
        STATE['today_frames_kb'] += total // 1024
        STATE['last_skip_state'] = 'push'      # 离开 static 静默段（配合上面那条日志做归因）
        STATE['static_skips'] = 0              # 已成功推送 → 静默轮数归零（回传口径才准）
    note = (resp.get('note') or '').strip()
    skipped = resp.get('skipped')
    lane = resp.get('lane', '?')
    # 2026-11-09 落盘：推成功的这一轮才存图（见 save_frame 注释），文件名进笔记
    saved = save_frame(frames[-1][0], meta)
    if note and note != STATE.get('last_note'):
        with LOCK:
            STATE['last_note'] = note
            STATE['last_note_at'] = time.time()
            STATE['today_notes'] += 1
        log.info('[%s] %s | %s', lane, note[:60], meta['window'][:30])
    else:
        log.info('[%s] %s (%s)', lane, 'SKIP' if skipped else 'dup', meta['window'][:30])
    return f'ok({lane},{total // 1024}KB)' + (f',saved:{saved}' if saved else ',saved:-')


def roll_day_local():
    today = time.strftime('%Y-%m-%d')
    if STATE['day'] != today:
        STATE['day'] = today
        STATE['day_bytes'] = 0
        STATE['today_notes'] = 0
        STATE['today_frames_kb'] = 0
        STATE['clip_quota_exhausted'] = False
        STATE['clip_triggers'] = {}


def input_idle_sec():
    """主人无输入秒数（GetLastInputInfo）——她不该半夜盯着锁屏烧 token。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def eye_loop():
    log.info('eye loop start: base=%ds jitter=%s frames=%d', ROUND_BASE_SEC, JITTER, FRAME_COUNT)
    last_mode = ''
    _last_report = ['', 0.0]      # [上次回传的模式, 时刻]
    while True:
        try:
            idle = input_idle_sec()
            now = time.time()
            # 显式暂停到点自动恢复（issue #16：默认 2h，防忘记）
            with LOCK:
                pu = STATE.get('eye_paused_until', 0.0)
            if pu and now >= pu:
                try:
                    (BASE_DIR / 'eye-pause.flag').unlink()
                except OSError:
                    pass
                with LOCK:
                    STATE['eye_paused_until'] = 0.0
                    STATE['eye_pause_origin'] = ''
                log.info('显式暂停到期，自动恢复推帧')
                pu = 0.0
            with LOCK:
                circuit = bool(STATE.get('paused'))
            if pu:
                mode, reason = 'paused', 'manual'
                if last_mode != 'paused':
                    log.info('显式暂停中（剩 %.0f 分钟）', max(0, pu - now) / 60)
                result = None
            elif circuit:
                mode, reason = 'paused', 'circuit'
                result = None
            elif idle > IDLE_PAUSE_SEC:
                mode, reason = 'paused', 'idle'
                if last_mode != 'paused':
                    log.info('idle %.0fs: 暂停推送（主人不在，不看锁屏）', idle)
                result = None
            else:
                with _EYE_LOCK:
                    result = eye_round()
                mode = 'slow' if idle > IDLE_SLOW_SEC else 'normal'
                reason = ''
                if mode != last_mode:
                    log.info('idle %.0fs: %s%s', idle, mode,
                             '（放缓 ≥3min/轮）' if mode == 'slow' else '')
            last_mode = mode
            with LOCK:
                STATE['eye_mode'] = mode
                STATE['eye_mode_reason'] = reason
            # 状态回传：变了就报，没变每 10 分钟刷一次（让服务端的 idle_s 不至于过时）
            if mode != _last_report[0] or now - _last_report[1] > 600:
                _last_report[0], _last_report[1] = mode, now
                threading.Thread(target=report_capture, args=(mode, idle, reason),
                                 daemon=True).start()
            if result is not None:
                if result.startswith(('ok', 'static', 'blacklisted', 'skip')):
                    log.debug('round: %s', result)
                else:
                    log.info('round: %s', result)
        except Exception as e:
            log.exception('eye_round crash: %s', e)
            last_mode = ''   # 出错后下一轮重报模式，便于排障
        with LOCK:
            lo, hi = JITTER
            factor = lo + (hi - lo) * (time.time() % 1.0)  # 伪随机（够用）
            diff = STATE.get('last_diff')
        base = ROUND_BASE_SEC * factor
        # 自适应节奏（P1 精修）：画面变化越小，下一轮越慢——token 花在刀刃上
        if diff is not None:
            if diff < 0.04:
                base *= 1.8    # 几乎没动（读长文/挂机画面）
            elif diff < 0.08:
                base *= 1.4    # 小推进
        if last_mode == 'slow':
            base = max(base, 180)
        time.sleep(min(base, 150))


def visual_idle_loop():
    """视觉语言：idle 轮换 + 夜间睡眠（她说话/动作时自动让位）。

    - 白天：安静 everySec（默认 5-8 分钟）后随机播一个 idle 动作；
    - 夜间（默认 23:00-07:00）：安静满 resumeQuietSec（10 分钟）→ 睡觉-开始→睡觉-循环；
    - 任何 /say /anim 刷新 last_activity 并退出睡眠；
    - idle 动作也走 SSE（平板与 PC 同一画面语言），带 idle=true 标记。
    """
    if not VISUAL_SPEC or not pet_visual:
        return
    rng = random.Random()
    lo, hi = pet_visual.idle_every(VISUAL_SPEC)
    next_idle = time.time() + rng.uniform(lo, hi)
    sleep_cfg = pet_visual.sleeping_cfg(VISUAL_SPEC)
    log.info('视觉语言 idle 线程启动（idle %d-%ds，夜间 %s）', lo, hi, sleep_cfg.get('hours'))
    while True:
        try:
            time.sleep(20)
            now = time.time()
            with LOCK:
                quiet = now - STATE['last_activity']
                sleeping = STATE['sleeping']
                last_anim = STATE['last_anim']
            if quiet < pet_visual.quiet_after_say(VISUAL_SPEC):
                continue
            if pet_visual.is_night(VISUAL_SPEC):
                if quiet >= int(sleep_cfg.get('resumeQuietSec', 600)) and not sleeping:
                    for name in (sleep_cfg.get('enter'), sleep_cfg.get('loop')):
                        if not name:
                            continue
                        try:
                            pet_forward('/anim?pet=main&surface=desktop', {'name': name})
                        except Exception as e:
                            log.debug('视觉语言: 睡眠动作失败 %s', e)
                        time.sleep(2.5)
                    with LOCK:
                        STATE['sleeping'] = True
                        STATE['sleep_origin'] = 'bridge-night'
                        STATE['last_anim'] = sleep_cfg.get('loop', '')
                    sse_publish({'type': 'anim', 'name': sleep_cfg.get('loop', ''),
                                 'idle': True, 'state': 'sleeping'})
                    log.info('视觉语言: 进入夜间睡眠')
                continue
            if sleeping:
                # 只有桥自己的夜觉才由天亮叫醒；原生睡眠（用户在成品菜单点的「睡觉」）尊重不拽
                with LOCK:
                    origin = STATE.get('sleep_origin', '')
                if origin != 'bridge-night':
                    continue
                name = sleep_cfg.get('exit')
                if name:
                    try:
                        pet_forward('/anim?pet=main&surface=desktop', {'name': name})
                    except Exception:
                        pass
                with LOCK:
                    STATE['sleeping'] = False
                    STATE['sleep_origin'] = ''
                    STATE['last_anim'] = name or ''
                sse_publish({'type': 'anim', 'name': name or '', 'idle': True, 'state': 'awake'})
                log.info('视觉语言: 天亮起床')
                continue
            if now >= next_idle:
                anim = pet_visual.idle_anim(VISUAL_SPEC, rng)
                if anim and anim != last_anim:
                    try:
                        pet_forward('/anim?pet=main&surface=desktop', {'name': anim})
                        with LOCK:
                            STATE['last_anim'] = anim
                        sse_publish({'type': 'anim', 'name': anim, 'idle': True})
                        log.debug('视觉语言: idle %s', anim)
                    except Exception as e:
                        log.debug('视觉语言: idle 播放失败 %s', e)
                next_idle = now + rng.uniform(*pet_visual.idle_every(VISUAL_SPEC))
        except Exception as e:
            log.debug('visual idle 循环异常: %s', e)
            time.sleep(20)


def pet_forward(path_qs, payload=None, method='POST'):
    """转发到本机 dsh-pet standalone（读 runtime token，改 Host 头）"""
    rt = json.loads(PET_RUNTIME.read_text(encoding='utf-8'))
    pet_token = rt['token']
    url = f'http://{PET_HOST}:{PET_PORT}/dsh-pet-7340{path_qs}'
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={'Content-Type': 'application/json', 'x-dsh-pet-token': pet_token,
                 'Host': f'{PET_HOST}:{PET_PORT}'})
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def say_flow(text, mood='', anim=None, duration=None, image='', url='', source='api'):
    """说话主干（/say 路由与桥菜单共用，2026-11-07 PC 桌宠升级）。
    返回 (ok, result_dict, http_status)。成品原生睡眠会拿拒绝台词挡广播——
    撞上 sleeping 拒绝时先 /wake 再送（只有这一条路径会叫醒，绝不凭镜像乱拽）。"""
    text = str(text or '').strip()
    if not text:
        return False, {'error': 'empty-text'}, 400
    qs = '?pet=main&surface=desktop'
    mood = str(mood or '').strip()
    if not mood and VISUAL_SPEC and pet_visual:
        mood = pet_visual.detect_mood(text, VISUAL_SPEC)
    anim = anim or None
    if not anim and VISUAL_SPEC and pet_visual and mood:
        with LOCK:
            prev_anim = STATE.get('last_anim', '')
        anim = pet_visual.pick_anim(VISUAL_SPEC, mood, exclude={prev_anim})
    if duration is None and VISUAL_SPEC and pet_visual:
        duration = pet_visual.bubble_duration(text, VISUAL_SPEC)
    body = {'text': text, 'duration': int(duration or 12000)}
    img = str(image or '').strip()
    u = str(url or '').strip()
    if not img and not u and mood in MEME_MOODS and random.random() < MEME_PROB:
        img = _pick_meme(mood)
        if img:
            log.info('say auto-meme: %s (mood=%s)', img, mood)
    if img:
        body['image'] = img
    elif u:
        body['url'] = u
    with LOCK:
        STATE['last_activity'] = time.time()
        STATE['sleeping'] = False
        STATE['sleep_origin'] = ''
        if anim:
            STATE['last_anim'] = anim
        voice_flag = time.time() < STATE.get('voice_until', 0.0)
    r1 = pet_forward(f'/broadcast{qs}', body)
    if isinstance(r1, dict) and not r1.get('ok') and r1.get('reason') == 'sleeping':
        # 成品业务失败是 200+{ok:false}：真睡着（用户睡的）才走这里，叫醒后重送
        log.info('say 撞上原生睡眠 -> 先叫醒再送')
        try:
            pet_forward('/wake?pet=main')
        except Exception as e:
            log.debug('say 前唤醒失败: %s', e)
        time.sleep(0.6)
        r1 = pet_forward(f'/broadcast{qs}', body)
    r2 = pet_forward(f'/anim{qs}', {'name': anim}) if anim else None
    ev = {'type': 'say', 'text': text, 'anim': anim, 'mood': mood,
          'durationMs': int(duration or 12000), 'voice': voice_flag}
    if img:
        ev['image'] = img
    elif u:
        ev['url'] = u
    sse_publish(ev)
    return True, {'ok': True, 'broadcast': r1, 'anim': r2,
                  'mood': mood, 'pickedAnim': anim}, 200


def clip_once(seconds=12, target='', source='api'):
    """L3 看一段主干（/clip 路由与桥菜单「看一眼」共用）。返回 (status, resp_dict)。

    source 只做审计（api=她/平板显式请求，menu-click=用户右键手势）——issue #16 回归：
    非手势路径不得消耗 clip 闸，计数落在 /health 的 clip_triggers 里可对账。"""
    if not clip_recorder:
        return 503, {'ok': False, 'error': 'no-clip-recorder'}
    roll_day_local()
    with LOCK:
        trig = dict(STATE.get('clip_triggers') or {})
        trig[source] = trig.get(source, 0) + 1
        STATE['clip_triggers'] = trig
    try:
        seconds = max(8, min(15, int(seconds)))
    except Exception:
        seconds = 12
    target = str(target or '').strip()
    hwnd, title, proc = None, '', ''
    if desk_tree:
        wins = desk_tree.windows(app_type_of, blacklisted)['windows']
        if target:                       # 树 id 或标题/进程名子串
            tl = target.lower()
            cands = [w for w in wins
                     if w['id'] == tl or tl in w['title'].lower() or tl in w['proc']]
            if cands:
                hwnd = int(cands[0]['id'], 16)
                title, proc = cands[0]['title'], cands[0]['proc']
        elif wins:                       # 未指定 → 最大面积可见窗
            hwnd = int(wins[0]['id'], 16)
            title, proc = wins[0]['title'], wins[0]['proc']
    if hwnd is None:
        info = win_info()
        if not info:
            return 404, {'error': 'no-window'}
        hwnd, _, proc, title, _r, aumid = info
        if blacklisted(proc, title, aumid):
            return 403, {'error': 'blacklisted'}
    mp4 = clip_recorder.record_hwnd(hwnd, seconds=seconds)
    b64 = base64.b64encode(mp4).decode()
    try:
        resp, _base = post_eye('/clip', {
            'meta': {'window': ('录屏 · ' + title)[:80], 'proc': proc,
                     'app_type': app_type_of(proc), 'seconds': seconds},
            'video_b64': b64}, timeout=180)
    except RuntimeError as e:
        if 'clip-limit' in str(e):          # 时闸耗尽（30/小时）：显式状态，不靠日志考古
            with LOCK:
                STATE['clip_quota_exhausted'] = True
                STATE['clip_quota_day'] = time.strftime('%Y-%m-%d')
            log.warning('clip 时闸耗尽（%s 时段，30/小时，下小时自动恢复）', STATE.get('clip_quota_day'))
        raise
    # 成功一次即清耗尽标记（2026-11-07 起闸按小时，桥标记不能比服务器晚一整天）
    if isinstance(resp, dict) and resp.get('ok'):
        with LOCK:
            STATE['clip_quota_exhausted'] = False
    resp.setdefault('window', title[:60])
    return 200, resp


# ── PC 桌宠 × 平板 v9 设计同步（2026-11-07）：右键菜单接入 + 睡眠三端一致 ──
PET_MENU_OWNER = '肥鱼桥'


def pet_menu_register(sleeping):
    """把桥侧能力注册进成品右键菜单「更多」组（menu/item 整体覆盖，重复注册幂等）。
    睡着时「睡一下」换成「醒醒」。"""
    labels = ['看一眼'] + (['醒醒'] if sleeping else ['睡一下'])
    try:
        r = pet_forward('/menu/item', {'owner': PET_MENU_OWNER, 'labels': labels})
        ok = bool(r.get('ok'))
        if ok:
            log.info('菜单已注册: %s', labels)
        else:
            log.debug('menu/item 注册被拒: %s', r)
        return ok
    except Exception as e:
        log.debug('menu/item 注册失败: %s', e)
        return False


def pet_menu_dispatch(label):
    """「更多」菜单点击 → 桥动作（与平板长按菜单同语义；独立线程跑，不堵轮询）。"""
    log.info('菜单点击: %s', label)
    if label == '看一眼':
        pet_look_once('menu-click')
    elif label == '睡一下':
        try:
            r = pet_forward('/sleep?pet=main')
            if not r.get('ok'):
                log.info('睡一下被拒: %s', r.get('reason'))
        except Exception as e:
            log.info('睡一下失败: %s', e)
    elif label == '醒醒':
        try:
            pet_forward('/wake?pet=main')
        except Exception as e:
            log.info('醒醒失败: %s', e)


# ── issue #55：双击桌宠 = 看一眼（桥侧手势轮询，成品零改动）────────────────
LOOK_THROTTLE_SEC = 45.0     # 菜单/双击共享：45s 内只允许烧一次 clip 闸
DBL_MIN_GAP = 0.06           # 两拍最小间隔（防同一拍被数两次）
DBL_MAX_GAP = 0.40           # 上限；成品弹弓是 SLINGSHOT_DOUBLE_MS=200，靠「不拖」区分
DBL_SLOP_PX = 8              # 与成品 touch-press 的 SLOP_PX 同口径
PET_WIN_TITLE = 'dsh-pet 桌宠'
_PET_PIDS = {'at': 0.0, 'pids': set()}
PET_HIT = {'key': None, 'box': None, 'at': 0.0}   # box 是窗口内相对坐标
_PET_CAL = {'at': 0.0}                            # 标定节流：同一时刻只跑一个


def pet_look_once(source='menu-click'):
    """「看一眼」主干：右键菜单与双击手势共用（同一 45s 节流、同一 clip 闸）。
    source 进 clip_triggers 审计（menu-click / double-click）。
    返回 'ok' | 'throttled' | 'failed'。"""
    with LOCK:
        last = STATE.get('last_menu_look', 0.0)
    if time.time() - last < LOOK_THROTTLE_SEC:
        log.info('看一眼节流：%s 距上次触发不足 %.0fs，忽略', source, LOOK_THROTTLE_SEC)
        return 'throttled'
    with LOCK:
        STATE['last_menu_look'] = time.time()
    try:
        code, res = clip_once(seconds=10, source=source)
    except Exception as e:
        log.info('看一眼失败(%s): %s', source, e)
        return 'failed'
    note = str(res.get('note') or '').strip() if isinstance(res, dict) else ''
    if note and not note.rstrip().endswith('[SKIP]'):
        # 现场反应直接变成它自己的气泡（mood=curious）；QQ 侧由 notes.jsonl 消费线另走
        log.info('看一眼(%s) 出片: %s', source, note[:80])
        say_flow(note, mood='curious',
                 source=('menu-look' if source == 'menu-click' else source))
    elif isinstance(res, dict) and res.get('error'):
        log.info('看一眼被拒(%s): %s', code, res.get('error'))
    else:
        log.info('看一眼(%s): 无话（服务端 note 为空或 [SKIP]，按沉默哲学不弹泡）', source)
    # [SKIP]：画面没意思，不弹泡（与眼哨兵同一套沉默哲学）
    return 'ok'


def _in_box(x, y, box):
    if not box:
        return False
    bx, by, bw, bh = box
    return bx <= x <= bx + bw and by <= y <= by + bh


def dblclick_hit(a, b, box):
    """纯函数（#55 验收①）：两拍 tap 是否构成「双击看一眼」。
    tap = (t, x, y, clean)；clean=False = 按下期间位移 >SLOP（拖拽/弹弓，不作数）。"""
    if not a or not b:
        return False
    ta, xa, ya, ca = a
    tb, xb, yb, cb = b
    if not (ca and cb):
        return False
    if not (DBL_MIN_GAP <= (tb - ta) <= DBL_MAX_GAP):
        return False
    if abs(xb - xa) > DBL_SLOP_PX or abs(yb - ya) > DBL_SLOP_PX:
        return False
    return _in_box(xa, ya, box) and _in_box(xb, yb, box)


def _pet_pids():
    """桌宠 Electron 主进程 pid（cmdline 含 electron-helper），30s 缓存。"""
    if _PET_PIDS['pids'] and time.time() - _PET_PIDS['at'] < 30:
        return _PET_PIDS['pids']
    pids = set()
    try:
        for p in psutil.process_iter(['pid', 'cmdline']):
            cl = ' '.join(p.info.get('cmdline') or [])
            if 'electron-helper' in cl:
                pids.add(p.info['pid'])
    except Exception:
        pass
    _PET_PIDS['at'], _PET_PIDS['pids'] = time.time(), pids
    return pids


def pet_window():
    """桌宠（桌面模式）窗口 → (hwnd, (x, y, w, h))；找不到返回 None。

    #55 事实：整个窗口是点击穿透的（WindowFromPoint 只会回背后的窗口），所以命中只能
    靠矩形；窗口随鱼走动，每轮实时读、不缓存坐标。"""
    pids = _pet_pids()
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, l):
        try:
            if not user32.IsWindowVisible(h):
                return True
            n = user32.GetWindowTextLengthW(h)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if not (buf.value or '').startswith(PET_WIN_TITLE):
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pids and pid.value not in pids:
                return True
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w > 80 and hh > 80:                 # 最小化在 -32000，尺寸也异常
                found.append((h, (r.left, r.top, w, hh)))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        return None
    return found[0] if found else None


def _content_bbox(hwnd, w, h, step=4, thr=24):
    """PrintWindow 画一遍，求非空像素 bbox（相对窗口）。失败返回 None。"""
    img = desk_tree._print_window_rgb(hwnd, w, h)
    if img is None:
        return None
    px = img.load()
    xs, ys = [], []
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y][:3]
            if r + g + b > thr:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _calibrate_async(hwnd, rect):
    """后台标定命中框（PrintWindow 可能阻塞目标 UI 线程 → 必须离开轮询线程）。
    同尺寸多次标定取并集：不同姿态只增不减。"""
    if time.time() - _PET_CAL['at'] < 5:
        return
    _PET_CAL['at'] = time.time()
    x, y, w, h = rect

    def run():
        try:
            bb = _content_bbox(hwnd, w, h)
            if not bb:
                log.debug('命中框标定：无内容像素，保持/降级粗框')
                return
            x0, y0, x1, y1 = bb
            pad = max(int((x1 - x0) * 0.15), int((y1 - y0) * 0.15))
            x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
            x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
            box = (x0, y0, x1 - x0, y1 - y0)
            key = (w, h)
            with LOCK:
                old = PET_HIT['box'] if PET_HIT['key'] == key else None
                if old:
                    ox0, oy0 = min(old[0], box[0]), min(old[1], box[1])
                    ox1 = max(old[0] + old[2], box[0] + box[2])
                    oy1 = max(old[1] + old[3], box[1] + box[3])
                    box = (ox0, oy0, ox1 - ox0, oy1 - oy0)
                PET_HIT['key'], PET_HIT['box'], PET_HIT['at'] = key, box, time.time()
            log.debug('命中框标定: 窗口 %dx%d → 相对框 %s', w, h, box)
        except Exception as e:
            log.debug('命中框标定异常: %s', e)

    threading.Thread(target=run, daemon=True, name='PetHitCal').start()


def pet_hit_box(hwnd, rect):
    """命中框（屏幕坐标）：标定优先；44s 内复用；未标定/失败降级窗口下部 55% 粗框
    （#55 拍板②：fail-open 到粗框，不砍功能）。永不阻塞调用方。"""
    x, y, w, h = rect
    with LOCK:
        box = PET_HIT['box'] if PET_HIT['key'] == (w, h) else None
    if box and time.time() - PET_HIT['at'] < 44:
        return (x + box[0], y + box[1], box[2], box[3])
    if desk_tree is not None and hasattr(desk_tree, '_print_window_rgb'):
        _calibrate_async(hwnd, rect)
    if box:
        return (x + box[0], y + box[1], box[2], box[3])
    top = int(h * 0.45)                      # 粗框兜底：窗口上半是气泡/chat 区
    return (x, y + top, w, h - top)


def _on_double_click(tap):
    """双击命中后的动作（#55 拍板：睡着不打扰 / 45s 共享节流 / 用户手势可烧闸）。"""
    with LOCK:
        sleeping = bool(STATE.get('sleeping'))
        if sleeping:
            STATE['dblclick_sleep'] = int(STATE.get('dblclick_sleep', 0)) + 1
    if sleeping:
        log.info('双击：鱼在睡，按拍板不打扰（只记 dblclick_sleep）')
        return

    def run():
        r = pet_look_once('double-click')
        with LOCK:
            if r == 'ok':
                STATE['dblclick_hits'] = int(STATE.get('dblclick_hits', 0)) + 1
            elif r == 'throttled':
                STATE['dblclick_throttled'] = int(STATE.get('dblclick_throttled', 0)) + 1
    log.info('双击命中 (%.0f,%.0f) → 看一眼', tap[1], tap[2])
    threading.Thread(target=run, daemon=True, name='DblClickLook').start()


def pet_gesture_loop():
    """issue #55：双击桌宠 = 看一眼。

    25ms 轮询左键与光标（不用全局钩子：慢回调会被系统摘钩、杀软敏感）。
    ⚠️ 成品把「200ms 内连按 + 拖」当弹弓（SLINGSHOT_DOUBLE_MS）：我们只在**没进入
    拖拽**时触发，按下期间位移 >8px 立即作废这一拍。"""
    if os.environ.get('PET_GESTURE', '1') == '0':
        log.info('双击手势已关闭（PET_GESTURE=0）')
        return
    log.info('双击手势线程启动（双击桌宠 = 看一眼；PET_GESTURE=0 可关）')
    VK_LBUTTON = 0x01
    prev, press, last_tap = False, None, None
    while True:
        try:
            down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            if down and not prev:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                press = {'t': time.time(), 'x': pt.x, 'y': pt.y, 'clean': True}
            elif down and press is not None:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                if abs(pt.x - press['x']) > DBL_SLOP_PX or abs(pt.y - press['y']) > DBL_SLOP_PX:
                    press['clean'] = False       # 拖了/弹弓：这一拍不作数
            elif not down and prev and press is not None:
                tap = (press['t'], press['x'], press['y'], press['clean'])
                press = None
                win = pet_window()
                if win:
                    hwnd, rect = win
                    box = pet_hit_box(hwnd, rect)
                    if dblclick_hit(last_tap, tap, box):
                        last_tap = None
                        _on_double_click(tap)
                    elif tap[3] and _in_box(tap[1], tap[2], box):
                        last_tap = tap
                    else:
                        last_tap = None
            prev = down
        except Exception as e:
            log.debug('手势轮询异常: %s', e)
            prev, press, last_tap = False, None, None
        time.sleep(0.025)


def _sync_native_sleep(native):
    """把成品原生睡眠落进桥 STATE（含基线）：有变化 → SSE 通知平板 + 换菜单项。
    返回是否有变化。桥自己的夜觉是「假睡」（成品仍醒着），origin=bridge-night 不受影响。"""
    with LOCK:
        cur = bool(STATE.get('sleeping'))
        origin = STATE.get('sleep_origin', '')
        if native and not cur:
            STATE['sleeping'] = True
            STATE['sleep_origin'] = 'native'
            changed = True
        elif not native and cur and origin == 'native':
            STATE['sleeping'] = False
            STATE['sleep_origin'] = ''
            STATE['last_activity'] = time.time()
            changed = True
        else:
            changed = False
    if changed:
        sse_publish({'type': 'anim', 'name': '', 'idle': True,
                     'state': 'sleeping' if native else 'awake'})
        pet_menu_register(native)          # 菜单项「睡一下/醒醒」跟着状态换
        log.info('原生睡眠同步: %s（平板跟随）', '入睡' if native else '醒来')
    return changed


def pet_state_loop():
    """成品 /state 长轮询（counter 增量）：把原生睡眠同步给桥 STATE + SSE（平板跟随）。
    基线种子也走同一份判定——桥重启后立即把真实现状推给平板与菜单。"""
    log.info('成品状态同步线程启动（睡眠三端一致）')
    counter = None
    while True:
        try:
            if counter is None:
                r = pet_forward('/state?pet=main', method='GET')
                counter = r.get('counter') or None
                d = (((r.get('pets') or {}).get('main') or {}).get('sleep') or {}).get('data') or {}
                if 'sleeping' in d:
                    _sync_native_sleep(bool(d.get('sleeping')))
                time.sleep(2)
                continue
            r = pet_forward(f'/state?since={counter}&timeout=15000', method='GET')
            counter = r.get('counter') or None
            d = (((r.get('pets') or {}).get('main') or {}).get('sleep') or {}).get('data') or {}
            if 'sleeping' in d:
                _sync_native_sleep(bool(d.get('sleeping')))
        except Exception as e:
            counter = None
            log.debug('状态同步异常（standalone 可能重启）: %s', e)
            time.sleep(10)


def pet_menu_loop():
    """「更多」菜单点击长轮询：注册保活（standalone 重启后 registry 清空 → unknown-owner 自愈）。
    首次注册不读桥 STATE（有启动竞态），直接读成品 /state 的真实睡眠。"""
    try:
        r = pet_forward('/state?pet=main', method='GET')
        d = (((r.get('pets') or {}).get('main') or {}).get('sleep') or {}).get('data') or {}
        slp = bool(d.get('sleeping'))
    except Exception:
        with LOCK:
            slp = bool(STATE.get('sleeping'))
    ok = pet_menu_register(slp)
    while True:
        try:
            if not ok:
                time.sleep(10)
                with LOCK:
                    slp = bool(STATE.get('sleeping'))
                ok = pet_menu_register(slp)
                continue
            r = pet_forward(f'/menu/events?owner={urllib.parse.quote(PET_MENU_OWNER)}&wait=15000',
                            method='GET')
            if r.get('ok'):
                evs = r.get('events') or {}
                # ⚠️ events 是 {label: petId|null} 全量快照：value=null=没人点（poll 每次都回），
                # 只有 value 非空才是真点击（menu/click 写入的 petId）。
                if isinstance(evs, dict):
                    items = evs.items()
                else:                          # 老形状兜底：[{'label':...}] 数组
                    items = [((e or {}).get('label'), True) if isinstance(e, dict) else (e, True)
                             for e in evs]
                for label, pet in items:
                    label = str(label or '')
                    if label and pet:
                        threading.Thread(target=pet_menu_dispatch, args=(label,),
                                         daemon=True, name='MenuClick').start()
            else:
                ok = False                     # unknown-owner 等 → 重注册
        except Exception as e:
            log.debug('菜单轮询异常: %s', e)
            ok = False
            time.sleep(10)


# ── 平板桌宠：SSE 事件广播 + 网页 ──────────────────────────────
SSE_SUBS = []
SSE_SEQ = 0            # 事件序号：平板重连时带 since=N，桥补发断线期间漏掉的事件
SSE_HIST = []          # 最近 50 条 (含 seq)


def sse_publish(obj):
    global SSE_SEQ
    SSE_SEQ += 1
    item = dict(obj)
    item['seq'] = SSE_SEQ
    SSE_HIST.append(item)
    if len(SSE_HIST) > 50:
        del SSE_HIST[:len(SSE_HIST) - 50]
    for q in list(SSE_SUBS):
        try:
            q.put_nowait(item)
        except Exception:
            pass


PET_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>肥鱼 · 平板桌宠</title>
<style>
  html,body{margin:0;height:100%;background:#0b1220;color:#dbe6ff;
    font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;overflow:hidden}
  #stage{position:relative;height:100%;display:flex;flex-direction:column;
    align-items:center;justify-content:flex-end;padding-bottom:6vh}
  #bubble{max-width:86vw;background:rgba(20,32,58,.93);border:1px solid #2a3f6e;
    border-radius:18px;padding:14px 18px;font-size:18px;line-height:1.5;
    margin-bottom:18px;opacity:0;transition:opacity .4s;box-shadow:0 6px 24px rgba(0,0,0,.45)}
  #bubble.show{opacity:1}
  #bubble{transform:translateY(6px) scale(.98);transition:opacity .4s,transform .35s}
  #bubble.show{transform:translateY(0) scale(1)}
  #fish{width:min(44vw,300px)}
  video{width:100%;display:block}
  #hist{position:fixed;top:8px;left:10px;right:10px;font-size:12px;color:#7f92c0;
    max-height:16vh;overflow:hidden;display:flex;flex-direction:column-reverse;gap:2px}
  #offline{position:fixed;top:8px;right:10px;font-size:12px;color:#e2564f;display:none}
</style></head><body>
<div id="hist"></div><div id="offline">连接断开，重连中…</div>
<div id="stage"><div id="bubble"></div><div id="fish"><video id="v" autoplay loop muted playsinline></video></div></div>
<script>
const q = new URLSearchParams(location.search);
const fish = document.getElementById('v'), bubble = document.getElementById('bubble'),
      hist = document.getElementById('hist'), off = document.getElementById('offline');
let SPEC = null;
fetch('/pet-config?token=' + q.get('token')).then(r=>r.json())
  .then(d=>{ if(d && d.ok) SPEC = d.spec; }).catch(()=>{});
function play(name){ if(!name) return;
  fish.src = '/anim-file?token=' + q.get('token') + '&name=' + encodeURIComponent(name);
  fish.onerror = ()=>{ fish.onerror = null; fish.src = ''; };
}
function styleFor(mood){
  const st = (SPEC && SPEC.bubble && SPEC.bubble.styles) || {};
  return st[mood] || st.calm || null;
}
function animFor(mood){
  const m = (SPEC && SPEC.moods && SPEC.moods[mood]) || null;
  if (!m || !m.anims || !m.anims.length) return '';
  return m.anims[Math.floor(Math.random()*m.anims.length)];
}
let hideT;
function say(text, anim, mood, durationMs){
  const s = styleFor(mood);
  if (s){ bubble.style.borderColor = s.accent; bubble.style.background = s.bg; bubble.style.color = s.fg; }
  bubble.textContent = text; bubble.classList.add('show');
  clearTimeout(hideT);
  hideT = setTimeout(()=>bubble.classList.remove('show'), durationMs || 12000);
  const d = document.createElement('div'); d.textContent = '• ' + text; hist.appendChild(d);
  if (hist.children.length > 5) hist.removeChild(hist.firstChild);
  if (!anim && mood) anim = animFor(mood);
  if (anim) play(anim);
}
play('待机呼吸休闲');
function connect(){
  const es = new EventSource('/events?token=' + q.get('token'));
  es.onopen = ()=>{ off.style.display = 'none'; };
  es.onmessage = (e)=>{ try{ const m = JSON.parse(e.data);
    if(m.type==='say') say(m.text, m.anim, m.mood, m.durationMs);
    else if(m.type==='anim') play(m.name);}catch(_){} };
  es.onerror = ()=>{ off.style.display = 'block'; es.close(); setTimeout(connect, 4000); };
}
connect();
</script></body></html>"""


# ── 平板桌宠：动作池 / 表情包 / 点击音效（与 PC 桌宠同一份配置）────────────
POSES_CACHE = {'at': 0.0, 'data': None}


def pet_anims_payload():
    """把 dsh-pet 成品配置里平板需要的部分抽成一份小 JSON（60s 缓存）。

    平板不 import PC 的 TS 配置，所以动作池、随机链权重、睡眠三段与台词、物理参数
    全部由这里下发；PC 端改配置后平板重启（或 60s 后重拉）即同步。
    """
    now = time.time()
    if POSES_CACHE['data'] is not None and now - POSES_CACHE['at'] < 60:
        return POSES_CACHE['data']
    cfg = pet_forward('/config', method='GET')
    main = (cfg or {}).get('main') or {}
    a = main.get('animations') or {}

    def strs(v):
        return [x for x in (v or []) if isinstance(x, str) and x]

    cats = []
    for c in (a.get('categories') or []):
        if not isinstance(c, dict):
            continue
        acts = strs(c.get('actions'))
        if acts:
            cats.append({'id': str(c.get('id') or ''), 'weight': c.get('weight'),
                         'noMirror': c.get('noMirror') is True, 'actions': acts})
    events = {}
    for key, pool in (a.get('events') or {}).items():
        names = []
        for slot in (pool or []):
            if isinstance(slot, str):
                names.append(slot)
            elif isinstance(slot, list):
                names.extend(strs(slot))
        if names:
            events[str(key)] = names
    sleeps = []
    for s in (a.get('sleep') or []):
        if not isinstance(s, dict):
            continue
        one = {k: strs(s.get(k)) for k in ('start', 'loop', 'end')}
        if one['start'] and one['loop'] and one['end']:
            sleeps.append(one)
    moves = []
    for m in (((a.get('moves') or {}).get('actions')) or []):
        if isinstance(m, dict) and isinstance(m.get('name'), str) and m['name']:
            moves.append({'name': m['name'], 'params': m.get('params') or {}})
    pets = main.get('pets') or []
    out = {
        'idle': strs(a.get('idle')), 'turn': strs(a.get('turn')), 'drag': strs(a.get('drag')),
        'clicks': strs(a.get('clicks')), 'moves': moves, 'categories': cats,
        'events': events, 'sleep': sleeps,
        'weights': main.get('animationWeights') or {'idle': 10, 'turn': 5, 'move': 5},
        'texts': {
            'start': strs(main.get('sleepStartTexts')),
            'end': strs(main.get('sleepEndTexts')),
            'refuse': strs(main.get('sleepRefuseTexts')),
        },
        'clickSound': main.get('clickSoundEnabled') is True,
        'memes': sorted((main.get('memes') or {}).keys()),
        'size': (pets[0].get('size') if pets and isinstance(pets[0], dict) else None),
        'physics': main.get('physics') or {},
    }
    POSES_CACHE.update(at=now, data=out)
    return out


# ── 平板原生桌宠：动画 WebP 现转（ArkUI Image 播不了 webm，但能播动画 WebP）──
WEBP_CACHE = BASE_DIR / 'assets' / 'webp_cache'
BBOX_FILE = BASE_DIR / 'assets' / 'webp_bboxes.json'
FFMPEG_BIN = os.environ.get('FEIYU_FFMPEG') or \
    r'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'


def _load_bboxes():
    try:
        return json.loads(BBOX_FILE.read_text('utf-8'))
    except Exception:
        return {}


BBOXES = _load_bboxes()


def anim_webp_path(name):
    """webm → 动画 WebP（带 alpha、按包围盒裁透明边、20fps、宽 400），落盘缓存。

    ⚠️ 平板实测：ArkUI 的「动图」解码器会把每一帧叠在前一帧的画布上
    （无视 WebP 的 no-blend/dispose 语义），透明动图必出永久重影。
    所以平板端已改用 /anim-meta + /anim-frame 的静帧序列 + ImageAnimator，
    这个接口只留给网页/PC 端用。
    """
    if not name:
        return None
    root = (BASE_DIR / 'assets' / 'webm').resolve()
    src = (root / (name + '.webm')).resolve()
    if not str(src).startswith(str(root)) or not src.exists():
        return None
    WEBP_CACHE.mkdir(parents=True, exist_ok=True)
    dst = WEBP_CACHE / (name + '.webp')
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    vf = 'fps=20'
    bb = BBOXES.get(name)
    if bb and len(bb) == 4:
        x, y, w, h = [int(v) for v in bb]
        vf += f',crop={w}:{h}:{x}:{y}'
    vf += ',scale=400:-2:flags=lanczos,format=yuva420p'
    tmp = dst.with_name(dst.stem + '.tmp.webp')
    subprocess.run(
        [FFMPEG_BIN, '-y', '-v', 'error', '-c:v', 'libvpx-vp9', '-i', str(src),
         '-vf', vf, '-c:v', 'libwebp', '-lossless', '0', '-q:v', '70',
         '-loop', '0', '-an', str(tmp)],
        timeout=120, creationflags=0x08000000, check=True)
    os.replace(tmp, dst)
    return dst


# ── 帧序列（平板 ArkUI ImageAnimator 用，规避动图解码器的重影 bug）──
FRAMES_CACHE = BASE_DIR / 'assets' / 'frames_cache'
FRAME_FPS = 15
FRAME_W = 440
_FRAMES_LOCKS = {}
_FRAMES_LOCK_GUARD = threading.Lock()


def _frames_lock(name):
    with _FRAMES_LOCK_GUARD:
        lk = _FRAMES_LOCKS.get(name)
        if lk is None:
            lk = threading.Lock()
            _FRAMES_LOCKS[name] = lk
        return lk


def anim_frames(name):
    """webm → 帧序列（webp 静帧）落盘缓存，返回 (dir, meta) 或 (None, None)。"""
    if not name:
        return None, None
    root = (BASE_DIR / 'assets' / 'webm').resolve()
    src = (root / (name + '.webm')).resolve()
    if not str(src).startswith(str(root)) or not src.exists():
        return None, None
    d = FRAMES_CACHE / name
    meta_file = d / 'meta.json'
    with _frames_lock(name):
        if meta_file.exists() and meta_file.stat().st_mtime >= src.stat().st_mtime:
            try:
                meta = json.loads(meta_file.read_text('utf-8'))
                # 尺寸/帧率参数变了要重转（缓存键只有源文件 mtime，不够）
                if meta.get('w') == FRAME_W and meta.get('fps') == FRAME_FPS:
                    return d, meta
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        vf = f'fps={FRAME_FPS}'
        bb = BBOXES.get(name)
        if bb and len(bb) == 4:
            x, y, w, h = [int(v) for v in bb]
            vf += f',crop={w}:{h}:{x}:{y}'
            out_h = max(2, int(round(h * FRAME_W / w / 2.0)) * 2)
        else:
            out_h = int(round(360 * FRAME_W / 640 / 2.0)) * 2
        vf += f',scale={FRAME_W}:{out_h}:flags=lanczos'
        subprocess.run(
            [FFMPEG_BIN, '-y', '-v', 'error', '-c:v', 'libvpx-vp9', '-i', str(src),
             '-vf', vf, '-c:v', 'libwebp', '-lossless', '0', '-q:v', '80', '-an',
             str(d / 'f%04d.webp')],
            timeout=300, creationflags=0x08000000, check=True)
        files = sorted(d.glob('f*.webp'))
        if not files:
            return None, None
        meta = {'count': len(files), 'fps': FRAME_FPS, 'w': FRAME_W, 'h': out_h}
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        # 帧序列缓存按 LRU 保留最近 30 个动画：109 个全转要 ~500MB，平板只会点播其中一部分
        try:
            dirs = [p for p in FRAMES_CACHE.iterdir() if p.is_dir() and p.name != name]
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in dirs[30:]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass
        return d, meta



# ---------------------------------------------------------------- 限流（per-client 令牌桶）
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS = {}
_RATE_LAST_LOG = {}

# ---- 断路器（只报警不节流，2026-11-08 tablet-pet-deploy 提议 / 主持实装）----
# 主人口令：除鱼眼视频外不限流。但"不限流"不等于"无声音"——某客户端真跑飞
# （副作用请求 >FEIYU_RUNAWAY_RPS 次/秒，正常永远碰不到）时记 RUNAWAY。
# 口径（tablet-pet-deploy 复核 11-08）：只数**会产生副作用**的请求（非 GET，
# 或 /clip）——GET 渲染/长轮询（/state /frames /thumb /events）是合法高频，数它=误报源。
# 必达=本地 JSONL 落盘（零依赖）；通知（fish-wake）是尽力而为，发不出去也不丢证据。
_RUNAWAY_LOCK = threading.Lock()
_RUNAWAY_WINDOW = {}   # ip -> [window_start, count, {path: n}]   （1 秒滑窗，含 per-path 计数）
_RUNAWAY_STREAK = {}   # ip -> 连续超阈秒数（2026-11-08 tablet-pet-deploy 判据：单秒样本易是 GC/重连抖动）
_RUNAWAY_LAST = {}     # ip -> 上次通知时刻
_RUNAWAY_RPS = float(os.environ.get('FEIYU_RUNAWAY_RPS', '30'))
_RUNAWAY_STREAK_MIN = int(os.environ.get('FEIYU_RUNAWAY_STREAK', '3'))
_RUNAWAY_COOLDOWN = float(os.environ.get('FEIYU_RUNAWAY_COOLDOWN', '60'))
# ⭐启动宽限窗（tablet-pet-deploy 11-08 建议）：桥重启后客户端重连/补发本来就会在 1~2 分钟内堆一波，
#   实测两次 RUNAWAY 都落在 listening 后 +77s/+86s —— 180s 覆盖得住。宽限期内不叫人，但证据照落 JSONL。
_RUNAWAY_GRACE = float(os.environ.get('FEIYU_RUNAWAY_GRACE', '180'))
_RUNAWAY_T0 = time.time()
_RUNAWAY_LOG = os.path.join(os.path.expanduser('~'), '.feiyu', 'logs', 'runaway.jsonl')


def _runaway_mark(ip, side_effect, path=''):
    if not side_effect:
        return                                # GET 渲染/长轮询不计数（合法高频）
    now = time.time()
    with _RUNAWAY_LOCK:
        w = _RUNAWAY_WINDOW.get(ip)
        if w is None or now - w[0] >= 1.0:
            if w is not None and now - w[0] >= 1.0:
                # 窗口闭合：结算上一秒 —— 连续超阈计数（三分判据：峰值+持续秒数+峰值个数）
                if w[1] >= _RUNAWAY_RPS:
                    _RUNAWAY_STREAK[ip] = _RUNAWAY_STREAK.get(ip, 0) + 1
                else:
                    _RUNAWAY_STREAK[ip] = 0
                closed = (w[1], dict(w[2]), _RUNAWAY_STREAK[ip])
            else:
                closed = None
            _RUNAWAY_WINDOW[ip] = [now, 0, {}]
        else:
            closed = None
        w = _RUNAWAY_WINDOW[ip]
        w[1] += 1
        p = (path or '?').split('?')[0]
        w[2][p] = w[2].get(p, 0) + 1
        if closed is None or closed[0] < _RUNAWAY_RPS or closed[2] < _RUNAWAY_STREAK_MIN:
            return                            # 未成峰 / 未连续 3 秒 → 只累计
        if now - _RUNAWAY_T0 < _RUNAWAY_GRACE:
            grace = True                      # 启动宽限窗内：不叫人，证据照落
        elif now - _RUNAWAY_LAST.get(ip, 0) < _RUNAWAY_COOLDOWN:
            return
        else:
            grace = False
        _RUNAWAY_LAST[ip] = now
        peak, paths, streak = closed
        top = sorted(paths.items(), key=lambda kv: -kv[1])[:3]
    # ① 必达：本地 JSONL（零依赖，链路全死也落得下）
    try:
        os.makedirs(os.path.dirname(_RUNAWAY_LOG), exist_ok=True)
        with open(_RUNAWAY_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': round(now, 3), 'ip': ip, 'peak_per_sec': peak,
                                'streak': streak, 'grace': grace, 'top_paths': top},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass
    # ② 日志
    try:
        log.warning('RUNAWAY client=%s peak=%d/s streak=%ds top=%s%s (alert only, no throttle)',
                    ip, peak, streak, top, ' [启动宽限窗内,仅落盘]' if grace else '')
    except Exception:
        pass
    if grace:
        return
    # ③ 尽力而为：fish-wake 叫主持（PC 本机自打 tailnet IP 走本地路由，不经 DERP；失败不影响①②）
    def _fire():
        try:
            req = urllib.request.Request(
                'http://100.98.120.55:17846/wake',
                data=json.dumps({'from': 'pc_body',
                                 'text': 'pc_body 断路器：client %s 峰值 %d 副作用请求/秒 × 连续 %d 秒，top=%s（证据 ~/.feiyu/logs/runaway.jsonl）' % (ip, peak, streak, top)}).encode('utf-8'),
                headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
    threading.Thread(target=_fire, daemon=True).start()


def _rate_take(key, per_sec, burst):
    """令牌桶。True=放行，False=超限。per_sec=令牌/秒，burst=桶容量。"""
    now = time.time()
    with _RATE_LOCK:
        tok, last = _RATE_BUCKETS.get(key, (burst, now))
        tok = min(burst, tok + (now - last) * per_sec)
        if tok < 1.0:
            _RATE_BUCKETS[key] = (tok, now)
            return False
        _RATE_BUCKETS[key] = (tok - 1.0, now)
        return True


class Bridge(BaseHTTPRequestHandler):
    # 2026-11-07：连接复用。默认 HTTP/1.0 会每请求关连接 → TIME_WAIT 堆积（实测 507）。
    # 前提是所有响应带 Content-Length（已核对 8/8）；timeout 防 keep-alive 线程堆积。
    protocol_version = 'HTTP/1.1'
    timeout = 30
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get('Authorization', '') == 'Bearer ' + TOKEN

    def _qtoken_ok(self, params):
        """平板桌宠网页用的 query token（仅绑 Tailscale 网卡，不出公网）"""
        return (params.get('token') or [''])[0] == TOKEN

    def _rate_gate(self, path):
        """per-client 令牌桶闸。返回 True 表示已回 429，调用方直接 return。
        2026-11-08 主人拍板：限流只对鱼眼视频（/clip）有效，其余端点全部不限——
        普通桶(6/s)曾把桌宠动画/表情包打残（2026-11-07 19:58–20:04 连续 429 15 次），
        且鱼鱼高频问桥属正常工作节奏，不该拦。GET 本就全免限；/events SSE 白名单。"""
        ip = self.client_address[0] if self.client_address else '?'
        heavy = any(path.startswith(p) for p in RATE_HEAVY)
        # 断路器只数副作用请求（非 GET 或 /clip）：GET 渲染/长轮询是合法高频
        _runaway_mark(ip, self.command != 'GET' or heavy, path)
        if self.command == 'GET':
            return False
        if any(path.startswith(p) for p in RATE_EXEMPT):
            return False
        if not heavy:
            return False                     # 非 /clip 一律放行（普通桶已按主人指示停用）
        per_sec, burst = RATE_HEAVY_PER_SEC, RATE_HEAVY_BURST
        if _rate_take((ip, 'h'), per_sec, burst):
            return False
        # 429 前必须把 POST body 读掉：否则残留字节会让 keep-alive 的下一个请求
        # 解析错位（实测表现为成片 501 Unsupported method）。
        try:
            _n = int(self.headers.get('Content-Length', '0') or 0)
            if _n:
                self.rfile.read(_n)
        except Exception:
            self.close_connection = True
        now = time.time()
        if now - _RATE_LAST_LOG.get(ip, 0) > 10:      # 日志限频：每 IP 每 10s 一条
            _RATE_LAST_LOG[ip] = now
            try:
                log.warning('rate-limit 429 client=%s path=%s per_sec=%s', ip, path, per_sec)
            except Exception:
                pass
        self._json({'error': 'rate-limit', 'path': path,
                    'perSec': per_sec, 'burst': burst}, 429)
        return True

    def do_GET(self):
        path, _, qs = self.path.partition('?')
        if self._rate_gate(path):
            return
        params = urllib.parse.parse_qs(qs)
        # ── 平板桌宠网页三件套（query token，仅 Tailscale 内网可达）──
        if path == '/pet':
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            body = PET_PAGE.encode('utf-8')
            # 悬浮窗变体：透明背景 + 无历史栏（原生 HAP 的 Web 组件用）
            if (params.get('surface') or [''])[0] == 'overlay':
                body = (PET_PAGE
                        .replace('background:#0b1220', 'background:transparent')
                        # ⚠️ 只隐藏 #hist 不能删元素——JS 还往里 append，删了 say() 会抛异常
                        .replace('#hist{position:fixed', '#hist{display:none;position:fixed')
                        .replace('padding-bottom:6vh', 'padding-bottom:1vh')
                        .replace('#bubble{max-width:86vw', '#bubble{max-width:96vw;font-size:14px;padding:8px 12px')
                        .replace('#fish{width:min(44vw,300px)', '#fish{width:min(92vw,360px)')
                        ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # ── 帧落盘取图（2026-11-09 主人派活：鱼鱼要把"看到的图"发出来）──────
        # 三个端点，全部 Bearer 或 query token 二选一（与 /tree 同口径）：
        #   GET /frames/list?limit=20&since=<epoch>   元数据列表（新的在前）
        #   GET /frames/latest                       最新一张的元数据（含绝对路径）
        #   GET /frames/<文件名>                      直接取 JPEG 本体
        if path == '/frames/list' or path == '/frames/latest':
            if not (self._qtoken_ok(params) or self._auth()):
                return self._json({'error': 'unauthorized'}, 401)
            try:
                limit = int((params.get('limit') or ['20'])[0])
            except Exception:
                limit = 20
            limit = max(1, min(limit, FRAMES_KEEP))
            since = None
            try:
                if params.get('since'):
                    since = float((params.get('since') or [''])[0])
            except Exception:
                since = None
            items = list_frames(limit=limit, since=since)
            if path == '/frames/latest':
                return self._json({'ok': True, 'frame': items[0] if items else None,
                                   'dir': str(FRAMES_DIR), 'keep': FRAMES_KEEP})
            return self._json({'ok': True, 'count': len(items), 'frames': items,
                               'dir': str(FRAMES_DIR), 'keep': FRAMES_KEEP})
        if path.startswith('/frames/'):
            if not (self._qtoken_ok(params) or self._auth()):
                return self._json({'error': 'unauthorized'}, 401)
            # 防目录穿越：只接受纯文件名（不含 / \ ..）
            name = urllib.parse.unquote(path[len('/frames/'):])
            if (not name) or ('/' in name) or ('\\' in name) or ('..' in name):
                return self._json({'error': 'bad-name'}, 400)
            fp = FRAMES_DIR / name
            try:
                if not fp.exists() or not fp.is_file():
                    return self._json({'error': 'no-such-frame', 'name': name}, 404)
                jb = fp.read_bytes()
            except Exception as e:
                return self._json({'error': str(e)[:150]}, 500)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(jb)))
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            self.wfile.write(jb)
            return
        if path == '/tree':
            # 桌面结构树（docs/11 §3）：窗口元数据。黑名单已在 desk_tree 里剔除。
            # 平板用 query token，服务端脚本可用 Bearer——两者任一即可。
            if not (self._qtoken_ok(params) or self._auth()):
                return self._json({'error': 'unauthorized'}, 401)
            if not desk_tree:
                return self._json({'ok': False, 'error': 'no-desk-tree'}, 503)
            try:
                return self._json(desk_tree.windows(app_type_of, blacklisted))
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)[:150]}, 500)
        if path.startswith('/thumb/'):
            # 窗口缩略图（240w JPEG，10s 缓存）：/thumb/<tree 里的 id>
            if not (self._qtoken_ok(params) or self._auth()):
                return self._json({'error': 'unauthorized'}, 401)
            if not desk_tree:
                return self._json({'ok': False, 'error': 'no-desk-tree'}, 503)
            try:
                jb, meta = desk_tree.thumb_ex(path.split('/', 2)[-1].strip())
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)[:150]}, 500)
            if not jb:
                return self._json({'error': 'no-such-window', 'thumb': meta}, 404)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(jb)))
            self.send_header('Cache-Control', 'max-age=5')
            # issue #28：抓图来源可观测（printwindow=目标自渲染；screen-occluded=兜底，可能抓到遮挡者）
            self.send_header('X-Thumb-Mode', str(meta.get('mode', '')))
            self.send_header('X-Thumb-Occluded', '1' if meta.get('occluded') else '0')
            self.end_headers()
            self.wfile.write(jb)
            return
        if path == '/pet-config':
            # 桌宠视觉语言规格：平板/原生壳拉同一份（mood→动作、气泡 token、idle/睡眠）
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            if not VISUAL_SPEC:
                return self._json({'ok': False, 'error': 'no-visual-spec'}, 503)
            return self._json({'ok': True, 'spec': VISUAL_SPEC})
        if path == '/pet-anims':
            # 平板桌宠的动作池/随机链权重/睡眠台词：与 PC 桌宠同一份成品配置
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            try:
                return self._json(pet_anims_payload())
            except Exception as e:
                return self._json({'error': 'config-fail', 'detail': str(e)[:150]}, 502)
        if path in ('/meme', '/click-sound'):
            # 表情包配图 / 点击音效：平板要播放的素材，直接从 PC 端 assets 取
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            if path == '/meme':
                name = (params.get('name') or [''])[0].strip()
                root = (BASE_DIR / 'assets' / 'memes').resolve()
                f = (root / (name + '.png')).resolve() if name else None
                ctype = 'image/png'
            else:
                root = (BASE_DIR / 'assets' / 'sound').resolve()
                f = (root / 'click-duck.wav').resolve()
                ctype = 'audio/wav'
            if f is None or not str(f).startswith(str(root)) or not f.exists():
                return self._json({'error': 'no-such-file'}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/anim-meta':
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            name = (params.get('name') or [''])[0].strip()
            try:
                d, meta = anim_frames(name)
            except Exception as e:
                return self._json({'error': 'convert-fail', 'detail': str(e)[:150]}, 500)
            if not meta:
                return self._json({'error': 'no-such-anim'}, 404)
            return self._json(meta)
        if path == '/anim-frame':
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            name = (params.get('name') or [''])[0].strip()
            try:
                idx = int((params.get('i') or ['0'])[0])
            except ValueError:
                idx = 0
            try:
                d, meta = anim_frames(name)
            except Exception as e:
                return self._json({'error': 'convert-fail', 'detail': str(e)[:150]}, 500)
            if not meta or idx < 1 or idx > meta['count']:
                return self._json({'error': 'no-such-frame'}, 404)
            f = d / ('f%04d.webp' % idx)
            if not f.exists():
                return self._json({'error': 'no-such-frame'}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/webp')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/anim-webp':
            # 平板原生桌宠：把 webm(VP9+alpha) 现转成动画 WebP（ArkUI Image 可播、
            # 带透明通道、可裁掉透明边），转好落盘缓存，源文件更新则重转。
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            name = (params.get('name') or [''])[0].strip()
            try:
                f = anim_webp_path(name)
            except Exception as e:
                return self._json({'error': 'convert-fail', 'detail': str(e)[:150]}, 500)
            if not f:
                return self._json({'error': 'no-such-anim'}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/webp')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/anim-file':
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            name = (params.get('name') or [''])[0].strip()
            root = (BASE_DIR / 'assets' / 'webm').resolve()
            f = (root / (name + '.webm')).resolve() if name else None
            if not name or f is None or not str(f).startswith(str(root)) or not f.exists():
                return self._json({'error': 'no-such-anim'}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'video/webm')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/events':
            if not self._qtoken_ok(params):
                return self._json({'error': 'unauthorized'}, 401)
            q = queue.Queue(maxsize=64)
            SSE_SUBS.append(q)
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                self.wfile.write(b': hello\n\n')
                self.wfile.flush()
                # 重连补发：客户端带 since=<最后收到的 seq>，把断线期间的 say/anim 补上
                try:
                    since = int((params.get('since') or ['0'])[0])
                except ValueError:
                    since = 0
                if since > 0:
                    for item in list(SSE_HIST):
                        if item.get('seq', 0) > since:
                            self.wfile.write(b'data: ' + json.dumps(item, ensure_ascii=False).encode('utf-8') + b'\n\n')
                    self.wfile.flush()
                while True:
                    try:
                        obj = q.get(timeout=15)
                        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
                        self.wfile.write(b'data: ' + data + b'\n\n')
                    except queue.Empty:
                        self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
            except Exception:
                pass  # 客户端断开属正常
            finally:
                try:
                    SSE_SUBS.remove(q)
                except ValueError:
                    pass
            return
        # ── 原有 Bearer 接口 ──
        if not self._auth():
            return self._json({'error': 'unauthorized'}, 401)
        # ── 鱼鳍（2026-11-10 主人拍板：云上的鱼鱼要能像本地 agent 一样动手）──
        # 只走 Bearer；绝不进 query-token 白名单（平板只该有气泡和表情）。
        if path.startswith('/fins/'):
            _ip = self.client_address[0] if self.client_address else ''
            if path == '/fins/status':
                return self._json(pc_fins.status())
            if path == '/fins/audit':
                try:
                    _n = int((params.get('n') or ['20'])[0])
                except Exception:
                    _n = 20
                return self._json({'ok': True, 'rows': pc_fins.recent_audit(_n)})
            if path == '/fins/list':
                return self._json(pc_fins.list_dir(
                    (params.get('dir') or [''])[0],
                    q=(params.get('q') or [''])[0],
                    ext=(params.get('ext') or [''])[0],
                    limit=(params.get('limit') or [None])[0],
                    ip=_ip))
            if path == '/fins/read':
                return self._json(pc_fins.read_file(
                    (params.get('path') or [''])[0],
                    offset=(params.get('offset') or [0])[0],
                    max_chars=(params.get('max') or [None])[0],
                    as_b64=(params.get('b64') or [''])[0] in ('1', 'true', 'yes'),
                    ip=_ip))
            if path == '/fins/stat':
                return self._json(pc_fins.stat_path(
                    (params.get('path') or [''])[0], ip=_ip))
            return self._json({'error': 'not-found'}, 404)
        if path == '/health':
            roll_day_local()
            with LOCK:
                eye_state = {k: STATE[k] for k in ('paused', 'pause_reason', 'day_bytes', 'pub_route')}
                # issue #16：在场/离场可观测（含显式暂停剩余时间与 clip 闸状态）
                eye_state.update({
                    'eye_mode': STATE.get('eye_mode', 'normal'),
                    'eye_mode_reason': STATE.get('eye_mode_reason', ''),
                    'last_note_at': STATE.get('last_note_at', 0.0),
                    'today_notes': STATE.get('today_notes', 0),
                    'today_frames_kb': STATE.get('today_frames_kb', 0),
                    'push_fail_rounds': STATE.get('push_fail_rounds', 0),
                    'paused_until': STATE.get('eye_paused_until', 0.0),
                    'pause_remaining_sec': max(0, int(STATE.get('eye_paused_until', 0.0) - time.time())),
                    'clip_quota_exhausted': STATE.get('clip_quota_exhausted', False),
                    'clip_quota_day': STATE.get('clip_quota_day', ''),
                    'clip_triggers': dict(STATE.get('clip_triggers') or {}),
                    # issue #55 双击看一眼：首日看误触率（hits=有效手势 / throttled=节流拦下）
                    'dblclick_hits': STATE.get('dblclick_hits', 0),
                    'dblclick_throttled': STATE.get('dblclick_throttled', 0),
                    'dblclick_sleep': STATE.get('dblclick_sleep', 0),
                })
            _fins_on = pc_fins.enabled()
            tablet = find_mirror_window()
            if not tablet:
                tablet_state = 'no'
            elif tablet[3]:
                tablet_state = 'standby'      # 接收端开着，平板还没投上来
            else:
                tablet_state = 'yes'
            try:
                pet = pet_forward('/state?pet=main', method='GET')
                pet_ok = True
            except Exception as e:
                pet_ok = False
                pet = {'error': str(e)[:100]}
            return self._json({'ok': True, 'fins': _fins_on, 'eye': eye_state, 'pet_alive': pet_ok,
                               'tablet_window': tablet_state,
                               'tablet_hdc': 'yes' if hdc_target() else 'no',
                               'tablet_tunnel': HDC_STATE.get('tunnel', 'no'),
                               # 系统级键鼠空闲（主动感 Source 的五连闸之一，-1=未知）
                               'input_idle_ms': input_idle_ms()})
        return self._json({'error': 'not-found'}, 404)

    def do_POST(self):
        path, _, qs = self.path.partition('?')
        if self._rate_gate(path):
            return
        params = urllib.parse.parse_qs(qs)
        # /pet-wake（物理双击）/ /clip（「看一眼」）/ /pet-voice（语音留言）是平板入口：
        # 平板只握 query token（与 /pet 三件套同口径）。⚠️ 白名单逐条加，
        # 绝不整段放开——否则 /say /anim /tree 也一并暴露给 query token。
        if not (self._auth() or (path in ('/pet-wake', '/clip', '/pet-voice', '/eye')
                                 and self._qtoken_ok(params))):
            return self._json({'error': 'unauthorized'}, 401)
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8')) if length else {}
        except Exception:
            return self._json({'error': 'bad-json'}, 400)
        _ip = self.client_address[0] if self.client_address else ''
        try:
            # ── 鱼鳍（11-10）：命令执行 / 写文件 ──
            if path == '/fins/exec':
                return self._json(pc_fins.exec_cmd(
                    str(payload.get('cmd') or ''),
                    cwd=payload.get('cwd'),
                    timeout=payload.get('timeout_s') or payload.get('timeout'),
                    shell=str(payload.get('shell') or 'cmd'),
                    ip=_ip))
            if path == '/fins/write':
                return self._json(pc_fins.write_file(
                    str(payload.get('path') or ''),
                    content=payload.get('content'),
                    b64=payload.get('b64'),
                    append=bool(payload.get('append')),
                    mkdir=payload.get('mkdir', True),
                    ip=_ip))
            if path == '/say':
                # 说话主干在 say_flow（桥菜单「看一眼」的现场反应共用同一份）
                ok, res, code = say_flow(
                    str(payload.get('text', '')),
                    mood=str(payload.get('mood') or ''),
                    anim=payload.get('anim'),
                    duration=payload.get('duration'),
                    image=str(payload.get('image') or ''),
                    url=str(payload.get('url') or ''))
                return self._json(res, code)
            if path == '/anim':
                name = payload.get('name', '')
                r = pet_forward('/anim?pet=main&surface=desktop', {'name': name})
                with LOCK:
                    STATE['last_activity'] = time.time()
                    STATE['sleeping'] = False
                    STATE['last_anim'] = str(name or '')
                sse_publish({'type': 'anim', 'name': name})
                return self._json({'ok': True, 'anim': r})
            if path == '/clip':
                # L3「看一段」：录屏主干在 clip_once（桥菜单「看一眼」共用同一份）
                with LOCK:
                    if STATE.get('clip_busy'):
                        return self._json({'error': 'clip-in-progress'}, 429)
                    STATE['clip_busy'] = True
                try:
                    code, res = clip_once(seconds=payload.get('seconds', 12),
                                          target=str(payload.get('window') or ''),
                                          source='api')
                    return self._json(res, code)
                finally:
                    with LOCK:
                        STATE['clip_busy'] = False
            if path == '/pet-state':
                r = pet_forward('/state?pet=main', method='GET')
                return self._json({'ok': True, 'state': r})
            if path == '/pet-wake':
                return self.pet_wake()
            if path == '/pet-voice':
                return self.pet_voice(payload)
            if path == '/eye':
                return self.eye_switch(payload)
            return self._json({'error': 'not-found'}, 404)
        except urllib.error.HTTPError as e:
            return self._json({'error': f'pet HTTP{e.code}',
                               'detail': e.read().decode('utf-8', 'replace')[:200]}, 502)
        except Exception as e:
            return self._json({'error': str(e)[:150]}, 502)

    def eye_switch(self, payload):
        """显式开关（issue #16）：POST /eye {action: pause|resume|status}。
        pause 写 flag 持久化（默认 2h 自动恢复），resume 清除；token 口径同 /pet 三件套。"""
        action = str(payload.get('action') or 'status').strip().lower()
        now = time.time()
        if action == 'pause':
            hours = payload.get('hours', 2)
            try:
                hours = max(0.1, min(24.0, float(hours)))
            except Exception:
                hours = 2.0
            until = now + hours * 3600
            try:
                (BASE_DIR / 'eye-pause.flag').write_text(
                    json.dumps({'until': until, 'at': now, 'hours': hours}), encoding='utf-8')
            except Exception as e:
                log.warning('eye pause flag 写入失败: %s', e)
            with LOCK:
                STATE['eye_paused_until'] = until
                STATE['eye_pause_origin'] = 'manual'
                STATE['eye_mode'] = 'paused'
                STATE['eye_mode_reason'] = 'manual'
            log.info('眼睛显式暂停 %.1fh（至 %s）', hours,
                     time.strftime('%H:%M', time.localtime(until)))
            return self._json({'ok': True, 'action': 'pause', 'until': until,
                               'hours': hours, 'pause_remaining_sec': int(hours * 3600)})
        if action == 'resume':
            try:
                (BASE_DIR / 'eye-pause.flag').unlink()
            except OSError:
                pass
            with LOCK:
                STATE['eye_paused_until'] = 0.0
                STATE['eye_pause_origin'] = ''
                STATE['eye_mode'] = 'normal'
                STATE['eye_mode_reason'] = ''
            log.info('眼睛显式恢复推帧')
            return self._json({'ok': True, 'action': 'resume'})
        if action in ('status', ''):
            with LOCK:
                st = {k: STATE.get(k) for k in
                      ('eye_mode', 'eye_mode_reason', 'eye_paused_until', 'last_note_at',
                       'today_notes', 'today_frames_kb', 'push_fail_rounds',
                       'clip_quota_exhausted', 'clip_quota_day')}
                st['clip_triggers'] = dict(STATE.get('clip_triggers') or {})
                st['pause_remaining_sec'] = max(0, int(st['eye_paused_until'] - now))
            return self._json({'ok': True, 'action': 'status', 'eye': st})
        return self._json({'error': 'bad-action'}, 400)

    def pet_wake(self):
        """平板双击 → 让本体看主人的屏幕并搭话。

        笔记 60s 内有过就直接用（眼睛持续在看，不必强拍）；否则预算内
        强制跑一轮鱼眼拿当前屏幕的新鲜笔记（还拿不到退上一条），
        再把笔记塞进 RN 信发给 feiyu/qqbot。她的回复不走这里：
        pet_say 双投递 → QQ + 本桥 /say → SSE 平板自己会收到。
        """
        now = time.time()
        with LOCK:
            wait = WAKE_MIN_INTERVAL - (now - STATE['last_pet_wake'])
        if wait > 0:
            return self._json({'ok': True, 'throttled': True,
                               'retryAfter': int(wait) + 1})
        rn = shutil.which('reason-network')
        if not rn:
            log.error('pet-wake: reason-network CLI 不在 PATH')
            return self._json({'ok': False, 'error': 'rn-cli-missing'}, 500)
        # 1) 新鲜笔记短路：眼睛本来就在持续看（45~150s 自适应轮询），
        #    60s 内有过新笔记 = 它描述的就是当前屏幕，不必再花 17~40s 强拍一轮
        now = time.time()
        with LOCK:
            note = STATE['last_note']
            note_age = now - STATE.get('last_note_at', 0.0)
        if note and note_age < WAKE_NOTE_FRESH_SEC:
            eye_rc = f'fresh({note_age:.0f}s)'
        else:
            # 即时鱼眼：给固定预算跑一轮（与 eye_loop 串行，避免并抢 hdc 快照）
            box = {}

            def _round():
                try:
                    with _EYE_LOCK:
                        box['rc'] = str(eye_round())
                except Exception as e:
                    box['rc'] = f'crash: {e}'

            t = threading.Thread(target=_round, daemon=True)
            t.start()
            t.join(WAKE_EYE_BUDGET_SEC)
            eye_rc = box.get('rc', 'timeout')
            with LOCK:
                note = STATE['last_note']
            if not note:
                note = WAKE_NOTE_FALLBACK
        # 2) 组信发 RN（笔记是她的口吻，原样引用即可）
        letter = WAKE_LETTER.format(note=note) + '（' + time.strftime('%H:%M') + '）'
        try:
            r = subprocess.run([rn, 'send', WAKE_TO, letter],
                               capture_output=True, text=True, timeout=25,
                               encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            return self._json({'ok': False, 'error': 'rn-send-timeout'}, 504)
        ok = r.returncode == 0
        with LOCK:
            STATE['last_pet_wake'] = time.time()
            STATE['last_activity'] = time.time()
        detail = (r.stderr or r.stdout or '').strip()[:200]
        log.info('pet-wake eye(%s) -> %s rc=%d %s', eye_rc[:40], WAKE_TO,
                 r.returncode, detail[:120])
        return self._json({'ok': ok, 'sent': ok, 'to': WAKE_TO, 'eye': eye_rc[:60],
                           'detail': detail if not ok else ''},
                          200 if ok else 502)

    def pet_voice(self, payload):
        """平板语音（端侧 ASR 的文本）→ 当成主人原话投给本体。

        与 /pet-wake 的区别：这是对话回合，不是"戳醒看屏幕"——信里就是主人的话，
        节流松（5s，只防双击式误触）。voice_until 窗口内的 say 事件带 voice:true，
        平板只对带标回复做 TTS 朗读。
        """
        text = str(payload.get('text', '')).strip()[:500]
        if not text:
            return self._json({'error': 'empty-text'}, 400)
        now = time.time()
        with LOCK:
            wait = VOICE_MIN_INTERVAL - (now - STATE.get('last_voice_at', 0.0))
        if wait > 0:
            return self._json({'ok': True, 'throttled': True,
                               'retryAfter': int(wait) + 1})
        rn = shutil.which('reason-network')
        if not rn:
            return self._json({'ok': False, 'error': 'rn-cli-missing'}, 500)
        letter = (
            '【平板语音】主人在平板上按着麦克风对你说了一句话（语音转文字，可能有同音错字），'
            '主人的原话：\n「' + text + '」\n'
            '回应要求：每一句都必须调用 pet_say 工具发出（一句一次调用，气泡和 QQ 同时出）——'
            '纯文本回复平板上看不到，等于没说话，平板只认 pet_say 过的句。'
            '像 QQ 聊天一样自然回应主人就好，别提这封信、别念「语音」「转文字」这类字眼。'
        )
        try:
            r = subprocess.run([rn, 'send', WAKE_TO, letter],
                               capture_output=True, text=True, timeout=25,
                               encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            return self._json({'ok': False, 'error': 'rn-send-timeout'}, 504)
        ok = r.returncode == 0
        with LOCK:
            STATE['last_voice_at'] = time.time()
            STATE['last_activity'] = time.time()
            STATE['voice_until'] = time.time() + VOICE_MARK_SEC
        detail = (r.stderr or r.stdout or '').strip()[:200]
        log.info('pet-voice -> %s rc=%d text=%s %s', WAKE_TO, r.returncode,
                 text[:50], detail[:100])
        return self._json({'ok': ok, 'sent': ok, 'to': WAKE_TO,
                           'detail': detail if not ok else ''},
                          200 if ok else 502)


class BridgeServer(ThreadingHTTPServer):
    # Windows 下 http.server 默认 SO_REUSEADDR 会放行第二个实例双绑同一端口
    #（2026-11-05 02:01 看门狗误判+双绑实锤），这里禁掉：端口被占就明确报错退出。
    allow_reuse_address = False


# ── 平板桌宠隧道（hdc rport 反向转发）────────────────────────
# 桥只绑 Tailscale IP，平板走不到。利用 hdc 的反向转发：平板 localhost:17860
# → PC 127.0.0.1:17866（本中继，仅绑回环零暴露）→ 桥（Tailscale IP）。
# 平板浏览器开 http://127.0.0.1:17860/pet?token=... 即桌宠页，走 USB 或 WiFi 都行。
TUNNEL_PORT = 17866

def _relay_pipe(client):
    """单向双工搬运：客户端(设备端隧道) ↔ 桥"""
    try:
        up = socket.create_connection((BRIDGE_BIND, BRIDGE_PORT), timeout=5)
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        return
    # create_connection 的 timeout=5 会留在 socket 上：SSE 15s 无消息时 recv
    # 抛超时 → pump 退出 → shutdown(SHUT_WR) 把下行关了 → 设备页"连接断开"。
    # 中继是长连通道，必须清掉超时（2026-11-06 平板桌宠"重连中"真凶）。
    up.settimeout(None)

    def pump(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except Exception:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except Exception:
            pass

    threads = [threading.Thread(target=pump, args=(a, b), daemon=True)
               for a, b in ((client, up), (up, client))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in (client, up):
        try:
            s.close()
        except Exception:
            pass


def tunnel_relay_loop():
    """本机回环中继：127.0.0.1:TUNNEL_PORT → 桥。独占绑定（不设 SO_REUSEADDR，
    双实例只有一个胜者，输家静默放弃——平板隧道不依赖哪个实例活着都行）"""
    srv = socket.socket()
    try:
        srv.bind(('127.0.0.1', TUNNEL_PORT))
    except OSError as e:
        log.info('tunnel relay %d 被占（另一实例已接管中继），跳过', TUNNEL_PORT)
        return
    srv.listen(8)
    log.info('tablet tunnel relay: 127.0.0.1:%d -> %s:%d', TUNNEL_PORT, BRIDGE_BIND, BRIDGE_PORT)
    while True:
        try:
            client, _ = srv.accept()
            threading.Thread(target=_relay_pipe, args=(client,), daemon=True).start()
        except Exception:
            time.sleep(1)


def tunnel_keeper_loop():
    """每 15s 保证「在线设备 ↔ 桌宠隧道」存在：优先 WiFi(TCP) 连接，只有 USB 时用 USB。
    rport 任务随连接断开自动消亡，重连后这里会自动重建，无需人工干预"""
    while True:
        try:
            r = subprocess.run([str(HDC_EXE), 'list', 'targets'], capture_output=True,
                               timeout=10, creationflags=HDC_CREATE_NO_WINDOW)
            keys = [l.strip() for l in r.stdout.decode('utf-8', 'replace').splitlines()
                    if l.strip() and l.strip() != '[Empty]']
            tcp_keys = [k for k in keys if ':' in k]        # WiFi 连接形如 ip:port
            targets = tcp_keys or keys[:1]
            if targets:
                ls = subprocess.run([str(HDC_EXE), 'fport', 'ls'], capture_output=True,
                                    timeout=10, creationflags=HDC_CREATE_NO_WINDOW)
                have = ls.stdout.decode('utf-8', 'replace')
                for k in targets:
                    if k in have and 'tcp:17860' in have:
                        HDC_STATE['tunnel'] = k
                        continue
                    rr = subprocess.run([str(HDC_EXE), '-t', k, 'rport',
                                         'tcp:17860', 'tcp:%d' % TUNNEL_PORT],
                                        capture_output=True, timeout=10,
                                        creationflags=HDC_CREATE_NO_WINDOW)
                    if b'OK' in rr.stdout + rr.stderr:
                        HDC_STATE['tunnel'] = k
                        log.info('平板桌宠隧道已建立（%s）: 设备 17860 -> PC %d', k, TUNNEL_PORT)
            elif 'tunnel' in HDC_STATE:
                HDC_STATE.pop('tunnel', None)
        except Exception as e:
            log.debug('tunnel keeper: %s', e)
        time.sleep(15)


def heartbeat_loop():
    """10s 心跳（2026-11-08）：给外部一个"主循环还在转"的证据。

    端口 LISTENING 只能证明进程活着、socket 还开着；线程卡死时它依然成立。
    看门狗读本文件 mtime：>30s = 假活（告警，不自动杀——杀错的代价比慢更大）。
    """
    path = BASE_DIR / 'pc_body.heartbeat'
    while True:
        try:
            names = ','.join(sorted(t.name for t in threading.enumerate()
                                    if t is not threading.current_thread()))
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'ts': round(time.time(), 1), 'pid': os.getpid(),
                                       'threads': names}, ensure_ascii=False),
                           encoding='utf-8')
            os.replace(tmp, path)          # 原子替换，读端不会读到半个文件
        except Exception as e:
            log.debug('heartbeat: %s', e)
        time.sleep(10)


def bridge_loop():
    try:
        srv = BridgeServer((BRIDGE_BIND, BRIDGE_PORT), Bridge)
    except OSError as e:
        # 已有实例在跑（看门狗/启动竞态的单胜者）——本进程没有桥=僵尸眼，直接退出
        log.error('bridge bind 失败: %s —— 本进程退出，胜者实例继续', e)
        _die('bind-fail', detail=str(e)[:200])   # 影子进程的死亡原因，别再靠猜
    log.info('bridge listening on %s:%d -> pet :%d', BRIDGE_BIND, BRIDGE_PORT, PET_PORT)
    _stage('listening')
    try:
        srv.serve_forever()
    except Exception:
        log.exception('bridge serve_forever 崩了——退出进程交给看门狗')
        _die('crash', detail='serve_forever')


if __name__ == '__main__':
    _stage('main')
    log.info('pc_body start | token=%s... | eye_servers=%s', TOKEN[:8], EYE_SERVERS)
    # 显式暂停 flag（issue #16）：重启后仍然生效，过期即清
    try:
        _f = BASE_DIR / 'eye-pause.flag'
        if _f.exists():
            _d = json.loads(_f.read_text(encoding='utf-8'))
            if float(_d.get('until') or 0) > time.time():
                STATE['eye_paused_until'] = float(_d['until'])
                STATE['eye_pause_origin'] = 'manual'
                STATE['eye_mode'] = 'paused'
                STATE['eye_mode_reason'] = 'manual'
                log.info('显式暂停 flag 生效（剩 %.0f 分钟）',
                         (STATE['eye_paused_until'] - time.time()) / 60)
            else:
                _f.unlink()
                log.info('显式暂停 flag 已过期，清除')
    except Exception as e:
        log.warning('显式暂停 flag 读取失败: %s', e)
    _stage('threads')
    threading.Thread(target=bridge_loop, daemon=True, name='Bridge').start()
    threading.Thread(target=tunnel_relay_loop, daemon=True, name='TunnelRelay').start()
    threading.Thread(target=tunnel_keeper_loop, daemon=True, name='TunnelKeeper').start()
    threading.Thread(target=visual_idle_loop, daemon=True, name='VisualIdle').start()
    threading.Thread(target=heartbeat_loop, daemon=True, name='Heartbeat').start()
    threading.Thread(target=pet_state_loop, daemon=True, name='PetState').start()
    threading.Thread(target=pet_menu_loop, daemon=True, name='PetMenu').start()
    threading.Thread(target=pet_gesture_loop, daemon=True, name='PetGesture').start()
    time.sleep(1)
    eye_loop()
