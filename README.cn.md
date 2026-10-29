# dropline

> 一个本地运行的 AI 虚拟人物邮件系统 — 让 AI 角色通过**真实邮箱**与你保持长期通信。

🌏 English: [README.md](README.md)

[![CI](https://github.com/Asnly1/dropline/actions/workflows/ci.yml/badge.svg)](https://github.com/Asnly1/dropline/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-%E2%89%A5%203.12-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-lightgrey)
![Storage](https://img.shields.io/badge/Storage-SQLite%20%2B%20JSON%2FMarkdown-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ✨ 项目亮点

- 📬 **真实邮箱通信** — IMAP/SMTP 拉取和发送,不是临时聊天窗口,随时可翻阅邮件记录
- 👥 **Profile-First 结构** — 每个"角色 × 关系"组合拥有独立的 prompt、记忆、归档
- 🧠 **双阶段 LLM pipeline** — Stage 1 理解来信并提炼候选记忆,Stage 2 以角色口吻写回信
- 🖼️ **可选图片生成** — Stage 2 产出 `scene_card`,由独立图片 provider 渲染,失败则整次运行中断
- ⏰ **跨平台定时调度** — macOS `launchd` / Windows Task Scheduler 一键安装
- 🔒 **本地存储、可审计** — 运行状态用 SQLite;长期记忆和 episode 都是纯 JSON 文件,人类可读、可手工修改

> ⚠️ **隐私提示 ——「本地」指存储,不指处理。** dropline **不是**本地模型方案:每次运行都会把**来信正文、你的记忆文件、角色 prompt** 发送给你所配置的外部 LLM / 图片 provider(OpenAI、Gemini、Anthropic 等)。只有存储留在你本机,请选用你信任的 provider 来处理这些内容。

---

## 🏗️ 工作原理

每一次 pipeline 的完整生命周期:

```mermaid
flowchart TD
    A[Scheduler 或 run-once] --> B[加载 Active Profile<br/>角色 + 关系]
    B --> C[IMAP 拉取新邮件]
    C --> D[标准化入站内容]
    D --> E[Stage 1 LLM<br/>理解 + 候选记忆]
    E --> F[代码按规则写入 memory/]
    F --> G[Stage 2 LLM<br/>生成角色回信]
    G -->|image.enabled=true| H[图片生成]
    G -->|false| I[跳过图片]
    H --> J[SMTP 发送]
    I --> J
    J --> K[归档 archive/ + state.db]
```

设计要点:

- **LLM 提议,代码决策**:Stage 1 只输出候选记忆,真正写入哪个文件由本地代码按 [docs/memory_promotion_rules.md](docs/memory_promotion_rules.md) 决定
- **图片是硬门槛**:若启用图片生成且失败,本轮运行中断、不发信(设 `llm.image.on_failure=text_only` 可改为降级成纯文本发信);Stage 1 已写入的用户侧记忆保留,角色侧 episode 不写入
- **归档留痕**:每次运行的输入、Stage 1/2 输出、生成的图片都保存在 `archive/YYYY-MM-DD/run-*/`

> 📬 **看真实运行:** [**examples/conversation_keqing.cn.md**](examples/conversation_keqing.cn.md) 完整走一遍刻晴在首次 `run-once` 时发出的主动来信 + 第一轮一来一回——附每轮真实生成的配图,以及每封回信背后 Stage 1 记忆候选的可折叠「幕后」。([English](examples/conversation_keqing.en.md))

---

## 📂 Profile 结构

角色、关系、会话三层目录,彼此独立:

```mermaid
flowchart LR
    P[profiles/] --> A[active.json]
    P --> C[characters/]
    C --> CN["&lt;character&gt;/"]
    CN --> M[meta.json]
    CN --> PR["prompt/<br/>persona · world_state<br/>cast_relations · cast_pool<br/>translation_layer"]
    CN --> R["relationships/&lt;rel&gt;/<br/>prompt/relationship.md"]
    CN --> S["sessions/&lt;rel&gt;/"]
    S --> MEM["memory/<br/>user_profile · relationship_memory<br/>active_arc · conflict_notes"]
    S --> EP["episodes/<br/>user/ + character/"]
    S --> AR["archive/YYYY-MM-DD/run-*"]
```

- `profiles/active.json` 指向当前生效的角色 + 关系
- 每个 **session = 角色 × 关系**,切换不同关系就是一个全新记忆空间
- CLI 按名字、`名字 / 作品名`(如 `刻晴 / 原神`)或 slug 匹配角色,按名字或 slug 匹配关系;关系描述不参与匹配

---

## 🚀 快速开始

### 环境要求

- Python `>= 3.12`
- 一个可开启 IMAP/SMTP 授权码的邮箱(QQ / Gmail / 163 等)作为**角色邮箱**
- 至少一家 LLM provider 的 API key

### 安装

```bash
git clone <this-repo>
cd dropline
python3 -m pip install -e ".[dev]"
```

如果你已经在使用 `uv`,也可以等价执行:

```bash
uv pip install -e ".[dev]"
```

安装后可以直接运行 `dropline ...`,也可以继续使用 `python3 -m dropline ...`。
开发验证命令:

```bash
python3 -m pytest
```

### 配置

仓库根目录自带一份占位符 `config.example.yaml`,复制并编辑它即可。

```bash
cp config.example.yaml config.yaml   # 可选:保留一份私有配置副本
# 然后手动修改 config.yaml
```

#### 核心配置

dropline 会用到**两个**邮箱,首次运行前都要配好:

- **角色邮箱** — 一个真实、专用的邮箱账号,AI 用它登录来读取你的来信、发送回信。请给角色单独开一个账号,不要复用你的私人邮箱。
- **你的收件箱**(`primary_user_recipient`)— 角色的来信投递到这里。你写信 _发给_ 角色邮箱,角色 _回信_ 给你。

`allowlist_senders` 是一道硬门槛:任何不在列表中的发件人来信都会被忽略,因此只有你能联系到角色。

```yaml
mail:
  allowlist_senders:
    - "you@example.com" # 只处理这些发件人的来信
  primary_user_recipient: "you@example.com" # 回信投递到这里(你自己的收件箱)
  character_mailbox:
    address: "character@example.com"
    imap:
      host: "imap.example.com"
      port: 993
      ssl: true
      username: "character@example.com"
      password: "APP_PASSWORD" # 邮箱授权码,不是登录密码
      folder: "INBOX"
    smtp:
      host: "smtp.example.com"
      port: 465
      security: "ssl"
      username: "character@example.com"
      password: "APP_PASSWORD"

project:
  timezone: "America/Los_Angeles" # IANA 时区名;锚定下方的调度时间窗
```

如果你想把真实配置留在本地而不进入公开仓,可以继续维护 `config.yaml`,并在运行时显式传 `--config /绝对/路径/config.yaml`。
如果这份私有配置文件不在仓库根目录下,必须在 `project` 里显式指定真实仓库根目录:

```yaml
project:
  repo_root: "/absolute/path/to/dropline-repo"
```

未配置 `project.repo_root` 时,兼容旧行为:配置文件所在目录会被当作仓库根目录。

SMTP 加密方式使用明确枚举:

```yaml
mail:
  character_mailbox:
    smtp:
      security: "ssl" # ssl | starttls | none
```

`ssl` 表示直连 SMTP_SSL,`starttls` 表示普通 SMTP 连接后升级 STARTTLS,`none` 表示不启用传输层加密。`ssl` 与 `starttls` 都会按系统信任库校验服务器证书;`none` 会把授权码明文发送,每次发信都会在日志里打出警告。IMAP 侧由 `imap.ssl` 控制,同样校验证书。所有邮件连接都有 60 秒超时。

如果日志里出现 `CERTIFICATE_VERIFY_FAILED`:dropline 按系统信任库校验证书,当 Python 自身没有系统 CA 时(macOS 上 python.org 安装包常见)会退回到 `certifi` 自带的证书包。自建邮件服务器用自签名证书时,需要把它的 CA 加进系统信任库;`host` 必须填证书上的域名,不能填 IP。

#### 收件箱游标、引用链与附件

这几项没有配置开关,是拉信阶段的固定行为:

- **UIDVALIDITY 变化**:每次拉信都会读取 IMAP 文件夹的 `UIDVALIDITY` 并记在 `state.db`。邮箱重建索引或迁移后 UID 会重新编号,此时 dropline 会打一条 ERROR 日志,把 `last_seen_imap_uid` 游标归零,并从首封出站邮件的日期起重新扫描收件箱。已入库的来信按 `Message-ID` 去重,不会被再回一次;同理,把一封信移出 INBOX 再移回来(UID 变了)也不会触发第二封回信。没有 `Message-ID` 头的来信会合成一个带 UIDVALIDITY 的 ID(`<imap-<uidvalidity>-<uid>@dropline.invalid>`),邮箱重建后在同一个 UID 上出现的另一封信仍会被回复。
- **引用链剥离**:来信正文在送入 Stage 1 前会剥掉被引用的上一封信,只保留用户自己写的部分。纯文本识别 `>` 引用行、`On … wrote:`、`-----Original Message-----`、`From:`,以及 QQ 邮箱 / Foxmail 的 `------------------ 原始邮件 ------------------`、中文 Outlook / QQ 邮箱的 `发件人:`、163 / Apple Mail / Thunderbird 的 `在 … 写道:`(行内须含 HH:MM 时间或邮箱地址)、中文 Gmail 的 `… 于 … 写道:`(行内须含 `<地址>`)、Outlook 的 `________________` 分隔线(只有后面紧跟 `发件人:` / `From:` 之类的引用头才截断,用户自己画的分隔线只是被丢弃),因此像 `在 2026-03-31 写道：` 这样用户自己的叙述句会被保留;HTML 正文会先删掉注释,再整块删掉 `<blockquote>`、`gmail_quote` 以及 id/class 含 `quote` 的元素。此外,来信中与角色最近两封信逐段相同且长度不少于 20 字的段落也会被删掉,避免角色自己写过的话被当成用户说的话写进记忆;如果读不到角色的最近来信,拉信照常进行,只是跳过这一步并记一条警告。
- **附件**:只记录元数据(文件名、类型、大小、是否内嵌)。所有图片部件无论 disposition 都算附件,带文件名的内嵌部件(例如以 inline 方式附上的 `.txt` / `.ics`)也算附件且不会并入正文;附件内容不会被读取、归档或发给模型,角色只知道"收到了一个什么文件"。

如果你想统一控制模型输出的文本语言,可以在 `llm` 下增加 `language`:

```yaml
llm:
  language: "简体中文"
  stage1:
    provider: "openai"
  stage2:
    provider: "openai"
  image:
    enabled: true
    provider: "openai"
```

`llm.language` 会固定 Stage 1、Stage 2、`generate-character`、`generate-relationship`、`scene_card` 的文本输出语言,并让最终图片 prompt 跟随目标语言。当前只接受精确值 `简体中文` 和 `English`。

如果只想发送文字邮件,把 `llm.image.enabled=false`:

```yaml
llm:
  image:
    enabled: false
```

关闭后会完全跳过图片生成,Stage 2 不再要求输出 `scene_card`,邮件只发送正文,也不会调用图片 provider。

邮件正文按纯文本发送。Stage 2 使用 `body` 字段输出正文;不会渲染 Markdown。HTML alternative 只做转义、换行和段落包装。

### 生成第一个角色 + 关系

仓库自带两套可直接使用的 profile:`刻晴`(原神)+ 关系 `青梅竹马`,以及 `keqing`(Genshin Impact)+ 关系 `childhood-friends`,可以直接跳到[运行一次 pipeline](#运行一次-pipeline)。要生成自己的角色:

```bash
# 动漫/游戏角色:附带作品名
python3 -m dropline generate-character --character "胡桃" --work "原神"

# 现实公众人物:不填作品名,自动走"公开形象优先"模式
python3 -m dropline generate-character --character "某位作家"

# 生成关系 prompt。--name 是之后传给 select 的关系名;不填则取描述的前 24 个字符
python3 -m dropline generate-relationship \
    --character "胡桃" \
    --relationship "诗社认识的笔友,爱开玩笑但真诚" \
    --name "笔友"
```

两个生成命令都不会覆盖已存在的角色或关系。

### 运行一次 pipeline

```bash
# 选中 active profile(会自动创建 session);这里用的是仓库自带的示例 profile
python3 -m dropline select --character "刻晴" --relationship "青梅竹马"

# 初始化 session 的 memory 和 state.db
python3 -m dropline init-state

# 立即跑一次完整流程(拉信 → 理解 → 写回信 → 发送 → 归档)
python3 -m dropline run-once
```

> ⚠️ 首次运行语义:第一次 `run-once` 不会处理历史来信,只会建立首封主动邮件;首封主动邮件之前已经收到的来信可能不会进入对话。

> 💡 如果不想记命令参数,直接 `python3 -m dropline` 进入交互式 CLI,输命令名后会自动追问所需参数。

---

## 🧠 记忆系统

session 的记忆分 6 类,Stage 1 只负责**提议**,代码负责**裁决**是否写入:

| 文件 / 目录                       | 类型       | 典型内容                                 |
| --------------------------------- | ---------- | ---------------------------------------- |
| `memory/user_profile.json`        | 稳定事实   | 用户长期目标、核心偏好(门槛最高)         |
| `memory/relationship_memory.json` | 关系约定   | 称呼、共同梗、关系定义                   |
| `memory/active_arc.json`          | 进行中主线 | 最近 7 天内反复出现、未解决(最多 1~2 条) |
| `episodes/user/*.json`            | 用户短期   | 日常心情、临时烦恼(只有最近 7 天会进入提示词,没有自动降权) |
| `episodes/character/*.json`       | 角色短期   | 角色本次处理的事务                       |
| `memory/conflict_notes.json`      | 冲突记录   | 新信息与旧记忆冲突,先入此处等待二次确认  |
| `memory/suppressed.json`          | 删除记录   | 你手动删除过的记忆,模型不得再写回        |

这些记忆文件和 episode 都是纯 JSON 数组,UTF-8 纯文本,可直接打开手工编辑;pipeline 每次运行后会重写它们。

晋升门槛由代码执行,模型自报的 confidence / durability 只是必要条件,不是充分条件:

- 候选记忆的 `evidence` 必须能在来信正文里找到:归一化后原样包含,或与某一句近乎逐字(字符 3-gram 覆盖率 ≥ 0.8 且否定状态一致,所以漏掉"不"的改写不算);少于 4 个汉字或 2 个英文单词的引用不算。找不到的候选只留在 episode,不进长期记忆。
- 用户在信里明确要求("记住……"、"别忘了……"、"以后叫我……"、"remember …")且该句引用了这条内容,当封即可写入;"不要记住"、"我记得小时候"这类不算。其余内容必须在至少 2 个不同的来信日期被提到(按证据所在那封信的 Date 头算,同一天多封不算,一批里混了多天的信也只算证据所在的那封)。
- transient 的候选永远不写;user_profile 还要求 confidence ≥ 0.85 且 durability 为 stable,relationship_memory 要求 confidence ≥ 0.8;除非用户明确要求,content 带"今天、这周"之类措辞的不进 user_profile。每封信最多新增 2 条 user_profile、1 条 relationship_memory、1 条 active_arc。
- 与旧记忆相反的说法先进 conflict_notes(否定词映射覆盖 喜欢/不喜欢、是/不是、有/没有、希望/不希望、会/不会、不再、从不 以及英文 not / never / no longer / doesn't 等)。用户在信里明确更正("我说错了"、"更正一下"、"不是……而是……";"其实"只在同句带否定或回指"之前/上次"时算)会立即覆盖;否则要在第 2 个不同日期再次出现才覆盖。被覆盖的旧条目保留在文件里并标记 `status: superseded`,不再进入提示词。
- 你手动删除的记忆会记入 `memory/suppressed.json`,模型之后再提出同样内容也不会写回。

完整规则见 [docs/memory_promotion_rules.md](docs/memory_promotion_rules.md)。

---

## 🗂️ 记忆管理界面

想看看角色到底记住了你什么,或者改掉记错的内容,可以打开本地网页:

```bash
python3 -m dropline memory-ui
# 可选:--host 127.0.0.1  --port 8765(0 表示随机端口)  --no-browser(不自动打开浏览器)
```

命令会在本机启动一个只监听 `127.0.0.1` 的小服务,打印访问地址并自动打开浏览器,Ctrl+C 退出;没有 active profile 时会报错退出。页面是单个 HTML 文件,不引用任何外部资源,离线可用,适配手机宽度和系统深色模式,界面文案跟随 `llm.language`(简体中文 / English)。

页面按当前 active session 分区显示:

| 分区                             | 内容                                                                     | 能做什么                                                                      |
| -------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| 用户画像 / 关系记忆 / 进行中主线 | 每条记忆的内容、为什么重要、依据、来源日期,以及状态徽标(生效中 / 已被替代) | 新增、编辑、删除                                                              |
| 待确认的冲突                     | 来信里与旧记忆相反、还在等第二次确认的说法                               | 「保留旧值」丢弃新说法;「采用新值」写入新说法,旧条目标记 `status: superseded` |
| 已删除的记忆(墓碑)               | 你删除过的内容                                                           | 「恢复」移除墓碑                                                              |
| 最近 7 天 episode                | 用户侧与角色侧的短期记录                                                 | 只读                                                                          |

手动新增的条目 `confidence` 为 1.0、`evidence` 记为 `manual entry`,不经过晋升门槛;手动修改会在 `evidence` 里追加 `manual edit`。

**删除的语义**:在页面上删除一条记忆,会把它从记忆文件里移除,并在 `memory/suppressed.json` 写一条墓碑;之后模型再提出同样的内容也不会写回。「恢复」只是移除墓碑,记忆本身不会自动回来,需要的话再手动新增(新增与墓碑相同的内容时,墓碑会自动移除)。

**安全**:服务默认只绑定 `127.0.0.1`;所有修改请求都必须带上启动时随机生成、注入在页面里的令牌,并且 `Origin` / `Host` 头必须指向这个服务本身,所以其他网页无法借你的浏览器改动记忆;响应一律 `Cache-Control: no-store`。如果用 `--host` 绑定到别的地址,请直接用那个地址打开页面。

---

## ⏰ 定时调度

推荐模式:**定时拉起一次 `scheduler-tick`,由它自己判断是否到点**,而非常驻进程。

### 一键安装

```bash
python3 -m dropline install-scheduler --config /绝对/路径/config.yaml --yes
python3 -m dropline uninstall-scheduler --config /绝对/路径/config.yaml --yes
```

两个命令都会要求确认,加 `--yes` 跳过;无人值守时不加会停在确认提示并以退出码 1 取消。

可选参数:

- `--task-name` 自定义任务名(默认 `dropline`)
- `--python` 指定 Python 解释器(默认跟随当前 `sys.executable`)
- `--no-load` 只写出安装产物,不调用 `launchctl` / `schtasks`。注意 macOS 上 plist 仍会被复制到 `~/Library/LaunchAgents/`,launchd 会在下次登录时自动加载;不想这样就手动删掉它

日志统一输出到:

- `runtime_logs/dropline.out.log`
- `runtime_logs/dropline.err.log`

触发逻辑:每 `schedule.check_every_minutes` 分钟唤起一次;不在时间窗口或未到每日目标发送时间则快速退出,到点则跑一次完整 pipeline。

完整调度配置项:

```yaml
schedule:
  enabled: true # 调度总开关
  window_start: "20:00" # 每天最早发送时间
  window_end: "23:59" # 每天最晚发送时间(设为小于 start 即跨午夜)
  daily_target_pick_time: "00:05" # 过了这个时间,dropline 才锁定当天的发送时刻
  check_every_minutes: 30 # OS 定时器多久唤起一次 scheduler-tick
```

每天的实际发送时刻会在时间窗口内**随机选取**(按日期固定),角色不会每天卡在同一分钟写信。

调度窗口支持跨午夜。例如:

```yaml
schedule:
  window_start: "22:00"
  window_end: "01:00"
```

这表示从目标日期当天 22:00 到次日 01:00。次日 01:00 之后不会继续补发这个窗口内的任务。

### macOS (launchd)

```bash
# 查看任务状态
launchctl print gui/$(id -u)/dropline

# 手动立即触发
launchctl kickstart -k gui/$(id -u)/dropline

# 手动卸载
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dropline.plist
rm ~/Library/LaunchAgents/dropline.plist
```

### Windows (Task Scheduler)

```powershell
# 查看任务状态
schtasks /Query /TN "dropline" /V /FO LIST

# 手动立即触发
schtasks /Run /TN "dropline"

# 手动卸载
schtasks /Delete /TN "dropline" /F
```

---

## 🛠️ 故障处理与告警

无人值守运行时,dropline 会自己收住常见故障,并在需要人介入时发邮件告诉你。

### 失败后怎么重试

- **先退避,再重试**:一次 run 失败后,调度器至少等 `schedule.retry_after_minutes` 分钟才会再试(tick 日志事件 `skipped_backoff`);每天由调度器触发的 run 不超过 `schedule.max_runs_per_day` 次(`skipped_daily_limit`),手动 `run-once` 不受此限制;"昨日漏发补发"只会尝试 1 次(`catch_up_succeeded` / `catch_up_failed`)。成功一次后连续失败计数归零。拿不到 run 锁的 run(例如手动 `run-once` 与 tick 撞上)只记为 `error_stage=lock`:不算失败(不退避、不告警),也不占用每日次数和那一次补发机会。
- **重试复用归档产物**:同一批次(同一 `batch_id`)重试时,若上次失败的 run 已归档 `stage2.json` 且该 run 是 24 小时内开始的,直接跳过 Stage 1 / Stage 2 进入配图和发信;若只有 `stage1.json`,则只重跑 Stage 2,且不会重复写入用户侧 episode。更早的草稿会被丢弃,两个阶段重新执行。日志会写明复用了哪个 run 的产物(`reused_run_id=...`)。
- **来信隔离**:reply 模式的 run 在 stage1 / stage2 / validation 阶段失败时(配图供应商故障不是来信的问题,不计入),本批来信的 `attempt_count` +1 并记录 `last_error`,按来信所在的 `(UIDVALIDITY, UID)` 行计数,旧 epoch 里同号的来信不会被一起计数;达到 `schedule.max_batch_attempts` 次(Stage 2 校验失败只需 2 次)就被**隔离**(`quarantined_at`)。被隔离的来信不再进入批次,下一次 run 会恢复为主动来信模式,不会让一封坏信卡住整个角色。`show-inbox` 会标出 `QUARANTINED` 和 `last_error`,并打印每行的 `uidvalidity`;修好问题后用 `requeue-inbound --uid N` 把已隔离的来信放回队列(已回复或仍在排队的 uid 会给出相应提示,不会被改动),`delete-inbound --uid N` 也可以直接删掉它。两条命令都只接受 uid:同号来信存在于多个 UIDVALIDITY epoch 时优先当前 epoch,否则取最新的 epoch,所以迁移前保留的旧行(epoch 0)仍然能被删掉。
- **发出的信不会重发**:邮件已发出但 run 在收尾时失败(例如角色侧记忆写入失败),来信会先被标记为已回复,重试只补做收尾;这类失败记为 `pipeline` 阶段,不会被误记为 `imap`。
- **配图降级**:`llm.image.on_failure` 默认 `abort`(保持"图片是硬门槛");设为 `text_only` 时图片生成失败只记 WARNING,并在归档目录写 `image_failure.json`,邮件改为纯文本发送。
- **僵尸 run 自动收尾**:每次 `scheduler-tick` 开始时,`started_at` 早于 6 小时前且仍为 `running` 的 run 会被自动标为 `failed`(`error_stage=stale_running`);`mark-stale-runs-failed` 仍可手动处理更久远的记录。

### 查看状态

```bash
python3 -m dropline status --config /绝对/路径/config.yaml
```

输出当前 session 的最近一次成功 / 失败 run(run_id、阶段、错误信息)、连续失败次数、未消费与已隔离的来信数、pending 出站数、IMAP 游标(`last_seen_uid` / `last_consumed_uid`,以及 kv 里若存在的 `uidvalidity`)、今日调度状态(`scheduled_for_date` / `target_send_time` / `sent_time` / `runs_today`)和每类告警(`failures` / `quarantine`)最近一次的发送时间。

### 告警邮件

连续失败次数达到 `mail.alerts.after_failures`,或有来信被隔离时,dropline 会用角色邮箱给 `mail.alerts.recipient` 发一封**纯文本系统通知**:发件人显示名固定为 `dropline`(不是角色名),主题以 `[dropline] ` 开头,正文写明这是系统通知而非角色来信,包含失败阶段、错误信息和建议的下一步(看 `runtime_logs/`、`dropline status`、`requeue-inbound`)。每类告警同一天最多发一封——`failures` 对应连续失败阈值,`quarantine` 对应来信被隔离——所以同一天早些时候发过的失败告警不会把隔离告警吞掉;告警本身发送失败只记日志,不影响主流程。

相关配置项(都有默认值,可省略):

```yaml
schedule:
  retry_after_minutes: 120 # 失败后至少等多久再试(必须小于 1440)
  max_runs_per_day: 3 # 调度器每天最多触发几次 run(run-once 不计)
  max_batch_attempts: 3 # 同一批来信最多尝试几次,之后隔离(validation 失败 2 次即隔离)

mail:
  alerts:
    enabled: true # 告警邮件总开关
    recipient: "you@example.com" # 默认等于 primary_user_recipient
    after_failures: 2 # 连续失败几次后告警

llm:
  image:
    on_failure: "abort" # abort | text_only
```

---

## 🔌 支持的 LLM Provider

| Provider        | 默认文本模型                   | 默认图片模型                 |
| --------------- | ------------------------------ | ---------------------------- |
| OpenAI          | `gpt-5.5`                      | `gpt-image-2-2026-04-21`     |
| Gemini          | `gemini-3.1-pro-preview`       | `gemini-3-pro-image-preview` |
| Anthropic       | `claude-opus-4-8`              | —                            |
| Grok            | `grok-4.20-0309-non-reasoning` | `grok-imagine-image-pro`     |
| Qwen (通义千问) | `qwen3-max-2026-01-23`         | `qwen-image-2.0-pro`         |
| Doubao (豆包)   | `doubao-seed-2-0-pro-260215`   | `doubao-seedream-5-0-260128` |
| DeepSeek        | `deepseek-v4-pro`              | —                            |
| GLM (智谱)      | `GLM-5.2`                      | —                            |

> 以上是 `config.yaml` 里预填的默认模型,你可以随时改 `<provider>.text_model` / `image_model` 切换到别的版本。

配置原则:

- `llm.stage1.provider` / `llm.stage2.provider` 决定**文本** provider
- `llm.image.provider` 决定**图片** provider
- 只需要配置当前 `llm.stage1.provider`、`llm.stage2.provider`,以及启用图片时的 `llm.image.provider`
- 当前文本 provider 需要 `api_key` 和 `text_model`;当前图片 provider 还需要 `image_model`
- 未使用 provider 块可以删除,也可以保留占位;缺 `api_key` / 模型字段不会阻塞启动,等切换 provider 时再补齐
- `llm.image.enabled=false` 时完全跳过图片生成,Stage 2 也不再要求输出 `scene_card`

---

## 📘 CLI 参考

进入交互式 CLI:

```bash
python3 -m dropline
```

<details>
<summary><b>完整命令清单</b></summary>

| 命令                                        | 说明                                                                       |
| ------------------------------------------- | -------------------------------------------------------------------------- |
| `list`                                      | 列出所有角色与关系                                                         |
| `show-active`                               | 显示当前 active profile                                                    |
| `show-inbox` / `show-outbox`                | 查看当前 session 的收/发件箱                                               |
| `generate-character`                        | LLM 生成角色 prompt(persona/world_state/…)                                 |
| `generate-relationship`                     | LLM 生成关系 prompt                                                        |
| `select`                                    | 选中角色 + 关系为 active,并创建 session                                    |
| `delete-session`                            | 删除目标 session 的 memory/episodes/archive;可用 `--yes` 跳过确认          |
| `delete-inbound`                            | 删除单条 inbound(`--uid`)或整箱清空(`--all`);可用 `--yes` 跳过确认         |
| `delete-outbound`                           | 删除单条 outbound(`--message-id`)或整箱清空(`--all`);可用 `--yes` 跳过确认 |
| `requeue-inbound`                           | 清除一封来信的隔离标记与失败计数(`--uid`),让它重新进入队列                 |
| `init-state`                                | 初始化当前 active session 的 memory 和 state.db                            |
| `run-once`                                  | 立即执行一次完整 pipeline                                                  |
| `scheduler-tick`                            | 执行一次调度检查,到点才跑 pipeline                                         |
| `mark-stale-runs-failed`                    | 把当前 active session 中超过指定天数仍为 running 的 run 标为 failed        |
| `status`                                    | 查看当前 session 的最近 run、失败计数、收/发件箱状态与今日调度              |
| `install-scheduler` / `uninstall-scheduler` | 安装 / 卸载系统定时任务                                                    |
| `reset-dev-state`                           | 清空所有 session、日志、数据库,再为当前 active 重建一个空 session;可用 `--yes` 跳过确认 |
| `memory-ui`                                 | 打开本地网页查看、新增、修改、删除记忆,处理冲突,恢复已删除的记忆;删除会写入墓碑 |

</details>

## 📚 更多文档

- [examples/conversation_keqing.cn.md](examples/conversation_keqing.cn.md) — 一段真实的首轮通信(首封主动信 + 第一轮),含配图与幕后
- [docs/run_pipeline.md](docs/run_pipeline.md) — Pipeline 每阶段的输入输出约定
- [docs/memory_promotion_rules.md](docs/memory_promotion_rules.md) — 记忆晋升的完整规则

---

## 📝 License

本项目基于 [MIT License](LICENSE) 开源 — 你可以自由使用、修改、分发,只需保留版权声明。
