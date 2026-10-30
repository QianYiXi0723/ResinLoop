# ResinLoop · 游戏每日任务未完成提醒

> **ResinLoop（日常巡查）**：名字取自「树脂循环」——原神的树脂、崩铁的开拓力、
> 绝区零的电量，这些每天重置一次的体力就是「日常」本身，也是它每一轮要巡检的东西。

只做「查询 → 判断 → 提醒」三件事：**不签到、不领奖励、不修改任何游戏数据**。

内置常驻守护进程 + 系统托盘，**不需要 Windows 任务计划**；所有配置都在网页控制台里完成。

---

## 更新日志

### v2.1.2

- **修复全新机器首次运行必崩**：没有 `device_identity.json` 时（也就是任何新用户第一次
  解压运行），`core/mihoyo_api.py` 的模块级初始化会调用定义在文件后方的 `_IDENTITY_LOCK`
  与 `_generate_id`，直接抛 `NameError`，导致 `/api/device` 空响应，扫码、查询、巡检一起失效。
  已把两处定义上移到初始化之前，并在原位置留下位置说明防止被搬回去。
  **v2.1.1 的包里就是这份坏代码，请换成本版。**
- **发行形态改为多文件**：去掉 ILRepack 合并。net48 没有官方单文件发布，手搓合并在
  `/internalize` 下会把 WebView2 类型降为 internal → `TypeLoadException`（v2.0.0/v2.1 的病根）。
  实测合并只省 10,888 字节和两个文件，而 `WebView2Loader.dll` 本来就是原生件、合不进去，
  「单文件」一直是假象。现在根目录是 exe + 3 个 dll，与 BetterGI、绘世启动器等成熟项目一致。
- **新增构建门禁**：`shell/build.ps1` 每次构建都会把四件 + engine（剥掉 `logs/`）复制到
  `%TEMP%` 空目录实跑，断言窗口标题、`/api/env`、`/api/device`、首启自动生成身份文件、
  页面 DOM 已渲染、页面内 `fetch('/api/*')` 真的走通。
  上面两个 bug 都只在「无历史产物、无身份文件」的全新目录复现，旧冒烟永远绿。
- **页面不再依赖本地 HTTP 服务**：壳接管 `https://resinloop.local/*`，`index.html`、
  `app.js`、`style.css`、图标全部从 `engine\ui` 与 `engine\static` 直接读盘；
  页面发出的 `/api/*` 由壳转发到引擎端口（显式 `Proxy = null` 绕过系统代理）。
  窗口出现得比引擎更早，也不再受端口与代理影响。浏览器/`app.py` 路径仍走原来的 HTTP 服务。

### v2.1.1

- **修复发行版双击打不开**：v2.0.0 / v2.1 的 exe 启动后进程立刻退出，窗口根本不出现
  （Windows 事件日志里是 `System.TypeLoadException`）。原因是壳在单文件化合并时把
  WebView2 的类型降成了 internal，主窗口构造时类型加载失败。现已改为保留类型可见性的
  合并方式，exe 体积不变（653,824 字节）。**如果下载过 v2.0.0 或 v2.1，请换成这个版本。**
- 壳的构建流程固化成 `shell/build.ps1`（dotnet publish + ILRepack + 冒烟），
  不再靠手工敲命令；脚本里写明了为什么不能加 `/internalize`。

### v2.0.0

- **Windows 桌面壳**：C# WinForms + WebView2 外壳 + 便携 Python 引擎，
  以单文件 exe 形态发行；托盘负责「彻底退出」并连带收尾后台引擎进程，
  开机自启登记进系统启动项。
- **演示模式引导**：未配置数据源时不显示编造数据，而是引导去扫码；
  配置齐全后才展示真实信息。
- **定时任务与状态页重构**：改为三卡布局；守护进程状态折进状态卡的状态条，
  巡检结果改为三列时间轴。

### v2.1

- **开机自启自动巡检**：过去开机自启后守护进程不会自动拉起，定时提醒会静默断线；
  现在桌面壳在引擎就绪后自动拉起，拉起失败时用托盘气泡提示。
- **凭证失效告警**：某账号连续两次鉴权失败时单独推一条提醒（同一账号 24 小时内
  只提醒一次），不再混在日常「查询失败」里被忽略。
- **推送回压**：某个通道连续 5 次发送失败后自动静默 6 小时，避免手机被失败消息刷屏；
  静默期间仍照常本地记录，恢复后自动解除。
- **托盘「立即巡检」**：托盘右键可直接触发一次检查，不必等定时点。
- **查询代理**：账号绑定页可为米游社查询单独指定出口代理（留空即直连），
  用于换网络环境排查风控。
