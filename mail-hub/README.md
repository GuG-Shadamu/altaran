# mail-hub

把 5–6 个 Gmail 汇总成一个 dashboard：

- 📅 预约 & 行程（航班、酒店、餐厅、医生、会议…）
- 📦 购物 & 到货（同一订单的多封邮件合并成一张卡，显示物流进度）
- 💼 认真的工作对话（标出"待我回复"；你回复后自动消失）
- 🤝 工作 reach-out（猎头、合作邀约）
- 🏷️ 值得关注的 deal（按 `config.toml` 里写的兴趣挑）
- 📣 广告（只留最近 3 天，默认折叠）
- 🗓️ 日历：6 个账号的 Google Calendar 加上邮件里提取的预约，合成一个周视图（手机上是按天的列表）。Gmail 自己已经加进日历的航班、酒店不会重复显示，预约卡片上会标"已在日历"

顶部"今天需要关注"会自动挑出：24 小时内的安排、派送中或有问题的包裹、高优先级的待回复和快过期的 deal。

## 架构

```
6 个账号 ──Gmail + Calendar API (只读)──▶ mail-hub tick（systemd timer，每 15 分钟）
                                 │ 规则先过滤（垃圾、社交、自己发的）
                                 │ claude -p 批量分类 + 提取字段
                                 ▼
                              SQLite ──▶ Discord 推送（高优先级 / 24 小时内的预约）
                                 │
你 ──Cloudflare Access──▶ mail.altaran.xyz ──现有 tunnel──▶ mail-hub serve (127.0.0.1:8095)
```

- 只用 Python 3.11+ 标准库，**不需要 pip install**。
- `serve` 常驻约 20–30 MB；`tick` 跑完就退出，`claude -p` 只在有新邮件时才启动。
- AI 后端可选：`claude_cli`（Claude 订阅，不额外付费）、`anthropic`（API 按量计费）、`ollama`（本地模型）。改 `[llm] backend` 一行就能换。
- 邮件内容是不可信输入：`claude -p` 在空目录里运行，所有工具都禁用。邮件里就算写了什么"指令"，最多也只会导致分错类。
- 页面跟 workbench 一样做两层校验：Cloudflare Access 只放你进来，服务自己再验一次 Access JWT。

## 安装（在 Air 上）

### 1. 代码和配置

```bash
git clone <repo> ~/Git/mail-hub-src && ln -s ~/Git/mail-hub-src/mail-hub ~/Git/mail-hub
mkdir -p ~/.config/mail-hub && chmod 700 ~/.config/mail-hub
cp ~/Git/mail-hub/config.example.toml ~/.config/mail-hub/config.toml
install -m 600 ~/Git/mail-hub/env.example ~/.config/mail-hub/env
```

编辑 `config.toml`：填上你的邮箱和 `[prefs]`。`aud` 先空着，第 5 步再填。

### 2. 先看效果（可选，不需要任何授权）

```bash
cd ~/Git/mail-hub
sed -e 's#~/.local/share/mail-hub/mailhub.db#/tmp/mailhub-demo.db#' -e 's/required = true/required = false/' ~/.config/mail-hub/config.toml > /tmp/demo.toml
python3 -m mailhub --config /tmp/demo.toml demo && python3 -m mailhub --config /tmp/demo.toml serve
```

打开 http://127.0.0.1:8095 就能看到用示例数据渲染的页面。看完 Ctrl-C 停掉。

### 3. Google OAuth（只需要做一次）

1. https://console.cloud.google.com 新建项目 `mail-hub`。
2. **APIs & Services → Library** 里搜索并 Enable 两个 API：**Gmail API** 和 **Google Calendar API**。
3. **OAuth consent screen**（Google Auth Platform）：User type 选 External，填应用名，然后把 5–6 个邮箱都加到 **Test users**。
4. **Audience → Publish app**，切到 *In production*。不切的话 refresh token 7 天就过期。个人自用不需要提交审核，授权时会出现"Google 尚未验证此应用"的提示，点"继续"即可。
5. **Clients → Create client → Desktop app**，下载 JSON，保存为 `~/.config/mail-hub/client_secret.json`（权限 `chmod 600`）。
6. 每个邮箱授权一次：

```bash
cd ~/Git/mail-hub && python3 -m mailhub auth you@gmail.com
```

终端会打印一个链接。在浏览器里打开，用对应的账号登录，**Gmail 和 Calendar 两项都要勾上**（都是只读）。如果浏览器不在 Air 上，授权后跳转到 `127.0.0.1` 的页面会打不开，这时把地址栏里的完整 URL 粘回终端即可。

### 4. Claude 订阅 token

```bash
claude setup-token
```

把输出的 token 填进 `~/.config/mail-hub/env` 的 `CLAUDE_CODE_OAUTH_TOKEN=`。Discord webhook 也填在这个文件里。再用 `which claude` 查到完整路径，填到 `config.toml` 的 `[llm.claude_cli] command`。

先手动跑一次：

```bash
cd ~/Git/mail-hub && set -a && . ~/.config/mail-hub/env && set +a && python3 -m mailhub tick && python3 -m mailhub status
```

第一次会把每个账号最近 14 天、最多 300 封邮件拉下来。每次 tick 最多分类 120 封，剩下的由之后的 timer 慢慢处理完。

### 5. 对外：Cloudflare Tunnel + Access（跟 climate 一样）

1. Zero Trust → **Networks → Tunnels** → Air 上那个 tunnel → **Public hostname** → Add：`mail.altaran.xyz` → `http://localhost:8095`。
2. Zero Trust → **Access → Applications** → Add → Self-hosted：域名 `mail.altaran.xyz`，Policy 设为 Allow、Emails = 你的邮箱。
3. 打开这个 Application 的 Overview 页面，复制 **Application Audience (AUD) Tag**，填进 `config.toml` 的 `[web.access] aud`。

### 6. systemd

```bash
mkdir -p ~/.config/systemd/user
cp ~/Git/mail-hub/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mail-hub-web.service mail-hub-tick.timer
```

查看状态和日志：

```bash
systemctl --user list-timers mail-hub-tick.timer
journalctl --user -u mail-hub-tick -n 50
```

### 7. 装到 iPhone 主屏幕

1. 用 **Safari** 打开 `https://mail.altaran.xyz`，先完成 Cloudflare Access 登录。
2. 点底部的分享按钮 → **添加到主屏幕**。

之后从主屏幕打开就是全屏的 App，有自己的图标，右上角 ↻ 可以手动刷新，每次切回这个 App 也会自动刷新。Access 登录过期后会再要求你登录一次；有效期可以在 Access application 的 Session Duration 里调长，比如 1 个月。

## 常用

| 命令 | 作用 |
|---|---|
| `python3 -m mailhub status` | 每个账号的 token、邮件数、同步错误，以及各类别的条目数 |
| `python3 -m mailhub tick --no-sync` | 不拉新邮件，只把已经存下的待处理邮件分类 |
| `python3 -m unittest discover -s tests` | 跑测试 |

想改分类口径，比如什么算"认真的工作"：改 `mailhub/classify.py` 里的 `PROMPT`，或者 `config.toml` 里的 `[prefs]`。
