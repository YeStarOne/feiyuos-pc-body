# -*- coding: utf-8 -*-
"""1110 女仆探针6: 持续复刻标准像(少女体型锚已入预设) × 脸恢复四臂。同 seed 109030。
subject = 标准像构图: 正面全身叉腰白底微笑; 视角词压句尾(规则15), 负向压背面。
  f0 sm0.9/176dn0.5/17dn0.55 | f1 sm1.0/0.3/0.55 | f2 sm1.1/0.3/0.55
  f3 sm1.0/0.3/17dn0.40      | f4 sm1.2/0.25/17dn0.40 (激进)
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SEED = 109030
SUBJ = ('maid version girl standing, full body, hands on hips, '
        'simple white background, gentle smile')
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
pos = pos + ', (facing the viewer:1.3), (looking at the viewer:1.25), (front view:1.2)'
neg = neg + ', (from behind:1.3), (back view:1.3), (rear view:1.25), (tall:1.2), (mature:1.2)'
base = M.CHARACTER_PRESETS['fish_maid']
M.ensure_comfy()
OUT = M.OUT_BASE / '_fish_probe6'
OUT.mkdir(parents=True, exist_ok=True)

for arm, (sm, d176, d17) in {'f0': (0.9, 0.5, 0.55), 'f1': (1.0, 0.3, 0.55),
                             'f2': (1.1, 0.3, 0.55), 'f3': (1.0, 0.3, 0.40),
                             'f4': (1.2, 0.25, 0.40)}.items():
    p = dict(base, strength=sm, face_dn=d176, dn17=d17)
    api = M.build_api(pos, neg, SEED, '_fish_probe6', preset=p)
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe6/%s_%d' % (arm, SEED)
    r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
    f = None
    deadline = time.time() + 300
    while time.time() < deadline and not f:
        time.sleep(4)
        hits = sorted(OUT.glob('%s_%d_*.png' % (arm, SEED)))
        f = hits[0] if hits else None
    print(arm, 'sm%.2f d176%.2f d17%.2f ->' % (sm, d176, d17), 'OK' if f else 'NONE', flush=True)
print('=== probe6 done ===')