- **控制台细节**：最近推送改为等高的可展开格子；活动日历游戏名与图标垂直居中；
  F5 刷新后整页闪一下作为反馈。

### v1.2.0

- **控制台 UI 全面重构**：满幅 12 列 Bento 网格、KPI 摘要条、游戏概览迷你卡、
  GitHub 式热力图、紧迫感系统（data-urgency）、双主题、通知绑定三区工作流。
- **并行查询提速**：collect_statuses 改线程池并发，5 款游戏 16~17 秒 → 约 4~5 秒；
  设备指纹获取加锁（防风控回归）。
- **推送历史结构化**：新增 data/push_history.json，「最近推送」不再解析日志。
- **config.json 滚动备份**：写入前自动备份 .bak-1/.bak-2/.bak-3。
- **前端源码拆分**：HTML/CSS/JS 独立成 ui/ 三件套，启动时装配。

### v1.1.8

- **原神图标换回经典版**：之前那张左上角有「6th」周年角标。实测 App Store 国区、
  国际区、TapTap、B站官方号、官网 favicon **全都是同一张 6 周年图**，
  只有本机米哈游启动器的 `ico/hk4e_cn.ico` 还是经典版 —— 改成从那里取，
  裁掉底部 miHoYo 名称条后导出（256 → 228）。
- `tools/fetch_game_icons.py` 重写：优先本机启动器 ico，没有再退回 App Store，
  并在输出里标明每张图的来源与是否带角标。

### v1.1.7

- **修「换了图标还显示旧图」**：图标 URL 带上 `?v=<文件 mtime>`，换图后 URL 必变；
  静态资源的 `Cache-Control` 从 `max-age=86400` 改成 `no-store`。
  WebView2 的磁盘缓存再顽固也绕不过去。
- 侧栏版本号升到 v1.1.7 —— 页面左上角看到的版本就是这个，
  如果重启后还是 v1.1.6，说明程序没真的重启。

### v1.1.6

- **活动列表只留抽卡资源**：原神只认「原石」、星铁只认「星琼」、绝区零看
  `monochrome_cnt`（菲林池）、方舟正文含「合成玉/源石」才留。
  奖励字段拿不到时默认保留并标注「奖励未公开」（`KEEP_WHEN_REWARD_UNKNOWN`）。
- **两段分组 + 精确排序**：`进行中`按结束时间升序、`未开启`按开启时间升序，
  已结束直接过滤，缺时间的排所在组末尾。
- **左侧换成官方头像**：按 `game_id` 固定渲染派蒙 / 三月七 / 邦布 / 阿米娅，
  36×36、8px 圆角、轻阴影；图来自 App Store 官方商品图，
  `py tools/fetch_game_icons.py` 可重新下载，新增 `/static/` 静态路由（只吐图片，挡目录穿越）。
- 活动行改版：图标 + 标题 + 「游戏名 · 倒计时 · 进度」+ 奖励标签 + `×`，
  72 小时内倒计时变橙加粗。

### v1.1.5

- **活动数据换成米游社「活动日历」**：不再抓公告。原神 `act_calendar`、星铁
  `get_act_calender`（官方把 calendar 拼错了）、绝区零 `activity_calendar`
  （参数是 `uid`/`region`），拿到的就是 App 里那份：活动名、起止时间、
  进度（36/36、0/900）、状态（未开启 / 进行中 / 已完成）。
- **删掉就是删掉**：每行的「提醒」开关换成 `×`，点完立刻消失，ID 写进
  `data/ignored_activities.json`，之后无论刷新还是推送都不会再出现；
  底部有「恢复全部」兜底。原来的 `MUTED_KEYWORDS` / `alert_enabled` 整套逻辑已删除。
- **排版向手机端靠**：行内是「图标 + 名称 + ⏱ 倒计时 + 进度 + 状态标签」，
  倒计时按「剩 5 天 9 小时 / 2 小时 / X 天后开启」显示，72 小时内会变成橙色。
- 终末地仍不支持（官方没有带绝对日期的活动数据）。

### v1.1.4

- **活动提醒扩到四款游戏**：星铁、绝区零、明日方舟都接上了官方数据源，加上原神一共
  27 条进行中的活动 / 公告，全部带真实起止时间，照样走 ≤72 小时倒计时。
  - **崩坏：星穹铁道**：`hkrpg-ann-api` 的 `getAnnList`（国服 `type_list` 只有「公告」），
    版本说明、无名勋礼、限时玩法都在这一组里；周边 / 问卷 / 防沉迷按标题排除。
  - **绝区零**：域名必须是 `api-takumi.mihoyo.com`（`ann-api` 一直 `retcode=-1000`），
    按 `tag_label` 取「活动公告」「福利活动」。
  - **明日方舟**：鹰角官网 `api/news` 的 `tab=1` 是活动，结束时间只写在详情正文
    「活动时间：10月24日20:00~11月24日03:59」里，按发布时间补年份后正则解析。
  - **终末地**仍标「暂不支持」：官网公告只写「版本更新后至版本维护前」，没有可计算的截止日期。
