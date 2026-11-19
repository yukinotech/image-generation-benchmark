每个 UTF-8 `.txt` 文件放一条完整提示词。可多行，可放在子目录中。脚本保留提示词中的换行及空格，跳过全空白文件。

提示词明确指定画幅时，在 [size_rules.json](size_rules.json) 中登记。键为相对于提示词目录的文件路径（子目录用 `/`），值为 `config.json` 的 `sizes` 中定义的宽高比。实际像素尺寸由该配置提供。没有登记的提示词使用本批次选中的所有比例。

脚本每次规划批次时读取该表。若 `--ratios` 未包含某条提示词的固定比例，该条会跳过。规则会随批次写入 `manifest.json`，供续跑校验和看板使用。提示词、比例或适用规则变动后，使用 `--run` 指定新批次。自定义 `--prompts` 目录可放自己的同名规则表。

| 文件 | 对照时看什么 | 来源 |
| --- | --- | --- |
| [01_editorial_portrait.txt](01_editorial_portrait.txt) | 侧脸、单侧硬光、衣料和皮革质感、绿色拼贴背景 | 改写自 [ComfyUI 官方 Qwen-Image 2.1 文生图示例](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_qwen_image_2_1_t2i.json) |
| [02_spatial_relation.txt](02_spatial_relation.txt) | 能否区分胡萝卜与橙子，胡萝卜是否在橙子左侧 | 采用 [GenEval 位置关系原题](https://github.com/djghosh13/geneval/blob/main/prompts/evaluation_metadata.jsonl#L385) |
| [03_chinese_typography.txt](03_chinese_typography.txt) | 「松鹤延年」是否四字正确并竖排，松树、双鹤、留白是否符合描述 | 改写自 [WeGenBench 中文文字渲染 CN_1](https://github.com/WeChatCV/WeGenBench/blob/main/data/prompts/wegenbench_text_cn.jsonl#L1) |

## 第三轮

| 文件 | 比例 | 对照时看什么 |
| --- | --- | --- |
| [09 雨水走过一座湿地](09_wetland_water_cycle_infographic.txt) | 2:3 | 六站的顺序、流向、局部标注与两种水位 |
| [10 博物馆展柜推理](10_museum_grid_logic.txt) | 16:9 | 两排三列的唯一解，展品、标签和验证带是否一致 |
| [11 折叠检修台 CAD](11_folding_workbench_cad.txt) | 16:9 | 展开与折叠结构一致性、尺寸标注和零件计数 |
| [12 发条星图馆剖面](12_clockwork_observatory_cutaway.txt) | 3:2 | 剖面空间、楼梯和传动路径、主图与局部线稿对应 |
| [13 图书馆客流数据板](13_library_flow_dashboard.txt) | 4:3 | 总览、柱图、折线和数据表是否一致 |
