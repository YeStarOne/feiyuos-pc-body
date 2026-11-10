#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1111 补跑机位2/3(前脚本漏了节流间隔)。"""
import json
import time
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
    'clientInfo': {'name': 'fish-final2', 'version': '1'}}})
call({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

POSES = [
    'whale maid girl, maid version, standing in her cozy room, waving, happy open-mouth smile, warm indoor light',
    'whale maid girl, maid version, upper body, head tilt one side, big smile, blush, simple background',
]

rid = 20
for i, subj in enumerate(POSES):
    time.sleep(4.0)                      # QQ 发图节流: 上一单完成→下一单 ≥3s
    r = call({'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call', 'params': {
        'name': 'gen_image',
        'arguments': {'subject': subj, 'count': 2}}})
    rid += 1
    payload = json.loads(r['result']['content'][0]['text'])
    if not payload.get('ok'):
        print('POSE%d REJECT: %s' % (i + 2, payload.get('reason')), flush=True)
        continue
    print('POSE%d ok -> %s' % (i + 2, json.dumps(payload.get('images', []), ensure_ascii=False)), flush=True)
print('DONE', flush=True)