- **活动列表不再截断**：改成全量展示（原来只取结束最早的 12 条，接上四款游戏后
  正好会把绝区零、方舟整个藏掉），末尾提示共多少个。
- 抓取失败按游戏隔离：哪款接口挂了只丢那一款，其余照常更新缓存。

### v1.1.3

- **单条活动可静音**：「信息查询」页的每条活动右侧多了「提醒」开关，可以单独决定
  这条活动要不要参与 72 小时倒计时推送。关掉只是**不推送**，列表里照常展示并标注
  「已静音」；点开又能恢复，**不用改代码也不用重启守护进程**。
- **默认静音四类常驻公告**：标题命中 `祈愿` / `砺行修远` / `祝圣移涌` / `幽境危战`
  的活动默认关闭提醒 —— 卡池和刷素材类公告天天催只会让人对提醒脱敏。
- **刷新不覆盖你的选择**：`data/activities.json` 里每条活动多了 `alert_enabled` 字段，
  每天重新拉取时按活动 ID（`ann_id`）继承上次的手动设置；只有真正的新活动才按
  关键词取默认值（普通活动默认开、命中关键词默认关）。
- 新增 `POST /api/activities/toggle`（活动 ID + 目标状态），写入走独立并发锁 +
  `mkstemp` 原子替换，连点多个开关不会互相覆盖。

### v1.1.2

- **左侧导航栏**：五个页签从顶部横排改成左侧 Sidebar，内容区在右侧；
  窄屏（<820px）自动折回顶部横向导航。
- **新增「信息查询」页**：实时体力看板（原神树脂 / 星铁开拓力 / 绝区零电量 /
  方舟·终末地理智，点刷新即时查询，不写历史、不推送）+「即将结束的活动」列表。
- **活动结束提醒**：原神官方公告接口能拿到**真实起止时间**；活动缓存
  `data/activities.json` 每 20 小时最多拉一次；剩余 ≤72 小时时在推送末尾追加
  「📅 活动提醒」，同一活动每天最多提醒一次；「定时与状态」里有独立开关。
- **如实标注能力边界**：星铁公告接口没有活动分组、绝区零公告接口异常、
  森空岛没有公开活动日历 —— 这四款在界面上标为「暂不支持」，不编造数据。

### v1.1.1

- **「定时与状态」改成手风琴**：提醒时间点常驻平铺，守护进程 / 开机自启动 /
  休眠提醒 / 体力溢出提醒四块默认收起，标题栏右侧直接放主操作
  （启动·停止 / 三个 Toggle），其余技术细节展开才出现。
- **两个总开关**：`app.catch_up_enabled`、`app.resource_alert_enabled`。
  关掉后补发窗口与溢出判定整体失效，但各自的数值会保留，重新打开即恢复。
- **顶部页签重排**：每日报表 → 账号绑定 → 通知绑定 → 定时与状态，首页直接看结果。
- **运行状态与开机自启彻底分开**：两条之间加了分割线；「停止」只停本次运行，
  提示会明确说明「开机自启设置保持不变」。
- 修掉两个开关几乎同时保存时，`config.json` 临时文件互相覆盖导致「配置写进去了、
  接口却返回 500」的并发写盘问题。

### v1.1.0

- **体力溢出提醒扩展到森空岛**：明日方舟、终末地的「理智」也能预警了。
  明日方舟取 `player/info` 的 `status.ap`，终末地取 `card/detail` 的 `dungeon`
  （`curStamina/maxStamina`）。达到阈值时即使日常全做完也强制推一条。
- **阈值可视化设置**：「定时与状态」页新增「体力溢出警告」卡片，五个输入框
  （原神树脂 / 星铁开拓力 / 绝区零电量 / 明日方舟理智 / 终末地理智）直接改、
  直接保存，**不用手动编辑 `config.json`，也不用重启守护进程**。
- **全屏布局垂直居中**：控制台内容在全屏时上下留白对称，不再挤在中上部。
- **明日方舟静默失效修复**：修好了「绑定了却在报表、推送、日志里完全查无此游戏」的问题
  （根因是 `accounts[].enabled=false`），并给调度层加了逐账号 Debug 日志，
  以后再遇到"某游戏完全没动静"能一眼看出是被禁用了。
- **报表列动态化**：每日报表的列 = 当前启用的账号 ∪ 历史记录，
  刚启用还没跑过一轮的游戏也会立刻占一列。

---

## 零、三步走（给不想折腾的人）

