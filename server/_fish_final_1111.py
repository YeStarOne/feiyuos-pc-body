#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1111 女仆版美图直出: 3 机位 × 2 seed, 走 18082 生产链(闸/洗图/记账全自动)。"""
import json
import sys
import urllib.request

URL = 'http://127.0.0.1:18082/mcp'
SID = [None]


def call(body):
    hdr = {'Content-Type': 'application/json',
           'Accept': 'application/json, text/event-stream'}
    if SID[0]:
        hdr['mcp-session-id'] = SID[0]
    req = urllib.request.Request(URL, data=json.dumps(body).encode('utf-8'), headers=hdr)
    r = urllib.request.urlopen(req, timeout=900)
    sid = r.headers.get('mcp-session-id')
    if sid:
        SID[0] = sid
    raw = r.read().decode('utf-8')
    if not raw.strip():
        return None
    if raw.startswith('event:'):
        raw = raw.split('data:', 1)[1].strip()
    return json.loads(raw)


call({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {},
    'clientInfo': {'name': 'fish-final', 'version': '1'}}})
call({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

POSES = [
    # 主参考复刻: 叉腰全身白底
    'whale maid girl, maid version, standing, hand on hip, full body, gentle smile, white background',
    # 房间场景美图: 招手
    'whale maid girl, maid version, standing in her cozy room, waving, happy open-mouth smile, warm indoor light',
    # 半身可爱: 歪头
    'whale maid girl, maid version, upper body, head tilt one side, big smile, blush, simple background',
]

rid = 10
for i, subj in enumerate(POSES):
    r = call({'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call', 'params': {
        'name': 'gen_image',
        'arguments': {'subject': subj, 'count': 2}}})
    rid += 1
    payload = json.loads(r['result']['content'][0]['text'])
    if not payload.get('ok'):
        print('POSE%d REJECT: %s' % (i + 1, payload.get('reason')), flush=True)
        continue
    print('POSE%d ok -> %s' % (i + 1, json.dumps(payload.get('images', []), ensure_ascii=False)), flush=True)
print('DONE', flush=True)
