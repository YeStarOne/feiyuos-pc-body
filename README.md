# 肥鱼 PC 身体（feiyu-pc-body）

给你的 QQ 机器人（dsh / DeepSeek Harness 系 + OneBot 通道）接一套 **PC 桌宠身体**：
她在云上想开口，主人的电脑屏幕上就有一只桌宠替她说；她还配了一双**常驻眼睛**，
坐在你旁边看你打游戏，随时能翻出「刚才看到了什么」。

> 命名由来：家里的 QQ bot 是一只蓝色大肥鱼（人设详见她的角色卡），这套东西是她的「身体」。

```
┌─────────────────── 主人的 PC ───────────────────┐      ┌──────────────── 云服务器 ────────────────┐
│                                                  │      │                                           │
│  dsh-pet 桌宠(:7340)  ◄──┐                        │      │  eye-server(:8901, systemd)               │
│                          │                        │      │    │ 收帧组 → 视觉模型(GO/池双车道)        │
│  pc_body.py(:17860) ─────┤ 转发指令(broadcast/anim)│ HTTP │    ▼                                       │
│    ├ Bridge  服务器→气泡/动画 ───────────────────────────► │  notes.jsonl（她看到了什么）              │
│    └ Eye     抓屏3帧/轮 ──────────────────────────────►  │  ledger.jsonl（全程账本）                 │
│             (公网直连优先 ~0.13s，Tailscale 兜底)          │        ▲                                  │
│  wd_pc_body.py  看门狗(端口探活自愈)                       │  她的大脑(dsh-web) ── 4 个工具 ──┘        │
└──────────────────────────────────────────────────┘      └───────────────────────────────────────────┘
                                                   pet_say 双投递：同一句话 = 桌宠气泡 + QQ，两处永远一致
```

## 这是什么 / 不是什么

**是**：我们自己写的胶水层——PC 端总控、看门狗、服务器端眼睛服务、四个模型工具、全部护栏。
**不是**：桌宠本体。本仓库**不含** dsh-pet 安装包和它的美术资源（见下「隐私与版权边界」），
你需要另行安装 dsh-pet（standalone 模式，`node lib/standalone.js --port 7340`）。

## 功能

- **双处同步说话**：`pet_say(text, anim?, duration?)` 同一句话同时出现在桌宠气泡和 QQ，
  一边挂了另一边照发，结果里说清哪边没到。
- **远程做动作**：`pet_anim(anim)`，动作名是中文（优雅女仆舞 / 写代码 / 撸猫 / 睡觉-起来 …），
  全量清单 = 桌宠 assets/webm 目录的文件名。
- **常驻眼睛**：pc_body 每 36-68s 抓一轮 3 帧（800w JPEG q72，间隔 2.5s）推给 eye-server，
  视觉模型写成笔记。她可以 `game_eye(n)` 翻最近看到的东西，用来找话题、确认你在干嘛。
- **身体健康自查**：`pet_state()` 看桌宠在不在、眼睛有没有熔断、今日流量。

- **鱼鳍系统（2026-11-10）**：她能在主人 PC 上**读文件 / 写文件 / 跑命令**了。
  云端 shell 一条 `fin exec "dir D:\"`（或 `fin read / fin write / fin ls / fin stat / fin audit`），
  链路复用 47860 隧道；每条动作落 `fins_audit.jsonl`，`fins.off` 一线刹车，
  凭据类路径禁读、系统目录禁写、灾难命令黑名单。详见
  [docs/2026-11-10-鱼鳍系统与双桌宠修复.md](docs/2026-11-10-鱼鳍系统与双桌宠修复.md)。
- **五道流量护栏**（下表）＋**静默治理**＋**空闲降频**＋**看门狗自愈**。

## 护栏与节流（实测数据驱动）

| 机制 | 规则 | 效果/实测 |
|---|---|---|
| 闸0 黑名单窗口 | 交易软件/密码管理器等 | PC 端直接不抓帧，**服务端永远看不到** |
| 闸1 单轮硬顶 | 1.5MB | 超限丢弃 |
| 闸2 日限额 | 500MB 双端 | 熔断暂停，跨日自动恢复 |
| 闸3 连败退避 | 连败 3 次 | 60→120→240→300s 冷却 |
| 闸4 滑窗异常 | 10 分钟 >100MB | 自动暂停，`POST /resume` 解除 |
| 闸5 账本 | 全事件记账 | ledger.jsonl 可审计 |
| 节流器 | 同窗口 6min≥3 条笔记 | 追加强制沉默提示（实测 skip 率 0.7%→62.5%） |
| 复读去重 | 同窗口最近 6 条 difflib>0.72 | 不写不推 |
| 空闲降频 | 无输入 15min→≥3min/轮；45min→暂停 | 不半夜看锁屏 |
| 看门狗 | 60s 端口探活，1.5s 复检，DETACHED 拉起 | 进程挂了 ~70s 自愈 |
| 帧差跳过 | 帧差 <2% 不推送 | 静态画面零流量 |
| 池车道自动恢复 | 探活连 2 次成功 | FORCE_GO 自动退役回免费车道 |