前提：装好 Python 3.10+，安装时勾选 **Add python.exe to PATH**。

```
1. 双击 start.bat
     → 自动装依赖 → 后台拉起守护进程 → 弹出独立的控制台窗口

2. 窗口里四个页签（默认停在「每日报表」）
     每日报表  ：看每款游戏的连续完成天数与最近 7 天
     账号绑定  ：贴设备指纹（推荐）→ 扫码登录
     通知绑定  ：填 Server酱 / Qmsg 的 Key → 发送测试推送
     定时与状态：加提醒时间点 → 启动守护进程 → 按需开补发 / 溢出提醒

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
    "check_times": ["08:30", "20:30"],
    "catch_up_minutes": 180,
    "catch_up_enabled": true,
    "resource_alert_enabled": true,
    "activity_alert_enabled": true,
    "genshin_resin_threshold": 160,
    "starrail_stamina_threshold": 240,
    "zzz_energy_threshold": 200,
    "arknights_ap_threshold": 135,
    "endfield_sanity_threshold": 300
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
| `app.check_times` | 每日检查时间点（HH:MM，24 小时制），改完即时生效 |
| `app.catch_up_enabled` | 休眠提醒总开关，`false` 时补发整体失效（窗口数值保留）。默认 `true` |
| `app.catch_up_minutes` | 休眠提醒窗口（分钟）。错过时间点后，在这个窗口内醒来会立刻补跑一次；`0` = 关闭补发。默认 180，上限 720 |
| `app.resource_alert_enabled` | 体力溢出提醒总开关，`false` 时完全跳过判定（各阈值保留）。默认 `true` |
| `app.activity_alert_enabled` | 活动结束提醒总开关，`false` 时不读活动缓存、不追加活动警告。默认 `true` |
| `app.genshin_resin_threshold` | 原神「原粹树脂」溢出预警阈值：当前值 ≥ 阈值就强制推一条（日常全做完也推）。`0` = 关闭。默认 160（上限 200） |
| `app.starrail_stamina_threshold` | 崩铁「开拓力」溢出预警阈值，同上。默认 240 |
| `app.zzz_energy_threshold` | 绝区零「电量」溢出预警阈值，同上。默认 200 |
| `app.arknights_ap_threshold` | 明日方舟「理智」溢出预警阈值，同上。默认 135（上限随账号等级，实测 205） |
| `app.endfield_sanity_threshold` | 终末地「理智」溢出预警阈值，同上。默认 300（上限实测 350） |
| `push.always_notify` | 平安通知：`true` 时日常**全部完成也发一条**「今日全部完成」，默认 `false`（无事不打扰） |
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

1. **真机环境**：`getFp` 的 `ext_fields` 是 53 个字段的完整 Android Build 环境（机型、Build 指纹、主板、屏幕分辨率、OAID、加速度计 / 陀螺仪读数、内存 / 存储容量、ROM 版本、运营商、电量、安装时间 …），字段名、顺序、字符串/数字类型与 GsCore `generate_fp` 完全一致。**机型不写死**：首次运行从内置的 7 款公开机型里随机挑一台（Xiaomi 14 / Xiaomi 13 / Redmi K70 Pro / Redmi K60 / Redmi Note 12 Turbo / OnePlus 11 / Samsung Galaxy S23），连同随机生成的 OAID 一起固化到 `logs/device_identity.json`，此后固定不变——每个人的环境都不一样，不存在「所有人共用一台设备」；
2. **请求头**：`x-rpc-app_version=2.102.1`、`x-rpc-client_type=5`、`X-Requested-With: com.mihoyo.hyperion`、UA 由当前档案拼出（`Android <系统版本>; <型号> Build/<Build 号>`）、`Referer/Origin: https://webstatic.mihoyo.com/`（尾斜杠也照抄）；
3. **设备注册**：首次使用某设备时调 `deviceLogin` → `saveDevice`（通行证盐 DS，body 的 `device_name=<品牌+型号>` / `os_version=<API level>` / 19 位 `registration_id`），保证"注册设备 = 查询设备"；
4. **风控重试**（GsCore `_DEAD_CODE` 分支）：命中 `5003 / 10041 / 1034 / 10035` 时，自动追加 `x-rpc-challenge_game` / `x-rpc-page` / `x-rpc-tool-verison` 三个挑战头，重算 DS 后再试一次；
5. **指纹缓存**：`logs/device_fp.json`，按「游戏 + UA + 机型 + OAID + 系统版本 + 代理」区分，任一变化自动重签，不会拿旧 IP 的指纹去查新 IP。

在这套随机机型方案之上，设备身份还有三条固定行为：

