# 云图素材脚本工作台

稳定 ID：`yuntu-script-workbench`。本仓独立交付云图榜单与详情取数、视频音轨准备、Agent 音频理解结果导入、第一阶段脚本报告及第二阶段用户自定义分析。本地 faster-whisper 模型链路已移除；平台文件理解工具只能由 Agent 调用，普通 Python 脚本不直接调用模型。

- 用户流程：[SKILL.md](SKILL.md)
- 取数契约：[references/collection-contract.md](references/collection-contract.md)
- 脚本契约：[references/script-contract.md](references/script-contract.md)
- CLI：`python3 scripts/run.py --help`；原子采集仍由 `scripts/collector.py` 执行
- 测试：`python3 -B tools/test.py`
- 构建：`python3 -B tools/build.py`，正式文件以 [packaging.json](packaging.json) 为准

`acquire` 默认在调用方当前目录的 `云图素材脚本数据/run-.../` 建立本次运行目录，完成采集并把缺失平台脚本的视频音轨保存到 `audio/by-video/`。`transcripts/manifest.json` 列出待 Agent 理解的音频；Agent 将完整工具回复和纯转写文本各存为 UTF-8 文件，使用 `import-transcript --video-sha ... --response-file ... --text-file ...` 导入。全部待处理音频导入后，`finalize-scripts` 重建事实和固定五分区的离线 `script-report/index.html`：素材筛选条件、完整素材榜单、脚本内容公式、单素材脚本、脚本片段库。结构数据及收据继续保存并绑定来源。同一视频 SHA 去重，平台原文始终优先，转写无可靠时间码。

运行数据、客户视频与音频、转写结果、凭证和模型服务回复不入 Git 或 ZIP。旧运行目录中的 `asr/` 文件作为历史数据保留，不由新版流程继续生成或改写。在线 RPA、平台音频工具及目标平台安装须分别实测；本地离线测试不代表这些步骤已验收。

总体规划和当前进度见[项目管理](../../项目管理/docs/AI短视频Skill开发批次进度.md)。
