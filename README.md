# FaceFusion 口播换脸（Windows NVIDIA 独立便携版 & 云表格协同工作台）

基于 **FaceFusion 3.9.0** 深度定制的口播视频换脸独立便携工作台，内置一键场景预设、严格的本地隐私隔离（`userdata/`）、首次启动官方源自动模型下载，以及多人协同的 **Google Sheets + Google Drive 云表格自动接单中心**。

---

## 快速下载与使用（普通用户直接看这里）

> **无需安装 Python、CUDA、FFmpeg 或 Git**，下载即用：

1. 前往本仓库右侧 **[Releases](../../releases/latest)** 页面，下载 **`FaceFusion-Portable-Windows-NVIDIA.zip`**（仅约 **14 MB** 极简便携初始包）。
2. 解压到任意英文或中文路径文件夹（建议预留 ≥ 15 GB 可用磁盘空间）。
3. 双击 **`后台启动FaceFusion.cmd`**（或 `启动FaceFusion.cmd`）：
   - **首次启动全自动初始化**：控制台窗口会自动从官方国际源（GitHub Official Releases / PyPI / HuggingFace）依次安装 FFmpeg、Python 与 NVIDIA CUDA 12 / cuDNN 9 运行库及口播所需 AI 模型，全程支持字节级断点续传与 SHA-256 / CRC32 校验（下载一次永久缓存，后续启动秒开）。
   - **自动打开界面**：初始化完成后浏览器自动打开 `http://127.0.0.1:7860`。
4. **后续版本无感热更新（每次仅 ~0.4 MB）**：
   - 每次双击启动软件时，会自动检测 GitHub 最新 Release 并秒级同步 `0.4 MB` 轻量代码热更新包（也可点击网页右上角 **🔄 检查在线热更新** 或双击 **`一键检查更新.cmd`**），个人配置、Google 授权与已下载环境/模型 100% 保留。
5. 使用完毕双击 **`停止FaceFusion.cmd`** 即可关闭后台服务。

### 硬件与系统要求
- **操作系统**：Windows 10 / Windows 11（64 位）
- **显卡要求**：NVIDIA 独立显卡（RTX 20 / 30 / 40 / 50 系列、GTX 16 / 10 系列等，建议显存 ≥ 6 GB，推荐 ≥ 8 GB），已安装 **NVIDIA 官方显卡驱动**（建议驱动版本 ≥ 531）
- **内存建议**：≥ 16 GB

---

## 核心功能亮点

- **双模式极简 Web 界面**
  - **🎬 手动精细换脸**：三步极简流程（① 上传源人脸图与目标视频 → ② 一键选择场景预设 → ③ 开始处理），成品自动保存在 `userdata\manual_outputs`，专业参数折叠在底部“高级参数微调”。
  - **📋 云表格任务中心**：对接 Google Sheets 多人协同领单，支持动态表头识别（任意挪列、插行、排序均不错行）、并发抢单冲突校验、FFprobe 智能检测与按需标准化转码（`H.264 + yuv420p + AAC`）、成品自动上传 Google Drive 并回填共享链接、20 分钟间隔饱和式自动补单（上限 `1 制作中 + 1 排队中`）及断网保底补传。
- **三大口播实战一键预设**
  - `✨ 没问题选这个`：默认推荐参数（`inswapper_128_fp16` + 上下半脸表情恢复 `80` + `gfpgan_1.4` 混合 `65`）
  - `👄 解决嘴巴张不开`：针对口播张嘴受限优化（仅下半脸表情恢复 `100` + `gfpgan_1.4` 混合 `30`）
  - `👀 解决眼睛斜`：针对眼神偏移优化（关闭上半脸恢复，保留下半脸恢复 `80` + `gfpgan_1.4` 混合 `65`）