- **全机唯一设备身份**：一台电脑上，本程序对外只有一个设备标识——扫码登录、日常查询、
  设备注册三处共用同一个；首次运行随机选定机型后随即固化，此后不再变化。
- **长效标识固化与时间语义**：部分安装级标识与「安装时间」基准在第一次运行时确定并
  长期复用，因此上报的「安装至今时长」会随时间正常增长，而不是每次请求都
  声称「刚刚安装」。
- **换机的代价**：「换一台设备」会重新签发设备标识并清空本地注册记录，
  但**旧的设备记录在服务端不会被注销**——每换一次，账号名下就多留一个历史设备。
  因此不建议频繁换机；风控未解除时，换机通常也解决不了问题。

> 想换设备身份：网页「设备伪装」里的「换一台设备」按钮会换一款机型、重生成 OAID 并清掉旧指纹缓存；也可以把自己手机导出的完整档案粘进去覆盖（存 `logs/device_profile.json`，优先级高于随机身份）。两者都在 `.gitignore` 里，不会进版本库。

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

**2026-10-26 04:00 实测突破（当前已可用）**：把机型参数从「公共示例值」换成按真机结构拼出的完整档案（Android 16，Build / 主板 / 分辨率 / OAID 全部自洽）后：

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
4. **自动写入** config.json 里原神 / 崩铁 / 绝区零三款游戏的 `credential.cookie`（uid/server 原样保留）；
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

### 7.2 Token 怎么填（扫码登录，推荐）

给 `arknights` / `endfield` 的 `credential` 各填一个**鹰角登录 token**——即扫码流程中
`token_by_scan_code` 返回的那串**短 token**（**40~100 字符、纯 base64 字符集、不以 eyJ 开头**）。
扫一次码，本次要更新的游戏一起写入；凭证缓存按游戏分文件
（`logs/skland_cred_arknights.json` / `logs/skland_cred_endfield.json`），不会互相污染。

> ⚠️ **不要填 eyJ 开头的网页 JWT**。skland 换票接口 `/user/oauth2/v2/grant` 对
> `GrantV2Req.token` 有 **max=128 字符**的硬校验（2026-10-26 实测：128 通过、129 起即报
> `failed on the 'max' tag`）。旧版脚本误换的 176 字符"长 token"就是这么翻车的，现已修正：
> 登录流程不再调第二次 grant，直接使用 `token_by_scan_code` 的登录短 token。

**不要再手动去网页里找了**——通行证网页端的 Local Storage / Cookie 里只有短期 cred，拿不到登录 token。直接用内置的扫码登录：

```bash
python main.py --login-skland arknights   # 只登明日方舟的账号，写入 arknights 节点
python main.py --login-skland endfield    # 只登终末地的账号，写入 endfield 节点
python main.py --login-skland             # 不带参数：一次扫码写入两款
```

流程（全自动，接口为鹰角官方扫码登录，已实测）：

1. 终端打印 ASCII 二维码，同时保存 `qr_login.png`（扫码不便时用手机相册/大图打开）；
2. 用手机 **森空岛 / 明日方舟 / 终末地 App** 任一款扫码，并在手机上点确认；
3. 打印 `gen_scan/login`、`scan_status`、`token_by_scan_code` 的**服务端完整返回**，并体检 token 长度/前缀/是否含空白；
4. **写入前双重校验**：格式检查（空 / 含空白 / eyJ 开头 / >128 字符 / 过短 → 红字报错且**不落盘**）+ 语义预检（真调一次 grant，`status=0` 才写）；
5. **只写入本次指定的游戏节点**：`--login-skland arknights` 只动 arknights，不带参数则两款一起写，
   没被指定的游戏 Token 原样保留；被写入的游戏旧 cred 缓存会清掉，下次用新 token 重新换取。

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
python main.py --login-skland             # 一次扫码写入两款
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

- 标题：`游戏日常：2/5 已完成`（分子是已完成数，分母是参与检查的账号数，查询失败也占位）
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
| 休眠提醒 | 合盖 / 休眠 / 关机错过整点后，`app.catch_up_minutes` 分钟内醒来会**立刻补跑一次**（默认 180 分钟，填 `0` 关闭）。同一个时间点一天只补一次 |
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

#### 到点了却没动静？三分钟定位

先确认**守护进程真的在跑**（这是最常见的原因 —— 只配了时间点、没点「启动守护进程」）：

```bat
python daemon.py --status
```
看 `running` / `pid` / `heartbeat_age`；任务管理器里应该有 **2 个 `pythonw.exe`**
（守护进程 ~35MB + 控制台窗口 ~150MB）。也可以看 `logs\daemon.heartbeat` 的修改时间
（每 30 秒写一次）和 `logs\daemon.pid`。

