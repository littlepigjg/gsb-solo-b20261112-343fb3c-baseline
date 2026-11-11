# 光屿 · 图像工作室

一个可以直接运行的图形与多媒体领域项目。前端使用原生 **HTML + CSS + JavaScript**，后端使用 **Python + Flask + Pillow + NumPy**，业务数据存储为 **JSON**。不需要数据库、不需要 Node.js 构建、不依赖在线图像处理接口。

## 快速开始

Windows 双击 **`start.bat`**，然后打开：

**http://127.0.0.1:8765/**

当前电脑已安装 Python 3.13，并已为本项目创建 `.venv` 和安装所需依赖。首次换到其他 Windows 电脑运行时，启动脚本会创建虚拟环境并安装依赖；没有 Python 时尝试通过 `winget` 安装 Python 3.13。若电脑没有 winget，请先从 [Python 官网](https://www.python.org/downloads/) 安装 Python 3.13。

手动启动：

```powershell
cd "项目所在路径\gsb_1"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

端口冲突时可使用：

```powershell
.\.venv\Scripts\python.exe run.py --port 8766
```

终端启动的服务用 `Ctrl+C` 停止。本次交付保留了后台服务，`logs/server.pid` 记录其进程；需要停止时运行 `stop.bat`。停止服务会中断正在运行的批量任务，重启后任务将标记为“已中断”，已完成结果保留。

## 十项核心功能

| 序号 | 功能 | 实际能力 |
| --- | --- | --- |
| 01 | 裁剪构图 | 自由坐标裁剪、中心裁剪、比例裁剪、方形、圆形、去纯色边、去透明边 |
| 02 | 尺寸调整 | 指定宽高、等比缩放、适应边界、铺满画幅、背景留白、最长边限制、调整画布 |
| 03 | 旋转翻转 | 任意角度旋转、直角旋转、镜像、对角转置、斜切、平移、透视、四向镜像 |
| 04 | 色彩调整 | 亮度、对比度、饱和度、线性曝光、Gamma、色温、色相、色阶、阴影高光、通道处理等 |
| 05 | 艺术滤镜 | 棕褐、胶片、暗角、颗粒、像素化、海报、曝光反转、素描、卡通、浮雕、网点等 |
| 06 | 降噪锐化 | 高斯/方框/中值滤波、保边双边滤波、反差保留锐化、运动模糊、局部清晰度等 |
| 07 | 边缘检测 | Sobel、Prewitt、Scharr、Laplacian、Roberts、轮廓、二值边缘、彩色轮廓叠加 |
| 08 | 水印标注 | 中文文字水印、平铺水印、画框、圆角、标题条、构图网格 |
| 09 | 格式转换 | PNG、JPEG、WebP、BMP、TIFF、GIF 实际编码下载；调色板压缩、透明度、纯色背景抠除 |
| 10 | 批量处理 | 多图、多步骤流水线，保存预设、任务进度、取消、失败报告、结果 ZIP 打包 |

前九项共 **86 种可选图像处理操作**。第十项负责将这些操作组合并应用到多张图片。格式转换除六种编码器外，还提供六种像素格式准备操作。

## 使用方式

1. 首次打开会自动生成三张原创程序绘制示例，可直接试用。
2. 点击“导入图片”，或将图片拖入窗口。支持多文件导入。
3. 左侧选择功能，右侧选择处理方式、调整参数，点击“应用到当前图片”。
4. 每次处理生成独立副本；“返回上一步”切换到父版本，“原图对比”显示最初导入图与当前结果。
5. 点击“加入流程”收集步骤，在“处理流程”中调整顺序、保存预设或运行整个流程。
6. 在素材库勾选图片，进入“批量任务”，选择流程并开始处理。结果可打包下载。
7. 点击“导出”，选择格式和质量，生成下载文件。浏览器若未自动下载，可点击窗口中的下载链接。

“图像信息”提供 RGB 直方图、色谱、亮度、信息熵与处理历史。“系统与数据”提供运行状态、数据检查和 JSON 元数据备份。

## 代码规模

代码统计只计算 `backend/**/*.py`，**不包含第三方依赖、前端、测试、生成脚本或启动入口**。

交付时：35 个后端 Python 文件，5,574 行物理源码；排除空行、注释及文档字符串后，仍有 **5,074 行代码**。其中 `backend/catalog/` 为运行时直接使用的参数协议与功能元数据，参与接口校验、前端表单生成与操作发现，并非无效填充。

可复核统计：

```powershell
.\.venv\Scripts\python.exe scripts/count_backend.py
```

逐文件统计保存在 `docs/backend-line-count.json`。维护功能目录时可运行 `scripts/generate_catalog.py`，该脚本不是运行服务的前置步骤。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q --junitxml=docs/test-results.xml
```

测试覆盖 86 种操作默认参数执行、像素正确性、透明通道、非法参数、实际格式解码验证、上传与下载、处理历史、原图不可变、批量任务完成/失败/取消、JSON 并发写入、损坏恢复、重启保留和后端行数约束。

## 目录结构

```text
gsb_1/
├── run.py                    # Waitress 服务入口
├── start.bat / start.ps1      # Windows 启动与环境安装
├── stop.bat / stop.ps1        # 停止本次交付的后台服务
├── requirements.txt          # 应用依赖
├── requirements-dev.txt      # 测试依赖
├── frontend/
│   ├── index.html            # 中文 HTML 页面
│   ├── style.css             # 自适应桌面和窄屏样式
│   └── app.js                # 前端交互和 API 调用
├── backend/
│   ├── app.py / routes.py    # 应用组装与 REST API
│   ├── storage.py            # JSON 原子替换、线程锁、备份恢复
│   ├── assets.py             # 上传、缩略图、素材、历史
│   ├── processing.py         # 非破坏式处理流水线
│   ├── jobs.py               # 后台任务与取消
│   ├── exports.py            # 格式编码与 ZIP 导出
│   ├── bundles.py            # 可迁移素材包：祖先闭包导出与原子导入
│   ├── analysis.py           # 直方图和图像诊断
│   ├── diagnostics.py        # 系统与数据完整性检查
│   ├── presets.py            # 可复用处理预设
│   ├── samples.py            # 原创演示图片生成
│   ├── registry.py           # 功能发现与执行注册
│   ├── validation.py         # 严格参数校验
│   ├── imaging.py            # 图像读写与算法公共函数
│   ├── settings.py           # 资源限制与路径
│   ├── operations/           # 7 个算法模块
│   └── catalog/              # 9 组处理操作参数协议
├── data/
│   ├── json/                 # assets/jobs/presets/events/exports.json
│   ├── media/                # 原图规范化副本、结果 PNG、缩略图
│   └── exports/              # 实际编码文件和 ZIP
├── tests/                    # 自动化测试
├── scripts/                  # 目录生成、统计与打包
├── docs/                     # 接口、架构、测试及行数报告
└── logs/                     # 当前后台服务日志
```

## 数据与运行边界

- 业务实体和任务状态全部使用 JSON；图片文件保存为二进制文件，JSON 保存其索引、元数据和处理记录。
- 导入图统一为 RGBA PNG 编辑副本，自动应用 EXIF 方向并移除原 EXIF 信息，不覆盖用户选择的原文件。
- 单图最多 24 MB、1,600 万像素、单边 8,192 像素；一批最多 24 张、12 步，最多 4 个活动任务。
- 默认仅监听本机 `127.0.0.1`。这是单用户本地工作室，不提供账号体系或多用户隔离。
- 使用单进程 Waitress 和共享线程锁。多个独立服务进程不要同时使用同一个 `data` 目录。
- JSON 写入采用临时文件加原子替换，并保留上一个有效版本的 `.bak`。恢复备份可能丢失最后一次变更；文件与 JSON 跨资源提交仍可能在断电时留下孤立文件，“系统与数据”会报告。
- 动态 GIF、多页 TIFF 仅导入第一帧；导出 GIF 为单帧。纯色背景抠除基于颜色距离，不是 AI 人像分割。
- 高计算量操作在本机 CPU 执行；后台任务取消在当前算法结束后生效。页面预览适应画布，实际结果保留处理后的真实尺寸。
- 导出 DPI 是文件元数据，不会额外增加像素。JPEG/WEBP 质量参数仅对对应编码器生效。
- JSON 元数据下载不包含图片；完整备份请复制整个 `data` 目录。
- 素材迁移包（`POST /api/bundles/export` 与 `/api/bundles/import`）只交换一个 ZIP：自动补齐祖先、父先子后打包，导入时全量校验通过后才写入，始终生成新 ID、不覆盖现有资产；上传 24 MiB、展开 64 MiB、资产 24 项，详见 [docs/MIGRATION.md](docs/MIGRATION.md)。

接口详见 [docs/API.md](docs/API.md)，架构详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。图像与上传实现参考 [Pillow Image 官方文档](https://pillow.readthedocs.io/en/stable/reference/Image.html) 和 [Flask 上传官方文档](https://flask.palletsprojects.com/en/stable/patterns/fileuploads/)。

## 相对路径约定

项目源码、数据配置、静态资源定位、启动脚本和打包脚本使用相对路径，不绑定盘符、用户名或固定系统安装目录。数据目录保存为相对当前工作目录的路径；默认数据位于本项目 `data/`。

启动脚本仅通过系统提供的脚本位置切换到项目目录，之后使用 `.venv/Scripts/python.exe`、`logs/` 等相对路径。Python 解释器通过 `py`、`python` 命令发现。Python 模块使用 `os.path.relpath` 保持资源路径为相对路径，因此也支持从项目上级目录加载。HTTP 地址和 `/api/...` 路由是网络地址，不是磁盘绝对路径。

`.venv` 中的 `pyvenv.cfg`、激活脚本及第三方可执行入口由 Python 自动生成，会包含本机解释器位置。这些环境文件不属于项目源码，也不进入源码 ZIP；跨机器使用请解压源码包后运行 `start.bat` 重建环境，不要搬运 `.venv`。

检查结果见 `docs/path-audit.json`；两个项目均增加了相对数据目录及从上级目录加载页面、静态资源的测试。