- **严格隐私与数据隔离（`userdata/`）**
  - 程序绝不读取本机电脑主机名，工位名称由用户自行填写（如 `Alex`、`Worker-01`）。
  - 所有 Google OAuth 凭证、工位配置、自定义预设、自动下载的 `.onnx` 模型、临时帧缓存、运行日志与成品视频全部隔离在根目录 `userdata\` 文件夹内，不写注册表、不污染系统盘。

---

## 仓库目录结构

```text
.
├── app/facefusion/                 # 应用核心源码与官方模型哈希清单
│   ├── .assets/models/*.hash       # 19 个官方模型 CRC32 校验清单（防传输损坏）
│   ├── facefusion/
│   │   ├── model_bootstrap.py      # 首次启动官方国际源模型自动下载与 CRC32 + SHA-256 强校验器
│   │   ├── download.py             # 纯 Python httpx 流式下载引擎（断点续传 + 双源自动切换）
│   │   ├── presets_manager.py      # 口播场景预设管理器
│   │   ├── google_sheets_client.py # Google OAuth 2.0、Sheets v4 与 Drive v3 协同客户端
│   │   ├── media_normalizer.py     # FFprobe 视频规格检测与按需标准化转码
│   │   ├── sheet_worker.py         # 后台任务队列、看门狗保护与自动补单调度器
│   │   └── uis/components/         # 双页签精简版 Gradio Web 界面组件
│   └── tests/                      # 自动化单元测试套件
├── auto_updater.py                 # ~0.4 MB 在线代码热更新模块（启动自动同步 + SHA-256 校验）
├── env_bootstrap.py                # 首次运行 FFmpeg 与 Python/CUDA 官方源断点续传初始化器
├── create-portable-zip.py          # 双轨极简打包脚本（~14 MB 初始包 + ~0.4 MB 热更新包）
├── publish-update.ps1              # 维护者一键递增版本号、打包并发布 GitHub Release 脚本
├── run-facefusion-portable.ps1     # 便携环境隔离与启动入口脚本
├── start-facefusion-background.ps1 # 后台守护启动与浏览器自动唤起脚本
├── stop-facefusion.ps1             # 进程安全停止脚本
├── 后台启动FaceFusion.cmd           # 一键后台启动（含自动热更新与环境检测）
├── 启动FaceFusion.cmd               # 一键前台控制台启动
├── 停止FaceFusion.cmd               # 一键停止服务
├── 一键检查更新.cmd                 # 用户手动检查并应用最新热更新
├── 一键发布热更新.cmd               # 维护者 5 秒一键打包并发布新版本到 GitHub
├── 打包分享版.cmd                   # 生成 ~14 MB 极简初始包 + ~0.4 MB 热更新包
├── 打包轻量更新包.cmd               # 秒级生成 ~0.4 MB 轻量代码更新包
└── 使用说明.txt                     # 详细中文图文式操作说明
```

---

## 安全与供应链策略

- **纯官方国际源直连**：环境组件、模型下载与云端接口仅连接 `https://pypi.org`、`https://files.pythonhosted.org`、`https://huggingface.co`、`https://github.com` 与 `https://*.googleapis.com`，不使用任何第三方镜像源。
- **双重哈希防篡改校验**：每个下载的 `.onnx` 核心模型在启用前均强制比对仓库内置的 CRC32 `.hash` 传输校验值与 `model_bootstrap.py` 固化的官方 **SHA-256** 密码学摘要，任何损坏或碰撞篡改文件均自动拦截清理。
- **本地回环隔离与防 SSRF**：Web 工作台默认仅绑定 `127.0.0.1` 并启用上传缓存路径沙箱（可通过 `.env` 配置 `FACEFUSION_UI_PASSWORD` 开启登录密码）；云表格外部素材下载内置逐跳重定向 SSRF 防护与 FFmpeg 本地协议白名单。
- **敏感凭证硬隔离**：本地 OAuth 令牌自动收紧操作系统当前用户专属读写权限；`.gitignore` 与 `create-portable-zip.py` 强制排除 `userdata/`、`.env*`、私钥及一切 `*client_secret*.json` / `*google-token*.json` 文件，一旦检测到目录外残留凭证文件立即中止打包。

---

## 维护者打包与测试命令

在包含 `runtime/` 与 `tools/` 的完整工作目录下：

```powershell
# 1. 运行自动化单元测试
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
.\runtime\python\python.exe -m unittest discover -s app\facefusion\tests -p "test_presets.py"
.\runtime\python\python.exe -m unittest discover -s app\facefusion\tests -p "test_sheet_worker.py"

# 2. 仅生成轻量代码更新包（约 1~2 秒，输出至 userdata\dist\FaceFusion-Update-CodeOnly.zip）
.\runtime\python\python.exe .\create-portable-zip.py --update-only

# 3. 生成完整便携包 + 轻量代码更新包（输出至 userdata\dist\）
.\runtime\python\python.exe .\create-portable-zip.py
```

---

## 许可证与合规声明

- 上游 **FaceFusion 3.9.0** 遵循 OpenRAIL-AS 许可证（详见 `app/facefusion/LICENSE.md`）。
- 首次启动自动下载的各 AI 模型遵循其各自上游许可证（详见 `THIRD-PARTY-LICENSES.txt` 及 <https://docs.facefusion.io/introduction/licenses>）。
- 请仅处理已获得合法肖像与版权授权的音视频素材，并严格遵守所在地法律法规。