再看日志 `<解压目录>\logs\dailycheck.log`（午夜后自动切分为 `dailycheck.log.YYYY-MM-DD`），
直接搜那天的那个时间点，按下面三行对号入座：

| 关键行 | 说明 |
| --- | --- |
| `命中时间点 2026-10-26 21:30，开始检查` | 这一轮**确实触发了** |
| `时间点 2026-10-26 21:30 处理完毕（退出码 0）` | 查询执行完成 |
| `推送决策：不推送（自动模式：全部日常已完成，保持安静）` | 没收到消息是**正常的**：那天没事可报 |

判定顺序：

1. 三行都在 → 没坏，只是当天日常都做完了（想每轮都收到消息，打开 **平安通知**）；
2. 有「开始检查」没「处理完毕」→ 查询阶段出错，往下看 `ERROR` 行，多半是凭证过期；
3. 一行都没有 → 才真的是没触发，依次检查：守护进程没启动 / 睡着了错过（看下面）/ 时间点格式；
4. 时间点是 `21:30:00` 这种带秒的写法会被静默跳过 —— 界面只会保存 `HH:MM`，手改配置时注意。

**睡过去的情况**：错过整点后，在 `app.catch_up_minutes` 窗口内（默认 180 分钟）醒过来会
自动补跑一次，日志里长这样：
```
错过了时间点 2026-10-26 21:30（迟到 15 分钟），现在补发一次检查
```
跨过零点才算彻底错过（那是"昨天"的时间点，不会再补）。

其它排查手段：

```bat
python daemon.py --once        :: 手动跑一轮，区分「调度问题」和「查询问题」
python main.py --force-alert   :: 强制发一条，验证推送通道本身通不通
```

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

按平台分成两块，**每款游戏一个复选框**，勾选只决定这次更新哪几款：

- 勾哪几款就只更新哪几款；没勾的游戏，`config.json` 里的 Cookie/token 和
  `logs/device_fp.json` 里的设备指纹都**原样不动**。
- 某一款换了账号，重新勾上扫一次，只覆盖这一款的凭证。
- 每款游戏的凭证、有效期、状态点分开记录，互不影响。

相比命令行版的区别：

- 二维码是图片，尺寸够大，直接对着屏幕扫；
- 状态实时刷新：等待扫码 → 已扫码待确认 → 写入中 → 完成；
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

> **二维码显示「不可用」？** 这里有一个独立的小服务（`core/tools_server.py`，
> 只读暴露 `tools/`，默认端口 8790）在局域网分发 APK。起不来时界面会直接
> 写明原因（哪个端口段被占 / `tools/` 里没有 APK / 被防火墙拦），旁边有
> **「换个端口重试」**按钮——8790~8795 都被占时会自动改用系统分配的空闲端口，
> 二维码里的地址跟着刷新，不用重启控制台。

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

**换设备是低频操作，别当排障按钮**：`getFp` 按出口 IP 签发指纹，风控（1034 / 10041 / 5003）
绝大多数是出口网络被标记，换机型解决不了；短时间内连着换，反而会让同一个账号在同一个 IP
下冒出一串陌生设备，更容易被拦。换完就固定用，隔几天再动。

> 这一步是可选的。不填就用首次运行随机分配的公开机型档案，也能跑。
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

**休眠提醒**：填一个分钟数，默认 180。合盖休眠、关机、断网让那个整点悄悄过去时，
只要在窗口内醒来，守护进程立刻补跑一次（同一天同一时间点只补一次）。
填 `0` = 完全关闭，错过就当天不再提醒。改完同样不用重启守护进程。

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

### 5.7 体力溢出预警（阈值告警）

树脂 / 开拓力 / 电量快满却没花掉，是另一种"损失"。在 `config.json` 的 `app` 段里设阈值：

```json
"app": {
  "resource_alert_enabled": true,
  "genshin_resin_threshold": 160,
  "starrail_stamina_threshold": 240,
  "zzz_energy_threshold": 200,
  "arknights_ap_threshold": 135,
  "endfield_sanity_threshold": 300
}
```

- `resource_alert_enabled` 是**总开关**：`false` 时整块判定直接跳过（阈值原样保留），
  适合「这几天不想收这类提醒」；
- 当前值 **≥ 阈值**就强制推一条（日常全做完也会推），`0` = 关闭该游戏的预警；
- **不额外发请求**：数值来自本轮定时检查已经拿到的实时便笺，只是拿来比一下；
- 推送标题会变成 `… · ⚠️ 体力将溢出（N）`，正文第一行就是
  `⚠️ 警告：绝区零电量已达到 240/240，即将溢出！`；
- 想立刻验证效果：把阈值临时调低（比如崩铁设 `30`），跑一次 `python main.py --dry-run`，
  控制台会出现 `⚠️` 警告行；验完记得改回去。

