# 脚本、报告与分析契约

第一阶段 `collect` 保留云图原始榜单、详情、平台脚本、视频及事实包。`process-scripts` 只读取已通过取数门禁且身份核验后落盘的 `normalized/detail-records/<id>.json`，先用事实 manifest 的哈希复验。平台脚本非空时直接使用；仅空脚本且本地视频存在时提取 16kHz 单声道 WAV，调用本地 faster-whisper 模型。视频哈希作为本次运行的去重键；转写结果单独写入 `asr/materials/<id>.json`，保留 `asr/by-video/` 去重结果及 `asr/manifest.json`，不改平台源文件。

状态区分 `platform_script`、`available`、`missing_video`、`no_audio_track`、`no_speech`、`audio_decode_failed`、`audio_timeout`、`audio_empty`、`ffmpeg_unavailable`、`transcription_failed`。可重试失败不写入成功缓存。来源中记录素材 ID、视频相对路径和 SHA、音频 SHA、提供方、模型版本、时间码和原文；置信度未提供则不编造。ASR 不保证恢复视觉文字或完整营销脚本。

重新构建 `facts/script.json` 时，原始平台文本放入 `platform_script_text`，ASR 对象放入 `asr_transcript`，`script_text` 优先平台、其次有效 ASR，`script_source` 显式标明 `platform|asr|unavailable`。事实 manifest 绑定输入详情、ASR 文件和所用视频哈希。每次运行保存在 `云图素材脚本数据/<run-id>/` 下；报告 `script-report/index.html` 与 `report.json` 展示完整榜单、详情覆盖、脚本来源及转写状态，`receipt.json` 绑定报告及事实清单。榜单素材没有详情时只显示榜单，不补造视频或脚本。

第二阶段沿用 `workflow --run-dir --business-question`：先生成 `FactSelectionSpec` 交接，使用同一 `analysis_id` 收取模型返回后再生成 `AnalysisResult` 交接，最后校验事实引用并发布独立分析报告。分析计划不能改变第一阶段账号、日期或素材采集范围，不能重新调用 RPA。脚本来源差异及缺口必须进入模型输入与结论说明；平台指标仅可支持分布和关联观察，不能证明脚本写法导致效果。
