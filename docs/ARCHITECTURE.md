# 项目设计

## 数据流

```text
HTML / CSS / JavaScript
        │ multipart 上传 / JSON 命令 / 图片响应
        ▼
Flask 路由与错误边界
        │
        ├── AssetService ── 图片校验 / EXIF 归一化 / PNG 与缩略图
        ├── Registry ────── 参数协议 / 严格校验 / 算法注册
        ├── Processing ──── 有序执行 / 资源限制 / 新副本和溯源
        ├── JobService ──── 单工作线程 / 进度 / 取消 / 重启恢复
        ├── ExportService ─ 实际格式编码 / ZIP 打包
        ├── Analysis ────── 采样直方图 / 色谱 / 误差分析
        ├── Presets ─────── 内置与自定义 JSON 流程
        ├── Palettes ────── GIMP Palette 严格解析 / 规范化 GPL / 色卡 PNG 入库
        └── Diagnostics ── 依赖与文件完整性检查
                     │
            JsonStore + 本地图片目录
```

## 技术选择

- **HTML/CSS/JavaScript**：无框架前端，不需要构建；页面通过后端同源加载，避免配置 CORS。
- **Flask**：API、静态文件、统一异常响应、上传体积限制。
- **Waitress**：Windows 可直接运行的 WSGI 服务，默认六个请求线程。
- **Pillow**：编码解码、裁剪、重采样、基础滤波、字体绘制和透明合成。
- **NumPy**：曝光传递函数、颜色矩阵、梯度卷积、双边滤波等像素运算。
- **JSON**：所有素材元数据、预设、批次、操作记录与导出索引均使用可读 JSON。

## 图像处理与编辑副本

导入时验证实际解码格式和像素数量，读取第一帧，应用 EXIF 方向，转为 RGBA。编辑过程中始终以规范化 PNG 为输入，每次处理保存新的 PNG，记录父版本和原始根版本。

`operations/` 中的函数接收 `(image, params, context)`，返回 `PIL.Image.Image`。参数从 `catalog/` 校验后传入；同一份目录定义也提供给前端生成参数表单。大多数色彩与边缘算法单独保留原始 Alpha 通道，几何操作则对整幅 RGBA 图变换。

`catalog/` 文件是运行时直接使用的 Python 源码，包含每个操作的描述、算法、参数类型、默认值、范围、步进、选项、帮助文本和示例请求。`scripts/generate_catalog.py` 是维护这些定义的编译工具。

## 存储模型

每个集合保存为：

```json
{
  "schema_version": 1,
  "records": {
    "32位uuid": {"id": "32位uuid", "created_at": "UTC时间", "updated_at": "UTC时间"}
  }
}
```

| 集合 | 主要字段 |
| --- | --- |
| assets | name、kind、width、height、sha256、parent_id、root_id、steps、metadata |
| jobs | title、asset_ids、steps、status、progress、results、errors |
| presets | name、description、steps、readonly、builtin_key |
| events | action、message、details |
| exports | filename、download_name、asset_ids、options、kind、bytes |

锁覆盖 JSON 读改写全过程，写入采用新临时文件、flush/fsync、备份旧文件、`os.replace`。读到损坏 JSON 时尝试恢复上一个有效备份；无有效备份时明确失败，不静默重置数据。

图片文件与 JSON 不是一个跨文件事务。服务写入失败会清理新建图片，但突然断电仍可能遗留孤立文件，诊断接口会列出。完整备份需要同时保存 `data/json`、`data/media`、`data/exports`。

## 任务与资源控制

同步处理与后台任务共享计算锁，避免多个大图算法同时占用内存。批量工作线程串行处理图片，步骤结束后写入进度；用户取消由线程事件传递，在下一个步骤边界或保存前生效。

默认限制 1,600 万像素、单边 8,192、单文件 24 MB、每批 24 图和每流程 12 步。几何算法在分配新画布前检查目标尺寸。图像诊断缩小到最多 768×768 再计算统计量。

## 验证方式

1. 逐项执行全部 86 种操作，检查输出图像、模式、尺寸与透明通道。
2. 用明确像素关系检查裁剪、旋转、反相、Gamma 和颜色抠除。
3. HTTP 测试验证真实编码文件能够重新解码，检查 PNG/JPEG/WebP/BMP/TIFF/GIF。
4. 测试多步流程、原图不变、历史、ZIP、预设、批次完成/失败/取消。
5. 测试 JSON 并发写入、损坏恢复及重启后数据和任务状态。
6. 浏览器实际执行编辑、原图对比、JPEG 导出与批量流程。
