# -*- coding: utf-8 -*-
"""1110 女仆脸探针: LoRA 脸被 176(dn0.5)/节点17(dn0.55) 洗掉 —— 同 seed 109009 找回。

臂:
  b1 sm1.0 / 176dn0.5 / 17dn0.55   b2 sm1.1 / 176dn0.5 / 17dn0.55
  b3 sm1.0 / 176dn0.3 / 17dn0.55   b4 sm1.1 / 176dn0.3 / 17dn0.55
  b5 sm1.0 / 176dn0.3 / 17dn0.40
对照 = fyi_109009 (sm0.9 / 0.5 / 0.55)
"""
import copy, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SEED = 109009
SUBJ = 'maid version girl standing full body, hands on hips, simple white background, gentle smile'
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
base = M.CHARACTER_PRESETS['fish_maid']

ARMS = {'b1': (1.0, 0.5, 0.55), 'b2': (1.1, 0.5, 0.55), 'b3': (1.0, 0.3, 0.55),
        'b4': (1.1, 0.3, 0.55), 'b5': (1.0, 0.3, 0.40)}

for arm, (sm, d176, d17) in ARMS.items():
    p = dict(base, strength=sm)
    api = M.build_api(pos, neg, SEED, '_fish_probe2', preset=p)
    api['176']['inputs']['denoise'] = d176
    api['17']['inputs']['denoise'] = d17
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe2/%s_%d' % (arm, SEED)
    r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
    f = M._wait_file(r['prompt_id'], SEED, M.OUT_BASE / '_fish_probe2', timeout=300)
    print(arm, 'sm%.1f d176%.2f d17%.2f ->' % (sm, d176, d17), 'OK' if f else 'NONE', flush=True)
print('=== probe2 done ===')
