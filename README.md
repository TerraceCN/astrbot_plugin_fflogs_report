# astrbot_plugin_fflogs_report

AstrBot 插件：查询 FF14 角色在 FF Logs 上的全副本排名，并生成适合移动端阅读的图片报告。

## 功能

- 指令 `/fflogs <服务器名> <角色名>`，例如 `/fflogs 拂晓之间 丝瓜卡夫卡`
- 使用 `html-simple` 模板生成报告，再通过 AstrBot 的 `html_render` 渲染成图片：
  - `full_page=True` 截取完整长图
  - 默认按 430 CSS px 视口、1.8x 设备像素比渲染（约 774px 宽），手机阅读清晰
  - 字体首选 PingFang SC，回退 Hiragino Sans GB / Noto Sans CJK / 苹方 / 微软雅黑（渲染服务器需安装对应字体）
- 将地图/副本/首领的排名、百分位、DPS/HPS、时长等信息汇总为一张长图
- 报告按「进行中的区域」和「已封存的区域」分区展示，分区标题条上同一行统计该区的
  最高 / 平均百分位，以及高难度副本（零式、绝境、极神、幻巧、异闻）的最高 / 平均百分位
- 高难度副本的 Zone ID 徽章会显示为橙色样式作为标识
- 查询失败（角色不存在、数据隐藏、未配置 API Key 等）会返回明确的文字提示

## 缓存

| 缓存 | 位置 | 说明 |
| --- | --- | --- |
| FF Logs 查询数据 | `data/plugin_data/astrbot_plugin_fflogs_report/logs_cache/` | 按角色/服务器/指标缓存，已封存副本直接复用 |
| 报告图片 | `data/plugin_data/astrbot_plugin_fflogs_report/image_cache/` | 默认 1 小时内重复查询直接返回缓存图片 |

图片缓存支持定时清理与大小上限：默认每 30 分钟清理一次过期图片，总大小超过 128MB 时自动删除最旧的图片。相关参数均可在插件配置中调整。

## 配置

在 AstrBot 管理面板 -> 插件 -> FF14 Logs 报告查询 中配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | 空 | FF Logs v1 API Key，留空时回退读取环境变量 `FFLOGS_V1_APIKEY` |
| `region` | `CN` | 服务器区域：`CN` / `US` / `EU` / `KR` / `TW` |
| `metric` | `dps` | 统计指标：`dps` / `hps` / `bossdps` / `tankhps` / `playerspeed` |
| `timeframe` | `current` | `current` 当前榜单，`history` 历史榜单 |
| `base_url` | `https://cn.fflogs.com/v1` | FF Logs API 地址 |
| `render_width` | `430` | 渲染视口宽度（CSS px），移动端建议 390-480 |
| `render_scale` | `ultra` | 渲染清晰度：`normal`(1.0x) / `high`(1.3x) / `ultra`(1.8x) |
| `image_format` | `jpeg` | 图片格式：`jpeg` / `png` |
| `image_quality` | `90` | JPEG 质量（1-100） |
| `image_cache_ttl_minutes` | `60` | 图片缓存有效期（分钟） |
| `image_cache_max_mb` | `128` | 图片缓存大小上限（MB） |
| `image_cache_cleanup_interval_minutes` | `30` | 定时清理间隔（分钟） |

## 依赖

插件依赖列于 `requirements.txt`，AstrBot 会在加载插件时自动安装缺失依赖：

```
httpx>=0.28.1
jinja2>=3.1.0
python-dotenv>=1.0.0
tenacity>=9.1.2
```

## 说明

- 报告中的职业图标来自 [xivapi/classjob-icons](https://github.com/xivapi/classjob-icons)，已内嵌为 base64，渲染无需联网。
- FFXIV 素材版权归 SQUARE ENIX 所有。
