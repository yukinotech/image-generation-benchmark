# ComfyUI 文生图 Benchmark

使用项目里的便携版 Python 和当前运行的 ComfyUI。将每条提示词分别保存为 UTF-8 `.txt`，放到 [prompts](prompts/) 目录。支持中文、多行文本和子目录。

在项目根目录打开 PowerShell，启动本项目的 ComfyUI 后执行以下命令。

```powershell
# 检查 ComfyUI、六组权重和工作流节点
.\benchmark\run.cmd --check

# 查看将要生成多少张
.\benchmark\run.cmd --dry-run

# 生图、自动翻译，同时提供对比看板
.\benchmark\run.cmd --with-view

# 只查看已有结果，可在 ComfyUI 关闭时使用
.\benchmark\run.cmd --view
```

默认顺序为 MOSS → Krea 2 → MiniMax H3 Image → Qwen-Image 2.1 → Z-Image → FLUX.2 Klein 9B。每个模型先跑完所有提示词、尺寸和 seed。MiniMax 按提示词分组提交，其余模型逐图提交。同组使用相同原文、目标画幅和 seed，像素尺寸按模型设置。流程使用原生尺寸直接生成 PNG，关闭提示词扩写、风格 LoRA 和图像后处理。

每个批次会先保存 PNG、HTML 和 CSV，全部图片校验通过后再自动用本地 Qwen 补齐中文译文。普通 `run.cmd` 同样会自动翻译。只需要生图和报告时可加 `--no-translate`。

## 模型和采样设置

以下设置取自 Comfy-Org 的文生图工作流。模型路径已按项目的 `extra_model_paths.yaml` 和运行中 ComfyUI 返回的文件名适配。