## 快速开始

### 0. 前置

- PC：Windows、Python 3.10+（`psutil`）、Node.js、一个跑在 standalone 模式的 dsh-pet（:7340）
- 服务器：任意 Linux + systemd（用户级），Python 3；一个支持 OpenAI 兼容
  `image_url` 的视觉模型端点（如 opencode-go 的 GO 端点 / 你自己的池网关）

### 1. 共享令牌

```bash
# 生成 64 hex 令牌，两边放同一份
python -c "import secrets; print(secrets.token_hex(32))"
# PC:   存到 D:\StardewAI\dsh-pet\.eye-token
# 服务器: 存到 ~/.dsh/eye-server.token（chmod 600）
```

### 2. 服务器端（eye-server）

```bash
mkdir ~/pc-body && cp server/eye_server.py ~/pc-body/
# 模型 key 从环境读：~/.dsh/dsh-web.env 里放 OPENCODE_API_KEY（GO）与 ZCODE2API_KEY（池，可选）
# 池网关地址用环境变量 POOL_URL 覆盖模板占位符
systemctl --user 略——参考 eye_server.py 头部注释写一个 unit（MemoryMax=120M 建议）
curl -H "Authorization: Bearer $(cat ~/.dsh/eye-server.token)" http://127.0.0.1:8901/health
```

### 3. PC 端（pc_body + 看门狗）

```bat
:: 把三个文件放到 dsh-pet 安装目录，改 pc/pc_body.py 顶部的
:: BRIDGE_BIND / EYE_SERVERS（或设同名环境变量），然后
start_pet_body.bat
```

- 服务器→PC 的指令通道走 PC 的 Tailscale IP:17860（Bearer 同 eye-server.token）
- 云服务器安全组放行 **入方向 TCP 8901**（源可限你的出口 IP；有 Bearer 保护）

### 4. 给她装四个工具

把 `patches/dsh-onebot-qq-pet-tools.snippet.js` 粘进 dsh-onebot-qq 插件的
`lib/tools.js`（registerTools 末尾），重启 dsh-web。
⚠️ 该插件运行时读的是 `~/.dsh/profiles/web/node_modules/` 下的拷贝（非软链），
**源码区和运行份两处都要同步**——这是最容易白干的坑。

### 5. 教她规矩

AGENTS.md（她的角色卡）里建议写明：不是每条笔记都要 `pet_say`；黑名单承诺不许绕过；
只能看和说话，代打未开放。

## 安卓平板陪看视频（桌宠方案）

她在平板上"陪你追剧"由两条链路拼成，**零安卓开发**：

**眼睛——四条通道，按优先级自动选**

| 优先级 | 通道 | 适用 | 起法 |
|---|---|---|---|
| ① | **hdc 直连（USB）** | 纯血鸿蒙（HarmonyOS NEXT，无 adb）+ 标准安卓 | 数据线插 PC，开发者模式开 USB 调试。**插着即用，什么都不用点** |
| ② | Windows 无线显示器（Miracast） | 安卓 / 鸿蒙都行，零线 | Win+K 开「连接」窗，平板下拉控制中心→无线投屏→选这台电脑 |
| ③ | scrcpy（adb） | 标准安卓 | USB 调试 → `scrcpy --no-audio --stay-awake` |
| ④ | 华为电脑管家多屏协同 | 华为 PC + 华为平板 | 管家 → 多屏协同 |

hdc 直连的原理：`hdc shell snapshot_display` 截平板屏（1920×2880）→ 设备端 `base64` 走
stdout 拉回（`hdc file recv` 与 NEXT 7 文件协议不兼容，一跑就卡死通道）→ PC 压到 800 宽
与 PC 眼睛同口径。实测 1.05s/帧、3 帧 5.6s。**每轮先拍平板一帧探动静：画面在动才走平板
通道，静止就自动回看 PC 前台**——平板插着充电也不会顶掉 PC 眼睛。