### 5.8 每日报表（完成统计）

控制台**第一个页签（默认首页）**。每次定时检查结束会顺手把结果写进 `data/history.json`
（原子写，坏了不影响主流程；dry-run 不写，免污染统计），**不额外发请求**。

页面内容：

- **连续完成**：每款游戏当前连续完成天数、本月完成 / 未完成天数；
- **最近 7 天**：每天一行，✅ 完成 · ❌ 查过但没做完 · – 没运行/没查到；
- 一天跑了多次检查（比如 08:30 没做完、20:30 补完）时，当天算 ✅；
- 只在切到这个页签时拉一次数据，**不做后台轮询**。

当天还没跑过检查时，连胜不会断（今天还没结束，不能算输）。想看接口直接拿 JSON：
`curl http://127.0.0.1:8765/api/history?days=7`。

### 5.9 信息查询与活动结束提醒

「信息查询」页分两块：

**实时体力**：点「刷新」会调用与定时检查同一套 `collect_statuses`
（`GET /api/live?refresh=1`），把五款游戏的当前体力 / 理智做成卡片。
**不写历史、不推送**，结果在进程内缓存 45 秒，来回切页签不会反复打接口。

**活动日历**：读的是各游戏**官方的「活动日历」**（就是米游社 App 里那份），
不是公告。每条都带名字、起止时间、进度和状态，缓存到 `data/activities.json`，
**20 小时内最多真正请求一次**（定时检查里的 `ensure_fresh()` 也遵守同一个 TTL）。

| 游戏 | 数据源 | 说明 |
| --- | --- | --- |
| 原神 | `POST …/genshin/api/act_calendar` | `act_list`（活动）+ `fixed_act_list`（渊月螺旋等），带 `countdown_seconds`、`is_finished` |
| 崩坏：星穹铁道 | `GET …/hkrpg/api/get_act_calender` | 官方把 calendar 拼成了 **calender**；`act_list` + `challenge_list`，带 `current/total_progress`、`act_status` |
| 绝区零 | `GET …/zzz/activity_calendar` | 参数是 **`uid` / `region`**（不是 role_id/server）；`activity_list` 带 `monochrome_got_cnt/cnt` |
| 明日方舟 | `ak.hypergryph.com/api/news` 的 `tab=1` | 官网列表没有结束时间，解析活动正文「活动时间：10月24日20:00~11月24日03:59」 |
| 终末地 | —— | 只有公告且不含绝对日期，标「暂不支持」 |

三家米游社接口都要**带账号 Cookie 与 DS 签名**（复用 `core/mihoyo_api.py` 那套），
所以必须先绑定对应游戏；没绑定的那款只会缺它自己的数据，不影响其它游戏。

**只留「抽卡资源」**：只给摩拉、经验书、材料、名片的纯养成活动一律不展示。

| 游戏 | 判定 | 数据从哪来 |
| --- | --- | --- |
| 原神 | 奖励含「原石」 | `reward_list[].name` |
| 星铁 | 奖励含「星琼」 | `reward_list[].name` |
| 绝区零 | `monochrome_cnt > 0` | 接口没有 reward_list，菲林池就是 `monochrome_cnt` |
| 明日方舟 | 正文含「合成玉」或「源石」 | 活动详情正文 |

**安全兜底**：奖励字段拿不到（字段缺失 / 解析失败 / 空数组）时**默认保留**，
界面上标一个灰色的「奖励未公开」。想反过来（缺数据也过滤）把
`core/activities.py` 里的 `KEEP_WHEN_REWARD_UNKNOWN` 改成 `False` 即可。

**两段排序**：`进行中`（按结束时间由近到远）在上，`未开启`（按开启时间由近到远）在下；
**已结束的直接过滤掉**；时间缺失的排在所在分组的最后面。

**不想要的活动直接删**：列表里每条右侧有个 `×`。点掉之后：

1. 立刻从界面消失；
2. ID 写进 `data/ignored_activities.json`；
3. **之后无论怎么刷新、什么时候推送，都不会再出现、也不会再提醒**
   （`ongoing()` 读缓存时就按黑名单过滤，`pending_alerts()` 走同一条路径）。

```
POST /api/activities/delete   {"id": "genshin:act:449"}
POST /api/activities/restore  {}          # 底部「恢复全部」
→ {"ok": true, "items": [...], "ignored_count": 1, ...}
```

黑名单只做「读时过滤」：缓存里仍保留全量，所以点「恢复全部」能立刻回来，不用等下一轮重拉。

**图标**：`static/icons/<game_id>.png`（官方原图，另配 72×72 的 `@2x`）。
`py tools/fetch_game_icons.py` 一次生成四张，来源按优先级：

