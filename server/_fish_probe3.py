# -*- coding: utf-8 -*-
"""1110 女仆探针3: 构图修正(鲸尾=从腰后长, 压背景鲸) × 脸恢复(LoRA权重/脸重画强度)。

同 seed 109009, 对照 = fyi_109009(旧锚, 伴鲸构图, sm0.9/176dn0.5/17dn0.55):
  c1 修锚+neg_extra / sm0.9 / 176dn0.5 / 17dn0.55  (只验构图)
  c2 = c1 + sm1.0 / 176dn0.3                        (脸臂)
  c3 = c1 + sm1.1 / 176dn0.3                        (脸臂)
  c4 = c1 + sm1.0 / 176dn0.3 / 17dn0.40             (脸+全身重画双压)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feiyu_image_mcp as M

SEED = 109009
SUBJ = 'maid version girl standing full body, hands on hips, simple white background, gentle smile'
pos, neg, err, pk = M.compose(SUBJ)
assert err is None and pk == 'fish_maid'
base = M.CHARACTER_PRESETS['fish_maid']
M.ensure_comfy()

for arm, (sm, d176, d17) in {'c1': (0.9, 0.5, 0.55), 'c2': (1.0, 0.3, 0.55),
                             'c3': (1.1, 0.3, 0.55), 'c4': (1.0, 0.3, 0.40)}.items():
    p = dict(base, strength=sm)
    if d17 != 0.55:
        p['dn17'] = d17
    if d176 != 0.5:
        p['face_dn'] = d176
    api = M.build_api(pos, neg, SEED, '_fish_probe3', preset=p)
    api['77']['inputs']['filename_prefix'] = '肥鱼OS/_fish_probe3/%s_%d' % (arm, SEED)
    r = M._post('/prompt', {'prompt': api, 'client_id': 'probe'})
    f = M._wait_file(r['prompt_id'], SEED, M.OUT_BASE / '_fish_probe3', timeout=300)
    print(arm, 'sm%.1f d176%.2f d17%.2f ->' % (sm, d176, d17), 'OK' if f else 'NONE', flush=True)
print('=== probe3 done ===')