平板侧一次性设置（HarmonyOS NEXT）：`设置→关于本机→连点版本号 7 次`（5.x 需重启生效）→
`设置→系统和更新→开发人员选项→USB 调试` 开启。Windows 侧需要 hdc.exe：官方不给独立
下载，从 [Hadice 便携版](https://github.com/HuolalaTech/hadice/releases)（20MB）解压拿
`bin/hdc/hdc.exe + libusb_shared.dll`（官方 OpenHarmony 3.2 二进制，实测兼容 NEXT 7.0），
放到 pc_body 同目录的 `bin/hdc/` 下即可。平板插上后 `/health` 的 `tablet_hdc` 变 `"yes"`。

任意通道通了，`app_type=video`，eye-server 切到「陪看视频」提示词（朋友追剧语气）。
她就"坐进"你的视频里了：`game_eye` 能翻出刚才的剧情笔记，想吐槽就 `pet_say`。

注意：

- **别把投屏窗口最小化**（最小化抓出来是黑帧，pc_body 会自动跳过并回退到前台窗口）。
  窗口可以缩小、放角落，或拖到副屏。
- 接收端**开着但没设备连着**时 `/health` 报 `tablet_window: "standby"`，眼睛不抓那张静态
  待机画面（白烧 token）。设备投上来窗口铺满 → 自动变 `"yes"`，不用重启。
- 走 Miracast 前 PC 要确认：`设置→系统→投影到此电脑` 允许，且 Wi-Fi 网络类别不是「公用」
  （公用网络下不广播，平板搜不到）。
- ⚠️ **hdc 设备在 `list targets` 里时有时无、shell 报 "communication channel is being
  established" 都是正常握手抖动**，重试即可；真卡死 `hdc kill` 重启守护进程立刻恢复。
- 平板想脱线：开发者选项里有「无线调试」，`hdc tconn <平板IP>:<端口>` 可转 WiFi
  （USB 首配对后生效）。

**桌宠——平板浏览器开一个页面（不装任何 APK）**

1. 平板装 Tailscale App 登同一账号（这样平板能进内网）
2. 浏览器打开：`http://<PC的Tailscale IP>:17860/pet?token=<共享令牌>`
3. 页面底部站着小鱼（webm 动画），气泡+动画跟随每次 `pet_say` 实时出现（SSE 推送），
   左上有最近消息历史。页面只在 Tailscale 内网可达，token 走 query 参数
4. 想要"全屏陪伴"就浏览器全屏横屏——效果就是一只鱼站在视频软件旁边

浏览器方案和 APK 悬浮窗方案二选一：网页版零安装、够用；以后要"系统级悬浮窗压在
视频 App 上"再写小 APK（SYSTEM_ALERT_WINDOW + 同一个 /events 接口），接口已经留好。

## 已知坑（都踩过，别再踩）

1. **插件双份拷贝**：改了源码区不同步运行份 = 白改（她调工具报 unknown tool）。
2. **Windows SO_REUSEADDR 双绑**：`http.server` 默认允许两个进程绑同一端口 →
   桥必须独占绑定（`allow_reuse_address = False`），且 bind 失败要**退出整个进程**
   （桥在 daemon 线程里，不退出就是"僵尸眼"——眼睛在跑、身体不听指令）。
3. **Clash TUN 等代理劫持**：Python urllib 读系统代理对内网 IP 会拿到错误结果，
   全程 `build_opener(ProxyHandler({}))` 直连。
4. **bat 启动五坑**：`start` 的重定向要包 `cmd /c` 三引号；纯 ASCII + CRLF；
   启动器拉起的子进程会被终端任务树连坐——所以所有拉起统一归看门狗（DETACHED）。
5. **服务器低内存**：eye-server 给 `MemoryMax=120M`，否则 OOM 杀手挑软柿子捏。
6. **纯血鸿蒙没有 ADB**：HarmonyOS 5 设备开 USB 调试后 PnP 只出「HDC Interface」，
   `adb devices` 永远是空的，scrcpy 物理上不可行 → 走 Miracast 无线投屏。
7. **Win11 的「连接」接收端进程不是 WWAHost**：它挂在 `ApplicationFrameHost.exe` 下，
   窗口标题就叫「无线显示器」。按旧版 `WWAHost` 判断会永远找不到投屏窗。

## 隐私与版权边界

- 本仓库**不含**：桌宠安装包与美术资源（疑似含游戏解包资源，绝不公开）、
  令牌、日志、notes.jsonl（她的屏幕观察记录 = 主人隐私）、任何真实 IP
  （代码里的地址全部是占位符，用环境变量或直接改默认值填自己的）。
- 黑名单窗口在 PC 端就地拦截，服务端根本收不到帧——这是给她的承诺，也是给你的承诺。

## License

MIT
