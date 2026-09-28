# 云图素材脚本工作台

稳定 ID：`yuntu-script-workbench`。本仓独立交付云图榜单/详情 RPA 取数、视频落盘、缺失脚本 ASR 补全、第一阶段脚本报告及第二阶段用户自定义分析。

- 用户流程与边界：[SKILL.md](SKILL.md)
- 取数契约：[references/collection-contract.md](references/collection-contract.md)
- 脚本与分析契约：[references/script-contract.md](references/script-contract.md)
- CLI：`python3 scripts/run.py --help`，原子采集命令仍在 `scripts/collector.py`
- 测试：`python3 -B tools/test.py`
- 构建：`python3 -B tools/build.py`，正式文件以 [packaging.json](packaging.json) 为准

默认运行数据根目录是调用方当前目录下的 `云图素材脚本数据/`，每次 `acquire` 建立独立 `run-.../` 子目录。命令返回 `runDir`，报告在该运行目录的 `script-report/index.html`；可用 `--output-root` 改数据根目录，或以 `--output-dir` 指定已有运行目录续跑。

常规运行依赖 `requirements.txt`；本地转写安装 `requirements-asr.txt`，并预置 faster-whisper 模型目录，由 `--model-dir` 或 `YUNTU_ASR_MODEL_DIR` 指定。主入口在采集前做 ASR 环境预检；转写使用独立子进程和 20 秒分块，子进程返回 `-9/137` 时改用低内存口径重试。可用 `--asr-hotwords` 提示已知专有名词，转写文件记录实际解码口径。ASR 运行环境、内存峰值和真实效果须在目标平台实测。媒体或转写失败可在原运行目录离线重试，不重新提交 RPA。运行数据、视频、模型、凭证和客户输入不入 Git 或 ZIP。

ASR 每次尝试的退出码、步骤与可用内存计数保存在运行目录的 `asr/attempts/`，用于区分异常退出与有证据的 OOM。资源更充足时可执行 `process-scripts --retry-low-memory`，只对当前缓存中的降级口径尝试 quality；失败保留旧脚本并返回非零状态。该命令不重新取数，且会先核对成功缓存、模型与 `--asr-hotwords` 是否与原运行一致。

本仓从云图元技能的已提交源码迁移当前 RPA 与两阶段实现，运行时不访问原仓；原仓继续独立维护。总体规划和当前进度见 [项目管理](../../项目管理/docs/AI短视频Skill开发批次进度.md)。
