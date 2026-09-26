# 云图取数契约

取数模块以 `yuntu-meta-skill@6dfb8c3` 的正式源码为基线，连同榜单/详情动态连接器、账号鉴权、QuerySpec、日期策略、首批 CSV 门禁、批次与恢复状态、CSV 下载/解析、规范化、视频/高光、事实包及独立分析编排复制到本包。运行时仅使用本包文件，不导入原仓。配置 `config/yuntu-material-field-registry-v1.json` 和 `config/yuntu-content-formula-taxonomy-v1.json` 随包交付。

云图榜单与详情使用同一个已授权 `rpa_shop.extra.shop_id`。首次实时调用前核对账号授权及动态连接器定义；列表获取 TOP1000 后在本地按 `material_list` 筛选、排序并选详情目标。行业、日期、榜单指标、品牌范围、年龄、性别、八大人群及素材筛选规则沿用元技能的 `metadata.yaml.skill_form` 和 `scripts/config_specs.py`。自定义日期 T+4、369 天下界、1–45 天跨度在请求前校验；近 7/30 天保留连接器原生周期语义。

详情首批最多 3 项，必须收齐确认结果并检查真实 CSV 的业务字段、素材身份与日期。零有效 CSV 记录 `firstBatchCsvGate.status=blocked` 与 `rpa_paused`，停止所有后续详情、补位与重提；未知任务记录 `unconfirmed`，只核实已登记任务，不盲目重提。至少一份有效且首批均确认才放行。视频或高光后处理失败不误判为 CSV 无效。重启沿用持久门禁，修正参数使用新运行目录。异步任务登记 `task_group_id`，采集状态及部分失败保持原义。

`--highlight-mode off` 时仍保留本地视频并跳过高光帧；`on` 时沿用原高光规则及恢复入口。视频落在 `videos/<material_id>/source-video.mp4`。所有 RPA 实际运行须具备宿主网关、授权和目标平台连接器；离线测试只验证本地契约，不代表线上通过。
