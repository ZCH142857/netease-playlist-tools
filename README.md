# 网易云歌单工具 · macOS

基于 PySide6 的桌面工具，支持 Apple Silicon，通过自建的 NeteaseCloudMusicApi Enhanced 管理网易云音乐歌单。

- **歌单查重**。支持歌单内和跨歌单查重，匹配时忽略大小写、全半角、空白和末尾圆括号后缀。结果显示红心人数、评论数和播放状态，也可搜索站内同名版本。
- **歌单归集与分类**。查找自建歌单中尚未点红心的歌曲，按标题文字为未归入自建歌单的红心歌曲初分语言。可加载上次扫描结果，或将某个分类批量导入新建的公开歌单。
- **歌单管理**。搜索自建歌单、筛选纯音乐，批量点红心、取消红心、添加到其他自建歌单或从来源歌单移除。

## 日常启动

已安装 Enhanced API 和桌面工具依赖后，按下面的顺序启动。首次配置请看[首次安装](#首次安装)。

### 1. 启动 Enhanced API

如果后端使用 Homebrew 安装的 `node@24`，先在启动后端的终端执行。

```bash
export PATH="$(brew --prefix node@24)/bin:$PATH"
```

在安装 Enhanced API 的机器上运行，保持终端打开。

```bash
cd "$HOME/.local/ncm-api/node_modules/@neteasecloudmusicapienhanced/api" &&
  HOST=127.0.0.1 PORT=3100 ENABLE_GENERAL_UNBLOCK=false ENABLE_PROXY=false node app.js
```

### 2. 建立 SSH 转发（后端在服务器时）

在 Mac 的另一终端执行，将 `user@server` 换成实际的登录目标，并保持转发运行。

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:3100:127.0.0.1:3100 user@server
```

### 3. 启动桌面工具

在 Mac 上打开包含 `run.py` 的项目目录，使用已创建的 `.venv` 启动。

```bash
.venv/bin/python run.py --api-port 3100
```

`--api-port` 要与本机后端或 SSH 转发的本地端口一致，省略时默认为 `3000`。

如果 Enhanced 需要通过 HTTP 代理访问网易云，使用下面这条启动命令。代理地址须能从后端所在机器访问。

```bash
NCM_PROXY=http://127.0.0.1:7897 .venv/bin/python run.py --api-port 3100
```

## 首次安装

### Enhanced API

后端可运行在本机或可信的 Linux 服务器。准备 Node.js 22 或更新版本和 npm。Mac 可用 Homebrew 安装 Node.js 24。

```bash
brew install node@24
export PATH="$(brew --prefix node@24)/bin:$PATH"
```

在运行后端的机器上安装 Enhanced API。

```bash
npm install --prefix "$HOME/.local/ncm-api" --omit=dev --ignore-scripts @neteasecloudmusicapienhanced/api@4.41.1
```

### 桌面工具

在 Mac 上准备 Python 3.9 或更新版本，推荐 3.11 或更新版本。下载项目后，在包含 `run.py` 的目录中创建虚拟环境并安装依赖。

```bash
git clone https://github.com/ZCH142857/netease-playlist-tools.git
cd netease-playlist-tools
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装完毕后，按[日常启动](#日常启动)运行。

## 登录和使用

在窗口点「扫码登录」，用网易云音乐 App 确认。也可点「网页 Cookie」，粘贴从 `music.163.com` 已登录请求中复制的 Cookie 请求头值，须含 `MUSIC_U`。登录成功后，凭据保存在 macOS 钥匙串。下次使用同一 API 端口时会尝试恢复会话，过期后可重新扫码。

- **歌单查重**。选中歌单后点「开始查重」。需要合并比较时，勾选「跨歌单查重」。从「我喜欢的音乐」移除歌曲会取消红心。
- **歌单归集与分类**。点「扫描当前歌单」或「加载上次结果」。在「待补红心」可按组或逐首选择，在「待分类」可按语言新建公开歌单。语言是根据标题文字推断的，请在导入前复核。扫描结果按账号保存在 `~/Library/Application Support/网易云歌单查重/classification/<UID>.json`。
- **歌单管理**。进入页面时优先加载上次结果，无本地结果时自动读取歌单。可手动点击「加载上次结果」或「刷新结果」。勾选「仅纯音乐」后，工具会核对当前歌单和搜索范围内的歌词，只显示歌词区标注「纯音乐，请欣赏」的歌曲。已核对的结果会保存到本地。切换筛选会保留勾选，批量操作只处理当前筛选范围内的勾选项。批量取消红心会保留原自建歌单中的歌曲。

写入操作需要确认，工具会复查处理结果。同批红心操作间隔至少 4 秒，遇到限流会等待 60 秒再重试，可在界面中停止剩余操作。

## 测试

安装依赖后运行。测试使用模拟 API 和钥匙串。

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```
