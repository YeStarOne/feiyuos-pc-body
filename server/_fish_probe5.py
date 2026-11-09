# -*- coding: utf-8 -*-
"""1110 女仆探针5: 视角词压句尾(规则15) + 负向点背面 + 换 seed, 正面看脸定档。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SEED = 109020
SUBJ = ('maid version girl standing, full body, hands on hips, '
        'simple white background, gentle smile')
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
pos = pos + ', (facing the viewer:1.3), (looking at the viewer:1.25), (front view:1.2)'
neg = neg + ', (from behind:1.3), (back view:1.3), (rear view:1.25)'
base = M.CHARACTER_PRESETS['fish_maid']
M.ensure_comfy()
OUT = M.OUT_BASE / '_fish_probe5'
OUT.mkdir(parents=True, exist_ok=True)

for arm, (sm, d176, d17) in {'e0': (0.9, 0.5, 0.55), 'e1': (1.0, 0.3, 0.55),
                             'e2': (1.1, 0.3, 0.55), 'e3': (1.0, 0.3, 0.40)}.items():
    p = dict(base, strength=sm)
    if d17 != 0.55:
        p['dn17'] = d17
    if d176 != 0.5:
        p['face_dn'] = d176
    api = M.build_api(pos, neg, SEED, '_fish_probe5', preset=p)
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe5/%s_%d' % (arm, SEED)
    r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
    f = None
    deadline = time.time() + 300
    while time.time() < deadline and not f:
        time.sleep(4)
        hits = sorted(OUT.glob('%s_%d_*.png' % (arm, SEED)))
        f = hits[0] if hits else None
    print(arm, 'sm%.1f d176%.2f d17%.2f ->' % (sm, d176, d17), 'OK' if f else 'NONE', flush=True)
print('=== probe5 done ===')
