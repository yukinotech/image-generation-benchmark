# 文生图 Benchmark

对比六个模型在相同目标画幅、提示词与 Seed 下的输出。静态页面包含 15 组对比、90 张生成图和单图复现参数。

- `docs/` 是 GitHub Pages 的发布目录。页面可由普通静态 HTTP 服务直接提供。
- `benchmark/` 包含跑图、提示词、尺寸规则、查看器和静态导出代码。运行本地 benchmark 需要另行准备 ComfyUI、模型和本地运行环境。
- 图片重新编码为 PNG，公开的 JSON 保留提示词、尺寸、Seed、模型文件名与复现参数。

本地运行与导出说明见 [benchmark/README.md](benchmark/README.md)。
