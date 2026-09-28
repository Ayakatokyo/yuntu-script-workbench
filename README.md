# 云图素材脚本工作台

稳定 ID：`yuntu-script-workbench`。本仓独立交付云图榜单与详情取数、视频音轨准备、Agent 音频理解结果导入、第一阶段脚本报告及第二阶段用户自定义分析。本地 faster-whisper 模型链路已移除；平台文件理解工具只能由 Agent 调用，普通 Python 脚本不直接调用模型。

- 用户流程：[SKILL.md](SKILL.md)
- 取数契约：[references/collection-contract.md](references/collection-contract.md)
- 脚本契约：[references/script-contract.md](references/script-contract.md)
- CLI：`python3 scripts/run.py --help`；原子采集仍由 `scripts/collector.py` 执行
- 测试：`python3 -B tools/test.py`
- 构建：`python3 -B tools/build.py`，正式文件以 [packaging.json](packaging.json) 为准

`acquire` 默认在调用方当前目录的 `云图素材脚本数据/run-.../` 建立本次运行目录，完成采集并把缺失平台脚本的视频音轨保存到 `audio/by-video/`；抽取临时文件放在音频目标目录，避免跨设备移动。`transcripts/manifest.json` 列出待 Agent 理解的音频；Agent 将完整工具回复和纯转写文本各存为 UTF-8 文件，使用 `import-transcript --video-sha ... --response-file ... --text-file ...` 导入。识别后仍无可用文本时，使用 `mark-unavailable --video-sha ... --response-file ... --review-note ...` 记录可追溯缺口；未经处理的音频仍阻断 `finalize-scripts`。最终报告的筛选区展示平台品牌范围；榜单和公式页共享基于已核验品牌事实 ID 的标题规则分类筛选，不把该分类称为平台权威品牌归属。完整榜单不按标题品牌词自动裁剪。`finalize-scripts` 重建事实和固定五分区的离线 `script-report/index.html`：素材筛选条件、完整素材榜单、脚本内容公式、单素材脚本、脚本片段库。榜单自定义指标使用云图元技能式分组弹窗、默认列、确定/取消和本地持久化；公式一级柱状图联动二级环形图，单素材核心数据与内容公式沿用云图元技能的字段默认值、选择交互和来源文本定位；缺失数据保持空态。结构数据及收据继续保存并绑定来源。同一视频 SHA 去重，平台原文始终优先，转写无可靠时间码。

运行数据、客户视频与音频、转写结果、凭证和模型服务回复不入 Git 或 ZIP。旧运行目录中的 `asr/` 文件作为历史数据保留，不由新版流程继续生成或改写。在线 RPA、平台音频工具及目标平台安装须分别实测；本地离线测试不代表这些步骤已验收。

总体规划和当前进度见[项目管理](../../项目管理/docs/AI短视频Skill开发批次进度.md)。

第二阶段默认分区按本轮 manifest 解析；新结论用 `field_refs` 声明实际证据字段，原话用 `quotes` 绑定逐字来源。正文可用而分段缺失不再误拦截，旧结果仍按原保守规则核验。详见脚本契约。

## 第二优先级：稳定分析交接与正式交付

分析准备完成后，先读返回的 `handoff-index.json`：其中列出全部事实页、输入 SHA-256、记录数量、缺口和结果模板。每页最多五条完整记录，保留全文与来源；不得自行截断正文或重排云图原始 fact_ref 下标。按问题选择的完整范围读完所需分页后，将 `result-template.json` **复制到另一个结果文件**，填写结论及证据；空白模板不能提交为完成结果。事实页、索引和模板属于输入快照，不原地修改。

```bash
python "$SKILL_ROOT/scripts/run.py" analysis-page --analysis-root "$ANALYSIS_ROOT" --page 1
python "$SKILL_ROOT/scripts/run.py" analysis-status --analysis-root "$ANALYSIS_ROOT"
python "$SKILL_ROOT/scripts/run.py" render-analysis-report --analysis-root "$ANALYSIS_ROOT"
```

标准发布流程自动生成 `analysis-report/index.html`、`report.json` 和 `receipt.json`，正文展示结论、类型、原始字段值、可比较的数值图及缺口。HTML 仅从已校验结果和对应事实生成，原始数值不因显示格式改变。`analysis-status` 分别返回业务状态、`deliveryStatus`、`reportVerified` 和下一步；进程退出码为 0、等待模型或分析结果已发布，都不能单独代表 HTML 已交付。报告生成失败时，先检查同一分析目录的状态，再运行 `render-analysis-report` 本地恢复；输入或结果已被修改时拒绝恢复，应创建新的分析。

新阶段回执追加在调用所属运行/分析目录的 `execution-events.jsonl`，记录本地阶段真实起止时间、单调时钟耗时、状态和退出码；`returned` 只表示函数返回，业务完成状态以正式回执为准。嵌套阶段不能直接求和，外部 Agent 等待时间和历史缺失耗时保持未知。命令返回后仍在运行的后台任务须另读业务终态，不能把启动命令耗时当作后台任务总耗时。本轮未改变阶段一五页报告。

云图标准 `workflow` 在选材后返回分页索引与模板路径；模板按全部已选分区生成 `results`，每个分区填写结论或明确缺口，保留 `_handoff` 的身份与输入哈希。提交时校验该绑定，并重新执行本地事实选择验证来源；兼容旧的无绑定结果，但仍走字段及来源校验。结果完成时另存 SHA-256，报告恢复必须匹配该值。历史无结果哈希的分析不补写伪造收据，应另建分析。旧 `analyze` 兼容入口保持原 Markdown 流程，正式 HTML 交付使用 `workflow`。

CLI 主入口在 stderr 最后追加一行 `event=command_completed` 的 JSON，记录实际退出码和本地命令耗时；stdout 保留业务回执。stderr 可能包含错误行和耗时行，按 JSON Lines 分别读取，不能把整个 stderr 当一个 JSON 对象。库调用 `run.main()` 默认不额外输出耗时行。

榜单远端失败将任务组、错误码与脱敏说明写入 `batch-manifest.json.failureDetails` 和 `run-summary.json.stages.list.failureDetails`；详情远端失败在拒收素材中保留相同结构。保留 `retrySubmitted=false`，本功能不自动重新提交失败 RPA。凭证、请求完整参数和下载 URL 不进入失败摘要。
