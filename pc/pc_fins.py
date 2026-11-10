# -*- coding: utf-8 -*-
"""pc_fins.py —— 鱼鳍：让云上的鱼鱼在本机读写文件、跑命令（2026-11-10 主人拍板）

定位：鱼眼 = 看，pc_body = 说话/表情，鱼鳍 = 动手。三者都是「身体」，不是新实例，
      她的大脑和会话始终只有云上那一个（鱼鱼不能换新）。

边界（全部在本文件落地）：
  * 只经 pc_body.py 的 /fins/* 暴露，Bearer 令牌与鱼眼同源（.eye-token）
  * 每一动作落 logs/fins_audit.jsonl（谁/什么/结果/耗时/输出大小），GET /fins/audit 可查
  * fins.off 文件存在 = 立即全停（503）。主人一线开关，不用改代码不用重启
  * 灾难命令黑名单（format/diskpart/bcdedit/清盘级 del）直接拒 —— 防手滑，不防人
  * 超时默认 20s、上限 300s；超时 taskkill /T /F 杀整棵进程树
  * 输出不走管道走临时文件（避免死锁），各截 200KB，原始大小如实上报
  * 无 stdin、CREATE_NO_WINDOW、不继承桥的句柄；exec 并发上限 4
"""
import base64
import fnmatch
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / 'logs'
AUDIT_FILE = LOG_DIR / 'fins_audit.jsonl'
CONF_FILE = BASE_DIR / 'fins.json'
OFF_FLAG = BASE_DIR / 'fins.off'

CREATE_NO_WINDOW = 0x08000000
MAX_OUT_BYTES = 200 * 1024          # 单路输出截断
MAX_FILE_BYTES = 8 * 1024 * 1024    # 读文件上限
MAX_CHARS = 40000                   # 单次返回字符上限
MAX_LIST = 300

# 默认可读可写的「文档」类型（主人 11-10 拍板：纯文本 + Word/Excel）
TEXT_EXT = {
    '.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.json', '.jsonl',
    '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.log', '.srt', '.vtt',
    '.py', '.js', '.mjs', '.ts', '.sh', '.bat', '.ps1', '.html', '.htm',
    '.css', '.xml', '.sql', '.java', '.c', '.h', '.cpp', '.go', '.rs', '.php',
    '.rb', '.pl', '.tex', '.bib',
}
DOC_EXT = {'.docx', '.xlsx'}

# 永不读写：泄漏不可逆的凭据（主人可改 fins.json 里的 deny_globs 覆盖）
DENY_GLOBS = [
    '*/.ssh/*', '*/.aws/*', '*/.gnupg/*',
    '*/id_rsa*', '*/id_ed25519*', '*/id_ecdsa*', '*/id_dsa*',
    '*.pem', '*.key', '*.pfx', '*.p12', '*.ppk', '*.kdbx', '*.jks',
    '/d/stardewai/dsh-pet/.eye-token', '*/.eye-token',
    '*/.git-credentials', '*/.netrc', '*/admin-password', '*/keys_tmp/*',
]
# 写入禁区：系统目录（读不限；写了可能把 Windows 弄坏）
DENY_WRITE_PREFIX = [
    'c:/windows', 'c:/program files', 'c:/program files (x86)',
    'c:/programdata/microsoft', 'c:/$recycle.bin', 'c:/system volume information',
]
# 灾难命令（小写正则，命中直接拒）：这些一条就能毁机，正常帮忙永远用不到
DENY_CMD = [
    r'\bformat(\.com)?\s+[a-z]:',
    r'\bdiskpart\b',
    r'\bbcdedit\b',
    r'\bvssadmin\b[^\n]*\bdelete\b',
    r'\bcipher\s+/w',
    r'\b(rd|rmdir|del|erase)\b[^\n]*\s[a-z]:\s*/?\s*(/s|/q|\*)*\s*$',
    r'\bremove-item\b[^\n]*[a-z]:[\\/]?["\']?\s*(-recurse)?\s*-force',
    r'\bmkfs(\.\w+)?\b', r'\bdd\s+if=',
]

_AUD = threading.Lock()
_EXEC_SEM = threading.BoundedSemaphore(4)


