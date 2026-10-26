# ResinLoop · 游戏每日任务未完成提醒

> **ResinLoop（日常巡查）**：名字取自「树脂循环」——原神的树脂、崩铁的开拓力、
> 绝区零的电量，这些每天重置一次的体力就是「日常」本身，也是它每一轮要巡检的东西。

只做「查询 → 判断 → 提醒」三件事：**不签到、不领奖励、不修改任何游戏数据**。

内置常驻守护进程 + 系统托盘，**不需要 Windows 任务计划**；所有配置都在网页控制台里完成。

---

## 零、三步走（给不想折腾的人）

前提：装好 Python 3.10+，安装时勾选 **Add python.exe to PATH**。

```
1. 双击 start.bat
     → 自动装依赖 → 后台拉起守护进程 → 弹出独立的控制台窗口

2. 窗口里配三个页签
     账号绑定  ：贴设备指纹（推荐）→ 扫码登录
     通知绑定  ：填 Server酱 / Qmsg 的 Key → 发送测试推送
     定时与状态：加提醒时间点 → 启动守护进程

3. 挂机
     右下角托盘常驻，关掉窗口不影响；到点自动检查并推送
```

托盘图标右键：**打开控制台 / 立即执行一次 / 退出程序**。
其中「打开控制台」会重新拉起那个独立窗口，不需要再去浏览器里找标签页。

---

## 一、目录结构与文件职责

```
check in/
├── start.bat               # 一键启动：装依赖 + 守护进程 + 独立控制台窗口
├── daemon.py               # 常驻守护进程：30 秒轮询 check_times、托盘、防重复触发
├── app.py                  # 桌面入口：pywebview 独立窗口，内嵌本地控制台
├── main.py                 # 命令行入口：run_once 编排（查询→判断→推送→退出码）
├── login_ui.py             # 网页控制台本体：账号绑定 / 通知绑定 / 定时与状态
├── login_ui.bat            # 只开网页版控制台（调试用，会走浏览器）
├── run.bat                 # 手动跑一次检查（不涉及调度）
├── tools/设备信息.apk       # 手机端「设备信息」App，供扫码下载
├── config.json             # 本地私有配置（含凭据）。首次运行自动生成，.gitignore 已排除
├── config.example.json     # 配置示例，字段与默认值同源（core/config.py 的 DEFAULT_CONFIG）
├── requirements.txt        # 依赖清单（requests / qrcode / pillow / pystray / pywebview）
├── .gitignore              # 排除 config.json / logs / 缓存 / APK
├── logs/                   # 运行时创建：dailycheck.log、desktop.log、device_fp.json、daemon.pid 等
└── core/
    ├── __init__.py         # 包声明与分层说明
    ├── models.py           # 数据模型：TaskItem / GameStatus / PushResult
    ├── config.py           # 默认值、强类型对象、load/save/校验、check_times 计算
    ├── mihoyo_api.py       # 米游社 HTTP 层：DS 签名 / 设备指纹 / 设备档案覆盖 / 风控重试
    ├── skland_api.py       # 森空岛 HTTP 层：HMAC+MD5 签名、cred 缓存与 401 自动重换
    ├── querier.py          # 查询层：Mock / Miyoushe / Skland + provider 注册表与回退链
    ├── analyzer.py         # 判断层：汇总未完成项，渲染控制台文本 / 推送文案
    ├── notifier.py         # 推送层：Bark / Server酱 / Qmsg / 飞书 / 钉钉
    ├── tools_server.py     # 局域网只读文件服务：只为手机下载 tools/ 里的 APK
    └── logger.py           # 日志初始化：控制台 + 按天切分的文件日志
```

分层原则：查询层只负责「拿到状态」，判断层只负责「要不要催、催什么」，推送层只负责「发出去」，守护进程只负责「什么时候跑」，配置与日志横切。

---

## 二、安装依赖

```bash
# 建议先建虚拟环境（Windows Git Bash / PowerShell 均可）
python -m venv .venv
source .venv/Scripts/activate      # Windows
# source .venv/bin/activate        # macOS / Linux

pip install -r requirements.txt
# 或者直接：
pip install "requests>=2.28.0" "qrcode>=7.4" "pillow>=10.0" "pystray>=0.19" "pywebview>=5.0"
# （qrcode/pillow 用于二维码与托盘图标，pystray 提供托盘菜单，
#   pywebview 提供独立桌面窗口，Windows 上复用系统自带 WebView2）
```

> 用 `start.bat` 启动的话，这一节可以跳过 —— 它会先 `import` 试一遍，缺依赖自动装上。
>
> **`pywebview` 没装也能用**：`app.py` 会捕获导入失败，弹一句提示后自动退回默认浏览器，
> 控制台功能不受影响。想彻底退回浏览器版也可以 `python login_ui.py`。
>
> `requests` 采用惰性导入：即使没装，脚本仍能完成查询、判断、控制台输出和日志，只在真正推送时报「缺少依赖 requests」并以退出码 3 结束。
>
> 没装 `pystray` 时守护进程不会崩，会打印一条警告并**转为无界面常驻**（托盘没了，调度照常）。

---

## 三、配置

首次运行生成默认配置：

```bash
python main.py --init
```

`config.json` 示例（即 `config.example.json`）：

