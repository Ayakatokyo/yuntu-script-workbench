"""Offline five-section script report, adapted from the Qianchuan workbench layout.

Only verified fact-package fields are used for material content. JavaScript filters
already rendered content; the document stays readable with JavaScript disabled.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
import json
from pathlib import Path
from typing import Any

try:
    from content_formula_labels import formula_detail_labels, resolve_formula_ref
except ImportError:
    from .content_formula_labels import formula_detail_labels, resolve_formula_ref


def _e(value: Any) -> str:
    if value is None or value == "":
        value = "未提供"
    return escape(str(value), quote=True)


def _metric_columns(rows: list[dict]) -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "config/yuntu-material-field-registry-v1.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    labels = {
        item["path"][1]: item["label"]
        for item in registry["fields"]
        if len(item.get("path", [])) == 2 and item["path"][0] == "metrics"
    }
    keys = list(dict.fromkeys(
        key for row in rows for key in (row.get("metrics") or {})
    ))
    return [{"key": key, "label": labels.get(key, key)} for key in keys]


_PERIOD_LABELS = {"LAST_7_DAYS": "近 7 天", "LAST_30_DAYS": "近 30 天", "CUSTOM": "自定义日期"}
_RANKING_LABELS = {"EXPOSURE_TOP1000": "曝光量 Top 1000", "CTR_TOP1000": "点击率 Top 1000", "INTERACTION_RATE_TOP1000": "互动率 Top 1000", "COMPLETION_RATE_TOP1000": "完播率 Top 1000"}

def _formula_pairs(row: dict) -> list[tuple[str, str | None]]:
    formula = row.get("contentFormula")
    if not isinstance(formula, dict) or not isinstance(formula.get("labels"), list):
        return []
    detail_labels = formula_detail_labels(formula)
    pairs = []
    for entry in formula["labels"]:
        if not isinstance(entry, dict):
            continue
        _, primary = resolve_formula_ref(entry.get("level_1_label"), "primary", detail_labels)
        if not primary:
            continue
        children = entry.get("level_2_labels")
        if isinstance(children, list) and children:
            for child in children:
                _, secondary = resolve_formula_ref(child, "secondary", detail_labels)
                pairs.append((primary, secondary))
        else:
            pairs.append((primary, None))
    return pairs


def _formula_counts(rows: list[dict]) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
    primary: dict[str, set[str]] = defaultdict(set)
    secondary: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        material_id = row["materialId"]
        for first, second in _formula_pairs(row):
            primary[first].add(material_id)
            if second:
                secondary[first][second].add(material_id)
    return primary, secondary


def _quotes(rows: list[tuple[int, dict]]) -> list[dict]:
    """Use only exact excerpts from the verified complete script text."""
    found = []
    seen = set()
    for index, row in rows:
        text = row.get("scriptText")
        formula = row.get("contentFormula")
        if not isinstance(text, str) or not text.strip() or not isinstance(formula, dict):
            continue
        sources = formula.get("sources")
        script = sources.get("script") if isinstance(sources, dict) else None
        detail = script.get("detail") if isinstance(script, dict) else None
        if not isinstance(detail, dict):
            continue
        for value in detail.values():
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                quote = candidate.strip()
                key = (row["materialId"], quote)
                if quote and quote in text and key not in seen:
                    found.append({"row": row, "index": index, "text": quote})
                    seen.add(key)
    return found


_STYLE = """
:root{color-scheme:light;--bg:#f4f7fb;--surface:#fff;--soft:#f8fafc;--line:#dfe6f0;--line-strong:#c9d5e5;--text:#172033;--muted:#63708a;--accent:#2563eb;--accent-soft:#eff6ff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--text);background:var(--bg)}
*{box-sizing:border-box}body{margin:0;padding:32px}main{max-width:1120px;margin:auto}h1{margin:0 0 10px;font-size:30px;line-height:1.25}h2{font-size:20px;margin:0 0 16px}h3{font-size:16px;margin:0 0 12px}p,li{line-height:1.65}p{margin:8px 0 16px}a{color:var(--accent)}button,input{font:inherit}button{min-height:40px;padding:8px 14px;border:1px solid var(--line-strong);border-radius:9px;background:var(--surface);color:var(--text);cursor:pointer}button:hover,.section-nav a:hover{border-color:var(--accent);background:var(--accent-soft)}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #93b4ef;outline-offset:2px}.muted,.subtitle{color:var(--muted)}.subtitle{font-size:13px;margin-bottom:20px}.section-nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}.section-nav a{min-height:40px;padding:8px 14px;border:1px solid var(--line-strong);border-radius:9px;background:#fff;color:var(--text);text-decoration:none;font-size:14px}.section-nav a.active{background:var(--accent);border-color:var(--accent);color:#fff}.panel,.material-card,.toolbar{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 24px rgba(34,64,112,.05)}.panel{padding:22px;margin:16px 0}.summary-cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:0 0 20px}.summary-cards div{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}.summary-cards span{display:block;font-size:12px;color:var(--muted)}.summary-cards strong{display:block;font-size:24px;margin-top:4px}.context{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:0}.context>div{padding:12px;background:var(--soft);border:1px solid var(--line);border-radius:10px}.context dt{font-size:12px;color:var(--muted)}.context dd{margin:6px 0 0;font-size:14px;overflow-wrap:anywhere}.toolbar{padding:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.toolbar input{flex:1;min-width:210px}.toolbar details{position:relative}.toolbar details[open] .metric-picker{position:absolute;z-index:5;right:0;top:38px;width:min(310px,80vw);max-height:320px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px;box-shadow:0 12px 25px rgba(34,64,112,.13)}.metric-picker label{display:block;padding:7px;font-size:13px}.metric-picker input{min-width:0;margin-right:7px}input[type=search]{min-height:42px;padding:9px 12px;border:1px solid var(--line-strong);border-radius:9px;background:#fff;color:var(--text)}.count-note{font-size:13px;color:var(--muted);margin:16px 2px 10px}.table-wrap{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:14px;margin:16px 0}table{width:100%;min-width:750px;border-collapse:separate;border-spacing:0;font-size:13px}th,td{text-align:left;padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:top}th{position:sticky;top:0;background:var(--soft);white-space:nowrap}th button{border:0;background:none;padding:4px 0;min-height:28px;font-weight:650}.material-name-column{min-width:340px;width:340px;position:sticky;left:0;background:#fff}th.material-name-column{background:var(--soft);z-index:2}.material-title{display:block;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text);text-decoration:none;font-weight:550}.material-title:hover{color:var(--accent)}.material-id{display:block;font-size:12px;color:var(--muted);margin-top:6px;overflow-wrap:anywhere}.pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:13px}.pager div{display:flex;align-items:center;gap:8px}.formula-grid{display:grid;grid-template-columns:1fr;gap:16px}.formula-chart{padding:20px;background:#fff;border:1px solid var(--line);border-radius:14px}.bar-row{display:grid;grid-template-columns:130px minmax(0,1fr) 48px;align-items:center;gap:10px;margin:15px 0;font-size:13px}.bar-track{height:14px;background:#e9eef7;border-radius:8px;overflow:hidden}.bar-fill{display:block;height:100%;background:#5b8def;border-radius:8px}.formula-children{padding:0 0 0 18px}.formula-children li{padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}.single-shell{display:grid;grid-template-columns:230px minmax(0,1fr);gap:16px;align-items:start}.selector{position:sticky;top:18px;padding:16px;background:#fff;border:1px solid var(--line);border-radius:14px;max-height:70vh;overflow:auto}.selector h2{font-size:17px}.selector input{width:100%;min-width:0}.material-option{display:block;width:100%;text-align:left;margin-top:7px;padding:10px 11px;border:1px solid var(--line);border-radius:9px;color:var(--text);background:#fff;font-size:12px;overflow-wrap:anywhere;line-height:1.55;text-decoration:none}.material-option.active,.material-option:hover{background:var(--accent-soft);border-color:var(--accent)}.material-card{padding:22px;margin:0 0 16px;min-width:0}.material-card h2{overflow-wrap:anywhere}.material-card h3{margin-top:22px}.eyebrow{font-size:12px;color:var(--muted)}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric-card{padding:14px;border:1px solid var(--line);border-radius:10px;background:#fbfdff;min-width:0}.metric-card span{display:block;font-size:12px;color:var(--muted)}.metric-card strong{display:block;font-size:21px;overflow-wrap:anywhere}.script-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.9;padding:16px;border:1px solid var(--line);border-radius:10px;background:#fbfdff;font-size:14px}.source-note{font-size:12px;color:var(--muted)}.source-copy{border-top:1px solid var(--line);padding:14px 0}.quote-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.quote-card{border:1px solid var(--line);border-top:3px solid var(--accent);border-radius:13px;background:#fff;padding:20px}.quote-card blockquote{margin:0 0 14px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85}.empty{padding:30px;text-align:center;color:var(--muted)}.footer{font-size:12px;color:var(--muted);margin:22px 0}[hidden]{display:none!important}
@media(max-width:760px){body{padding:18px 12px}h1{font-size:24px}.summary-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.context,.formula-grid,.single-shell,.quote-grid{grid-template-columns:1fr}.selector{position:static;max-height:280px}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.panel,.material-card{padding:16px}.material-name-column{min-width:250px;width:250px}.material-title{max-width:220px}.bar-row{grid-template-columns:90px minmax(0,1fr) 38px}}
@media print{body{background:#fff;padding:0}.section-nav,.toolbar,.pager,.selector{display:none!important}.single-shell{display:block}.panel,.material-card{box-shadow:none}}
"""

_SCRIPT = """
(() => {
  const all = selector => Array.from(document.querySelectorAll(selector));
  const panels = all("[data-panel]");
  const tabs = all("[data-section]");
  const cards = all("[data-card]");
  const options = all(".material-option");
  const openLinks = all("[data-open]");
  const rows = all("[data-row]");
  let page = 1, sortKey = "", direction = 0;
  const pageSize = 10;
  const normalize = value => String(value || "").normalize("NFKC").toLocaleLowerCase().trim();
  function section(name) {
    panels.forEach(item => { item.hidden = item.dataset.panel !== name; });
    tabs.forEach(item => {
      const active = item.dataset.section === name;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    });
  }
  function openMaterial(index) {
    cards.forEach(item => { item.hidden = item.dataset.card !== index; });
    options.forEach(item => item.classList.toggle("active", item.dataset.open === index));
    section("single-material");
  }
  tabs.forEach(item => item.addEventListener("click", event => {
    event.preventDefault(); section(item.dataset.section);
  }));
  openLinks.forEach(item => item.addEventListener("click", event => {
    event.preventDefault(); openMaterial(item.dataset.open);
  }));
  const search = document.getElementById("material-search");
  function updateRows() {
    const query = normalize(search.value);
    const matching = rows.filter(row => normalize(row.dataset.search).includes(query));
    if (sortKey && direction) matching.sort((a,b) => {
      const number = row => {
        const cell = Array.from(row.querySelectorAll("[data-metric]")).find(item => item.dataset.metric === sortKey);
        const value = cell ? Number(cell.dataset.value) : NaN;
        return Number.isFinite(value) && cell.dataset.value !== "" ? value : null;
      };
      const av = number(a), bv = number(b);
      if (av === null || bv === null) return av === bv ? Number(a.dataset.row) - Number(b.dataset.row) : av === null ? 1 : -1;
      return (av - bv) * direction || Number(a.dataset.row) - Number(b.dataset.row);
    });
    page = Math.max(1, Math.min(page, Math.max(1, Math.ceil(matching.length / pageSize))));
    rows.forEach(row => { row.hidden = true; });
    matching.slice((page-1)*pageSize, page*pageSize).forEach(row => {
      row.hidden = false; document.getElementById("material-rows").appendChild(row);
    });
    document.getElementById("visible-count").textContent = "共 " + matching.length + " 条";
    document.getElementById("table-range").textContent = matching.length ? "显示 " + ((page-1)*pageSize+1) + "-" + Math.min(page*pageSize,matching.length) + " / " + matching.length : "暂无记录";
    document.getElementById("page-number").textContent = page + " / " + Math.max(1,Math.ceil(matching.length/pageSize));
    document.getElementById("previous-page").disabled = page <= 1;
    document.getElementById("next-page").disabled = page >= Math.max(1,Math.ceil(matching.length/pageSize));
    document.getElementById("no-results").hidden = matching.length > 0;
  }
  search.addEventListener("input", () => { page = 1; updateRows(); });
  all("[data-sort]").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.sort;
    direction = sortKey === key ? direction === -1 ? 1 : direction === 1 ? 0 : -1 : -1;
    sortKey = direction ? key : "";
    all("[data-sort] span").forEach(mark => { mark.textContent = ""; });
    if (direction) button.querySelector("span").textContent = direction === 1 ? " ↑" : " ↓";
    updateRows();
  }));
  document.getElementById("previous-page").onclick = () => { page--; updateRows(); };
  document.getElementById("next-page").onclick = () => { page++; updateRows(); };
  const defaults = ["show_cnt","ctr","cvr","interact_rate"];
  const checks = all("[data-metric-choice]");
  const available = checks.map(item => item.dataset.metricChoice);
  const chosen = defaults.filter(key => available.includes(key));
  if (!chosen.length) chosen.push(...available.slice(0,4));
  function showColumns() {
    const selected = new Set(checks.filter(item => item.checked).map(item => item.dataset.metricChoice));
    all("[data-col]").forEach(item => { item.hidden = !selected.has(item.dataset.col); });
    all("[data-metric]").forEach(item => { item.hidden = !selected.has(item.dataset.metric); });
  }
  checks.forEach(item => {
    item.checked = chosen.includes(item.dataset.metricChoice);
    item.addEventListener("change", showColumns);
  });
  showColumns();
  if (cards.length) {
    cards.forEach((item,index) => { item.hidden = index !== 0; });
    options[0]?.classList.add("active");
  }
  document.getElementById("report-controls").hidden = false;
  document.getElementById("table-pager").hidden = false;
  document.getElementById("quote-search").hidden = false;
  const quoteCards = all("[data-quote]");
  const quoteSearch = document.getElementById("quote-search");
  quoteSearch.addEventListener("input", () => {
    const query = normalize(quoteSearch.value);
    quoteCards.forEach(card => { card.hidden = !normalize(card.textContent).includes(query); });
  });
  section("filters"); updateRows();
})();
"""


def render_script_report_html(report: dict) -> str:
    rows = report["rows"]
    detail_rows = [(index, row) for index, row in enumerate(rows) if row["sourcePaths"]]
    detail_indexes = {row["materialId"]: index for index, row in detail_rows}
    columns = _metric_columns(rows)
    selection = report.get("selection") or {}
    counts = report["counts"]
    primary, secondary = _formula_counts([row for _, row in detail_rows])
    formula_total = len(set().union(*primary.values())) if primary else 0
    quotes = _quotes(detail_rows)
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; script-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">',
        '<title>云图素材脚本数据工作台</title><style>', _STYLE, '</style></head><body><main>',
        '<h1>云图素材脚本数据工作台</h1>',
        '<p class="subtitle">第一阶段 · ', _e(selection.get("industry")), ' · 榜单 ',
        str(counts["ranking"]), ' 条 · 已获详情 ', str(counts["detail"]), ' 条</p>',
        '<div class="summary-cards">'
    ]
    for label, key in (("榜单素材", "ranking"), ("已获详情", "detail"), ("平台脚本", "platform"),
                       ("Agent 音频转写", "agent_audio"), ("详情无脚本", "unavailable")):
        parts.append('<div><span>' + label + '</span><strong>' + str(counts[key]) + '</strong></div>')
    parts.append('</div><nav class="section-nav" aria-label="报告部分">')
    for key, label in (("filters", "素材筛选条件"), ("materials", "素材榜单"), ("scripts", "脚本内容公式"),
                       ("single-material", "单素材脚本"), ("quote-library", "脚本片段库")):
        parts.append('<a href="#' + key + '-panel" data-section="' + key + '">' + label + '</a>')
    parts.append('</nav><noscript><p class="panel">浏览器未启用 JavaScript；全部分区和素材保持展开，可以直接阅读。</p></noscript>')
    period = selection.get("period")
    period = period if isinstance(period, dict) else {}
    material_list = selection.get("materialList")
    material_list = material_list if isinstance(material_list, dict) else {}
    parts.append('<section id="filters-panel" class="panel" data-panel="filters"><h2>素材筛选条件</h2><dl class="context">')
    context = [
        ("行业", selection.get("industry")),
        ("取数周期", _PERIOD_LABELS.get(period.get("date_type") or period.get("type"), period.get("date_type") or period.get("type"))),
        ("开始日期", period.get("custom_start_date") or period.get("start_date")),
        ("结束日期", period.get("custom_end_date") or period.get("end_date")),
        ("榜单口径", _RANKING_LABELS.get(selection.get("rankingMetric"), selection.get("rankingMetric"))),
        ("脚本素材数 TopN", selection.get("targetTopN")),
        ("本地筛选条件", json.dumps(material_list.get("filters"), ensure_ascii=False) if material_list.get("filters") else "无"),
        ("本地排序", json.dumps(material_list.get("sort"), ensure_ascii=False) if material_list.get("sort") else "未提供"),
    ]
    for label, value in context:
        parts.append('<div><dt>' + label + '</dt><dd>' + _e(value) + '</dd></div>')
    parts.append('</dl></section><section id="materials-panel" data-panel="materials"><div class="toolbar" id="report-controls" hidden><input id="material-search" type="search" placeholder="输入素材标题或 ID" aria-label="搜索素材标题或 ID"><details><summary>自定义指标</summary><div class="metric-picker">')
    for column in columns:
        parts.append('<label><input type="checkbox" data-metric-choice="' + _e(column["key"]) + '">' + _e(column["label"]) + '</label>')
    if not columns:
        parts.append('<p class="muted">本次榜单未返回指标。</p>')
    parts.append('</div></details></div><p class="count-note" id="visible-count">共 ' + str(len(rows)) + ' 条</p><div class="table-wrap"><table aria-label="素材榜单"><thead><tr><th class="material-name-column" scope="col">素材名称</th>')
    for column in columns:
        parts.append('<th scope="col" data-col="' + _e(column["key"]) + '"><button type="button" data-sort="' + _e(column["key"]) + '">' + _e(column["label"]) + '<span></span></button></th>')
    parts.append('</tr></thead><tbody id="material-rows">')
    for index, row in enumerate(rows):
        title = row["title"] or "素材名称未提供"
        material_id = row["materialId"]
        parts.append('<tr data-row="' + str(index) + '" data-search="' + _e(title + " " + material_id) + '"><td class="material-name-column">')
        if material_id in detail_indexes:
            parts.append('<a class="material-title" href="#material-' + str(index) + '" data-open="' + str(index) + '" title="' + _e(title) + '">' + _e(title) + '</a>')
        else:
            parts.append('<span class="material-title" title="' + _e(title) + '">' + _e(title) + '</span>')
        parts.append('<div class="material-id">素材 ID：' + _e(material_id) + '</div></td>')
        metrics = row.get("metrics") or {}
        for column in columns:
            value = metrics.get(column["key"])
            numeric = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
            parts.append('<td data-metric="' + _e(column["key"]) + '" data-value="' + _e(numeric) + '">' + _e(value) + '</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div><div class="empty" id="no-results" hidden>没有匹配素材。</div><div class="pager" id="table-pager" hidden><span id="table-range"></span><div><button type="button" id="previous-page">上一页</button><span id="page-number"></span><button type="button" id="next-page">下一页</button></div></div></section>')
    parts.append('<section id="scripts-panel" data-panel="scripts"><div class="panel"><h2>脚本内容公式</h2><p class="muted">' + str(formula_total) + ' 个素材有可读公式标签；仅统计平台事实中的显式标签，转写文本不推断公式。</p></div><div class="formula-grid">')
    if primary:
        max_count = max(len(ids) for ids in primary.values())
        for label, ids in sorted(primary.items(), key=lambda item: (-len(item[1]), item[0])):
            parts.append('<div class="formula-chart"><h3>' + _e(label) + '</h3><p class="muted">一级公式 · 覆盖 ' + str(len(ids)) + ' 个素材</p><div class="bar-row"><span>' + _e(label) + '</span><span class="bar-track"><span class="bar-fill" style="width:' + str(round(100*len(ids)/max_count, 2)) + '%"></span></span><strong>' + str(len(ids)) + '</strong></div>')
            children = secondary.get(label, {})
            if children:
                parts.append('<h3>二级内容公式</h3><ul class="formula-children">')
                for child, child_ids in sorted(children.items(), key=lambda item: (-len(item[1]), item[0])):
                    parts.append('<li>' + _e(child) + ' · ' + str(len(child_ids)) + ' 个素材</li>')
                parts.append('</ul>')
            else:
                parts.append('<p class="muted">没有可读的二级标签。</p>')
            parts.append('</div>')
    else:
        parts.append('<div class="panel empty">本次详情没有可读的内容公式标签。</div>')
    parts.append('</div></section><section id="single-material-panel" data-panel="single-material"><div class="single-shell"><aside class="selector"><h2>单素材脚本</h2>')
    for index, row in detail_rows:
        parts.append('<a class="material-option" data-open="' + str(index) + '" href="#material-' + str(index) + '">' + _e(row["rank"]) + '. ' + _e(row["title"]) + '<span class="material-id">' + _e(row["materialId"]) + '</span></a>')
    parts.append('</aside><div>')
    for index, row in detail_rows:
        transcript = row["audioTranscript"] if isinstance(row["audioTranscript"], dict) else {}
        source_label = {"platform": "平台原文", "agent_audio": "Agent 音频转写", "unavailable": "无可用脚本"}.get(row["scriptSource"], "未提供")
        parts.append('<article class="material-card" id="material-' + str(index) + '" data-card="' + str(index) + '"><p class="eyebrow">素材排名 ' + _e(row["rank"]) + '</p><h2>' + _e(row["title"]) + '</h2><p class="material-id">素材 ID：' + _e(row["materialId"]) + ' · 脚本来源：' + source_label + '</p><h3>核心指标</h3><div class="metric-grid">')
        metrics = row.get("metrics") or {}
        for column in columns:
            if column["key"] in metrics:
                parts.append('<div class="metric-card"><span>' + _e(column["label"]) + '</span><strong>' + _e(metrics[column["key"]]) + '</strong></div>')
        if not metrics:
            parts.append('<p class="muted">未提供指标。</p>')
        parts.append('</div><h3>完整脚本</h3><div class="script-text">' + (_e(row["scriptText"]) if row["scriptText"] else '无可用脚本文本') + '</div>')
        if row["scriptSource"] == "agent_audio":
            parts.append('<p class="source-note">音频转写无可靠时间码，不映射高光帧或画面文字。</p>')
        parts.append('<details class="source-copy"><summary>查看分层来源文本</summary><h3>平台原文</h3><div class="script-text">' + (_e(row["platformScriptText"]) if row["platformScriptText"] else '未提供') + '</div><h3>Agent 音频转写</h3><div class="script-text">' + (_e(transcript.get("text")) if transcript.get("text") else '未提供') + '</div></details></article>')
    if not detail_rows:
        parts.append('<div class="panel empty">本次没有可展示的素材详情。</div>')
    parts.append('</div></div></section><section id="quote-library-panel" data-panel="quote-library"><div class="panel"><h2>脚本片段库</h2><p class="muted">仅展示内容公式来源中能在完整脚本中精确命中的原句；不自动切分或补写转写文本。</p><input type="search" id="quote-search" placeholder="搜索原句或素材名称" aria-label="搜索脚本片段" hidden></div><div class="quote-grid">')
    for item in quotes:
        row = item["row"]
        parts.append('<article class="quote-card" data-quote><blockquote>' + _e(item["text"]) + '</blockquote><p class="eyebrow">素材排名 ' + _e(row["rank"]) + '</p><a data-open="' + str(item["index"]) + '" href="#material-' + str(item["index"]) + '">' + _e(row["title"]) + '</a></article>')
    if not quotes:
        parts.append('<div class="panel empty">没有可核对的原句片段；完整脚本可在“单素材脚本”中查看。</div>')
    parts.append('</div></section><p class="footer">云图素材脚本数据 · 指标按本次取数口径展示；脚本与指标的关联不代表因果结论。用户提出分析问题后才进入第二阶段。</p></main><script>')
    parts.append(_SCRIPT)
    parts.append('</script></body></html>')
    return "".join(parts)
