#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日看门道 · 门户构建器
读取情报知识库 markdown（日报/专题/索引），生成产品化单文件静态站 docs/index.html

用法：
  python3 build.py                                  # 默认读取 ../AI情报知识库，输出 ./docs
  python3 build.py --content /path/知识库 --out ./docs
"""
import argparse
import html
import re
from pathlib import Path

# ===== 站点配置 =====
SITE_TITLE = "每日看门道"
SITE_SUB = "AI 资讯五层解码 · 团队共读 · 深挖转化"
SHEET_URL = "https://docs.qq.com/sheet/DV0tCaEFGRFl6UUFs?_fid=WKBhAFDYzQAl"
SHOW_TOPICS = False  # 2026-09-13 用户要求：专题页对外隐藏（分享对象看不懂），数据保留，改 True 即恢复

LAYERS = [
    ("说人话", "l-a", "一句话讲清发生了什么"),
    ("概念扫盲", "l-b", "术语翻译成大白话"),
    ("商业逻辑", "l-c", "谁掏钱、谁受益、护城河"),
    ("行业信号", "l-d", "意味着什么趋势"),
    ("与我何干", "l-e", "对业务的启发与动作"),
]

DIVE_PROMPT = """请对以下 AI 行业资讯做深度调研并输出五层解读：
【资讯】{ctx}
【要求】
1) 先联网检索：官方公告 + 媒体报道 + 行业分析；
2) 按五层输出：说人话 / 概念扫盲（大白话，照顾非技术读者）/ 商业逻辑（谁掏钱、谁受益、护城河）/ 行业信号（意味着什么趋势）/ 与读者业务的关联；
3) 给出 1-2 个可落地的产品动作建议；
4) 末尾列出参考来源链接。"""

# ===== 迷你 markdown 渲染 =====
def md_inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"(https?://[^\s<>）)】]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    return s

def md_to_html(md):
    out, in_list, in_code, code_buf, para, tbl = [], False, False, [], [], []

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(md_inline(x) for x in para) + "</p>")
            para = []

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def flush_tbl():
        nonlocal tbl
        if tbl:
            rows = [r for r in tbl if not re.match(r"^\|[\s:\-|]+\|$", r.strip())]
            h = ["<table>"]
            for i, r in enumerate(rows):
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                tag = "th" if i == 0 else "td"
                h.append("<tr>" + "".join("<%s>%s</%s>" % (tag, md_inline(c), tag) for c in cells) + "</tr>")
            h.append("</table>")
            out.append("\n".join(h))
            tbl = []

    for line in md.split("\n"):
        raw = line.rstrip()
        if raw.strip().startswith("```"):
            flush_tbl()
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf, in_code = [], False
            else:
                flush_para(); flush_list(); in_code = True
            continue
        if in_code:
            code_buf.append(raw); continue
        s = raw.strip()
        if not s:
            flush_para(); flush_list(); flush_tbl(); continue
        if s.startswith(">"):
            s = s.lstrip("> ").strip()
            if not s:
                continue
        if s.startswith("|"):
            flush_para(); flush_list()
            tbl.append(s)
            continue
        if s == "---":
            flush_para(); flush_list(); flush_tbl(); out.append("<hr>"); continue
        m = re.match(r"^(#{2,5})\s+(.*)$", s)
        if m:
            flush_para(); flush_list(); flush_tbl()
            lv = len(m.group(1)) + 2
            out.append("<h%d>%s</h%d>" % (lv, md_inline(m.group(2)), lv))
            continue
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            flush_para(); flush_tbl()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append("<li>" + md_inline(m.group(1)) + "</li>")
            continue
        flush_tbl()
        para.append(s)
    flush_para(); flush_list(); flush_tbl()
    return "\n".join(out)

# ===== 日报解析（结构化五层）=====
ITEM_RE = re.compile(r"^(\d+)\.\s*(.+)$")
LABEL_RE = re.compile(r"\*\*(说人话|概念扫盲|商业逻辑|行业信号|与我何干|标签)\*\*\s*[：:]\s*")

def parse_item_body(body):
    """把条目正文拆成：intro / 五层 dict / tags / extra(深挖版等额外markdown)"""
    extra = ""
    m = re.search(r"^####", body, flags=re.M)
    if m:
        extra = body[m.start():].strip()
        body = body[:m.start()].strip()
    layers, tags, intro = {}, [], ""
    matches = list(LABEL_RE.finditer(body))
    if matches:
        intro = body[:matches[0].start()].strip()
        for i, mm in enumerate(matches):
            name = mm.group(1)
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            val = body[mm.end():end].strip()
            if name == "标签":
                tags = [t.strip() for t in re.split(r"[、，,/]+", val) if t.strip()]
            else:
                layers[name] = val
    else:
        intro = body
    return intro, layers, tags, extra

def parse_daily(path):
    text = path.read_text(encoding="utf-8")
    date = path.stem
    overview, items, cur, mode = "", [], None, None
    for line in text.split("\n"):
        m = re.match(r"^(#{2,3})\s+(.*)$", line.strip())
        if m:
            head = m.group(2).strip()
            im = ITEM_RE.match(head)
            if head.startswith("今日总览"):
                mode = "overview"; cur = None; continue
            if head.startswith("归档确认"):
                mode = "skip"; cur = None; continue
            if im:
                cur = {"num": im.group(1), "title": im.group(2).strip(), "body": []}
                items.append(cur); mode = "item"; continue
            if "批" in head and "·" in head:
                items.append({"divider": True, "title": head, "num": "", "body": []})
                cur = None; mode = "divider"; continue
            mode = None; cur = None; continue
        if mode == "overview":
            overview += line + "\n"
        elif mode == "item" and cur is not None:
            cur["body"].append(line)
    for it in items:
        it["body"] = "\n".join(it["body"]).strip()
    return {"date": date, "overview": overview.strip(), "items": items}

# ===== 卡片渲染 =====
def card_html(date, it):
    if it.get("divider"):
        return '<div class="divider">%s</div>' % html.escape(it["title"])
    intro, layers, tags, extra = parse_item_body(it["body"])
    anchor = "a-%s-%s" % (date, it["num"])

    ctx_parts = [date + " · " + it["title"]]
    if layers.get("说人话"):
        ctx_parts.append("发生了什么：" + layers["说人话"])
    if layers.get("商业逻辑"):
        ctx_parts.append("商业逻辑：" + layers["商业逻辑"][:200])
    ctx = html.escape("\n".join(ctx_parts), quote=True)

    tag_html = "".join('<span class="tag">%s</span>' % html.escape(t) for t in tags)

    layer_html = ""
    for name, cls, hint in LAYERS:
        if layers.get(name):
            layer_html += (
                '<div class="layer %s"><div class="layer-head"><span class="layer-label">%s</span>'
                '<span class="layer-hint">%s</span></div><div class="layer-body">%s</div></div>'
            ) % (cls, name, hint, md_to_html(layers[name]))

    intro_html = '<div class="card-intro">%s</div>' % md_to_html(intro) if intro else ""
    extra_html = ('<details class="extra"><summary>深挖版（联网调研增量）</summary>%s</details>'
                  % md_to_html(extra)) if extra else ""

    return (
        '<article class="card" id="{anchor}" data-date="{date}" data-title="{title}">'
        '<div class="card-head"><span class="num">{num}</span><h3>{title}</h3></div>'
        '{tags}{intro}<div class="layers">{layers}</div>{extra}'
        '<div class="card-actions">'
        '<button class="btn btn-primary" data-dive="{ctx}">立即深挖</button>'
        '<button class="btn btn-ghost" data-mine="{ctx}">按我的行业看「与我何干」</button>'
        '<button class="btn btn-ghost" onclick="openNeed()">转化需求</button>'
        '</div></article>'
    ).format(
        anchor=anchor, date=date, title=html.escape(it["title"], quote=True),
        num=html.escape(it["num"]), tags=tag_html, intro=intro_html,
        layers=layer_html, extra=extra_html, ctx=ctx, sheet=SHEET_URL,
    )

def day_block(day, open_=False):
    cards = "\n".join(card_html(day["date"], it) for it in day["items"])
    n = len([i for i in day["items"] if not i.get("divider")])
    ov = ""
    if day["overview"]:
        ov = '<div class="overview"><div class="ov-title">今日总览</div>%s</div>' % md_to_html(day["overview"])
    if open_:
        return '<section class="day" id="day-%s">%s%s</section>' % (day["date"], ov, cards)
    return ('<details class="day" id="day-%s"><summary>'
            '<span class="day-date">%s</span><span class="day-count">%d 条</span>'
            '</summary><div class="day-body">%s%s</div></details>') % (day["date"], day["date"], n, ov, cards)

# ===== 索引解析 =====
THEME_RE = re.compile(r"^##\s+(.+)$")
ENTRY_RE = re.compile(r"^-\s+\[(\d{2}-\d{2}|W\d+)\]\s*(.+)$")

def parse_index(path):
    themes, cur = [], None
    for line in path.read_text(encoding="utf-8").split("\n"):
        s = line.strip()
        m = THEME_RE.match(s)
        if m:
            cur = {"name": m.group(1).strip(), "entries": []}
            themes.append(cur); continue
        m = ENTRY_RE.match(s)
        if m and cur is not None:
            cur["entries"].append({"mmdd": m.group(1), "text": m.group(2).strip()})
    return themes

def themes_html(themes, week_anchor=""):
    chips = '<button class="chip active" data-theme="all">全部</button>'
    for t in themes:
        chips += '<button class="chip" data-theme="%s">%s</button>' % (
            html.escape(t["name"], quote=True), html.escape(t["name"]))
    groups = ""
    for t in themes:
        lis = ""
        for e in sorted(t["entries"], key=lambda x: x["mmdd"], reverse=True):
            if e["mmdd"].startswith("W"):
                # 周报条目：跳到历史页顶部的周报区块
                lis += ('<li><a class="t-link" data-day="%s"><span class="t-date">%s</span>%s</a></li>'
                        % (week_anchor, e["mmdd"], html.escape(e["text"])))
            else:
                day = "2026-" + e["mmdd"]
                lis += ('<li><a class="t-link" data-day="%s"><span class="t-date">%s</span>%s</a></li>'
                        % (day, e["mmdd"], html.escape(e["text"])))
        groups += ('<div class="theme-group" data-theme="%s"><div class="theme-name">%s'
                   '<span class="theme-n">%d</span></div><ul class="t-list">%s</ul></div>'
                   % (html.escape(t["name"], quote=True), html.escape(t["name"]), len(t["entries"]), lis))
    return '<div class="chips">%s</div>%s' % (chips, groups)

# ===== 主流程 =====
def main():
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--content", default=str(here.parent / "AI情报知识库"))
    ap.add_argument("--out", default=str(here / "docs"))
    args = ap.parse_args()
    base, out_dir = Path(args.content), Path(args.out)

    days = [parse_daily(p) for p in sorted((base / "日报").glob("*.md"))]
    days = [d for d in days if d["items"]]
    days.sort(key=lambda d: d["date"], reverse=True)
    latest, rest = days[0], days[1:]

    total_items = sum(len([i for i in d["items"] if not i.get("divider")]) for d in days)

    topic_dir = base / "专题"
    topics = ""
    if topic_dir.exists():
        for p in sorted(topic_dir.glob("*.md")):
            topics += ('<details class="day"><summary><span class="day-date">%s</span></summary>'
                       '<div class="day-body">%s</div></details>'
                       % (html.escape(p.stem), md_to_html(p.read_text(encoding="utf-8"))))
    if not topics:
        topics = '<p class="muted">暂无专题内容</p>'

    idx = base / "索引.md"
    theme_data = parse_index(idx) if idx.exists() else []

    # ===== 周报（最新页置顶展示 + 历史页存档）=====
    week_dir = base / "周报"
    weeks_html, week_anchor, week_today = "", "", ""
    if week_dir.exists():
        wfiles = sorted(week_dir.glob("*.md"))
        for p in wfiles:
            wid = "day-week-" + p.stem
            body = md_to_html(p.read_text(encoding="utf-8"))
            weeks_html += ('<details class="day week" id="%s"><summary>'
                           '<span class="day-date">📅 周报 · %s</span><span class="day-count">本周复盘</span>'
                           '</summary><div class="day-body week-body">%s</div></details>'
                           % (wid, html.escape(p.stem), body))
        if wfiles:
            week_anchor = "week-" + wfiles[-1].stem
            # 最新周报若新于最新日报，作为"最新"页主内容展开显示
            if wfiles[-1].stem[:10] > latest["date"]:
                body = md_to_html(wfiles[-1].read_text(encoding="utf-8"))
                week_today = ('<section class="week-today">'
                              '<div class="week-banner">📅 本周复盘 · %s</div>'
                              '<div class="week-body">%s</div></section>'
                              % (html.escape(wfiles[-1].stem), body))

    theme_html = themes_html(theme_data, week_anchor) if theme_data else '<p class="muted">暂无索引</p>'

    board = (
        '<div class="panel"><div class="panel-title">需求池怎么玩</div>'
        '<p>看到任何一条资讯有启发，点条目下方的「转化需求」，在腾讯文档「需求池」子表填一行：'
        '对你的启发、建议落地动作、期望优先级。每周五异步评审一轮，状态回填在表里。</p>'
        '<p>状态流转：<strong>线索 → 评审中 → 已采纳 / 已搁置</strong>，被采纳的需求署名提议人。</p></div>'
        '<div class="panel"><div class="panel-title">情报众筹</div>'
        '<p>刷到值得解读的 AI 资讯，点右上角「投稿」，在「情报投稿」子表填一行：标题 + 链接或原文 + 来源渠道。'
        '主编消化后做五层解读并归档进日报。</p></div>'
        '<p class="center"><a class="btn btn-primary btn-big" href="' + SHEET_URL + '" target="_blank" rel="noopener">打开「情报众筹与需求池」表格</a></p>'
        '<p class="muted center">表格内含两个子表：情报投稿 / 需求池，在文档底部切换</p>'
    )

    today_html = week_today + (
        '<div class="latest-mark" style="margin-top:30px">最近一篇日报 · <b>%s</b></div>' % latest["date"]
        if week_today else ""
    ) + day_block(latest, open_=True)

    data = {
        "title": SITE_TITLE, "sub": SITE_SUB, "sheet": SHEET_URL,
        "date": ("周报 " + wfiles[-1].stem) if week_today else latest["date"], "today": today_html,
        "archive": weeks_html + ("\n".join(day_block(d) for d in rest) or '<p class="muted">暂无历史</p>'),
        "topics": topics, "themes": theme_html, "board": board,
        "ndays": str(len(days)), "nitems": str(total_items), "nthemes": str(len(theme_data)),
        "dive_prompt": DIVE_PROMPT.replace("{ctx}", "__CTX__"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")
    print("OK ->", out_dir / "index.html", "| days:", len(days), "| items:", total_items)

def render(d):
    nav_topics = ('\n<button data-page="page-topics" onclick="switchPage(\'page-topics\',this)">专题</button>' if SHOW_TOPICS else "")
    page_topics = ('\n<div class="page" id="page-topics">%s</div>' % d["topics"] if SHOW_TOPICS else "")
    return PAGE.replace("__CSS__", CSS).replace("__JS__", JS) \
        .replace("__NAV_TOPICS__", nav_topics).replace("__PAGE_TOPICS__", page_topics) \
        .replace("__TITLE__", d["title"]).replace("__SUB__", d["sub"]) \
        .replace("__SHEET__", d["sheet"]).replace("__DATE__", d["date"]) \
        .replace("__NDAYS__", d["ndays"]).replace("__NITEMS__", d["nitems"]) \
        .replace("__NTHEMES__", d["nthemes"]).replace("__TODAY__", d["today"]) \
        .replace("__ARCHIVE__", d["archive"]).replace("__TOPICS__", d["topics"]) \
        .replace("__THEMES__", d["themes"]).replace("__BOARD__", d["board"]) \
        .replace("__DIVEPROMPT__", js_str(d["dive_prompt"]))

def js_str(s):
    return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

CSS = r"""
:root{color-scheme:light}
* {box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:70px}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
background:#faf9f6;color:#22242a;line-height:1.75;-webkit-font-smoothing:antialiased}
a{color:#0f766e}
body[data-theme="dark"]{color-scheme:dark;background:#141412;color:#e9e9e5}
body[data-theme="dark"] a{color:#5DCAA5}

/* ===== 顶部导航 ===== */
nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:6px;padding:10px 16px;
background:rgba(250,249,246,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
border-bottom:1px solid #ebe9e2}
body[data-theme="dark"] nav{background:rgba(20,20,18,.85);border-bottom-color:#2c2c26}
.brand{font-weight:700;font-size:15px;margin-right:8px;white-space:nowrap}
.brand em{font-style:normal;color:#0f766e}
body[data-theme="dark"] .brand em{color:#5DCAA5}
.tabs{display:flex;gap:2px;overflow-x:auto;flex:1}
.tabs button{border:none;background:none;font:inherit;font-size:13.5px;color:#666;padding:7px 12px;
border-radius:8px;cursor:pointer;white-space:nowrap}
.tabs button.active{background:#22242a;color:#fff;font-weight:500}
body[data-theme="dark"] .tabs button{color:#999}
body[data-theme="dark"] .tabs button.active{background:#e9e9e5;color:#141412}
.search-wrap{position:relative;flex:0 0 auto}
#search{width:150px;font:inherit;font-size:13px;padding:7px 12px;border:1px solid #ddd;border-radius:9px;
background:#fff;color:inherit;outline:none;transition:width .2s}
#search:focus{width:210px;border-color:#0f766e}
body[data-theme="dark"] #search{background:#22221e;border-color:#3a3a32}
.search-results{position:absolute;right:0;top:42px;width:320px;max-height:60vh;overflow-y:auto;
background:#fff;border:1px solid #e5e3da;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.12);
display:none;padding:6px}
body[data-theme="dark"] .search-results{background:#22221e;border-color:#3a3a32}
.search-results.show{display:block}
.sr-item{display:block;padding:9px 12px;border-radius:8px;cursor:pointer;font-size:13px;line-height:1.5}
.sr-item:hover{background:#f2f1ea}
body[data-theme="dark"] .sr-item:hover{background:#2e2e28}
.sr-item .d{color:#999;font-size:11.5px;margin-right:6px}
.sr-empty{padding:14px;text-align:center;color:#999;font-size:13px}
.icon-btn{border:1px solid #ddd;background:#fff;border-radius:9px;width:34px;height:34px;cursor:pointer;
font-size:15px;flex:0 0 auto;color:#555}
body[data-theme="dark"] .icon-btn{background:#22221e;border-color:#3a3a32;color:#bbb}

/* ===== Hero ===== */
.hero{max-width:780px;margin:0 auto;padding:44px 20px 8px}
.hero-kicker{display:inline-block;font-size:12px;letter-spacing:2px;color:#0f766e;border:1px solid #99d5c9;
border-radius:99px;padding:3px 12px;margin-bottom:14px}
body[data-theme="dark"] .hero-kicker{color:#5DCAA5;border-color:#1d4a41}
.hero h1{font-size:34px;font-weight:800;letter-spacing:1px;line-height:1.3;
background:linear-gradient(120deg,#0f766e 20%,#4f46e5 80%);
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hero .sub{margin-top:8px;color:#777;font-size:14px}
body[data-theme="dark"] .hero .sub{color:#999}
.stats{display:flex;gap:28px;margin-top:20px;flex-wrap:wrap}
.stat b{font-size:24px;font-weight:700;display:block;line-height:1.2}
.stat span{font-size:12px;color:#999}
.hero-actions{margin-top:20px;display:flex;gap:10px;flex-wrap:wrap}
.latest-mark{margin-top:26px;font-size:13px;color:#999;display:flex;align-items:center;gap:8px}
.latest-mark::after{content:"";flex:1;height:1px;background:#e5e3da}
body[data-theme="dark"] .latest-mark::after{background:#33332c}
.latest-mark b{color:#22242a;font-size:14px}
body[data-theme="dark"] .latest-mark b{color:#e9e9e5}

/* ===== 主体 ===== */
main{max-width:780px;margin:0 auto;padding:8px 20px 90px}
.page{display:none}.page.active{display:block}

/* ===== 卡片 ===== */
.card{background:#fff;border:1px solid #eceae2;border-radius:18px;padding:22px;margin:18px 0;
box-shadow:0 1px 2px rgba(0,0,0,.03);transition:box-shadow .2s,transform .2s}
.card:hover{box-shadow:0 8px 30px rgba(0,0,0,.06)}
body[data-theme="dark"] .card{background:#1e1e1a;border-color:#2e2e27}
.card-head{display:flex;gap:12px;align-items:flex-start}
.card-head .num{flex:0 0 auto;background:linear-gradient(135deg,#0f766e,#14b8a6);color:#fff;
font-size:12px;font-weight:600;border-radius:8px;padding:3px 9px;margin-top:3px}
.card-head h3{font-size:17px;font-weight:650;line-height:1.6;flex:1}
.tag{display:inline-block;font-size:11px;color:#7c3aed;background:#f3effe;border-radius:99px;
padding:2px 10px;margin:10px 8px 0 0}
body[data-theme="dark"] .tag{background:#2b2440;color:#c4b5fd}
.card-intro{font-size:14px;margin-top:10px;color:#444}
body[data-theme="dark"] .card-intro{color:#ccc}

/* 五层结构 */
.layers{margin-top:14px;display:flex;flex-direction:column;gap:10px}
.layer{border-radius:12px;padding:12px 14px;border:1px solid transparent}
.layer-head{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}
.layer-label{font-size:12.5px;font-weight:700;white-space:nowrap}
.layer-hint{font-size:11px;color:#aaa;font-weight:400}
.layer-body{font-size:13.8px}
.layer-body p{margin:4px 0}
.layer-body ul{margin:4px 0 4px 18px}
.l-a{background:#fdf6ec;border-color:#f3e3c3}.l-a .layer-label{color:#b45309}
.l-b{background:#eff5ff;border-color:#d3e2f8}.l-b .layer-label{color:#1d4ed8}
.l-c{background:#ecf8f4;border-color:#c9ebe0}.l-c .layer-label{color:#0f766e}
.l-d{background:#f4f1fd;border-color:#e0d8f8}.l-d .layer-label{color:#6d28d9}
.l-e{background:#fdf1ef;border-color:#f6d9d2}.l-e .layer-label{color:#c2410c}
body[data-theme="dark"] .l-a{background:#2b2416;border-color:#443a22}
body[data-theme="dark"] .l-b{background:#1a2438;border-color:#2a3a58}
body[data-theme="dark"] .l-c{background:#142822;border-color:#1f4438}
body[data-theme="dark"] .l-d{background:#241d3a;border-color:#3a3058}
body[data-theme="dark"] .l-e{background:#33201a;border-color:#523227}
body[data-theme="dark"] .layer-hint{color:#777}

.extra{margin-top:12px;border:1px dashed #d8d5c8;border-radius:12px;padding:10px 14px;font-size:13.5px}
body[data-theme="dark"] .extra{border-color:#3a3a30}
.extra summary{cursor:pointer;font-weight:600;font-size:13px;color:#6d28d9}
body[data-theme="dark"] .extra summary{color:#c4b5fd}
.extra h4,.extra h5{font-size:13.5px;margin:12px 0 4px}
.extra p,.extra ul{margin:6px 0}
.extra ul{margin-left:18px}
.extra code{background:#f0efe9;border-radius:4px;padding:1px 5px;font-size:12px}
body[data-theme="dark"] .extra code{background:#2e2e28}

.card-actions{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:6px;border-radius:10px;padding:8px 15px;font:inherit;
font-size:13px;cursor:pointer;border:1px solid transparent;transition:filter .15s}
.btn:hover{filter:brightness(1.06)}
.btn-primary{background:linear-gradient(135deg,#0f766e,#0d9488);color:#fff;font-weight:500}
.btn-ghost{background:transparent;border-color:#ddd;color:#555}
body[data-theme="dark"] .btn-ghost{border-color:#45453c;color:#bbb}
.btn-big{font-size:15px;padding:12px 24px}

/* 总览/面板 */
.overview{background:linear-gradient(135deg,#ecf8f4,#f4f1fd);border-radius:14px;
padding:14px 18px;margin:16px 0;font-size:14px}
body[data-theme="dark"] .overview{background:linear-gradient(135deg,#142822,#241d3a)}
.ov-title,.panel-title{font-weight:700;font-size:13.5px;margin-bottom:6px}
.overview p{margin:5px 0}
.panel{background:#fff;border:1px solid #eceae2;border-radius:14px;padding:16px 18px;margin:14px 0;font-size:14px}
body[data-theme="dark"] .panel{background:#1e1e1a;border-color:#2e2e27}
.panel p{margin:6px 0}
.center{text-align:center}
.muted{color:#999;font-size:13px}

/* 周报（最新页） */
.week-today{background:#fff;border:1px solid #eceae2;border-radius:18px;margin:18px 0;overflow:hidden;
box-shadow:0 1px 2px rgba(0,0,0,.03)}
body[data-theme="dark"] .week-today{background:#1e1e1a;border-color:#2e2e27}
.week-banner{background:linear-gradient(135deg,#0f766e,#14b8a6);color:#fff;font-weight:650;
font-size:15px;padding:13px 20px}
.week-body{padding:6px 22px 20px;font-size:14px}
.week-body h3,.week-body h4,.week-body h5{margin:18px 0 6px;line-height:1.5}
.week-body h3{font-size:16.5px}
.week-body h4{font-size:15px}
.week-body h5{font-size:14px}
.week-body p{margin:8px 0}
.week-body ul{margin:8px 0 8px 20px}
.week-body li{margin:3px 0}
.week-body table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}
.week-body th,.week-body td{border:1px solid #e5e3da;padding:7px 10px;text-align:left;vertical-align:top}
.week-body th{background:#f2f1ea;font-weight:600}
body[data-theme="dark"] .week-body th,body[data-theme="dark"] .week-body td{border-color:#3a3a32}
body[data-theme="dark"] .week-body th{background:#2b2b25}
.week-body hr{border:none;border-top:1px solid #eceae2;margin:16px 0}
body[data-theme="dark"] .week-body hr{border-top-color:#2e2e27}
.week-body a{word-break:break-all}

/* 历史（折叠天） */
.divider{color:#999;font-size:12.5px;font-weight:600;margin:22px 0 2px;letter-spacing:1px}
details.day{background:#fff;border:1px solid #eceae2;border-radius:16px;margin:12px 0;overflow:hidden}
body[data-theme="dark"] details.day{background:#1e1e1a;border-color:#2e2e27}
details.day summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;
align-items:center;padding:15px 20px}
details.day summary::-webkit-details-marker{display:none}
.day-date{font-weight:650;font-size:15px}
.day-count{font-size:12px;color:#999;background:#f2f1ea;border-radius:99px;padding:2px 10px}
body[data-theme="dark"] .day-count{background:#2b2b25}
.day-body{padding:0 20px 8px}
.day-body .card{border:none;box-shadow:none;border-radius:0;border-top:1px solid #f0eee6;margin:0;padding:18px 0}
body[data-theme="dark"] .day-body .card{border-top-color:#2e2e27;background:transparent}
.flash{animation:flash 1.6s ease}
@keyframes flash{0%{background:#fff7d6}100%{background:transparent}}

/* 分类索引 */
.chips{display:flex;gap:8px;overflow-x:auto;padding:14px 0 6px;position:sticky;top:55px;
background:#faf9f6;z-index:5}
body[data-theme="dark"] .chips{background:#141412}
.chip{flex:0 0 auto;border:1px solid #ddd;background:#fff;border-radius:99px;padding:6px 16px;
font:inherit;font-size:13px;cursor:pointer;color:#555}
.chip.active{background:#22242a;color:#fff;border-color:#22242a}
body[data-theme="dark"] .chip{background:#22221e;border-color:#3a3a32;color:#999}
body[data-theme="dark"] .chip.active{background:#e9e9e5;color:#141412}
.theme-group{margin-top:18px}
.theme-name{font-weight:700;font-size:15px;display:flex;align-items:center;gap:8px}
.theme-n{font-size:11px;color:#999;background:#f2f1ea;border-radius:99px;padding:1px 8px;font-weight:400}
body[data-theme="dark"] .theme-n{background:#2b2b25}
.t-list{list-style:none;margin-top:8px}
.t-list li{margin:2px 0}
.t-link{display:flex;gap:10px;padding:9px 12px;border-radius:10px;cursor:pointer;font-size:13.5px;
line-height:1.6;color:#333}
.t-link:hover{background:#f2f1ea}
body[data-theme="dark"] .t-link{color:#ddd}
body[data-theme="dark"] .t-link:hover{background:#26261f}
.t-date{flex:0 0 auto;color:#0f766e;font-weight:600;font-size:12.5px;margin-top:1px}
body[data-theme="dark"] .t-date{color:#5DCAA5}

/* AI 弹窗 */
.modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;display:none;
align-items:flex-end;justify-content:center}
.modal-mask.show{display:flex}
.modal{background:#fff;width:100%;max-width:720px;max-height:86vh;border-radius:20px 20px 0 0;
display:flex;flex-direction:column;overflow:hidden}
@media(min-width:640px){.modal-mask{align-items:center}.modal{border-radius:20px;max-height:80vh;margin:0 16px}}
body[data-theme="dark"] .modal{background:#1e1e1a}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;
border-bottom:1px solid #eee}
body[data-theme="dark"] .modal-head{border-bottom-color:#2e2e27}
.modal-title{font-weight:650;font-size:15px;line-height:1.5;padding-right:10px}
.modal-close{border:none;background:none;font-size:20px;cursor:pointer;color:#999;line-height:1}
.modal-body{flex:1;overflow-y:auto;padding:16px 20px;font-size:14px}
.msg{margin:12px 0}
.msg-user{background:#f2f1ea;border-radius:12px;padding:10px 14px;font-size:13px;color:#666;white-space:pre-wrap}
body[data-theme="dark"] .msg-user{background:#2b2b25;color:#bbb}
.msg-ai p{margin:8px 0}.msg-ai ul{margin:8px 0 8px 20px}
.msg-ai h4,.msg-ai h5{font-size:14px;margin:12px 0 4px}
.msg-ai code{background:#f0efe9;border-radius:4px;padding:1px 5px;font-size:12.5px}
body[data-theme="dark"] .msg-ai code{background:#2e2e28}
.msg-ai.error{color:#c2410c}
.typing{color:#999;font-size:13px}
.modal-foot{border-top:1px solid #eee;padding:12px 16px;display:flex;gap:8px}
body[data-theme="dark"] .modal-foot{border-top-color:#2e2e27}
.modal-foot input{flex:1;font:inherit;font-size:13.5px;padding:9px 13px;border:1px solid #ddd;
border-radius:10px;background:#fff;color:inherit;outline:none}
body[data-theme="dark"] .modal-foot input{background:#26261f;border-color:#3a3a32}
.form-row{margin:12px 0}
.form-row label{display:block;font-size:12.5px;color:#888;margin-bottom:5px}
.form-row input,.form-row select{width:100%;font:inherit;font-size:13.5px;padding:9px 13px;
border:1px solid #ddd;border-radius:10px;background:#fff;color:#222;outline:none}
body[data-theme="dark"] .form-row input,body[data-theme="dark"] .form-row select{background:#26261f;border-color:#3a3a32;color:#e9e9e5}
.form-tip{font-size:12px;color:#999;line-height:1.7;margin-top:14px}

.toast{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#22242a;color:#fff;
font-size:13px;border-radius:10px;padding:11px 20px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:200}
.toast.show{opacity:1}
footer{max-width:780px;margin:0 auto;padding:0 20px 46px;color:#aaa;font-size:12px;line-height:2}

/* 提示词弹窗（免 Key 通用交互） */
.prompt-box{width:100%;min-height:220px;resize:vertical;border:1px solid #e2e0da;border-radius:10px;
padding:12px 14px;font-size:13px;line-height:1.8;font-family:inherit;background:#fbfaf7;color:inherit}
body[data-theme="dark"] .prompt-box{background:#1c1c1a;border-color:#3a3a36}
.quick-ai{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.quick-ai a{flex:1;min-width:100px;text-align:center;text-decoration:none;font-size:13px;font-weight:600;
border:1px solid #e2e0da;border-radius:10px;padding:9px 6px;color:inherit;background:#fff}
.quick-ai a:hover{border-color:#0f766e;color:#0f766e}
body[data-theme="dark"] .quick-ai a{background:#1c1c1a;border-color:#3a3a36}
body[data-theme="dark"] .quick-ai a:hover{border-color:#5DCAA5;color:#5DCAA5}
.byok-link{display:block;text-align:center;font-size:12px;color:#999;margin-top:12px;cursor:pointer}
.byok-link:hover{color:#0f766e}
"""

JS = r"""
var DIVE_TMPL = `__DIVEPROMPT__`;

/* ===== 主题 ===== */
(function(){
  var t = localStorage.getItem('kd_theme');
  if(!t){ t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark':'light'; }
  document.body.setAttribute('data-theme', t);
})();
function toggleTheme(){
  var t = document.body.getAttribute('data-theme')==='dark' ? 'light':'dark';
  document.body.setAttribute('data-theme', t);
  localStorage.setItem('kd_theme', t);
}

/* ===== 页签 ===== */
function switchPage(id, btn){
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active')});
  document.querySelectorAll('.tabs button').forEach(function(b){b.classList.remove('active')});
  document.getElementById(id).classList.add('active');
  if(btn) btn.classList.add('active');
  window.scrollTo(0,0);
}
function gotoDay(day, anchor){
  var tabBtn = document.querySelector('.tabs button[data-page="page-archive"]');
  switchPage('page-archive', tabBtn);
  var det = document.getElementById('day-'+day);
  if(det && det.tagName==='DETAILS') det.open = true;
  setTimeout(function(){
    var el = anchor ? document.getElementById(anchor) : det;
    if(el){
      el.scrollIntoView({behavior:'smooth', block:'start'});
      el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
    }
  }, 60);
}

/* ===== 搜索 ===== */
var searchIdx = [];
document.querySelectorAll('.card[data-title]').forEach(function(c){
  searchIdx.push({title:c.getAttribute('data-title'), date:c.getAttribute('data-date'), id:c.id});
});
var searchInput = document.getElementById('search');
var searchBox = document.getElementById('searchResults');
searchInput.addEventListener('input', function(){
  var q = this.value.trim();
  if(!q){ searchBox.classList.remove('show'); return; }
  var hits = searchIdx.filter(function(x){return x.title.indexOf(q)>=0}).slice(0, 20);
  if(!hits.length){ searchBox.innerHTML = '<div class="sr-empty">没有匹配的标题</div>'; }
  else{
    searchBox.innerHTML = hits.map(function(h){
      return '<span class="sr-item" data-day="'+h.date+'" data-anchor="'+h.id+'"><span class="d">'+h.date.slice(5)+'</span>'+escapeHtml(h.title)+'</span>';
    }).join('');
  }
  searchBox.classList.add('show');
});
document.addEventListener('click', function(e){
  if(!e.target.closest('.search-wrap')) searchBox.classList.remove('show');
  var it = e.target.closest('.sr-item');
  if(it){ searchBox.classList.remove('show'); searchInput.value=''; gotoDay(it.getAttribute('data-day'), it.getAttribute('data-anchor')); }
  var tl = e.target.closest('.t-link');
  if(tl){ gotoDay(tl.getAttribute('data-day'), null); }
  var chip = e.target.closest('.chip');
  if(chip){
    document.querySelectorAll('.chip').forEach(function(c){c.classList.remove('active')});
    chip.classList.add('active');
    var t = chip.getAttribute('data-theme');
    document.querySelectorAll('.theme-group').forEach(function(g){
      g.style.display = (t==='all' || g.getAttribute('data-theme')===t) ? '' : 'none';
    });
  }
});
function escapeHtml(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}

/* ===== AI 配置（BYOK，仅存本地浏览器）===== */
function aiCfg(){
  return {
    url: (localStorage.getItem('kd_api_url')||'https://api.deepseek.com/v1').replace(/\/+$/,''),
    key: localStorage.getItem('kd_api_key')||'',
    model: localStorage.getItem('kd_api_model')||'deepseek-chat'
  };
}
function openSettings(){
  var c = aiCfg();
  document.getElementById('cfgUrl').value = c.url;
  document.getElementById('cfgKey').value = c.key;
  document.getElementById('cfgModel').value = c.model;
  showModal('settingsModal');
}
function saveSettings(){
  localStorage.setItem('kd_api_url', document.getElementById('cfgUrl').value.trim());
  localStorage.setItem('kd_api_key', document.getElementById('cfgKey').value.trim());
  localStorage.setItem('kd_api_model', document.getElementById('cfgModel').value.trim()||'deepseek-chat');
  hideModal('settingsModal'); toast('已保存，仅存在你的浏览器里');
  var p = window._pendingPrompt; window._pendingPrompt = null;
  if(p){ setTimeout(function(){ p.kind==='dive' ? startDive(p.ctx) : startMine(p.ctx); }, 300); }
}

/* ===== AI 会话弹窗 ===== */
var chat = {messages:[], busy:false};
function showModal(id){document.getElementById(id).classList.add('show')}
function hideModal(id){document.getElementById(id).classList.remove('show')}

function startDive(ctx){
  if(!aiCfg().key){ openPromptModal('dive', ctx); return; }
  chat = {messages:[], busy:false};
  document.getElementById('chatBody').innerHTML = '';
  document.getElementById('chatTitle').textContent = 'AI 深挖';
  showModal('chatModal');
  askAI(DIVE_TMPL.replace('__CTX__', ctx));
}
function startMine(ctx){
  if(!aiCfg().key){ openPromptModal('mine', ctx); return; }
  chat = {messages:[], busy:false};
  document.getElementById('chatBody').innerHTML = '';
  document.getElementById('chatTitle').textContent = '与我何干 · 按我的行业';
  showModal('chatModal');
  var profile = localStorage.getItem('kd_profile')||'';
  if(!profile){
    hideModal('chatModal'); showModal('profileModal');
    window._pendingCtx = ctx; return;
  }
  askProfile(ctx, profile);
}

/* ===== 免 Key 通用交互：提示词弹窗 + 需求池说明 ===== */
function buildPrompt(kind, ctx){
  if(kind==='dive') return DIVE_TMPL.replace('__CTX__', ctx);
  var profile = localStorage.getItem('kd_profile')||'（在这里填一句你的行业和岗位，例：电商运营，负责私域转化）';
  return '以下是一条 AI 行业资讯：\n' + ctx +
    '\n\n我的背景：' + profile +
    '\n请结合我的行业与岗位，输出「与我何干」：先点出这条资讯和我工作的具体关联，再给 1-2 个可执行的动作建议，要具体，不要说空话。';
}
function openPromptModal(kind, ctx){
  window._pendingPrompt = {kind:kind, ctx:ctx};
  document.getElementById('promptTitle').textContent = kind==='dive' ? 'AI 深挖 · 提示词已备好' : '与我何干 · 提示词已备好';
  document.getElementById('promptBox').value = buildPrompt(kind, ctx);
  showModal('promptModal');
}
function copyPrompt(){
  var box = document.getElementById('promptBox');
  var done = function(){ toast('已复制 ✓ 粘贴给你常用的 AI 就行'); };
  box.select();
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(box.value).then(done, function(){ document.execCommand('copy'); done(); });
  }else{
    document.execCommand('copy'); done();
  }
  box.blur();
}
function gotoSettingsFromPrompt(){
  hideModal('promptModal'); openSettings(); toast('保存 Key 后会自动继续刚才的解读');
}
function openNeed(){ showModal('needModal'); }
function saveProfile(){
  var v = document.getElementById('profileInput').value.trim();
  if(!v){ toast('先写一句你的行业和岗位'); return; }
  localStorage.setItem('kd_profile', v);
  hideModal('profileModal');
  if(window._pendingCtx){ showModal('chatModal'); askProfile(window._pendingCtx, v); window._pendingCtx=null; }
}
function askProfile(ctx, profile){
  var p = '以下是一条 AI 行业资讯：\n' + ctx +
    '\n\n我的背景：' + profile +
    '\n请结合我的行业与岗位，输出「与我何干」：先点出这条资讯和我工作的具体关联，再给 1-2 个可执行的动作建议，要具体，不要说空话。';
  askAI(p);
}
function followUp(){
  var inp = document.getElementById('chatInput');
  var v = inp.value.trim();
  if(!v || chat.busy) return;
  inp.value = '';
  askAI(v);
}

function askAI(userText){
  chat.messages.push({role:'user', content:userText});
  var body = document.getElementById('chatBody');
  body.insertAdjacentHTML('beforeend', '<div class="msg"><div class="msg-user">'+escapeHtml(userText)+'</div></div>');
  var aiDiv = document.createElement('div');
  aiDiv.className = 'msg msg-ai';
  aiDiv.innerHTML = '<span class="typing">正在思考…</span>';
  body.appendChild(aiDiv);
  body.scrollTop = body.scrollHeight;
  chat.busy = true;

  var c = aiCfg();
  var msgs = [{role:'system', content:'你是一位资深的 AI 行业分析师兼产品顾问，擅长用大白话把行业动态讲透，并给出落地的产品建议。输出使用 markdown 格式。'}].concat(chat.messages);

  fetch(c.url + '/chat/completions', {
    method:'POST',
    headers:{'Content-Type':'application/json', 'Authorization':'Bearer '+c.key},
    body: JSON.stringify({model:c.model, messages:msgs, stream:true})
  }).then(function(resp){
    if(!resp.ok){
      return resp.text().then(function(t){
        throw new Error('HTTP '+resp.status+'：'+(resp.status===401?'API Key 无效，请检查设置':t.slice(0,200)));
      });
    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = '', full = '';
    function pump(){
      return reader.read().then(function(r){
        if(r.done){ finish(); return; }
        buf += decoder.decode(r.value, {stream:true});
        var lines = buf.split('\n');
        buf = lines.pop();
        lines.forEach(function(line){
          line = line.trim();
          if(line.indexOf('data:')!==0) return;
          var payload = line.slice(5).trim();
          if(payload==='[DONE]') return;
          try{
            var j = JSON.parse(payload);
            var delta = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
            if(delta){ full += delta; aiDiv.innerHTML = mdRender(full); body.scrollTop = body.scrollHeight; }
          }catch(e){}
        });
        return pump();
      });
    }
    function finish(){ chat.messages.push({role:'assistant', content:full}); chat.busy=false; }
    return pump();
  }).catch(function(err){
    aiDiv.classList.add('error');
    aiDiv.textContent = '调用失败：' + err.message + '（如果是网络跨域问题，可换一个支持浏览器直连的 OpenAI 兼容端点）';
    chat.busy = false;
  });
}

/* 简易 markdown 渲染（前端用） */
function mdRender(md){
  var s = escapeHtml(md);
  s = s.replace(/```([\s\S]*?)```/g, function(m,c){return '<pre><code>'+c+'</code></pre>'});
  s = s.replace(/^#{2,5}\s+(.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/^(?:[-*])\s+(.+)$/gm, '<li>$1</li>');
  s = s.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>');
  s = s.replace(/(https?:\/\/[^\s<）)】]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  s = s.split(/\n{2,}/).map(function(b){
    if(/^\s*<(h4|ul|pre)/.test(b)) return b;
    return '<p>'+b.replace(/\n/g,'<br>')+'</p>';
  }).join('');
  return s;
}

/* ===== 事件绑定 ===== */
document.addEventListener('click', function(e){
  var d = e.target.closest('[data-dive]');
  if(d){ startDive(d.getAttribute('data-dive')); return; }
  var m = e.target.closest('[data-mine]');
  if(m){ startMine(m.getAttribute('data-mine')); return; }
  if(e.target.classList && e.target.classList.contains('modal-mask')){ e.target.classList.remove('show'); }
});
document.getElementById('chatInput').addEventListener('keydown', function(e){
  if(e.key==='Enter') followUp();
});
function toast(msg){
  var t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(function(){t.classList.remove('show')}, 2200);
}
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · AI 资讯五层解码</title>
<meta name="description" content="__SUB__">
<style>__CSS__</style>
</head>
<body>
<nav>
<span class="brand">看<em>门</em>道</span>
<div class="tabs">
<button class="active" data-page="page-today" onclick="switchPage('page-today',this)">最新</button>
<button data-page="page-archive" onclick="switchPage('page-archive',this)">历史</button>__NAV_TOPICS__
<button data-page="page-themes" onclick="switchPage('page-themes',this)">分类索引</button>
<button data-page="page-board" onclick="switchPage('page-board',this)">需求池</button>
</div>
<div class="search-wrap">
<input id="search" type="search" placeholder="搜索标题…" autocomplete="off">
<div class="search-results" id="searchResults"></div>
</div>
<button class="icon-btn" onclick="toggleTheme()" title="深浅色切换">◐</button>
<button class="icon-btn" onclick="openSettings()" title="AI 设置">⚙</button>
</nav>

<header class="hero">
<span class="hero-kicker">AI INTELLIGENCE DAILY</span>
<h1>__TITLE__</h1>
<div class="sub">__SUB__</div>
<div class="stats">
<div class="stat"><b>__NDAYS__</b><span>归档天数</span></div>
<div class="stat"><b>__NITEMS__</b><span>解码条目</span></div>
<div class="stat"><b>__NTHEMES__</b><span>主题分类</span></div>
</div>
<div class="hero-actions">
<a class="btn btn-primary" href="__SHEET__" target="_blank" rel="noopener">投稿 · 情报众筹</a>
<a class="btn btn-ghost" href="__SHEET__" target="_blank" rel="noopener">需求池表格</a>
</div>
</header>

<main>
<div class="page active" id="page-today">
<div class="latest-mark">最新一期 · <b>__DATE__</b></div>
__TODAY__
</div>
<div class="page" id="page-archive">__ARCHIVE__</div>__PAGE_TOPICS__
<div class="page" id="page-themes">__THEMES__</div>
<div class="page" id="page-board">__BOARD__</div>
</main>

<footer>
每日看门道 · 内容由 ai-news-digest 五层解码生成<br>
「立即深挖 / 与我何干」默认生成提示词，复制后粘贴到任意 AI 即可使用；配置自己的 API Key（仅存浏览器）可解锁页内直聊。
</footer>

<!-- 设置弹窗 -->
<div class="modal-mask" id="settingsModal">
<div class="modal">
<div class="modal-head"><span class="modal-title">AI 设置（BYOK）</span>
<button class="modal-close" onclick="hideModal('settingsModal')">×</button></div>
<div class="modal-body">
<div class="form-row"><label>API 地址（OpenAI 兼容端点）</label>
<input id="cfgUrl" placeholder="https://api.deepseek.com/v1"></div>
<div class="form-row"><label>API Key</label>
<input id="cfgKey" type="password" placeholder="sk-..."></div>
<div class="form-row"><label>模型</label>
<input id="cfgModel" placeholder="deepseek-chat"></div>
<div class="form-tip">Key 只保存在你浏览器的 localStorage 里，不会上传到任何服务器。<br>
推荐 DeepSeek（注册即有额度、支持浏览器直连）；也可填通义 / Kimi / OpenAI 等任何 OpenAI 兼容端点。</div>
</div>
<div class="modal-foot">
<button class="btn btn-primary" style="flex:1" onclick="saveSettings()">保存</button>
</div>
</div>
</div>

<!-- 行业画像弹窗 -->
<div class="modal-mask" id="profileModal">
<div class="modal">
<div class="modal-head"><span class="modal-title">你的行业与岗位</span>
<button class="modal-close" onclick="hideModal('profileModal')">×</button></div>
<div class="modal-body">
<div class="form-row"><label>一句话描述（用于定制「与我何干」）</label>
<input id="profileInput" placeholder="例：电商运营，负责私域转化；或：三甲医院信息科工程师"></div>
<div class="form-tip">只需填一次，之后点「按我的行业看与我何干」都会按这个身份解读。想换身份，重新点该按钮前清空浏览器本地存储即可（后续版本会加修改入口）。</div>
</div>
<div class="modal-foot">
<button class="btn btn-primary" style="flex:1" onclick="saveProfile()">保存并开始解读</button>
</div>
</div>
</div>

<!-- AI 会话弹窗 -->
<div class="modal-mask" id="chatModal">
<div class="modal">
<div class="modal-head"><span class="modal-title" id="chatTitle">AI 深挖</span>
<button class="modal-close" onclick="hideModal('chatModal')">×</button></div>
<div class="modal-body" id="chatBody"></div>
<div class="modal-foot">
<input id="chatInput" placeholder="继续追问… 回车发送">
<button class="btn btn-primary" onclick="followUp()">发送</button>
</div>
</div>
</div>

<!-- 提示词弹窗（免 Key 通用交互） -->
<div class="modal-mask" id="promptModal">
<div class="modal">
<div class="modal-head"><span class="modal-title" id="promptTitle">提示词已备好</span>
<button class="modal-close" onclick="hideModal('promptModal')">×</button></div>
<div class="modal-body">
<textarea class="prompt-box" id="promptBox" readonly></textarea>
<div class="form-tip">第 1 步：点下方「一键复制」；第 2 步：打开你常用的 AI，粘贴发送。也可以直接点下面的快捷入口：</div>
<div class="quick-ai">
<a href="https://chat.deepseek.com" target="_blank" rel="noopener">DeepSeek</a>
<a href="https://www.doubao.com" target="_blank" rel="noopener">豆包</a>
<a href="https://yuanbao.tencent.com" target="_blank" rel="noopener">腾讯元宝</a>
<a href="https://www.kimi.com" target="_blank" rel="noopener">Kimi</a>
</div>
<a class="byok-link" onclick="gotoSettingsFromPrompt()">我有自己的 API Key，想在页内直接聊 →</a>
</div>
<div class="modal-foot">
<button class="btn btn-primary" style="flex:1" onclick="copyPrompt()">一键复制提示词</button>
</div>
</div>
</div>

<!-- 需求池说明弹窗 -->
<div class="modal-mask" id="needModal">
<div class="modal">
<div class="modal-head"><span class="modal-title">转化需求 · 怎么玩</span>
<button class="modal-close" onclick="hideModal('needModal')">×</button></div>
<div class="modal-body">
<div class="form-tip" style="margin-top:0">
这条资讯触发了你的什么灵感？去腾讯文档「需求池」子表填一行：<br>
① <b>需求标题</b>：一句话说清想要什么<br>
② <b>使用场景</b>：谁在什么情况下会用到<br>
③ <b>期望效果</b>：做成什么样算解决问题<br><br>
需求池每周五异步评审，被挑中的会进入产品讨论。填一行只要 1 分钟。
</div>
</div>
<div class="modal-foot">
<a class="btn btn-primary" style="flex:1;text-align:center;text-decoration:none" href="__SHEET__" target="_blank" rel="noopener">前往腾讯文档填写 →</a>
</div>
</div>
</div>

<div class="toast" id="toast"></div>
<script>__JS__</script>
</body>
</html>"""

if __name__ == "__main__":
    main()