def _conf():
    c = {
        'enabled': True,
        'default_timeout_s': 20,
        'max_timeout_s': 300,
        'max_chars': MAX_CHARS,
        'max_file_bytes': MAX_FILE_BYTES,
        'deny_globs': DENY_GLOBS,
        'deny_write_prefix': DENY_WRITE_PREFIX,
        'deny_cmd': DENY_CMD,
    }
    try:
        if CONF_FILE.exists():
            c.update(json.loads(CONF_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass
    return c


def enabled():
    if os.environ.get('FEIYU_FINS', '').lower() in ('0', 'off', 'false', 'no'):
        return False
    if OFF_FLAG.exists():
        return False
    return bool(_conf().get('enabled', True))


def _norm(p):
    return str(p).replace('\\', '/').lower()


def _deny_path(p, write=False):
    """返回拒绝原因或 None。p 必须是已 resolve 的绝对路径。"""
    c = _conf()
    n = _norm(p)
    for g in c.get('deny_globs') or []:
        if fnmatch.fnmatch(n, g.lower()):
            return '凭据/密钥类路径默认禁读：%s' % g
    if write:
        for pre in c.get('deny_write_prefix') or []:
            if n == pre or n.startswith(pre.rstrip('/') + '/'):
                return '系统目录默认禁写：%s' % pre
    return None


def audit(action, detail, ok, **extra):
    rec = {'ts': round(time.time(), 3), 'ts_local': time.strftime('%Y-%m-%d %H:%M:%S'),
           'action': action, 'detail': str(detail)[:500], 'ok': bool(ok)}
    rec.update({k: v for k, v in extra.items() if v is not None})
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _AUD:
            if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > 5 * 1024 * 1024:
                try:
                    AUDIT_FILE.replace(AUDIT_FILE.with_suffix('.1.jsonl'))
                except Exception:
                    pass
            with open(AUDIT_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return rec


def recent_audit(n=20):
    try:
        n = max(1, min(int(n), 200))
        if not AUDIT_FILE.exists():
            return []
        lines = AUDIT_FILE.read_text(encoding='utf-8', errors='replace').splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def _decode(b):
    """控制台输出解码：utf-8 → gbk → latin-1（Windows 中文机多为 cp936）。"""
    for enc in ('utf-8', 'gbk'):
        try:
            return b.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return b.decode('latin-1', errors='replace'), 'latin-1'


def _check_cmd(cmd):
    low = cmd.lower()
    for pat in _conf().get('deny_cmd') or []:
        if re.search(pat, low):
            return '命令命中灾难黑名单（%s）。确需执行请主人临时改 fins.json。' % pat
    return None


def exec_cmd(cmd, cwd=None, timeout=None, shell='cmd', ip=''):
    """跑一条命令。cmd/shell 二选一：cmd 走 cmd /c，explorer 类同理；ps 走 powershell。"""
    if not enabled():
        return {'ok': False, 'error': 'fins-off',
                'hint': '桥侧 fins.off 存在或 FEIYU_FINS=off'}
    cmd = (cmd or '').strip()
    if not cmd:
        return {'ok': False, 'error': 'empty-cmd'}
    bad = _check_cmd(cmd)
    if bad:
        audit('exec', cmd, False, ip=ip, reason=bad)
        return {'ok': False, 'error': 'denied', 'reason': bad}
    c = _conf()
    try:
        t = float(timeout or c.get('default_timeout_s', 20))
    except Exception:
        t = 20.0
    t = max(1.0, min(t, float(c.get('max_timeout_s', 300))))
    wd = str(cwd) if cwd else str(Path.home())
    if not os.path.isdir(wd):
        return {'ok': False, 'error': 'bad-cwd', 'cwd': wd}

    if not _EXEC_SEM.acquire(blocking=False):
        return {'ok': False, 'error': 'busy', 'hint': '同时最多 4 条命令，稍等'}
    t0 = time.time()
    timed_out = False
    try:
        if shell in ('ps', 'powershell', 'pwsh'):
            args = ['powershell', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-Command', cmd]
        else:
            args = ['cmd', '/c', cmd]
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            p = subprocess.Popen(args, cwd=wd, stdin=subprocess.DEVNULL,
                                 stdout=fo, stderr=fe,
                                 creationflags=CREATE_NO_WINDOW)
            try:
                p.wait(timeout=t)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)],
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   creationflags=CREATE_NO_WINDOW)
                except Exception:
                    pass
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass
            dur = int((time.time() - t0) * 1000)
            fo.seek(0, os.SEEK_END)
            fo_size = fo.tell()
            fo.seek(0)
            so = fo.read(MAX_OUT_BYTES)
            fe.seek(0, os.SEEK_END)
            fe_size = fe.tell()
            fe.seek(0)
            se = fe.read(MAX_OUT_BYTES)
        stdout, enc = _decode(so)
        stderr, _ = _decode(se)
        out = {'ok': (p.returncode == 0 and not timed_out),
               'exit': p.returncode, 'timed_out': timed_out,
               'dur_ms': dur, 'cwd': wd, 'shell': shell,
               'stdout': stdout, 'stderr': stderr,
               'stdout_bytes': fo_size, 'stderr_bytes': fe_size,
               'truncated': fo_size > MAX_OUT_BYTES or fe_size > MAX_OUT_BYTES,
               'encoding': enc, 'timeout_s': t}
        audit('exec', cmd, out['ok'], ip=ip, exit=p.returncode, timed_out=timed_out,
              dur_ms=dur, out_bytes=fo_size, err_bytes=fe_size)
        return out
    except Exception as e:
        audit('exec', cmd, False, ip=ip, error=str(e)[:200])
        return {'ok': False, 'error': 'exception', 'detail': str(e)[:300]}
    finally:
        _EXEC_SEM.release()


# ── 文件 ────────────────────────────────────────────────────────────────
def _resolve(path, write=False):
    """返回 (Path, err)。resolve 会解符号链接 → 逃逸的链接也会被 deny 规则看到。"""
    if not path:
        return None, 'empty-path'
    try:
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        p = p.resolve()
    except Exception as e:
        return None, 'bad-path: %s' % str(e)[:120]
    bad = _deny_path(p, write=write)
    if bad:
        return None, bad
    return p, None


def _docx_text(p):
    import docx
    d = docx.Document(str(p))
    parts = []
    for para in d.paragraphs:
        t = para.text.strip()
        if t:
            parts.append(t)
    for ti, tb in enumerate(d.tables, 1):
        parts.append('[表 %d]' % ti)
        for row in tb.rows:
            parts.append(' | '.join(c.text.strip().replace('\n', ' ') for c in row.cells))
    return '\n'.join(parts)


def _xlsx_text(p, max_rows=200):
    import openpyxl
    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            out.append('## sheet: %s  (rows=%s cols=%s)' % (ws.title, ws.max_row, ws.max_column))
            n = 0
            for row in ws.iter_rows(values_only=True):
                if row is None or all(v is None or v == '' for v in row):
                    continue
                out.append(' | '.join('' if v is None else str(v) for v in row))
                n += 1
                if n >= max_rows:
                    out.append('…(本表只取前 %d 行)' % max_rows)
                    break
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return '\n'.join(out)


def read_file(path, offset=0, max_chars=None, as_b64=False, ip=''):
    if not enabled():
        return {'ok': False, 'error': 'fins-off'}
    p, err = _resolve(path)
    if err:
        audit('read', path, False, ip=ip, reason=err)
        return {'ok': False, 'error': err}
    c = _conf()
    try:
        if not p.exists():
            return {'ok': False, 'error': 'no-such-file', 'path': str(p)}
        if not p.is_file():
            return {'ok': False, 'error': 'not-a-file', 'path': str(p)}
        size = p.stat().st_size
        cap = int(c.get('max_file_bytes', MAX_FILE_BYTES))
        if size > cap:
            return {'ok': False, 'error': 'too-large', 'path': str(p),
                    'bytes': size, 'cap': cap, 'hint': '用 exec 里 Get-Content -TotalCount / type 分段取'}
        ext = p.suffix.lower()
        meta = {'ok': True, 'path': str(p), 'ext': ext, 'size': size,
                'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))}
        if as_b64:
            raw = p.read_bytes()
            meta.update({'kind': 'base64', 'b64': base64.b64encode(raw).decode('ascii')})
            audit('read', path, True, ip=ip, bytes=size, kind='b64')
            return meta
        if ext in DOC_EXT:
            text = _docx_text(p) if ext == '.docx' else _xlsx_text(p)
            kind = 'docx' if ext == '.docx' else 'xlsx'
        elif ext in TEXT_EXT:
            raw = p.read_bytes()
            text, enc = _decode(raw.lstrip(b'\xef\xbb\xbf'))
            meta['encoding'] = enc
            kind = 'text'
        elif p.suffix.lower() in ('.doc', '.xls', '.pdf', '.ppt', '.pptx'):
            audit('read', path, False, ip=ip, reason='unsupported-type')
            return {'ok': False, 'error': 'unsupported-type', 'path': str(p), 'ext': ext,
                    'hint': 'doc/xls/pdf/ppt 暂不支持；用 b64=1 取原文件，或 exec 调本机工具转换'}
        else:
            audit('read', path, False, ip=ip, reason='unknown-type')
            return {'ok': False, 'error': 'unknown-type', 'path': str(p), 'ext': ext,
                    'hint': '文本/Word/Excel 之外的二进制请用 b64=1'}
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        total = len(text)
        try:
            off = max(0, int(offset or 0))
        except Exception:
            off = 0
        try:
            lim = max(1, min(int(max_chars or c.get('max_chars', MAX_CHARS)),
                             int(c.get('max_chars', MAX_CHARS))))
        except Exception:
            lim = MAX_CHARS
        chunk = text[off:off + lim]
        meta.update({'kind': kind, 'chars': total, 'offset': off,
                     'returned_chars': len(chunk), 'text': chunk,
                     'truncated': off + len(chunk) < total,
                     'next_offset': (off + len(chunk)) if off + len(chunk) < total else None})
        audit('read', path, True, ip=ip, bytes=size, kind=kind,
              chars=total, returned=len(chunk))
        return meta
    except Exception as e:
        audit('read', path, False, ip=ip, error=str(e)[:200])
        return {'ok': False, 'error': 'exception', 'detail': str(e)[:300]}


def write_file(path, content=None, b64=None, append=False, mkdir=True, ip=''):
    if not enabled():
        return {'ok': False, 'error': 'fins-off'}
    p, err = _resolve(path, write=True)
    if err:
        audit('write', path, False, ip=ip, reason=err)
        return {'ok': False, 'error': err}
    try:
        if b64:
            raw = base64.b64decode(b64, validate=True)
        else:
            raw = (content if content is not None else '').encode('utf-8')
        if mkdir:
            p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        with open(p, 'ab' if append else 'wb') as f:
            f.write(raw)
        audit('write', path, True, ip=ip, bytes=len(raw), append=append, created=not existed)
        return {'ok': True, 'path': str(p), 'bytes': len(raw),
                'created': not existed, 'append': append,
                'mtime': time.strftime('%Y-%m-%d %H:%M:%S')}
    except Exception as e:
        audit('write', path, False, ip=ip, error=str(e)[:200])
        return {'ok': False, 'error': 'exception', 'detail': str(e)[:300]}


def list_dir(directory='', q='', ext='', limit=None, ip=''):
    if not enabled():
        return {'ok': False, 'error': 'fins-off'}
    c = _conf()
    try:
        lim = max(1, min(int(limit or MAX_LIST), 1000))
    except Exception:
        lim = MAX_LIST
    if not (directory or '').strip():
        drives = ['%s:\\' % ch for ch in 'CDEFGHIJKLMNOPQRSTUVWXYZ'
                  if os.path.exists('%s:\\' % ch)]
        return {'ok': True, 'roots': drives, 'home': str(Path.home()),
                'cwd': os.getcwd(), 'deny_globs': len(c.get('deny_globs') or [])}
    p, err = _resolve(directory)
    if err:
        return {'ok': False, 'error': err}
    try:
        if not p.exists():
            return {'ok': False, 'error': 'no-such-dir', 'dir': str(p)}
        if not p.is_dir():
            return {'ok': False, 'error': 'not-a-dir', 'dir': str(p)}
        exts = {('.' + x.strip().lower().lstrip('.')) for x in (ext or '').split(',') if x.strip()}
        ql = (q or '').strip().lower()
        items, n = [], 0
        truncated = False
        with os.scandir(str(p)) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                except OSError:
                    is_dir = False
                if _deny_path(e.path, write=False):
                    continue
                if exts and not is_dir and Path(e.name).suffix.lower() not in exts:
                    continue
                if ql and ql not in e.name.lower():
                    continue
                n += 1
                if len(items) >= lim:
                    truncated = True
                    continue
                try:
                    st = e.stat()
                    items.append({'name': e.name, 'type': 'dir' if is_dir else 'file',
                                  'size': None if is_dir else st.st_size,
                                  'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                                                         time.localtime(st.st_mtime))})
                except OSError:
                    items.append({'name': e.name, 'type': 'dir' if is_dir else 'file'})
        items.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        audit('list', str(p), True, ip=ip, n=n, q=q or '', ext=ext or '')
        return {'ok': True, 'dir': str(p), 'count': n, 'returned': len(items),
                'truncated': truncated, 'items': items}
    except Exception as e:
        audit('list', str(p), False, ip=ip, error=str(e)[:200])
        return {'ok': False, 'error': 'exception', 'detail': str(e)[:300]}


def stat_path(path, ip=''):
    if not enabled():
        return {'ok': False, 'error': 'fins-off'}
    p, err = _resolve(path)
    if err:
        return {'ok': False, 'error': err}
    try:
        if not p.exists():
            return {'ok': False, 'error': 'no-such-path', 'path': str(p)}
        st = p.stat()
        return {'ok': True, 'path': str(p), 'type': 'dir' if p.is_dir() else 'file',
                'size': st.st_size, 'mode': oct(st.st_mode),
                'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)),
                'ctime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_ctime)),
                'readonly': not os.access(str(p), os.W_OK)}
    except Exception as e:
        return {'ok': False, 'error': 'exception', 'detail': str(e)[:300]}


def status():
    c = _conf()
    return {'ok': True, 'enabled': enabled(),
            'off_flag': str(OFF_FLAG) if OFF_FLAG.exists() else None,
            'audit': str(AUDIT_FILE), 'conf': str(CONF_FILE),
            'limits': {'default_timeout_s': c.get('default_timeout_s'),
                       'max_timeout_s': c.get('max_timeout_s'),
                       'max_chars': c.get('max_chars'),
                       'max_file_bytes': c.get('max_file_bytes')},
            'deny_cmd': len(c.get('deny_cmd') or []),
            'deny_globs': len(c.get('deny_globs') or [])}
