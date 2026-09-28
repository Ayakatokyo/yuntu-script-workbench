# 脚本、报告与分析契约

第一阶段 `collect` 保留云图原始榜单、详情、平台脚本、视频及事实包。`process-scripts` 只读取已通过取数门禁且身份核验后落盘的 `normalized/detail-records/<id>.json`，先用事实 manifest 的哈希复验。平台脚本非空时直接使用；仅空脚本且本地视频存在时提取 16kHz 单声道 WAV，调用本地 faster-whisper 模型。视频哈希作为本次运行的去重键；转写结果单独写入 `asr/materials/<id>.json`，保留 `asr/by-video/` 去重结果及 `asr/manifest.json`，不改平台源文件。

`acquire` 在 RPA 前预检本地模型文件、ASR 依赖与 ffmpeg。ASR 音轨固定 PCM16、16kHz、单声道，以 20 秒分块在每视频独立子进程中逐块解码；默认 `int8`/beam 5，子进程返回 `-9/137` 时以 beam 1/VAD 重试。两档都被杀时仅该视频记失败，其余素材继续。`--asr-hotwords` 仅用于已知品牌/产品词提示，并纳入缓存键；不得把提示词当作转写事实。缓存键还包含模型指纹和解码策略版本，重试成功才覆盖旧缓存。转写事实记录实际 `decodeProfile`，便于区分降级结果与人工复核。

每次子进程尝试写入独立 `asr/attempts/<videoSha256>/<attempt-id>.json`：档位、开始/结束时间、耗时、退出码、最后 worker 步骤与峰值 RSS，以及可读取时的 cgroup v2 内存限额、用量和 `oom_kill` 计数前后值。转写文件及 ASR manifest 列出尝试记录相对路径与 SHA。`-9/137` 本身不等于已确认 OOM；cgroup 计数不可用时保持缺失，计数增加也须考虑同一 cgroup 的其他进程。记录不保存口播文本或提示词。

`process-scripts --retry-low-memory` 先核验当前运行的成功缓存齐全且与本次模型、提示词匹配；否则在转写前拒绝。随后仅对 `memory_safe` 结果尝试 `quality` 档，成功后更新视频缓存及素材事实；失败或返回无语音时保留已有可用脚本、记失败回执并返回状态 2。相同视频 SHA 在一次重跑中只尝试一次；不调用 RPA，不覆盖平台原文。

状态区分 `platform_script`、`available`、`missing_video`、`no_audio_track`、`no_speech`、`audio_decode_failed`、`audio_timeout`、`audio_empty`、`ffmpeg_unavailable`、`transcription_failed`。可重试失败不写入成功缓存。来源中记录素材 ID、视频相对路径和 SHA、音频 SHA、提供方、模型版本、时间码和原文；置信度未提供则不编造。ASR 不保证恢复视觉文字或完整营销脚本。

重新构建 `facts/script.json` 时，原始平台文本放入 `platform_script_text`，ASR 对象放入 `asr_transcript`，`script_text` 优先平台、其次有效 ASR，`script_source` 显式标明 `platform|asr|unavailable`。事实 manifest 绑定输入详情、ASR 文件和所用视频哈希。每次运行保存在 `云图素材脚本数据/<run-id>/` 下；报告 `script-report/index.html` 与 `report.json` 展示完整榜单、详情覆盖、脚本来源及转写状态，`receipt.json` 绑定报告及事实清单。榜单素材没有详情时只显示榜单，不补造视频或脚本。

第二阶段沿用 `workflow --run-dir --business-question`：先生成 `FactSelectionSpec` 交接，使用同一 `analysis_id` 收取模型返回后再生成 `AnalysisResult` 交接，最后校验事实引用并发布独立分析报告。分析计划不能改变第一阶段账号、日期或素材采集范围，不能重新调用 RPA。脚本来源差异及缺口必须进入模型输入与结论说明；平台指标仅可支持分布和关联观察，不能证明脚本写法导致效果。
