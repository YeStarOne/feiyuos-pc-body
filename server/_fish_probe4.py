# -*- coding: utf-8 -*-
"""1110 女仆探针4: 正面构图(能看脸) × 脸恢复臂。同 seed 109009。
对照 = c1(背面, 构图对/脸不可见)。
臂: d0 修锚 sm0.9/0.5/0.55 | d1 sm1.0/0.3 | d2 sm1.1/0.3 | d3 sm1.0/0.3+17dn0.4
"""
import glob, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SEED = 109009
SUBJ = ('maid version girl standing facing the viewer, full body, hands on hips, '
        'simple white background, gentle smile')
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
base = M.CHARACTER_PRESETS['fish_maid']
M.ensure_comfy()
OUT = M.OUT_BASE / '_fish_probe4'
OUT.mkdir(parents=True, exist_ok=True)

for arm, (sm, d176, d17) in {'d0': (0.9, 0.5, 0.55), 'd1': (1.0, 0.3, 0.55),
                             'd2': (1.1, 0.3, 0.55), 'd3': (1.0, 0.3, 0.40)}.items():
    p = dict(base, strength=sm)
    if d17 != 0.55:
        p['dn17'] = d17
    if d176 != 0.5:
        p['face_dn'] = d176
    api = M.build_api(pos, neg, SEED, '_fish_probe4', preset=p)
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe4/%s_%d' % (arm, SEED)
    r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
    # 臂名前缀的专用等待(修 probe2/3 的 fyi_* 通配坑)
    f = None
    deadline = time.time() + 300
    while time.time() < deadline and not f:
        time.sleep(4)
        hits = sorted(OUT.glob('%s_%d_*.png' % (arm, SEED)))
        f = hits[0] if hits else None
    print(arm, 'sm%.1f d176%.2f d17%.2f ->' % (sm, d176, d17), 'OK' if f else 'NONE', flush=True)
print('=== probe4 done ===')