```json
{
  "app": {
    "test_mode": "auto",
    "check_times": ["08:30", "20:30"]
  },
  "accounts": [
    {
      "game": "genshin",
      "nickname": "旅行者",
      "enabled": true,
      "credential": {
        "uid": "100000000",
        "server": "cn_gf01",
        "cookie": "ltuid=请填写; ltoken=请填写"
      }
    },
    {
      "game": "starrail",
      "nickname": "开拓者",
      "enabled": true,
      "credential": {
        "uid": "100000000",
        "server": "prod_gf_cn",
        "cookie": "ltuid=请填写; ltoken=请填写"
      }
    },
    {
      "game": "zzz",
      "nickname": "绳匠",
      "enabled": true,
      "credential": {
        "uid": "100000000",
        "server": "prod_gf_cn",
        "cookie": "ltuid=请填写; ltoken=请填写"
      }
    },
    {
      "game": "arknights",
      "nickname": "博士",
      "enabled": true,
      "credential": { "uid": "10000000", "token": "请填写森空岛 token" }
    },
    {
      "game": "endfield",
      "nickname": "管理员",
      "enabled": true,
      "credential": { "uid": "10000000", "token": "请填写森空岛 token" }
    }
  ],
  "push": {
    "channels": ["bark"],
    "bark": {
      "enabled": true,
      "server": "https://api.day.app",
      "device_key": "",
      "group": "游戏日常",
      "sound": "birdsong",
      "icon": "",
      "level": "active",
      "timeout": 10
    }
  },
  "query": { "provider": "mock", "timeout": 10 },
  "logging": { "dir": "logs", "level": "INFO", "keep_days": 14 }
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `app.test_mode` | `auto` 自动判断 / `force_alert` 强制提醒 / `force_silent` 强制静默 |
| `app.check_times` | 每日检查时间点（HH:MM，24 小时制），本阶段用于日志展示"下次检查" |
| `accounts[].game` | 游戏标识：`genshin` / `starrail` / `zzz` / `arknights` / `endfield` |
| `accounts[].enabled` | 是否参与检查，`false` 直接跳过 |
| `accounts[].credential` | 凭证：米游社三款见下节；森空岛系本阶段不使用 |
| `push.channels` | 启用的推送通道，目前只实现 `bark` |
| `push.bark.device_key` | Bark App 首页那串 Key；留空时推送会跳过并提示 |
| `push.bark.server` | 自建 Bark 服务端地址，官方默认 `https://api.day.app` |
| `query.provider` | `mock` 全部写死数据；`miyoushe` 三款走真实接口、其余回退 mock |
| `logging.dir` | 相对路径按项目根目录解析，可写绝对路径 |

代码里读写配置：

```python
from core.config import load_config, save_config

cfg = load_config("config.json")          # 缺失/非法字段抛 ConfigError，消息是中文可读的
cfg.test_mode = "force_silent"
save_config(cfg, "config.json")           # UTF-8、缩进 2、保留中文；不传路径则写回原路径
```

---

## 四、米游社接入（原神 / 崩铁 / 绝区零）

### 4.1 需要填的字段

三款游戏的 `credential` 结构一致（字段名统一定义在 `core/mihoyo_api.py` 的 `COOKIE_FIELDS`，全局只有这一处）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `uid` | 是 | 游戏内角色 UID（原神 9 位、崩铁/绝区零 9 位左右，个人资料页可见），不是米游社账号 ID |
| `cookie` | 是（方式一） | 整串 Cookie，脚本自动抽取 `ltuid` 和 `ltoken` |
| `ltuid` / `ltoken` | 是（方式二） | 也可以直接分字段给，二选一 |
| `server` | 否 | 区服；不填用默认值：原神 `cn_gf01`、崩铁/绝区零 `prod_gf_cn`。原神渠道服填 `cn_qd01` |

### 4.2 Cookie 从浏览器哪里取

