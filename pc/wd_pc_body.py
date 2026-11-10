# -*- coding: utf-8 -*-
"""wd_pc_body.py —— 肥鱼 PC 身体看门狗（2026-11-05，目标：半夜进程挂了能自愈）

端口探活（本地监听表，不过网络）：
  :7340  dsh-pet standalone（桌宠本体）
  :17860 pc_body 桥
2026-11-08：旧实现用 TCP connect 探自己的 tailnet IP，tailscale 数据面一抖就误判
（实测 DOWN 407 次 / 影子进程绑端口失败 358 次 = >=84% 误判）。本机进程活着与否
不该过网络——改读 netstat 的 LISTENING；网络可达性只做 WARN，不拉进程。
挂了就用对应命令拉起（DETACHED 无窗口）。单实例互斥 = 独占绑定 :17861。
用法：python wd_pc_body.py        # 常驻循环，60s 一查
      python wd_pc_body.py once   # 单次检查后退出（测试用）
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).parent
PYW = Path(sys.executable)
PYTHON = PYW.with_name('python.exe')          # pythonw 同目录必有 python
NODE = Path(r'C:\Program Files\nodejs\node.exe')
LOG_FILE = DIR / 'wd_pc_body.log'
MUTEX_PORT = 17861
INTERVAL = 60

# 探活地址必须与真实绑定地址一致：桥只绑 Tailscale IP，不监听 127.0.0.1。
# （探 127.0.0.1 会永远 DOWN → 每 65s 白拉一个抢不到端口的影子进程）
CHECKS = [
    ('127.0.0.1', 7340, [str(NODE), 'lib/standalone.js', '--port', '7340']),
    ('100.98.120.55', 17860, [str(PYTHON), 'pc_body.py']),
]
CREATE_FLAGS = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    print(line, flush=True)


def _listen_ports():
    """本地监听端口集合（netstat -ano，零网络往返）。"""
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             text=True, timeout=20, errors='replace').stdout
    except Exception:
        return set()
    ports = set()
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].upper() == 'TCP' and p[3].upper() == 'LISTENING':
            try:
                ports.add(int(p[1].rsplit(':', 1)[1]))
            except (ValueError, IndexError):
                continue
    return ports


def alive(host, port):
    """本地是否有进程在 LISTEN 该端口（谁占着都算活，与旧语义一致）。

    不再 TCP connect：那条路会跟着 tailscale/DERP 一起抖，把链路故障写成进程死亡。
    """
    return port in _listen_ports()


def reachable(host, port):
    """网络可达性（旁证）：只用于 WARN，绝不据此拉进程。"""
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


_WARN_AT = {}
_REAP_AT = [0.0]

def _port_owner(port):
    """哪个 PID 在 LISTEN 这个端口（netstat -ano）。"""
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                             text=True, timeout=20, errors='replace').stdout
    except Exception:
        return None
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[0].upper() == 'TCP' and p[3].upper() == 'LISTENING'                 and p[1].rsplit(':', 1)[-1] == str(port):
            try:
                return int(p[4])
            except ValueError:
                continue
    return None

def reap_extra_pet(port=7340, every=600):
    """清掉抢输端口的孪生桌宠。

    2026-11-10 实证：Windows 上 `lib/standalone.js` 绑 7340 失败**不会退出**，会带着
    electron 子进程继续活着 = 屏幕上一只不接任何请求的假桌宠。9:05:42 启动脚本那次
    预热与常驻看门狗同秒各拉一份，于是有了两只。只杀"命令行含 standalone.js 且含
    --port 7340 且不是端口属主"的 node，10 分钟最多扫一次。
    """
    import time as _t
    if _t.time() - _REAP_AT[0] < every:
        return 0
    _REAP_AT[0] = _t.time()
    owner = _port_owner(port)
    if not owner:
        return 0
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"name='node.exe'\" | "
             "Where-Object { $_.CommandLine -match 'standalone\.js' -and $_.CommandLine -match '--port 7340' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=40, errors='replace').stdout
    except Exception:
        return 0
    killed = []
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid == owner:
            continue
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=20)
            killed.append(pid)
        except Exception:
            pass
    if killed:
        log('孪生桌宠已清 pid=%s（:7340 属主 %d）' % (','.join(map(str, killed)), owner))
    return len(killed)
HEARTBEAT = DIR / 'pc_body.heartbeat'
HB_STALE = 30          # 写间隔 10s，30s 不给新行 = 主循环卡了


def heartbeat_stale():
    """返回 (是否过期, 年龄秒)。文件不存在 = 老版本，返回 None 不吵。"""
    try:
        age = time.time() - os.stat(HEARTBEAT).st_mtime
    except OSError:
        return None
    return age > HB_STALE, age


def warn_heartbeat(age):
    now = time.time()
    if now - _WARN_AT.get(('hb',), 0) < 600:
        return
    _WARN_AT[('hb',)] = now
    log('WARN 桥在监听但心跳 %.0fs 未更新 -> 假活（线程可能卡死），只告警不乱杀' % age)


def warn_link(host, port):
    """在监听但连不上 = 链路问题。10 分钟最多一条，避免链路抖一晚上刷屏。"""
    k = (host, port)
    now = time.time()
    if now - _WARN_AT.get(k, 0) < 600:
        return
    _WARN_AT[k] = now
    log('WARN %s:%d 在监听但 TCP 连不上 -> 链路/DERP 问题，不拉进程' % (host, port))


def spawn(cmd):
    # 2026-11-08（tablet-pet-deploy 复核后的规格）：只把 stderr 落文件（行缓冲、文本
    # 模式，异常栈全留）；stdout 保持 DEVNULL（pc_body 自写 pc_body.log 已够全，别把
    # 噪音也引进来）。之前 DEVNULL，"grep Traceback=0" 只能证明没人看得见，65 次真死断不了案。
    ERR = open(DIR / 'pc_body_stderr.log', 'a', buffering=1, encoding='utf-8', errors='replace')
    p = subprocess.Popen(cmd, cwd=str(DIR), creationflags=CREATE_FLAGS,
                         stdout=subprocess.DEVNULL, stderr=ERR,
                         stdin=subprocess.DEVNULL)
    ERR.close()   # 子进程已继承句柄；父进程关掉防句柄泄漏
    return p


EXITS = DIR / 'pc_body_exits.jsonl'


def note_down(port, why='watchdog-down'):
    """进程不在了，atexit 就没机会写——由目击者补一行，让每次 DOWN 都有 pid 与心跳年龄。"""
    hb, age = {}, None
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding='utf-8'))
    except Exception:
        pass
    try:
        age = round(time.time() - os.stat(HEARTBEAT).st_mtime, 1)
    except OSError:
        age = None
    rec = {'ts': round(time.time(), 3), 'why': why, 'by': 'wd_pc_body',
           'port': port, 'pid': hb.get('pid'), 'hb_age_s': age}
    try:
        with open(EXITS, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def check_pass():
    for host, port, cmd in CHECKS:
        if alive(host, port):
            if port == 7340:
                try:
                    reap_extra_pet(port)
                except Exception as e:
                    log('reap 异常: %s' % e)
            if not reachable(host, port):
                warn_link(host, port)
            hb = heartbeat_stale()
            if hb and hb[0]:
                warn_heartbeat(hb[1])
            continue
        time.sleep(1.5)          # 复检：瞬时探活误判会导致重复拉起（Windows 双绑坑）
        if alive(host, port):
            continue
        note_down(port)
        log('DOWN %s:%d -> 拉起 %s' % (host, port, ' '.join(cmd[1:])))
        try:
            spawn(cmd)
        except Exception as e:
            log('spawn 失败: %s' % e)


def main():
    once = len(sys.argv) > 1 and sys.argv[1] == 'once'
    if once:
        # 单次检查不抢常驻互斥（bat 启动时与常驻看门狗并发检查是常态，
        # 重复拉起由 pc_body 的独占绑定兜底成"单胜者"，无害）
        check_pass()
        return
    mutex = socket.socket()
    # Windows: 不设 SO_EXCLUSIVEADDRUSE，另一个带 SO_REUSEADDR 的 socket 可能共绑同一端口
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        try:
            mutex.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    try:
        mutex.bind(('127.0.0.1', MUTEX_PORT))
    except OSError:
        owner = ''
        try:
            owner = (DIR / 'wd_pc_body.pid').read_text(encoding='utf-8').strip()
        except OSError:
            pass
        print('已有看门狗在跑（:17861 被占, pid=%s），退出' % (owner or '?'), flush=True)
        log('watchdog EXIT 互斥口被占 pid=%s' % (owner or '?'))
        return
    mutex.listen(1)
    try:
        (DIR / 'wd_pc_body.pid').write_text(str(os.getpid()), encoding='utf-8')
    except OSError:
        pass
    log('watchdog start pid=%d (interval=%ds)' % (os.getpid(), INTERVAL))
    while True:
        try:
            check_pass()
        except Exception as e:
            log('check 异常: %s' % e)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
