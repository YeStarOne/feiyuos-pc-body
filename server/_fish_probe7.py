# -*- coding: utf-8 -*-
"""1110 女仆探针7: f 臂参数 × 多 seed 验证克隆是 seed 彩票还是锚致(规则105)。
seed 109031/109032 × (f0参数, f1参数) = 4 张。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SUBJ = ('maid version girl standing, full body, hands on hips, '
        'simple white background, gentle smile')
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
pos = pos + ', (facing the viewer:1.3), (looking at the viewer:1.25), (front view:1.2)'
neg = neg + ', (from behind:1.3), (back view:1.3), (rear view:1.25), (tall:1.2), (mature:1.2)'
base = M.CHARACTER_PRESETS['fish_maid']
M.ensure_comfy()
OUT = M.OUT_BASE / '_fish_probe7'
OUT.mkdir(parents=True, exist_ok=True)

ARMS = {'g0': (0.9, 0.5, 0.55), 'g1': (1.0, 0.3, 0.55)}
for seed in (109031, 109032):
    for arm, (sm, d176, d17) in ARMS.items():
        p = dict(base, strength=sm, face_dn=d176, dn17=d17)
        api = M.build_api(pos, neg, seed, '_fish_probe7', preset=p)
        api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe7/%s_%d' % (arm, seed)
        r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
        f = None
        deadline = time.time() + 300
        while time.time() < deadline and not f:
            time.sleep(4)
            hits = sorted(OUT.glob('%s_%d_*.png' % (arm, seed)))
            f = hits[0] if hits else None
        print('%s_%d sm%.2f d176%.2f ->' % (arm, seed, sm, d176), 'OK' if f else 'NONE', flush=True)
print('=== probe7 done ===')
