# 网易云歌单工具 · macOS

基于 PySide6 的桌面工具，支持 Apple Silicon，通过自建的 NeteaseCloudMusicApi Enhanced 管理网易云音乐歌单。

- **歌单查重**。支持歌单内和跨歌单查重，匹配时忽略大小写、全半角、空白和末尾圆括号后缀。结果显示红心人数、评论数和播放状态，也可搜索站内同名版本。
- **歌单归集与分类**。查找自建歌单中尚未点红心的歌曲，按标题文字为未归入自建歌单的红心歌曲初分语言。可加载上次扫描结果，或将某个分类批量导入新建的公开歌单。
- **歌单管理**。按歌名、歌手、专辑或歌曲 ID 搜索自建歌单，批量点红心、添加到其他自建歌单或从来源歌单移除。

## 1. 启动 Enhanced API

后端可运行在本机或可信的 Linux 服务器，需要 Node.js 22 或更新版本和 npm。Mac 可用 Homebrew 安装 Node.js 24。

```bash
brew install node@24
export PATH="$(brew --prefix node@24)/bin:$PATH"
```

在运行后端的机器上安装并启动 Enhanced API。

```bash
npm install --prefix "$HOME/.local/ncm-api" --omit=dev --ignore-scripts @neteasecloudmusicapienhanced/api@4.41.1
cd "$HOME/.local/ncm-api/node_modules/@neteasecloudmusicapienhanced/api"
HOST=127.0.0.1 PORT=3100 ENABLE_GENERAL_UNBLOCK=false ENABLE_PROXY=false node app.js
```

如果后端在服务器上，请在 Mac 的另一终端建立 SSH 转发，将 `user@server` 换成实际的登录目标。

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:3100:127.0.0.1:3100 user@server
```

## 2. 启动桌面工具

需要 Python 3.9 或更新版本，推荐使用 3.11 或更新版本。在 Mac 上运行以下命令。

```bash
git clone https://github.com/ZCH142857/netease-playlist-tools.git
cd netease-playlist-tools
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py --api-port 3100
```

`--api-port` 要与本机后端或 SSH 转发的本地端口一致，省略时默认为 `3000`。如需让 Enhanced 通过 HTTP 代理访问网易云，可在启动桌面工具时设置 `NCM_PROXY`，例如 `NCM_PROXY=http://127.0.0.1:7897 python run.py --api-port 3100`。代理地址须能从后端所在机器访问。

## 3. 登录和使用

在窗口点「扫码登录」，用网易云音乐 App 确认。也可点「网页 Cookie」，粘贴从 `music.163.com` 已登录请求中复制的 Cookie 请求头值，须含 `MUSIC_U`。登录成功后，凭据保存在 macOS 钥匙串。下次使用同一 API 端口时会尝试恢复会话，过期后可重新扫码。

- **歌单查重**。选中歌单后点「开始查重」。需要合并比较时，勾选「跨歌单查重」。从「我喜欢的音乐」移除歌曲会取消红心。
- **歌单归集与分类**。点「扫描当前歌单」或「加载上次结果」。在「待补红心」可按组或逐首选择，在「待分类」可按语言新建公开歌单。语言是根据标题文字推断的，请在导入前复核。扫描结果按账号保存在 `~/Library/Application Support/网易云歌单查重/classification/<UID>.json`。
- **歌单管理**。支持公开和私有的自建音乐歌单。切换筛选会保留勾选，批量操作仅处理当前筛选范围内的勾选项。

写入操作需要确认，工具会复查处理结果。同批红心操作间隔至少 4 秒，遇到限流会等待 60 秒再重试，可在界面中停止剩余操作。

## 测试

安装依赖后运行。测试使用模拟 API 和钥匙串。

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m unittest discover -s tests -v
```