| 模型 | 步数 | CFG | 采样器 / 调度器 | 官方模板 |
| --- | ---: | ---: | --- | --- |
| MOSS-Image SFT INT8 | 25 | 2.5 | er_sde / simple | [MOSS-Image T2I](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_moss_image_t2i.json) |
| Krea 2 Turbo INT8 | 8 | 1 | euler / simple | [Krea 2 Turbo INT8 T2I](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_krea2_turbo_t2i_int8.json) |
| MiniMax H3 Image Pruned INT8 | 6 | 1 | euler / simple | [MiniMax H3 Image T2I](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_minimax_h3_image_t2i.json) |
| Qwen-Image 2.1 INT8 | 25 | 1 | euler / simple | [Qwen-Image 2.1 T2I](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_qwen_image_2_1_t2i.json) |
| Z-Image Turbo BF16 | 8 | 1 | res_multistep / simple | [Z-Image Turbo T2I](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_z_image_turbo.json) |
| FLUX.2 Klein 9B FP8 蒸馏版 | 4 | 1 | euler / Flux2Scheduler | [FLUX.2 Klein T2I](https://docs.comfy.org/tutorials/flux/flux-2-klein#flux2-klein-9b-workflows) |

- MOSS 保留官方模板的固定负向提示词，文本编码器沿用本地 `moodyMossMinimal_v10Standard25Steps.json` 的 `qwen3.5_9b_int8_convrot.safetensors`。其余模型沿用各自模板的空负向提示词或零条件。
- Krea 2 与 MiniMax 的权重和编码器路径参照本地 Krea 2 风格扩展流与 MiniMax H3 Image 生图流程。Qwen 沿用本地 Qwen 2.1 整合流的编码器和 VAE，加载标准 INT8 权重并保留官方标准模板的缓存设置。Z-Image 采用本地 Moody 工作流的编码器、VAE 和 AuraFlow shift 3，加载官方 Turbo BF16 权重。
- MiniMax H3 Image 使用 `model_pixel_budgets.minimax` 中的 16,777,216 像素预算，按目标比例计算宽高后，各自取最接近的 32 像素倍数。实际尺寸保存在批次清单的 `model_sizes` 中。普通续跑继续读取该清单。[MiniMax H3 Image 官方说明](https://blog.comfy.org/p/minimax-h3-image-is-now-available)
- Qwen-Image 2.1 使用官方 2K 画幅预设，方图为 2048 × 2048。其余四个模型沿用约 1 MP 的公共尺寸。[Qwen-Image 2.1 官方预设](https://github.com/QwenLM/Qwen-Image-2.1#supported-aspect-ratios)
- FLUX.2 使用本地 9B 蒸馏 FP8 权重、Qwen 3 8B FP8 mixed 编码器和完整 `flux2-vae`，调度器按每张图的实际宽高计算采样步进。

## 尺寸

默认每条无固定画幅规则的提示词生成 42 张，覆盖六个模型、七种画幅和一个 seed。下表是约 1 MP 的公共尺寸，MiniMax 和 Qwen-Image 2.1 各有独立的固定尺寸。宽高均对齐到 32 像素，实际画幅可与目标比例略有差异。

| 目标比例 | 宽 × 高 |
| --- | ---: |
| 1:1 | 1024 × 1024 |
| 4:3 | 1152 × 864 |
| 3:4 | 864 × 1152 |
| 3:2 | 1248 × 832 |
| 2:3 | 832 × 1248 |
| 16:9 | 1344 × 768 |
| 9:16 | 768 × 1344 |

比例、尺寸、seed、文件名和采样设置都可在 [config.json](config.json) 查看或修改。更换模型家族时也需核对对应的 API 工作流。可以先跑一个子集，或用内置测试词快速验证。

`seeds` 为 `null` 时，新批次随机抽取一个 64 位无符号 seed，全批共用。续跑从批次清单读取原 seed。需要固定 seed 时，可将 `seeds` 写为整数数组。用 `--exclude-prompts` 按提示词的相对路径跳过已测条目，其余提示词仍遵守 [尺寸规则表](prompts/size_rules.json)。

```powershell
.\benchmark\run.cmd --run pilot --models moss qwen21 --ratios 1:1 16:9
.\benchmark\run.cmd --smoke
```

## MiniMax H3 Image 分阶段流程

正式脚本按提示词提交 MiniMax 的待生成图片。每组编码一次，先采样全部尺寸，再进入 VAE 解码。已经成功保存且尺寸正确的图片直接沿用。新生成图片的 CSV 记录整组耗时，组内各图共享同一个 ComfyUI 任务 ID。每组的实际 API 图及任务状态保存在批次的 `minimax-groups/` 下。

只补跑现有批次中的 MiniMax 时，从项目根目录执行以下命令。

```powershell
.\benchmark\run.cmd --resume-models minimax
```

### 独立 A/B

在 ComfyUI 队列空闲时，从项目根目录运行以下命令。

```powershell
.\benchmark\minimax_ab.cmd
# 指定自己的提示词文件
.\benchmark\minimax_ab.cmd --prompt .\benchmark\prompts\01_editorial_portrait.txt
```

默认用中文水墨海报提示词和配置中的七种尺寸。A、B 共用本次随机抽取的 seed，或配置中的第一个固定 seed。A 逐图执行原工作流。B 共用一次原生 MiniMax 提示词编码，连续采样所有尺寸，再进入 VAE 解码阶段。B 复用本项目已安装的 Impact Pack 执行顺序节点。

每个方案开始前会清理 ComfyUI 的模型和执行缓存。结果保存到独立的 `runs/minimax-ab-*/`。打开 `report.html` 可逐尺寸对比原图，`summary.json` 记录整组耗时，`pixel_comparison.csv` 记录逐尺寸差异。B 的耗时以整组计算。

## 结果和续跑

正式批次默认保存到 `runs/main/`，测试批次保存到 `runs/smoke/`。

### 交互式对比看板

使用 `run.cmd --with-view` 时，在浏览器打开终端显示的地址。看板运行在本机，批次和翻译结束后仍可访问，按 Ctrl+C 关闭。只查看已有结果时，可在项目根目录执行以下命令。

```powershell
.\benchmark\run.cmd --view
```

首页汇总正式生成的图片，可按宽高比筛选。点击提示词打开对比详情，点击图片直接放大。滚轮、左右按钮和方向键可连续浏览当前筛选的图。新结果写入后刷新浏览器即可查看。默认端口为 8765，已占用时会自动选择可用端口，也可用 `--port 8766` 指定。`frontend/run.cmd` 也可独立启动看板。

### 导出静态站点

```powershell
.\benchmark\frontend\run.cmd --export-dir .\benchmark-public
```

导出目录应为新目录。里面包含主页、样式、脚本、汇总 JSON 和去除工作流元数据的 PNG，资源路径相对于页面。可以把该目录的内容作为 GitHub Pages 的发布源。导出时只纳入正式生成成功的结果。

汇总页第一列显示提示词摘要，画幅和 Seed 各占一行，点击可查看完整原文。每张图片下方显示宽高和元信息图标，点击可查看并复制该图的 JSON。内容包含原始提示词、尺寸、Seed、权重文件名和采样设置。Seed 用十进制字符串保存，避免浏览器舍入 64 位整数。

首轮三条提示词已登记为固定 `1:1`。有效批次报告、汇总页和静态导出每条只包含这一组。旧尺寸的 PNG 保留在本地输出目录中作为存档。

### 提示词中文含义

对比总览从批次的 `prompt_translations.json` 读取中文含义。翻译复用已有有效译文，只在有缺项时加载本地模型。如果自动翻译失败，已保存的图片、HTML 和 CSV 可继续使用。也可以独立补译或重新翻译。

```powershell
.\benchmark\translate_prompts.cmd
.\benchmark\translate_prompts.cmd --run smoke
.\benchmark\translate_prompts.cmd --run main --force
```

默认批次为 `main`。脚本直接通过 llama.cpp 加载便携版安装目录下的 `models/LLM/Qwen3.8-27B-Abliterated-Q4_K_M.gguf`，只需本地 Python、`llama-server` 和 GGUF 文件。翻译时可以关闭 ComfyUI。可通过 `--model`、`--llama-server` 和 `--run-dir` 指定其他模型文件、运行时和批次目录。默认复用已有有效译文。`--force` 重新处理全部条目。

后处理以该批次 `manifest.json` 的 `prompts` 为输入。原提示词为中文时原样复制，保留换行和标点。原提示词为英文时翻译为中文，保留数量、空间关系和画面中要求出现的原文。输出为 UTF-8 JSON 对象，键与 `prompts` 中的文件路径完全一致，包含子目录路径（如有），值为中文字符串。

```json
{
  "02_spatial_relation.txt": "一张照片，胡萝卜位于橙子的左侧。",
  "03_chinese_typography.txt": "中国水墨海报，画面左侧是一棵老松，横枝伸向右侧，树下两只丹顶鹤在起舞。宣纸上保留大片空白，用竖排毛笔字写清楚「松鹤延年」四个字。\n"
}
```

每条提示词只保存一份中文含义，同一批次的所有尺寸和 seed 共用。翻译可用后，刷新本地看板即可查看。生成脚本和独立翻译命令共用 `benchmark.translate_prompts.translate_local_run()`。整批翻译成功后才替换翻译文件，翻译结束后会释放本地模型进程。

### 批次产物

- `report.html` 将同一提示词、尺寸和 seed 的图并排显示。点击可打开原图。
- `results.csv` 记录提示词、尺寸、seed、采样设置、状态和 ComfyUI 执行耗时。每个模型第一张的执行耗时可能含模型加载时间。
- `manifest.json` 保存该批次的完整输入和各模型 API 图。原始 PNG 保存在 `images/<模型>/`，旁边的 JSON 记录 ComfyUI 任务 ID 和运行状态。

中断后用原命令续跑。脚本会跳过已有且尺寸正确的成功图片，继续等待已提交的任务，并重试失败项。修改提示词或配置后用 `--run` 指定新名称，例如 `--run round2`。运行依赖默认本机端口 8188，也可用 `--server http://127.0.0.1:8189` 覆盖。

使用 `--resume-models` 可按既有批次清单补跑指定模型，也可把已配置的新模型加入该批次。补跑沿用批次保存的提示词、尺寸规则和 Seed，完成后更新原批次的 HTML 报告、CSV 和公开看板。例如：

```powershell
.\benchmark\run.cmd --run main --resume-models flux2-9b
```

加 `--refresh-model-sizes` 可按当前模型尺寸和提示词画幅规则重绘指定模型，并沿用批次原本的提示词与 Seed。整批结果通过尺寸校验后，批次报告和看板会指向新图片。

```powershell
.\benchmark\run.cmd --run main --resume-models minimax qwen21 --refresh-model-sizes
```