1. 电脑浏览器打开 [bbs.mihoyo.com](https://bbs.mihoyo.com)（米游社）并登录；
2. 按 `F12` 打开开发者工具 → `Application`（应用）标签 → 左侧 `Cookies` → 选中 `https://bbs.mihoyo.com`；
3. 在列表里分别找到 **`ltuid`** 和 **`ltoken`** 两行，复制它们的值；
4. 或者用更省事的整串方式：`Network`（网络）标签 → 刷新页面 → 点任意一个发往 `mihoyo.com` 的请求 → `Request Headers` → 复制整行 `Cookie` 的值，原样粘进 `credential.cookie`（脚本会自己抽取）。

> Cookie 是登录凭证，等同于密码：只放在本地 `config.json`（已被 .gitignore 排除），不要提交、不要发给别人。Cookie 过期后日志会报 `retcode=10001`，重新抓一次即可。

### 4.3 DS 签名（已内置，无需你填）

米游社 game_record 接口要求 `DS` 签名请求头。脚本已内置并实测验证（2026-10-26，假 Cookie 探针，三个端点的 DS 均通过服务端校验）：

- **算法**：DS2（`md5(salt=..&t=..&r=..&b=&q=<按字典序排序的query>)`）
- **DS_SALT**：`xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs`（.so 提取的 x4 盐，跨版本稳定）
- **APP_VERSION**：`2.109.0`（米游社 App 当前版本号，来自 MihoyoBBSTools 主干）
- 备用盐 `DS_SALT_X6`（t0qEgfub6cvueAPgR5m9aQWWVciEer7v）与 DS1 盐也已写进 `core/mihoyo_api.py` 备查

实测细节：崩铁便笺会**拒绝 DS1**（retcode -10001 invalid request），所以统一用 DS2+x4，三款全过。关键判定方法：用假 Cookie 请求，返回 10001（请登录）说明 DS 已通过、卡在凭证；返回 -10001 说明 DS 被拒。

如果某天日志报 `retcode=-10003`（DS 校验失败），说明盐/版本被官方轮换，去 MihoyoBBSTools 的 `setting.py` 或 paimon-webext 抄当前值替换 `core/mihoyo_api.py` 顶部的 `DS_SALT` / `APP_VERSION` 即可。

> 注意：DS 通过不代表不会触发风控。如果带真实 Cookie 后报 `retcode=1034`，说明触发了极验验证码，需要设备指纹（device_fp）方案，见 4.4。

### 4.4 设备指纹与风控（GsCore 方案 + 代理换 IP）

dailyNote 除了 DS 还强制校验**设备指纹**。本脚本的米游社请求层是按 **GsCore（`Genshin-bots/gsuid_core`）** 的 `gsuid_core/utils/api/mys/{base_request,tools,api}.py` 逐字移植的：

1. **真机环境**：`getFp` 的 `ext_fields` 是 53 个字段的完整 Android Build 环境（**默认用你手机实测导出的档案**：Redmi 24122RKC7C / 机型代号 miro / 主板 sun / Android 16 / HyperOS 3.0.305.0 / OAID / 加速度计 / 陀螺仪 / 内存 / ROM / 运营商 / 电量 / 安装时间 …），字段名、顺序、字符串/数字类型与 GsCore `generate_fp` 完全一致；
2. **请求头**：`x-rpc-app_version=2.102.1`、`x-rpc-client_type=5`、`X-Requested-With: com.mihoyo.hyperion`、UA 为 `Android 16; 24122RKC7C Build/BP2A.250605.031.A3`、`Referer/Origin: https://webstatic.mihoyo.com/`（尾斜杠也照抄）；
3. **设备注册**：首次使用某设备时调 `deviceLogin` → `saveDevice`（通行证盐 DS，body 的 `device_name=Redmi24122RKC7C` / `os_version=36` / 19 位 `registration_id`），保证"注册设备 = 查询设备"；
4. **风控重试**（GsCore `_DEAD_CODE` 分支）：命中 `5003 / 10041 / 1034 / 10035` 时，自动追加 `x-rpc-challenge_game` / `x-rpc-page` / `x-rpc-tool-verison` 三个挑战头，重算 DS 后再试一次；
5. **指纹缓存**：`logs/device_fp.json`，按「游戏 + UA + 机型 + OAID + 系统版本 + 代理」区分，任一变化自动重签，不会拿旧 IP 的指纹去查新 IP。

> 换手机时只改一处：`core/mihoyo_api.py` 顶部的 `DEVICE_MODEL`（brand / model / device / board / oaid / device_info / os_version / sdk_version / screen_size / display），并按同样内容改 `USER_AGENT`。手机上用「设备信息」类 App 可以直接导出这些值。

#### 代理（换出口 IP，当前最有效的办法）

`getFp` 是**按出口 IP 签发**的：同一个出口 IP，无论换多少 device_id / seed / ext_fields，返回的 `device_fp` 都是同一个（实测反复得到同一个指纹，形如 `38xxxxxxxxxxx`）。所以 IP 被标记时，换设备身份没用，必须换出口。

```json
"query": {
  "provider": "miyoushe",
  "mihoyo_enabled": true,
  "proxy": "http://127.0.0.1:7890",
  "mihoyo_device_mode": "auto"
}
```

- 代理会**同时**用于 `getFp` / `deviceLogin` / `saveDevice` / `dailyNote` / 扫码登录——只在 dailyNote 上挂代理是无效的（getFp 仍从本机 IP 签指纹）；
- 也可用命令行临时覆盖，不改配置：`python main.py --probe-mihoyo --proxy http://127.0.0.1:7890`；
- `socks5://` 代理需额外装 `pip install "requests[socks]"`；只写 `127.0.0.1:7890` 会自动补 `http://`；
- 留空则直连；如果系统设置了 `HTTP_PROXY/HTTPS_PROXY` 环境变量，requests 会自动沿用（探针会打印"系统代理"提示）。

#### 一条命令验证代理与新指纹

```bash
python main.py --probe-mihoyo
```

输出：出口 IP → 新签发的 `device_fp` → `deviceLogin/saveDevice` retcode → `auto`（带设备指纹）与 `gs`（不带设备头）两种请求的 retcode。换代理后先跑这个，能立刻判断"IP 有没有换掉、指纹有没有变、风控有没有解"。

#### 设备模式（`query.mihoyo_device_mode`）

| 值 | 行为 |
|---|---|
| `auto`（默认） | 带设备指纹请求；命中风控码自动补 challenge 头重试一次（GsCore 行为） |
| `fp` | 始终带设备头，不做重试（纯对比用） |
| `gs` | 完全不带设备头，只发基础 8 个头（排障用，验证"设备指纹到底有没有用"） |

> 历史排查记录：2026-10-26 先后试过 iOS/安卓/okhttp UA × client_type 2/5 × DS2 双盐 × 新旧指纹 × 完整 App 设备头，全部稳定 5003/10041；`getGameRecordCard` 返回 0、错误 UID 返回 10102，证明 Cookie / DS / 请求头全部有效，问题只在 dailyNote 的指纹校验。最终定位为**出口 IP 的指纹被标记**（与 Hutao 文档"同 IP 频繁交互会被封禁、无感验证无法绕过"一致），故引入代理支持。

**2026-10-26 04:00 实测突破（当前已可用）**：把设备档案换成手机实测值（Redmi 24122RKC7C / Android 16）后：

- 出口 IP 发生变化后，getFp 返回**全新指纹**（不再是此前被标记的那个），retcode 归一 0；
- `deviceLogin=0`、`saveDevice=0`；
- `auto` 模式 dailyNote **`retcode=0`**，拿到真实数据：树脂 `97/200`、每日委托 `4/4`；
- 同账号 `gs` 模式（不带设备头）仍 `5003` —— 说明设备指纹确实生效，且这次签发的指纹被服务端接受。

本次同时变了「机型档案」和「出口 IP」两个变量，无法单独归因；但可以确定：**GsCore 的 ext_fields + 真机机型 + 有效指纹这套链路是通的**，之前失败纯粹是拿了一个被标记的 IP 指纹。

### 4.5 米游社扫码登录（解决 5003 风控，推荐）

Cookie 从浏览器手动复制容易过期，而且**登录设备和查询设备对不上**会触发风控（5003/1034）。用内置扫码登录，让 Cookie 和设备指纹一次性对齐：

```bash
python main.py --login-mihoyo
```

流程（协议核对自 UIGF 文档 + AUTO-MAS 现行实现）：

1. 终端打印 ASCII 二维码（同时保存 `qr_login.png`）；
2. 手机【**米游社 App**】扫一扫 → 在手机上点确认；
3. 程序从 `Set-Cookie` 自动取出 `ltuid` / `ltoken` / `cookie_token`（只给 stoken_v2 时自动用 `getLTokenBySToken` 派生 ltoken）；
4. **自动写入** config.json 里原神 / 崩铁 / 绝区零三个账号的 `credential.cookie`（uid/server 原样保留）；
5. **设备绑定**：扫码用的 `x-rpc-device_id` 会写进 `logs/device_fp.json`，并为它重新签发 `device_fp`，之后三款游戏的 dailyNote 请求用的就是"扫码登录那台设备"；
6. 自动把 `query.mihoyo_enabled` 改回 `true`。

验收：

```bash
python main.py --dry-run --debug
```

如果三款游戏从 `retcode=5003` 变成正常数据，说明设备绑定生效；若仍报 5003（账号被标记较深），保持 `mihoyo_enabled: false` 即可，不影响森空岛与推送。

> 扫码接口细节：`POST passport-api.miyoushe.com/account/ma-cn-passport/web/createQRLogin`（必带 `x-rpc-app_id: bll8iq97cem8` + `x-rpc-device_id`）→ `POST .../web/queryQRLoginStatus`（`Created/Scanned/Confirmed`，`-3501` 过期、`-3505` 取消）→ Set-Cookie 取凭证。二维码有效期约 3 分钟，`--login-mihoyo` 默认等 180 秒。

### 4.6 调试模式（亲眼核对真实数据来源）

```bash
python main.py --dry-run --debug
```

`--debug` 会把日志级别提到 DEBUG，额外输出：

- 每款游戏的实际请求 URL（含 UID/区服，不含 Cookie）；
- **接口原始返回 JSON（键名含 token/cookie 的值自动脱敏，超 1500 字截断）**——失败时也会打印，用来确认"到底返回了什么"；
- 指纹签发与缓存过程。

`--debug` 只影响本次运行的日志级别，不写回 config.json。日志同时写进 `logs/dailycheck.log`。

### 4.7 数据真伪的判定原则（重要）

- 控制台/日志里每条状态都来自唯一查询器；**mock 数据只在两种情况出现**：`provider=mock`，或 provider 为 miyoushe 但游戏未覆盖（明日方舟/终末地），后者一定伴随 `WARNING ... 回退 mock 数据源` 日志；
- miyoushe 三款游戏**任何失败都显示为"查询失败"，绝不会变成编造的数值**（解析层所有字段都用 require_field/require_int 强校验，缺字段直接报错，不存在默认占位值）；
- 想确认某条数据是不是真实的：加 `--debug` 看原始返回 JSON，对照 `core/querier.py` 里 `parse_*` 的字段路径。

### 4.8 切换到米游社数据源 / 临时停用米游社

```json
"query": {
  "provider": "miyoushe",
  "timeout": 10,
  "mihoyo_enabled": true,
  "proxy": "",
  "mihoyo_device_mode": "auto",
  "mihoyo_task_scope": "daily"
}
```

- `provider` 为 `miyoushe` 时：原神 / 崩铁 / 绝区零走真实接口，**明日方舟 / 终末地自动改用 skland 数据源**（日志提示"自动改用 skland 数据源"，见第七节），无需分开配置 provider。
- `provider` 拼错（注册表里不存在的值）会直接报错，不会静默变成假数据。
- **`mihoyo_enabled: false`**：跳过全部米游社游戏（原神/崩铁/绝区零），只查森空岛系。跳过不算查询失败、不会触发"所有账号查询失败"的退出码 2，日志会有一条 WARNING 说明跳过了谁。米游社持续风控（5003/1034）时用它止血。
- **`proxy`**：米游社全链路的 HTTP 代理（`http://127.0.0.1:7890`）。getFp 按出口 IP 签发指纹，IP 被标记时必须靠它换出口，详见 4.4。
- **`mihoyo_device_mode`**：`auto` / `fp` / `gs`，含义见 4.4。
- **`mihoyo_task_scope`**：任务口径，见下方。

#### 只查部分游戏（单独关掉某个账号）

把对应账号的 `enabled` 改成 `false`，脚本就不会查询它、不会报错、也不会出现在推送里：

```json
{ "game": "arknights", "nickname": "博士", "enabled": false, "credential": { "token": "..." } }
```

> `mihoyo_enabled` 只控制米游社三款（原神/崩铁/绝区零），对森空岛系无效。
> 只想看终末地、不想被明日方舟的"未绑定"报错打扰时，就是这个账号级开关。

#### 任务口径 `mihoyo_task_scope`

| 值 | 行为 |
|---|---|
| `daily`（**默认，推荐**） | 只有「每日委托 / 每日实训 / 每日活跃度」参与完成度判定；树脂、开拓力、电量、探索派遣、模拟宇宙周积分等资源数据只在控制台以 `· 仅展示：` 打印，**不进未完成列表、不触发推送** |
| `full` | 资源类也算完成度（树脂满 / 体力满 / 无进行中派遣才算完成），天天催体力没处花时别用这个 |

控制台示例（`daily` 口径）：

```
--  原神（旅行者）｜全部完成｜1/1
      · 仅展示：原粹树脂(99/200)、探索派遣(2/5)
```

---

## 七、森空岛接入（明日方舟 / 终末地）

### 7.1 数据源与边界

- 接口：`zonai.skland.com`（森空岛开放接口），签名算法与常量抄写自社区开源项目 `starudream/sign-task` 并逐字翻译为 Python；
- 能查什么：**绑定角色 + 游戏内每日任务进度**（只读，不代签、不领奖）：
  - **明日方舟**：`GET /api/v1/game/player/info?uid=<游戏UID>` → `data.routine.daily = {current, total}`
    （每日任务完成数/总数，对应游戏内「每日任务 0/10」）；
  - **终末地**：`GET /api/v1/game/endfield/card/detail`（必带 `sk-game-role: 3_<roleId>_<serverId>`）
    → `data.detail.dailyMission = {dailyActivation, maxDailyActivation}`
    （每日活跃度，对应游戏内「每日活跃度 60/100」）。
- 数据来源核对：`ProbiusOfficial/Skland_API` 文档与 `dump.json`（routine.daily）、
  `endfield-py`（card/detail → dailyMission.dailyActivation），均与 2026-10-26 真实请求实测一致。
- 查不到的（如基建、剿灭、周任务明细）：不在本阶段范围；周任务字段（`weeklyMission` / `routine.weekly`）已在接口里，但为避免"周任务没做完天天催"暂不纳入每日提醒。
- 森空岛签到（`/game/attendance`）仍保留在 `core/skland_api.py` 里备用，但**主流程不再用它判断完成度**。

### 7.2 Token 怎么填（扫码登录，推荐；支持两款游戏两个账号）

给 `arknights` / `endfield` 的 `credential` 各填一个**鹰角登录 token**——即扫码流程中
`token_by_scan_code` 返回的那串**短 token**（**40~100 字符、纯 base64 字符集、不以 eyJ 开头**）。
**两款游戏可以在不同的鹰角通行证账号下**——各自扫各自的码、各写各的节点，互不覆盖：
凭证缓存也按游戏分文件（`logs/skland_cred_arknights.json` / `logs/skland_cred_endfield.json`），不会互相污染。

> ⚠️ **不要填 eyJ 开头的网页 JWT**。skland 换票接口 `/user/oauth2/v2/grant` 对
> `GrantV2Req.token` 有 **max=128 字符**的硬校验（2026-10-26 实测：128 通过、129 起即报
> `failed on the 'max' tag`）。旧版脚本误换的 176 字符"长 token"就是这么翻车的，现已修正：
> 登录流程不再调第二次 grant，直接使用 `token_by_scan_code` 的登录短 token。

**不要再手动去网页里找了**——通行证网页端的 Local Storage / Cookie 里只有短期 cred，拿不到登录 token。直接用内置的扫码登录：

```bash
python main.py --login-skland arknights   # 只登明日方舟的账号，写入 arknights 节点
python main.py --login-skland endfield    # 只登终末地的账号，写入 endfield 节点
python main.py --login-skland             # 不带参数：两款依次各扫一次
```

流程（全自动，接口为鹰角官方扫码登录，已实测）：

1. 终端打印 ASCII 二维码，同时保存 `qr_login.png`（扫码不便时用手机相册/大图打开）；
2. 用手机 **森空岛 / 明日方舟 / 终末地 App** 任一款扫码，并在手机上点确认；
3. 打印 `gen_scan/login`、`scan_status`、`token_by_scan_code` 的**服务端完整返回**，并体检 token 长度/前缀/是否含空白；
4. **写入前双重校验**：格式检查（空 / 含空白 / eyJ 开头 / >128 字符 / 过短 → 红字报错且**不落盘**）+ 语义预检（真调一次 grant，`status=0` 才写）；
5. **只写入本次指定的游戏节点**，另一个游戏的 Token 原样保留；该游戏旧的 cred 缓存会清掉，下次用新 token 重新换取。

依赖：`pip install qrcode pillow`（已加入 requirements.txt）。未安装 qrcode 时会降级为打印扫码链接。

> 备选（抓包方案，扫码不通时用）：安卓用 HttpCanary / iOS 用 Stream 抓包，目标域名 `as.hypergryph.com`：
> 1. 手机 App 里完成一次鹰角账号登录（或扫码登录）；
> 2. 抓 `POST https://as.hypergryph.com/user/auth/v1/token_by_scan_code`（或 `.../v2/token_by_phone_code`）的响应，
>    其中 `data.token` 就是登录短 token（40~100 字符、不以 eyJ 开头）；
> 3. 复制该串填进 config.json 的 `credential.token`。

脚本自动用这个 token 走官方流程换取森空岛 cred/签名 token 并缓存到 `logs/skland_cred_<游戏>.json`（已 gitignore），token 失效时自动重换。两种进阶填法也支持：

- `{"cred": "...", "token": "..."}`：直接给森空岛 cred + 签名 token（如果你从其它社区工具导出过一对）；
- 只给 `{"cred": "..."}` 会明确报错——请求签名缺 token 密钥，无法工作。

### 7.3 验收方法

填好 token 后运行：

```bash
python main.py --dry-run
```

预期：明日方舟显示 `验证通过｜每日任务 10/10｜1/1`，终末地显示 `每日活跃度 100/100｜1/1`，数据来源 `source=skland`；未完成时形如 `未完成：每日活跃度(60/100)`。token 填错时这两行显示"查询失败：鹰角 token 换取授权失败"，且**不会**被当成未完成推送（失败 ≠ 未完成），单款失败不影响其它三款。

---

## 五、运行与验收方法

### 5.1 验收步骤（真实接口）

1. `pip install -r requirements.txt`；
2. 按 4.2 取 Cookie，填好三款游戏的 `credential`（ltuid / ltoken / uid）；
3. `query.provider` 改成 `"miyoushe"`；
4. 运行（建议先带 `--debug` 亲眼核对一次真实返回）：

```bash
python main.py --dry-run --debug
```

预期输出：原神 / 崩铁 / 绝区零三行显示**真实**任务状态（树脂显示真实值如 77/200 而非固定 160），明日方舟 / 终末地仍为 mock，合计行出现"查询失败 0"，退出码 0。若三款都报 `retcode=1034`/`10035`，见 4.4 的风控说明（换家庭网络重试）。

### 5.2 故意填错 Cookie 的验收

把某款游戏（或三款全部）的 `ltoken` 改成 `v2_故意的错误值`，再跑：

```bash
python main.py --dry-run; echo "EXIT=$?"
```

预期：

- 日志出现 `查询失败：接口 retcode=10001：Cookie 无效或已过期（未登录）`；
- 失败的游戏被标为 `ERR`，**不会**被算进"未完成"，也不会因此推送催促（失败 ≠ 未完成）；
- 退出码符合第 6 节定义：只启用三款米游社游戏且全部失败 → `EXIT=2`（所有账号查询失败）；五款全启用时另两款 mock 正常 → `EXIT=0`，但合计行会显示"查询失败 3"；
- 单款游戏失败不影响另外两款（例如只给原神填错 Cookie，崩铁、绝区零照常出真实数据）。

### 5.3 其它常用命令

```bash
python main.py                      # 按配置里的 test_mode 跑一轮
python main.py --force-alert        # 临时强制推送，用来验证 Bark 通不通
python main.py --force-silent       # 临时强制静默，只看日志
python main.py --dry-run            # 跑完整流程但不发真实推送请求
python main.py --dry-run --debug    # 调试模式：打印接口原始返回（脱敏），核对数据真伪
python main.py --login-skland arknights   # 扫码登录明日方舟的鹰角账号，只写 arknights 节点
python main.py --login-skland endfield    # 扫码登录终末地的鹰角账号，只写 endfield 节点
python main.py --login-skland             # 两款依次各扫一次（支持两个不同账号）
python main.py --login-mihoyo             # 米游社扫码登录（米游社 App 扫），写三款游戏 Cookie + 绑设备
python main.py --probe-mihoyo             # 米游社链路探针：出口 IP / device_fp / 设备注册 / 两种模式的 retcode
python main.py --probe-mihoyo --proxy http://127.0.0.1:7890   # 换代理试 IP（不写回配置）
python main.py --dry-run --proxy http://127.0.0.1:7890        # 整轮流程走代理
python main.py --config other.json  # 指定别的配置文件
python main.py --init               # 生成默认配置（已存在不覆盖）
```

mock 数据固定为：**原神未完成、崩铁已完成、绝区零未完成、明日方舟已完成、终末地未完成**。正常输出：

```
----------------------------------------------------
每日任务检查结果  2026-10-25 19:30:25
----------------------------------------------------
--  原神（旅行者）｜未完成 2 项｜2/4
      · 未完成：领取每日奖励
      · 未完成：消耗原粹树脂(80/160)
OK  崩坏：星穹铁道（开拓者）｜全部完成｜3/3
--  绝区零（绳匠）｜未完成 1 项｜2/3
      · 未完成：刮刮卡
OK  明日方舟（博士）｜全部完成｜3/3
--  终末地（管理员）｜未完成 1 项｜1/2
      · 未完成：每日活跃度(60/100)
----------------------------------------------------
合计 5 个账号：已完成 2，未完成 3（漏 4 项），查询失败 0
结论：还没做完 -> 原神、绝区零、终末地
----------------------------------------------------
```

推送内容（Bark 卡片）：

- 标题：`游戏日常：3/5 个未完成`
- 正文：
  ```
  10-25 19:30
  [未完成] 原神：领取每日奖励、消耗原粹树脂(80/160)
  [未完成] 绝区零：刮刮卡
  [未完成] 终末地：每日活跃度(60/100)
  [已完成] 崩坏：星穹铁道、明日方舟
  共漏 4 项，别拖到 23:59。
  ```

Bark 请求形态：`GET {server}/{device_key}/{quote(标题)}/{quote(正文)}?group=…&level=…&sound=…`，标题与正文逐段 `quote(safe="")`，正文里的 `/` 和换行不会破坏 path。

### 5.4 其它推送通道（Server酱 / Qmsg酱 / 飞书 / 钉钉）

在 `push.channels` 里加上想用的通道即可多通道并发（单通道失败不影响其它通道）：

```json
"channels": ["bark", "serverchan", "qmsg", "feishu", "dingtalk"]
```

| 通道 | 必填字段 | 说明 |
| --- | --- | --- |
| `serverchan` | `push.serverchan.sendkey` | Server 酱 Turbo 的 SendKey（sctapi.ftqq.com 微信推送），填裸 key 或带 `.send` 后缀都行 |
| `qmsg` | `push.qmsg.key` | Qmsg 酱的 Key（QQ 推送）。也兼容平铺写法 `push.qmsg_key`。可选 `push.qmsg.server`（默认 `https://qmsg.zendee.cn`） |
| `feishu` | `push.feishu.webhook` | 飞书群自定义机器人 Webhook 地址；机器人开了"加签"就再填 `secret` |
| `dingtalk` | `push.dingtalk.webhook` | 钉钉群自定义机器人 Webhook（access_token 那串）；机器人开了"加签"就再填 `secret` |

- 消息体统一为 `标题\n正文` 纯文本；成功判定：Server酱 `code==0`、Qmsg `success==true`、飞书 `code==0`、钉钉 `errcode==0`；
- Qmsg 走 `GET {server}/send/{key}?msg=<标题换行正文>`：**一次请求只发一条消息**，所有游戏合并在一个气泡里，不会拆成"标题一个、正文一个"。发送前会把 CRLF 统一成 `\n`、逐行去掉首尾空白、连续空行压成一行，避免长气泡里出现大段留白；**它会把含 URL、IPv4、长数字串的内容判为违规**（此时 `success=false`，日志里会带 `reason`），单条上限 1800 字符（超长自动截断）；
- 加签算法按官方文档实现（飞书：base64(hmac-sha256("{ts}\\n{secret}"))；钉钉：URL 上追加 timestamp+sign），填了 `secret` 自动启用；
- 某个通道 key/webhook 没填时优雅跳过并提示，不算推送失败；`--dry-run` 对所有通道生效。

### 5.5 常驻守护进程（推荐，取代 Windows 任务计划）

早期版本用 `schtasks` 建计划任务，现在已经**完全弃用**——小白配不明白、要输系统密码、每次运行还闪黑窗口、改时间得重装任务。

现在由 `daemon.py` 常驻负责调度：

| 特性 | 实现 |
| --- | --- |
| 轮询 | 每 **30 秒**醒一次，读 `config.json` 的 `app.check_times` |
| 命中判定 | 与目标时间相差 **±1 分钟**即触发（`SLOT_TOLERANCE = 60`） |
| 防重复 | 每个时间点按 `YYYY-MM-DD HH:MM` 记账到 `logs/.last_run`，**先记账再执行**，中途崩溃也不会重复推送；重启进程同样不会重发 |
| 异常防崩 | 单轮检查全部包在 `try/except` 里，断网 / 接口改版只写日志，进程不退出 |
| 资源占用 | 空闲时阻塞在 `Event.wait`（等价 sleep），**CPU 0%** |
| 托盘 | `pystray` + `Pillow`，右键菜单：打开控制台 / 立即执行一次 / 退出程序 |
| 线程模型 | 轮询跑在**独立线程**，主线程留给 pystray 的消息循环 |

启动方式（任选）：

```bat
:: 1. 一键（守护进程 + 独立控制台窗口）
start.bat

:: 2. 只起守护进程（后台，无窗口）
pythonw daemon.py

:: 3. 前台带托盘（能看到日志输出，调试用）
python daemon.py

:: 4. 只开控制台窗口（守护进程已经在跑的时候用）
python app.py
```

网页「定时与状态」页也能启停，背后调的是同一套逻辑（`pythonw.exe` 拉起、`taskkill` 兜底）。

命令行工具：

| 命令 | 作用 |
| --- | --- |
| `python daemon.py --status` | 打印状态 JSON（是否运行、PID、下次提醒、最近一次） |
| `python daemon.py --start` | 后台拉起 |
| `python daemon.py --stop` | 停止（先写 `logs/.stop` 温和退出，超时才强杀） |
| `python daemon.py --once` | 立即跑一次就退出 |
| `python daemon.py --once --dry-run` | 同上，但不真的发推送 |

**停止是怎么保证"彻底"的**：

1. 写 `logs/.stop` 标记 → 轮询线程在 1 秒内发现，置停止事件；
2. 监视线程（`tray-watchdog`）收到停止事件后调 `icon.stop()`，让主线程的 `icon.run()` 返回——
   否则进程会卡在托盘消息循环里永远退不掉（实测过，不这么做只能强杀）；
3. 等 8 秒仍未退出，才 `taskkill /PID <pid> /F` 强杀；
4. 清掉 `daemon.pid` / `daemon.heartbeat` / `.stop` 三个文件。

实测优雅停止耗时 ~2.5 秒，`forced=False`，进程消失、内存释放。

运行时文件（都在 `logs/`，已被 `.gitignore` 排除）：

| 文件 | 含义 |
| --- | --- |
| `daemon.pid` | JSON：`pid` / `started_at` / `python` |
| `daemon.heartbeat` | 每次轮询写一次；超过 120 秒没更新视为已死 |
| `.last_run` | 防重复记账 + 最近一次结果 |
| `.stop` | 停止标记 |

排查：

1. 看 `logs\dailycheck.log`，搜 `命中时间点` 确认是否触发过；
2. `python daemon.py --status` 看 `running` / `next_run`；
3. `python daemon.py --once` 手动跑一轮，区分「调度问题」和「查询问题」。

> **关机/重启后要重新双击 `start.bat`。** 想开机自动挂上，把 `start.bat` 的快捷方式丢进
> `shell:startup`（Win+R 输入即可打开启动文件夹）——比任务计划省事，也不会有密码问题。


---

### 5.6 桌面控制台（独立窗口）

双击 `start.bat`，或命令行 `python app.py`，会弹出一个**独立的应用窗口**——
标题「ResinLoop · 日常巡查」，初始 980×800，可自由缩放（最小 820×620），
没有地址栏、没有标签页，跟原生软件一样。

技术上是两层：`login_ui.py` 继续提供网页控制台（`http.server`，只监听 127.0.0.1），
`app.py` 在**后台线程**里把它跑起来，再用 pywebview 开一个窗口加载
`http://127.0.0.1:8765`。所以前端一行没改，只是换了个"外壳"。

| 场景 | 行为 |
| --- | --- |
| 关窗口（×） | 控制台服务随之关闭、端口释放；**守护进程不受影响**，定时提醒照常 |
| 已有控制台在跑 | 直接复用那个实例，不会另起一个抢端口 |
| 想看浏览器版 | `python login_ui.py` 或双击 `login_ui.bat` |
| 缺少 WebView2 运行时 | 弹窗提示并自动退回默认浏览器，功能不受影响 |
| 托盘「打开控制台」 | 优先拉起 `app.py` 这个窗口；`app.py` 不存在时才退回浏览器 |

> Windows 上 pywebview 用的是系统自带的 **WebView2**（Win11 与绝大多数 Win10 都预装了），
> 不打包 Chromium，所以启动快、占用低 —— 实测窗口起来后整个进程约 150 MB，
> 关掉窗口进程即退出。

页面顶部有三个导航按钮，点哪个就只显示哪个：

| 导航 | 作用 |
| --- | --- |
| **账号绑定** | 设备伪装 + 扫码写入 Cookie / token |
| **通知绑定** | 填推送 Key、发测试推送 |
| **定时与状态** | 守护进程启停、提醒时间点增删 |

地址栏带 `#account` / `#notify` / `#schedule` 可以直接落在对应页。

#### 账号绑定

按平台分成两块，**每款游戏一个复选框**：

| 平台 | 勾选方式 | 扫码次数 |
| --- | --- | --- |
| **米游社** | 原神 / 崩铁 / 绝区零，想更哪几款就勾哪几款 | **1 次**（同一账号的 Cookie 通用） |
| **森空岛** | 明日方舟 / 终末地，勾几款就登录几款 | **每款 1 次**（各有独立 token） |

所以两种极端情况都是同一个入口：

- **只登录一款**：只勾那一款 → 米游社扫 1 次，森空岛扫 1 次；
- **全部一起**：点"全选" → 米游社扫 1 次搞定三款，森空岛依次扫 2 次。

写入范围严格按勾选执行：没勾的游戏，`config.json` 里的 Cookie/token 和 `logs/device_fp.json`
里的设备指纹都**原样不动**。这点对"三款挂在两个不同米游社账号下"的情况很重要——
只登录原神时，不会把另外两款所在的账号设备身份一起换掉。

相比命令行版的区别：

- 二维码是图片，尺寸够大，直接对着屏幕扫；
- 状态实时刷新：等待扫码 → 已扫码待确认 → 写入中 → 完成；
- 森空岛多款时显示"第 1/2 步"，扫完一款自动出下一款的码；
- 成功后页面直接列出写入的米游社 ID、更新了哪几款游戏，不用回头翻日志；
- 失败时给出具体原因（二维码过期、手机上取消、Token 校验不通过等），
  并保留已完成的部分（比如第 2 款失败，第 1 款仍然写入了）。

#### 设备伪装（同一个页签下方）

米游社按「设备指纹」判风险，机型 / 系统版本 / Build 号 / OAID 必须自洽。用你自己手机的
真实档案，通过率最高：

1. 手机扫页面上的二维码（局域网地址，形如 `http://192.168.x.x:8790/设备信息.apk`），
   下载安装 `tools/设备信息.apk`；
2. 打开 App 点「复制」；
3. 把内容粘到文本框 → 点「保存并清空指纹缓存」。

后端做的事：

```
粘贴文本 → 截取首尾花括号（容错：连带说明文字一起复制也能用）
        → normalize_device_profile() 归一化字段
            deviceModel       -> model / device_type
            androidVersion    -> os_version，并推导 sdk_version
            deviceFingerprint -> device_info，并从中拆 brand / device / display / build_id
            oaid              -> oaid
        → 写入 logs/device_profile.json
        → reload_device_profile()：原地刷新 DEVICE_MODEL + 重建 USER_AGENT
        → 删除 logs/device_fp.json，下次查询按新档案重签指纹
```

**为什么要原地刷新**：`USER_AGENT` 里写着机型与系统版本，必须和 `getFp` 上报的
`ext_fields` 一致，否则会被判定"环境不一致"。`mihoyo_login` 的 `QR_HEADERS` 通过
订阅回调同步更新（`on_device_profile_change`）。

**保存后必须重新扫码**：设备身份变了，旧 Cookie 对应的还是旧设备。

> 这一步是可选的。不填就用内置的默认档案（Redmi K80 公开参数），也能跑。
>
> `logs/device_profile.json` 已被 `.gitignore` 排除，不会进版本库。

#### 通知绑定

扫码只解决"查得到"，收不收得到提醒取决于推送通道有没有 Key。这一页把
`config.json` 的 `push` 节点做成了表单，支持 Server酱（微信）和 Qmsg 酱（QQ）：

- 每个通道一个输入框 + 一个「启用」复选框；
- **Key 以掩码回显**（如 `SCT4*******C41`），浏览器里看不到明文，明文永远不出后端；
- **不动输入框 = 保持原值**。后端拿回传的掩码与当前 Key 的掩码比对，一致就跳过；
  若整个表单一个字都没改，`config.json` 连写都不会写；
- **清空后保存 = 删除该 Key**，并从 `push.channels` 里移除该通道；
- **填入新 Key = 覆盖**；含 `*` 的内容会被拒绝（防止把掩码当 Key 存进去）；
- 「发送测试推送」用输入框里的**当前值**试发一条，**不写盘**——先试通再保存。

写入只碰 `push.channels` 和 `push.{serverchan,qmsg}.{enabled,key/sendkey}`，
`timeout` / `server` / `bark` / `feishu` 等其它字段原样保留；落盘前会先过一遍
`PushConfig.from_dict()` 校验，并通过"临时文件 + 原子替换"写入，避免写坏配置。

#### 定时与状态

**守护进程状态**（每 3 秒刷新）：运行中 / 已停止、PID、启动时间、下次提醒、最近一次结果。
底下两个按钮直接调 `daemon.start_daemon()` / `daemon.stop_daemon()`。

**提醒时间点**：`+` 添加、`×` 删除，立即写入 `app.check_times` 并生效，**不用重启守护进程**
（`daemon.py` 每一轮都重新读 `config.json`）。至少保留一个，最多 12 个。

写入前会跑 `_normalize_times()`（校验 `HH:MM`、去重、排序）和 `parse_config()` 全量校验，
形如 `25:99`、空数组都会被拒绝，`config.json` 不会被写坏。

可用参数：

| 参数 | 说明 |
| --- | --- |
| `--port 8800` | 换端口（默认 8765，被占用会自动往后找空闲端口） |
| `--no-browser` | 不自动打开浏览器（只对 `login_ui.py` 命令行版有效） |
| `--browser` | 仅 `app.py`：不开独立窗口，改用默认浏览器 |
| `--debug` | 仅 `app.py`：打开开发者工具并写 DEBUG 日志 |

服务只监听 `127.0.0.1`，不对局域网/外网开放；登录态只写本机 `config.json`。用完按 `Ctrl+C` 结束即可。

> 两种方式写到配置文件里的结果完全一样，只是交互形式不同。日常用界面版，脚本/远程环境用命令行版。

---

## 六、测试模式与退出码

| test_mode | 行为 |
| --- | --- |
| `auto` | 有未完成项才推，全完成则安静 |
| `force_alert` | 无论结果都推（验证链路） |
| `force_silent` | 一律不推，只跑流程 + 写日志 |

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功（推没推都算成功；`--dry-run` 也算） |
| 1 | 配置缺失 / JSON 语法错误 / 字段非法 |
| 2 | 所有账号查询失败 |
| 3 | 需要推送但真实推送全部失败（含未装 requests 的情况） |

日志：控制台 + `logs/dailycheck.log`，零点切分为 `dailycheck.log.YYYY-MM-DD`，保留 `keep_days` 天。日志里的 device_key 已脱敏（只留前 4 位）。

---

## 七、当前边界与 TODO

已实现：配置读写与校验、五款游戏真实查询（米游社三款 + 森空岛两款）、汇总判断、
五通道推送（Bark / Server酱 / Qmsg / 飞书 / 钉钉）、日志、CLI 与退出码、
网页控制台（三个页签）、设备伪装、常驻守护进程 + 系统托盘、一键启动。

明确未做：

- 自动签到 / 领取奖励 —— 本脚本只读不写。
- 开机自启 —— 需要用户把 `start.bat` 快捷方式放进 `shell:startup`，不做注册表/服务安装。
- 多用户 / 多实例 —— 单机单实例，`daemon.pid` 做互斥。

### TODO 清单（未核实的接口细节，全部标注在代码里）

| 位置 | 内容 |
| --- | --- |
| ~~`core/mihoyo_api.py` `DS_SALT` / `APP_VERSION`~~ | 已内置并实测通过（DS2 + x4 盐 + 2.109.0），仅在报 -10003 时更新 |
| `core/mihoyo_api.py` `DEFAULT_SERVERS["zzz"]` | 绝区零 server 值待带凭证实测确认 |
| `core/mihoyo_api.py` `RETCODE_HINTS` | 常见 retcode 的可读解释，未逐一实测验证 |
| `core/querier.py` `parse_genshin` | `done_task` 语义（完成数 0~4 还是布尔）与 `expeditions[].status` 字段名需实测确认 |
| `core/querier.py` `parse_starrail` | 模拟宇宙积分路径 `rogue_tourned_info.weekly_acquired/weekly_max` 需实测确认 |
| `core/querier.py` `parse_zzz` | 每日活跃度 `vitality.progress` 与电量 `energy.progress` 的字段路径为占位写法，需对照真实返回修正 |
| `core/querier.py` 各 parse | 树脂/开拓力/电量"回满=完成"的信息项语义，可按喜好调整 |
| 风控 | 若真实 Cookie 报 retcode=1034，需极验/device_fp 方案，暂未实现 |

端点路径已实测确认（无凭证探测）：原神 `/game_record/app/genshin/api/dailyNote`、崩铁 `/game_record/app/hkrpg/api/note`、绝区零 `/event/game_record_zzz/api/zzz/note`，均为 GET。`DS_SALT` 未填时脚本会在发请求前直接报"DS_SALT 尚未配置"，不会发出一个必被拒绝的请求。

### 第 3 阶段接入森空岛只需三步

1. 在 `core/querier.py` 写 `SklandQuerier(BaseQuerier)`，实现 `fetch()` 返回 `GameStatus`；
2. `register_querier("skland", "arknights", SklandQuerier)` 注册（终末地同理）；
3. 配置里把 `query.provider` 改成对应 provider 名；主流程、判断、推送、日志都不用改。

> 参考：第 2 阶段接米游社时，`main.py`、`analyzer.py`、`notifier.py`、退出码定义均零改动，仅新增 `core/mihoyo_api.py` 并扩展了 `core/querier.py`。
