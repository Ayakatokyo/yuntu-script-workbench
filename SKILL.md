---
name: 云图素材脚本工作台
description: 采集云图行业榜单与素材详情，对已获视频中缺少平台脚本的素材提取音轨并转写，交付可追溯脚本报告，再按用户自定义问题分析。
metadata:
  argument_hint: 先选择云图授权账号、行业、周期、榜单口径和脚本素材数；阶段一报告完成后等待用户提出分析问题。
---

# 云图素材脚本工作台

本包以独立运行代码保留云图元技能的榜单与详情 RPA、筛选、视频、事实包和两阶段分析流程。详细输入字段与采集门禁见[取数契约](references/collection-contract.md)，脚本来源、报告和第二阶段见[输出与分析契约](references/script-contract.md)。运行入口 `scripts/run.py`，取数原子入口 `scripts/collector.py`；Python 3.11+，常规依赖见 `requirements.txt`，本地 ASR 另需 `requirements-asr.txt` 和预置的 faster-whisper 模型目录。所有运行目录必须在技能安装目录之外。

## 阶段一：取数、转写、报告、暂停

先按[取数契约](references/collection-contract.md)将表单的脚本素材数映射为 `QuerySpec.collection.target_top_n`，然后验证 QuerySpec。`acquire` 依次运行元技能同源的 `collect`（榜单→本地选材→详情→视频→事实包）、仅针对缺少有效平台脚本的已获视频提取音频并转写、重建带来源的事实包、生成 `script-report/index.html` 和收据。默认在调用方当前目录的 `云图素材脚本数据/<run-id>/` 保存每次运行；返回的 `runDir` 用于续跑和第二阶段。脚本工作台主流程不生成高光帧。平台原始 CSV、规范化详情与平台脚本不被 ASR 覆盖。

```bash
python "$SKILL_ROOT/scripts/run.py" validate --query-spec "$QUERY_SPEC"
python "$SKILL_ROOT/scripts/run.py" acquire --shop-id "$AUTHORIZED_SHOP_ID" --query-spec "$QUERY_SPEC" --model-dir "$LOCAL_ASR_MODEL_DIR"
# 已获可信云图运行目录可从本地加工，不再提交 RPA
python "$SKILL_ROOT/scripts/run.py" process-scripts --run-dir "$RUN_DIR" --model-dir "$LOCAL_ASR_MODEL_DIR"
# 在资源更充足的环境中，仅对缓存的低内存口径结果尝试高质量重转写
python "$SKILL_ROOT/scripts/run.py" process-scripts --run-dir "$RUN_DIR" --model-dir "$LOCAL_ASR_MODEL_DIR" --retry-low-memory
python "$SKILL_ROOT/scripts/run.py" render-script-report --run-dir "$RUN_DIR"
python "$SKILL_ROOT/scripts/run.py" status --output-dir "$RUN_DIR"
```

`--model-dir` 可由 `YUNTU_ASR_MODEL_DIR` 提供；模型和 ASR 依赖须已在目标环境本地就绪。`acquire` 在提交 RPA 前检查模型、依赖与 ffmpeg。缺模型会立即报错；媒体处理或转写失败会保留原事实并在 `asr/manifest.json` 和报告中显示，组合入口返回非零状态 2；修复环境后只重跑 `process-scripts`。有效平台脚本不触发 ASR；相同视频 SHA 在本次运行内只转写一次。无音轨、无语音、视频缺失、解码失败和转写失败分状态记录。ASR 只代表音轨中的可识别语音，不能补画面字幕或包装文字。

ASR 将音轨切成 20 秒片段，在独立进程逐片转写；默认 `int8`、CPU 双线程、beam 5。若子进程返回 `-9/137`，自动以 beam 1 和 VAD 重试该视频，不中断其余素材，并在转写事实中记录实际 `decodeProfile`。可通过 `--asr-hotwords '燕之屋,产品名'` 提供已知品牌和产品词以改善专有名词识别；用相同参数执行离线重试，改词后旧结果不复用。此提示不能保证识别正确，关键口播仍应人工核对。模型加载本身仍需内存，两档都失败时标记 `transcription_failed`；需结合尝试记录判断原因，而不把缺失结果当成功脚本。

每次 ASR 子进程尝试会在 `asr/attempts/<视频SHA>/` 保存独立 JSON，记录档位、退出码、耗时、最后完成的步骤和可获取的进程/cgroup 内存证据；素材转写文件与清单绑定这些记录的 SHA。`-9/137` 只说明子进程异常退出，需结合 cgroup `oom_kill` 变化判断是否为内存原因。`--retry-low-memory` 要求当前运行的成功缓存齐全且模型、提示词与原运行一致；预检不通过时直接拒绝，不会意外重转写所有素材。它只对 `memory_safe` 缓存增加一次仅 `quality` 的离线重试；失败时保留原脚本、返回非零状态并记录失败，不重新提交 RPA。

可用 `--output-root` 指定其他数据根目录；需要对已有运行目录续跑时使用 `--output-dir` 指定原目录。两者互斥，且运行数据目录不能位于技能安装目录内。报告入口为 `RUN_DIR/script-report/index.html`，原始采集、视频、转写与报告均留在同一 `RUN_DIR`。

首批最多 3 条详情必须全部确认且至少一份真实、非空业务、身份匹配的 CSV 才继续。全无有效 CSV 为持久 `blocked`，未知结果为 `unconfirmed`；两种状态都暂停后续 RPA，ASR 不能绕过。自定义日期按北京时间 T+4，非法日期必须在外部请求前拒绝。身份、授权及重启恢复规则沿用[取数契约](references/collection-contract.md)。取数失败或门禁暂停时不生成成功脚本报告。

报告展示完整已获榜单与已取得详情的脚本文本、来源和转写状态。脚本事实中的 `platform_script_text` 与 `asr_transcript` 并存；统一读取字段 `script_text` 优先使用可信平台原文，否则使用有效 ASR，并标记 `script_source`。无脚本保持缺口。报告完成后停在等待用户分析输入，不自动生成商业结论。

## 阶段二：用户自定义分析

用户提出业务问题后才启动 `workflow`。该命令沿用云图元技能的两次当前会话模型交接：先选择当前事实，再分析所选事实；每次使用独立 `analysis_id`，不重新取数。模型输入只读取本轮事实包，脚本观察须区分平台脚本与 ASR 来源并引用可解析 `fact_refs`。没有可用脚本文本的素材只记缺口，不用标题或标签代写脚本；指标关联不能表述为脚本导致效果。未提供模型结果时保持等待，不伪造报告。

```bash
python "$SKILL_ROOT/scripts/run.py" workflow --run-dir "$RUN_DIR" --business-question '分析该行业素材的开头表达与表现分布'
# 按回执将本轮模型返回保存成 JSON，再携带同一 analysis-id 继续 workflow
```

阶段二的具体输入、来源和验证见[输出与分析契约](references/script-contract.md)。新取数需要当前用户对云图账号和目标范围的授权；离线 `process-scripts`、报告重建与状态查询只处理已有本地运行。客户视频、转写、模型文件与报告均保留在用户工作目录，不进入技能 ZIP。