| 游戏 | 来源 | 说明 |
| --- | --- | --- |
| 原神 | 本机米哈游启动器 `ico/hk4e_cn.ico` | 256×256 **经典版**，无周年角标 |
| 崩坏：星穹铁道 | App Store `com.miHoYo.hkrpg` | 1024 原图，裁掉底部名称条 |
| 绝区零 | App Store `com.miHoYo.Nap` | 同上 |
| 明日方舟 | App Store `com.hypergryph.arknights` | 本身没有名称条 |

⚠️ 原神**不要用商店图**：2026-10 起 App Store 国区 / 国际区、TapTap、官网 favicon
给的全是「6 周年」版（左上角一个 `6th` 角标），只有启动器里的本地 ico 还是经典版。
启动器没装时脚本会自动退回商店图，并在输出里明确提示「该图带周年角标」。

想换图直接把同名 png 覆盖进 `static/icons/` 就行，前端只认文件名（mtime 变了 URL 就变，不用清缓存）。

推送侧：定时检查读取缓存，活动剩余 ≤72 小时、且不在黑名单里时，在推送正文**末尾**追加
`📅 活动提醒：原神「无忧无虑大雪仗」，还剩 2 天结束，请抓紧时间！`，
标题追加 `· 📅 活动将结束（N）`。同一活动每天最多提醒一次（记在缓存的 `notified` 里），
只有真的发出去才记账，没发成下一轮会重试。`app.activity_alert_enabled` 可整体关闭。

**怎么验证删除真的生效**：

1. 「信息查询」里点某条活动右侧的 `×` —— 该行立刻消失，底部提示「已删除 1 个」，
   `data/ignored_activities.json` 里出现这条的 ID。
2. 点卡片右上角「刷新」强制重拉一次，它依然不出现（拉回来也会被黑名单挡住）。
3. 想看推送侧：把某条**没删**的活动 `end` 临时改成明天，跑
   `python -c "from core import activities; [print(a.warn_line()) for a in activities.pending_alerts()]"`
   —— 只打印没被删的那条；验证完点一次「刷新」拿回真实时间。
4. 点错了就点底部「恢复全部」，所有删掉的活动立刻回来。

### 5.10 开发 / 日常 / 发行：三种启动方式

先分清两层，多数困惑来自把这两层当成一件事：

| 层 | 是什么 | 改完要做什么 |
|---|---|---|
| `ResinLoop.exe` | 只是 C# 外壳：窗口 + WebView2 + 托盘 + 端口探测 | 只有改 `shell/*.cs` 才需要重新编译 |
| `engine/` | 真正的程序：便携 Python + `*.py` + `ui/` | 重启进程即可，**不需要重新打包** |

所以三种用法互不影响：

| 场景 | 入口 | 说明 |
|---|---|---|
| 写代码 | `dev.bat`（默认桌面窗口）、`dev.bat web`、`dev.bat all` | 跑工作树源码；改 UI 刷新页面即可，改 `.py` 重启进程 |
| 日常使用 | `start.bat`，或双击 `ResinLoop.exe` | 前者源码 + 守护进程 + 依赖自检；后者是发行形态，用 `engine/python` 便携解释器 |
| 发版 | `_build_release.py` | 只在要把 zip 发给别人时才跑 |

**测 exe 形态不需要打包**：根目录的 `ResinLoop.exe` 读同级 `engine/`，所以只要把工作树
同步进去就能起壳：

```bash
dev.bat sync     # 只同步（改完代码跑这个，不启动）
dev.bat exe      # 同步 + 启动 ResinLoop.exe
```

同步脚本 `_dev_sync_engine.py` 与 `_build_release.py` 取同一份清单（`login_ui.py` /
`main.py` / `daemon.py` / `core/*.py` / `ui/` 三件套 / 图标 / `tools/`），`daemon.py` 同样改成
无托盘；**不会覆盖** `engine/config.json`、`engine/logs/`、`engine/data/`、`engine/python/`。
只想看有没有漂移：`python _dev_sync_engine.py --check`（有差异返回 1）。

必须重新编译壳的场合只有：`shell/MainForm.cs`、`Program.cs`、`ResinLoop.csproj`（窗口行为、
托盘、端口探测、图标、版本号）。业务逻辑、页面、接口一律不碰壳。

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
网页控制台（五个页签：每日报表 / 信息查询 / 账号绑定 / 通知绑定 / 定时与状态）、
设备伪装、常驻守护进程 + 系统托盘、开机自启（HKCU Run）、一键启动。

明确未做：

- 自动签到 / 领取奖励 —— 本脚本只读不写。
- 崩铁 / 绝区零 / 森空岛的活动日历 —— 没有可用的公开接口，界面上标「暂不支持」。
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
