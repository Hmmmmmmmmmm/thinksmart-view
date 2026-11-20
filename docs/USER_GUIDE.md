# 安装后用户自备配置清单与大模型选型指南

执行完 `sudo bash device/install.sh` 后，系统的底层驱动、桌面界面、76 路广播电台、白噪音、万年历、倒数日、局域网可视对讲、AirPlay 投屏与蓝牙音箱**均已就绪**。

为了让天气、语音对话、无损音乐等联网能力完整运转，本文档为您梳理了**用户需要自行准备的账号/API Key 清单**，以及**本地部署 vs 云端大模型的详细选型建议**。

---

## 一、全局总览：哪些必须准备？哪些免费即用？

| 类别 | 功能模块 | 需要准备的东西 | 是否必须 | 费用参考 |
| --- | --- | --- | --- | --- |
| **核心必配** | **小智语音智能对话与语音控屏** | 任意一家云端大模型 API Key（如智谱 / 通义 / DeepSeek / 豆包） | **必须（仅此 1 项）** | 智谱 `glm-4-flash` **0 元免费**；DeepSeek/通义日常家用约 **1~5 元/月** |
| **强烈推荐** | **桌面主页实况天气 / 24h 曲线 / 空气质量 / 自动日出浅色主题** | **和风天气** `API Host` + `API Key`（[dev.qweather.com](https://dev.qweather.com/)） | 强烈推荐 | 个人开发者免费订阅额度完全够单机每 15 分钟刷新使用 |
| **强烈推荐** | **QQ 音乐绿钻无损曲库与「我喜欢」歌单同步** | 手机 QQ 或微信在设备屏幕上**直接扫码登录** | 选配（不登录可听免费歌 + 全部 76 个电台/听书） | 普通账号免费；绿钻 VIP 可解锁 `SQ FLAC` 无损音质 |
| **体验增强** | **语音实时联网搜索 + 周边餐厅/商场/路况查询** | DeepSeek API Key + **秘塔搜索 Key** + **高德开放平台 Web 服务 Key** | 选配 | 高德个人认证每日免费配额极高；秘塔新用户送免费额度 |
| **按需进阶** | **AI 学习坐姿与专注陪伴（摄像头连拍分析）** | 本地 Apple Silicon Mac，**或**云端视觉模型 Key（通义 `qwen-vl-plus` / 智谱 `glm-4v-flash`） | 选配（家里有娃写作业再配） | 本地 Mac 0 元；云端智谱 `glm-4v-flash` **0 元免费** |
| **按需进阶** | **Nezha 服务器探针实时看板** | 自己的哪吒监控面板域名 + 服务器列表 JSON | 选配（手里有云服务器再配） | 自有面板 |
| **按需进阶** | **外网手机随时远程看家 / 双向视频通话** | Tailscale / Zerotier 异地组网，或 1 台公网云主机配置 SSH 反向隧道 | 选配（仅在家用局域网免配） | Tailscale 个人版免费 |
| **完全免配** | **76 路广播/故事/评书、助眠声景、万年历、倒数日、留言板、番茄钟、AirPlay 投屏、蓝牙音箱、U 盘音乐、本机语音唤醒与 EdgeTTS 发声** | **无需任何账号或 Key，装完即用** | 开箱即用 | **0 元** |

### AirPlay 自动播放

在发送端选择 **ThinkSmart** 后，音频会由设备扬声器播放，屏幕镜像会自动显示。支持 HLS 的 UxPlay 默认也接受兼容应用的视频直投，例如 iPhone 上的 YouTube 应用。仅发送音频的应用只能播放声音。如需显示这类应用的画面，可从 iPhone 控制中心启动「屏幕镜像」。

使用较旧的 UxPlay 时，音频和屏幕镜像仍可用。也可在 `thinksmart-airplay.service` 的用户服务配置中设置 `Environment=THINKSMART_AIRPLAY_DIRECT_VIDEO=0`，关闭 HLS 视频直投。

---

## 二、各项自备配置实操步骤

### 1. 小智管理后台基础配置（核心必配，网页点选即可）

刷机并装好本套件后，在电脑或手机浏览器打开小智本机管理后台：
- **后台地址**：`http://<联想设备局域网IP>:16688/admin`（首次打开按提示设置管理员密码）
- **配置步骤**：
  1. 进入 **「大模型」** 页面 → 选择您准备好的厂商（如 `智谱 GLM`、`DeepSeek`、`通义千问`、`豆包`）→ 填入 `API Key` → 点击「保存」并设为默认模型。
  2. 进入 **「语音识别」** 页面 → 无本地 Mac 时保持默认 **`SherpaXASR`（本机离线识别，免费免 Key）**，或填入火山引擎豆包流式识别 Key。
  3. 进入 **「语音合成」** 页面 → 无本地 Mac 时保持默认 **`EdgeTTS`（免费免 Key）**，可在线试听切换「晓晓（温暖女声）」「云希（阳光男声）」「云夏（可爱童声）」等 14 种音色。
  4. 进入 **「意图识别 / 插件」** 页面 → 确认勾选 `放音乐`、`查天气`、`热点新闻`、`中新网新闻`（如需联网搜索可一并勾选 `联网搜索` 并填入 Key）。

---

### 2. 和风天气 API 配置（点亮桌面天气卡片与日出日落自动换肤）

桌面主页的实时天气、多城市切换、24 小时温度曲线、7 天温差条、国标 6 项空气质量（PM2.5/PM10/O₃ 等）以及「日出自动切浅色、日落自动切深色」均由和风天气驱动。

1. **申请步骤**：
   - 注册并登录 [和风天气开发服务控制台](https://console.qweather.com/)；
   - 在「设置」中查看您的专属 **API Host**（形如 `abc123xyz.re.qweatherapi.com`）；
   - 创建一个项目并生成凭据，选择 **API KEY** 认证方式，复制生成的 Key，并在控制台开通免费开发订阅。
2. **在设备上写入配置（使用 `local.conf`，后续升级绝不丢失）**：
   ```bash
   sudo mkdir -p /etc/systemd/system/thinksmart-home.service.d
   sudo tee /etc/systemd/system/thinksmart-home.service.d/local.conf >/dev/null <<'EOF'
   [Service]
   Environment=QWEATHER_API_HOST=您的专属域名.re.qweatherapi.com
   Environment=QWEATHER_API_KEY=您的和风天气API_KEY
   Environment=WEATHER_DEFAULT_CITY=北京
   Environment=WEATHER_EXTRA_CITIES=上海,广州,深圳
   EOF
   sudo systemctl daemon-reload && sudo systemctl restart thinksmart-home.service
   ```

---

### 3. 语音联网搜索与本地生活地图增强（选配）

本套件内置增强版四合一搜索插件（支持查最新新闻政策、商场楼层餐厅、周边停车场、两地驾车距离路线，以及免 Key 的 A 股/港股/美股/黄金实时行情）：

1. **DeepSeek 联网搜索 Key**：在 `http://<设备IP>:16688/admin` →「插件」→「联网搜索 · 设置」中填入 DeepSeek 官方 API Key（接口保持默认 `https://api.deepseek.com/anthropic/v1`）。
2. **秘塔搜索 + 高德地图 Key（查本地吃喝玩乐与路线强烈建议配齐）**：
   - **秘塔搜索 Key**：前往 [秘塔 AI 搜索 API 平台 (`metaso.cn`)](https://metaso.cn/) 获取 `mk-...` 密钥；
   - **高德 Web 服务 Key**：前往 [高德开放平台 (`lbs.amap.com`)](https://lbs.amap.com/) → 应用管理 → 创建应用 → 添加 Key，**服务平台务必选择「Web服务」**；
   - 在设备 SSH 终端运行内置安全写入工具（自动在线验活密钥并重启小智 Hub）：
     ```bash
     printf '%s\n%s\n' '您的秘塔Key(留空跳过)' '您的高德Web服务Key(留空跳过)' | sudo /usr/local/sbin/thinksmart-set-search-keys
     ```

---

### 4. QQ 音乐扫码登录（选配）

无需改任何配置文件：直接在联想屏幕主页点击底部 **「音乐」** → 左侧栏最下方 **「账号」** → 点击「手机扫码登录」，用手机 QQ 或微信扫屏幕二维码即可同步个人歌单与绿钻无损权限。

---

### 5. 服务器探针看板与外网可视对讲（有需求再配）

- **Nezha 服务器探针**：若您有自建哪吒监控面板，新建 `/etc/systemd/system/thinksmart-monitor.service.d/local.conf` 写入 `Environment=NEZHA_HOST=您的面板域名`，并按 [`servers.example.json`](../device/monitor/servers.example.json) 编辑 `/usr/local/share/thinksmart-monitor/servers.json` 后重启 `thinksmart-monitor`。没有云服务器可完全忽略。
- **可视对讲与远程留言**：
  - **在家局域网**：手机浏览器直接访问 `http://<设备局域网IP>:18091` 即可实时看画面、双向视频通话、发家庭留言和管理公农历生日倒数日（家长学习统计页为 `http://<设备局域网IP>:8094`）。
  - **出门在外远程访问**：最省心安全的做法是在设备上安装 **Tailscale**（`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`），手机开 Tailscale 直接用内网 IP 访问，无需公网服务器；若自备云服务器，可参考 [`thinksmart-intercom-tunnel.sh`](../device/intercom/thinksmart-intercom-tunnel.sh) 配置反向 SSH 隧道。

---

## 三、大模型选型与安装建议（本地部署 vs 纯云端方案）

语音桌面中枢对大模型的核心要求只有两条：**① 首字响应必须快（TTFT < 0.6 秒，否则喊完话干等很难受）**、**② 工具调用（Function Calling）必须稳（关系到语音点歌、换电台、记倒数日、调音量能否精准触发）**。

> **避坑提醒**：日常语音交互**千万不要选慢思考推理模型**（如 `deepseek-reasoner`、`o1/o3`、`qwq`），它们每次回答前先思考十几秒，会导致语音对话严重卡顿！

### 方案 A：本地不方便部署时 —— 纯云端推荐组合（强烈推荐 90% 用户）

如果您家里没有 24 小时常开的 Apple Silicon Mac，**完全不需要买额外硬件，也完全不需要部署 `mac-companion/` 目录**，直接用云端模型即可获得极佳体验：

#### 1. 对话主模型（LLM）云端推荐榜（国内直连免代理）

| 优先级 | 平台与推荐模型 | 优势特点 | 适用人群 |
| --- | --- | --- | --- |
| **首选（综合最佳）** | **DeepSeek**<br/>`deepseek-chat` / `deepseek-v4-flash` | 中文情商高、Function Calling 调音乐/电台/倒数日极准，且同一个 Key 可同时喂给「联网搜索」插件，一鱼两吃。 | 绝大多数家庭主力首选 |
| **极速首选** | **阿里云百炼（通义千问）**<br/>`qwen-plus` / `qwen-flash` | 国内节点首包极快（`qwen-flash` 仅 `~250ms`），新开通百炼平台赠送百万级免费 Token，语音问答行云流水。 | 追求秒回响应速度 |
| **口语拟人** | **火山方舟（字节豆包）**<br/>`doubao-seed-2-0-lite-260215` | 专为实时语音助手调优，回复简练口语化，不会长篇大论念经。 | 喜欢自然聊天陪伴感 |
| **0 元白嫖首选** | **智谱开放平台（BigModel）**<br/>`glm-4-flash` | 官方长期**完全免费**开放 API，支持工具调用，实测日常控音乐、问天气、陪孩子百科问答完全够用。 | **不想花一分钱 API 费用的用户** |
| **海外可选** | **Google Gemini / OpenAI / Claude**<br/>`gemini-2.5-flash` / `gpt-5-mini` / `claude-haiku-5-5` | 多语种口语练习与综合智商顶尖（需家里软路由或本机配置透明代理）。 | 有顺畅海外网络环境的用户 |

#### 2. 本机内置语音能力说明（不方便配外部服务时，直接用机器自带的即可！）

刷入小智社区底包后，**联想机器内部其实已经自带了一整套免配置的语音识别与语音合成能力**，不方便折腾外部服务器的用户直接保持默认即可正常使用：

1. **机器内部自带 3 个本地离线语音模型（100% 跑在本机 CPU，断网可用）**：
   - **本机语音识别大模型 (`SherpaXASR`)**：预装在 `/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/models/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/`（`167 MB` INT8 量化中英双语带标点识别模型，无需任何 API Key）。
   - **本机唤醒词模型 (`kws-zh`)** 与 **人声检测模型 (`SileroVAD`)**：常驻监听「小麦小麦」。
2. **机器本机自带免配置的神经网络语音合成 (`EdgeTTS`)**：
   - 由于联想机器只有 `2 GB` 内存（跑完系统和 `167 MB` 本机语音识别大模型后余量有限，塞离线 TTS 大模型容易爆内存且合成慢），底包在本机 `/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/.venv/bin/edge-tts` 中预装了免注册、免 API Key 的 **微软 `EdgeTTS` 神经网络语音合成**。
   - 打开 `http://<设备IP>:16688/admin` → **「语音合成」**，即可直接试听并一键切换 **14 种内置中文音色**（晓晓、晓伊、云希、云夏童声、东北话晓北、陕西话晓妮、粤语晓佳等）。
   - **全系统自动联动**：当您未配置外部 Mac 克隆语音 (`tts_url`) 时，我们的**番茄专注计时提醒、学习坐姿温和提醒、以及家庭留言板/定时提醒语音播报**都会**自动复用机器本机自带的 `edge-tts` 与您在后台选定的音色发声**，零额外配置！
3. **可选付费云端升级（仅当环境嘈杂觉得本机识别不够准时）**：
   - 可在小智后台 `:16688/admin` 将语音识别切为 **火山引擎豆包流式识别 (`DoubaoStreamASRV2`)**（远场抗噪与儿童口音识别率更高，按时长计费每月仅几毛到一两元）；或在 `.config.yaml` 中选配火山豆包双向流式大模型语音合成（`HuoshanDoubleStreamTTSV2`）、硅基流动 `CosyVoice2` 等。

#### 3. 「AI 学习坐姿陪伴」无 Mac 纯云端跑法

如果您没有本地 Mac 跑 `Qwen3-VL-8B`，但又想用摄像头坐姿/玩手机检测功能，现在可以直接在**联想设备本机**用纯标准库运行 [`vision_bridge.py`](../mac-companion/study-vision/vision_bridge.py) 转发到云端视觉大模型（已适配 Linux `ffmpeg` 旋转与 Bearer API Key）：

- **0 元免费视觉模型**：智谱开放平台 `glm-4v-flash`（`STUDY_VISION_LLM=https://open.bigmodel.cn/api/paas/v4`，`STUDY_VISION_MODEL=glm-4v-flash`）
- **高准度推荐视觉模型**：阿里云百炼 `qwen-vl-plus`（`STUDY_VISION_LLM=https://dashscope.aliyuncs.com/compatible-mode/v1`，`STUDY_VISION_MODEL=qwen-vl-plus`）

只需生成共享密钥 `openssl rand -hex 32 | sudo tee /var/lib/thinksmart-study/secret >/dev/null`，以环境变量传入 `STUDY_VISION_SECRET_FILE=/var/lib/thinksmart-study/secret`、`STUDY_VISION_LLM`、`STUDY_VISION_MODEL` 与 `STUDY_VISION_API_KEY` 启动 `vision_bridge.py`，并将 `/var/lib/thinksmart-study/config.json` 的 `vision_url` 指向 `http://127.0.0.1:8084/study/v1` 即可。

---

### 方案 B：有闲置/常开 Apple Silicon Mac 时 —— 本地算力端分档建议

如果您家里有常开的 **Mac mini / Mac Studio（M1 / M2 / M3 / M4）**，部署 `mac-companion/` 可以获得云端买不到的三大体验：**① 家人/专属音色 `~300ms` 超低延迟流式克隆 (`Qwen3-TTS`)**、**② 支持注入家庭成员名字/地名词库的精准语音识别 (`Qwen3-ASR` + 留言板语音自动转文字)**、**③ 孩子学习画面 100% 局域网本地推理不出户 (`Qwen3-VL-8B`)**。

请根据您 Mac 的**统一内存容量**按需开启组件（切勿超载导致 macOS Swap 卡顿）：

| Mac 统一内存 | 推荐开启的本地模型组件 | 内存占用与调优建议 |
| --- | --- | --- |
| **8 GB** | **仅开 `voice` 主进程**（`Qwen3-ASR-1.7B-8bit` + `Qwen3-TTS-1.7B-8bit`） | 常驻约 `3.8 ~ 4.5 GB`；**不要开** `tts2` 副进程和本地 `Qwen3-VL-8B` 视觉模型（学习视觉改走云端 `qwen-vl-plus` 或 `glm-4v-flash`；对话 LLM 走云端 API）。 |
| **16 GB（黄金甜点）** | **`voice` 主进程 + `tts2` 休眠副进程 + `vision-llm` (`Qwen3-VL-8B-Q4_K_M`)** | 完美运行全套 `mac-companion`！保持模板默认的副 TTS 10 分钟空闲卸载（`TTS_IDLE_SLEEP_SECONDS=600`）与视觉模型 10 分钟空闲休眠（`--sleep-idle-seconds 600 --cache-ram 0`），平时总占用仅 `~4.5 GB`，学习时峰值约 `~10.5 GB`。对话 LLM 仍建议走云端 DeepSeek/通义。 |
| **24 GB ~ 32 GB+** | **全套 `mac-companion` 常驻 + 可选本地 `Ollama` / `LM Studio` 对话大模型** | 若追求连对话 LLM 也 100% 离线本地化，可在 Mac 上用 Ollama 运行 `qwen3:14b` 或 `qwen2.5:14b-instruct`，在小智后台 `:16688/admin` →「大模型」→「自定义」填入 `http://<Mac局域网IP>:11434/v1` 即可。 |
