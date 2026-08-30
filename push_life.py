# -*- coding: utf-8 -*-
"""
每日「日常英语」推送（北京时间早上 8:00）

结构：
  【往期复习】 同场景上一次的核心句与词（第 2 次出现起才有）
  【今日场景】 4 轮情景对话
  【高频表达】 7 句高频句
  【场景词汇】 8 个词
  【语法小知识】 从当天句子里长出来的一个语法点

场景排期见 scenes.json：41 个场景分三档，核心场景约 10 天回来一次，
每次换一个角度；改完场景表用 tools_gen_scenes.py 重新生成排期。

本地测试:
    $env:DEEPSEEK_API_KEY = "..."
    $env:DRY_RUN = "1"; $env:FORCE_DAY = "1"
    python push_life.py
"""
import os
import re
import sys
import glob
import hashlib
import json
import asyncio
import datetime
import urllib.request
import urllib.error

# ---------------- 可调参数 ----------------
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
START_DATE  = datetime.date(2026, 8, 30)   # 第 1 天（北京时间）

SCENES_FILE = "scenes.json"
AUDIO_DIR   = "audio"
HISTORY_DIR = "history"                    # 每天归档，供往期复习使用
CARD_CACHE  = "card_cache.txt"
KEEP_AUDIO_DAYS  = 7
REVIEW_SENTENCES = 4        # 往期复习展示几句
REVIEW_WORDS     = 5        # 往期复习展示几个词

# ---- 音频朗读 ----
ENABLE_AUDIO = True
VOICE        = "en-US-AvaMultilingualNeural"   # 多语种音色，中英混读自然
               # 备选：zh-CN-XiaoxiaoNeural（女声偏中文）
               #       en-US-BrianMultilingualNeural（男声）
SPEECH_RATE  = "-10%"       # 语速，初学者放慢一点。正常写 "+0%"
MAX_TOKENS   = 4000
GH_REPO      = os.environ.get("GH_REPO", "wilsonwuqin-gif/my-english-life")
# -----------------------------------------

DRY_RUN = os.environ.get("DRY_RUN") == "1"


PROVIDER = os.environ.get("PROVIDER", "deepseek")

PROVIDERS = {
    "deepseek": {
        "url":     "https://api.deepseek.com/chat/completions",
        "model":   "deepseek-v4-flash",     # 更强可换 deepseek-v4-pro（贵约 3 倍）
        "env":     "DEEPSEEK_API_KEY",
    },
    "claude": {
        "url":     "https://api.anthropic.com/v1/messages",
        "model":   "claude-haiku-4-5",
        "env":     "ANTHROPIC_API_KEY",
    },
}
# -----------------------------------------


def call_ai(prompt):
    cfg = PROVIDERS.get(PROVIDER)
    if cfg is None:
        raise SystemExit(f"PROVIDER 只能是 {list(PROVIDERS)}，当前是 {PROVIDER!r}")
    api_key = os.environ.get(cfg["env"])
    if not api_key:
        raise SystemExit(f"缺少环境变量 {cfg['env']}")

    if PROVIDER == "deepseek":
        # DeepSeek 是 OpenAI 兼容格式
        body = {
            "model": cfg["model"],
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # DeepSeek 思考模式默认开启且 effort=high，会把 max_tokens
            # 全部消耗在思维链上导致正文为空。这类格式化写作不需要思考。
            "thinking": {"type": "disabled"},
        }
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        }
    else:
        body = {
            "model": cfg["model"],
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    req = urllib.request.Request(
        cfg["url"], data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise SystemExit(f"[调用 {PROVIDER} 失败] HTTP {e.code}: {detail}")

    if PROVIDER == "deepseek":
        choice = resp["choices"][0]
        usage  = resp.get("usage", {})
        print(f"[AI] finish_reason={choice.get('finish_reason')} "
              f"tokens={usage.get('completion_tokens')}/{MAX_TOKENS}")
        return choice["message"].get("content") or ""
    return resp["content"][0]["text"]


SEC_RE = re.compile(r"\[(S[1-4]|A[1-4])\]")


def parse_sections(raw):
    """把 AI 输出切成 4 个正文小节和 4 段朗读稿。
    返回 ([(小节标题, 小节正文), ...], [朗读稿, ...])"""
    parts = SEC_RE.split(raw)      # [前言, 'S1', 内容, 'S2', 内容, ...]
    d = {}
    for i in range(1, len(parts) - 1, 2):
        d[parts[i]] = parts[i + 1].strip()

    sections, scripts = [], []
    for n in range(1, 5):
        body = d.get(f"S{n}", "")
        if body:
            head, _, rest = body.partition("\n")
            sections.append((head.strip(), rest.strip()))
        scripts.append(d.get(f"A{n}", ""))

    if not sections:               # 兜底：AI 没按格式输出就整段推送
        print("[切分] 未识别到分节标记，按整段处理")
        sections = [("今日内容", raw.strip())]
        scripts = [""]
    return sections, scripts


def make_audio(script, day_n, idx):
    """给第 idx 节生成 mp3，返回相对路径；失败返回 None（不影响文字推送）"""
    if not (ENABLE_AUDIO and script.strip()):
        return None
    try:
        import edge_tts
    except ImportError:
        print("[音频] 未安装 edge-tts，跳过")
        return None

    os.makedirs(AUDIO_DIR, exist_ok=True)
    # 文件名带内容哈希：内容变了文件名就变，URL 也就变了。
    # 否则同名文件被 jsDelivr 缓存后，改了内容 CDN 仍返回旧音频，
    # 会出现「文字是今天的、声音是上次的」。
    sig = hashlib.sha1(
        (script + VOICE + SPEECH_RATE).encode("utf-8")).hexdigest()[:8]
    stem = f"day-{day_n + 1:03d}-{idx}"
    path = f"{AUDIO_DIR}/{stem}-{sig}.mp3"

    if os.path.exists(path):                 # 内容没变就不用重新合成
        print(f"[音频] 第 {idx} 节内容未变，沿用 {path}")
        return path

    for old_file in glob.glob(f"{AUDIO_DIR}/{stem}-*.mp3"):
        os.remove(old_file)                  # 清掉同一节的旧哈希版本

    async def _run():
        tts = edge_tts.Communicate(script, VOICE, rate=SPEECH_RATE)
        await tts.save(path)

    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"[音频] 第 {idx} 节生成失败：{e}")
        return None

    print(f"[音频] 第 {idx} 节 {len(script)} 字 -> {path}"
          f"（{os.path.getsize(path) // 1024} KB）")
    return path


def cleanup_audio():
    """按天分组，只保留最近 KEEP_AUDIO_DAYS 天的 mp3"""
    def day_of(f):
        m = re.search(r"day-(\d+)-", os.path.basename(f))
        return int(m.group(1)) if m else None

    files = glob.glob(f"{AUDIO_DIR}/day-*.mp3")
    days = {d for d in (day_of(f) for f in files) if d is not None}
    keep = set(sorted(days)[-KEEP_AUDIO_DAYS:])
    for f in files:
        d = day_of(f)
        if d is not None and d not in keep:
            os.remove(f)
            print(f"[音频] 清理旧文件 {f}")


def audio_url(path):
    """jsDelivr CDN 链接（国内访问比 GitHub 原生链接快）"""
    return f"https://cdn.jsdelivr.net/gh/{GH_REPO}@main/{path}"


ACCENT = {
    "word": "#D93F3F",   # 单词：红
    "dlg":  "#3D8BD4",   # 对话英文：蓝
    "key":  "#B07A2E",   # 今日一句：棕
}
ACCENT_DARK = {
    "word": "#FF7A7A",
    "dlg":  "#7FBEFF",
    "key":  "#E3B461",
}

CSS = {
    "head":   "font-size:17px;font-weight:700;margin:22px 0 10px",
    "word":   f"font-size:17px;font-weight:700;color:{ACCENT['word']}",
    "phon":   "font-size:15px;opacity:.75",       # 音标：淡一点，不指定颜色
    "wzh":    "font-size:16px;font-weight:700",   # 词义：加粗，继承颜色
    "en":     "font-size:15px",
    "zh":     "font-size:15px",
    "topic":  "font-size:15px;opacity:.75",
    "dlg":    f"font-size:15px;font-weight:700;color:{ACCENT['dlg']}",
    "spk":    "font-size:15px;opacity:.75",
    "key":    f"font-size:15px;font-weight:700;color:{ACCENT['key']}",
    "note":   "font-size:15px",
}

STYLE_BLOCK = (
    "<style>"
    "@media (prefers-color-scheme: dark){"
    f".w{{color:{ACCENT_DARK['word']}!important}}"
    f".d{{color:{ACCENT_DARK['dlg']}!important}}"
    f".k{{color:{ACCENT_DARK['key']}!important}}"
    "}"
    "</style>"
)
# ------------------------------------------------------------------


def esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def push_wechat(title, content, token):
    # 内容已经是 HTML（含 <audio> 播放条），用 html 模板原样渲染
    template = os.environ.get("PUSH_TEMPLATE", "html")
    payload = json.dumps({
        "token": token,
        "title": title,
        "content": content,
        "template": template,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    if resp.get("code") != 200:
        raise SystemExit(f"[推送失败] {resp}")
    print("[推送成功]", resp.get("msg"))


# 绿色用于「正确示范」，深浅色各一套
GREEN, GREEN_DARK = "#2E8B57", "#7BD1A0"
STYLE_BLOCK = STYLE_BLOCK.replace(
    "@media (prefers-color-scheme: dark){",
    "@media (prefers-color-scheme: dark){" + f".g{{color:{GREEN_DARK}!important}}")


def resolve_day():
    today = datetime.datetime.now(BEIJING).date()
    day_n = (today - START_DATE).days
    print(f"[日期] 北京时间 {today}，起始日 {START_DATE}")
    if day_n < 0:
        force = os.environ.get("FORCE_DAY")
        if not force:
            print(f"还没到起始日 {START_DATE}，今天不推送。（测试可设 FORCE_DAY=1）")
            return None
        day_n = int(force) - 1
    return day_n


def load_scene(day_n):
    with open(SCENES_FILE, encoding="utf-8") as f:
        scenes = json.load(f)
    return scenes[day_n % len(scenes)]


# ---------------- 历史归档与往期复习 ----------------

def load_previous(scene_zh, day_n):
    """找同一场景最近的一次归档"""
    best = None
    for path in glob.glob(f"{HISTORY_DIR}/day-*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if rec.get("scene_zh") == scene_zh and rec.get("day_n", 10 ** 9) < day_n:
            if best is None or rec["day_n"] > best["day_n"]:
                best = rec
    return best


def save_history(day_n, scene, sections):
    """把当天的句子和词存档，供以后复习"""
    sentences, words = [], []
    for head, body in sections:
        for line in body.splitlines():
            code, _, rest = line.partition("|")
            f = [p.strip() for p in rest.split("|")]
            code = code.strip()
            if code == "H" and f[0]:
                sentences.append([f[0], ""])
            elif code == "C" and sentences and not sentences[-1][1]:
                sentences[-1][1] = f[0]
            elif code == "W" and len(f) >= 3:
                words.append(f[:3])

    os.makedirs(HISTORY_DIR, exist_ok=True)
    rec = {
        "day_n": day_n,
        "day": day_n + 1,
        "scene_zh": scene["scene_zh"],
        "round": scene["round"],
        "focus": scene["focus"],
        "sentences": sentences,
        "words": words,
    }
    with open(f"{HISTORY_DIR}/day-{day_n + 1:03d}.json", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    print(f"[归档] 第 {day_n + 1} 天：{len(sentences)} 句 / {len(words)} 词")


def build_review(prev):
    """把上一期记录渲染成【往期复习】小节，返回 (标题, 正文, 朗读稿)"""
    if not prev:
        return None
    lines = [f"P|第 {prev['day']} 天讲过「{prev['focus']}」，先回忆一下："]
    read = []
    for en, zh in prev["sentences"][:REVIEW_SENTENCES]:
        lines.append(f"H|{en}")
        if zh:
            lines.append(f"C|{zh}")
        read.append(en)
    ws = prev["words"][:REVIEW_WORDS]
    if ws:
        lines.append("P|上期词：" + "、".join(f"{w[0]} {w[2]}" for w in ws))
        read += [w[0] for w in ws]
    # 句尾已有标点就不再补句号，避免读成「please?.」
    script = " ".join(s if s.rstrip()[-1:] in ".?!" else s.rstrip() + "."
                      for s in read if s.strip())
    return ("往期复习", "\n".join(lines), script)


# ---------------- Prompt ----------------

def build_prompt(scene, prev):
    avoid = ""
    if prev:
        old = "；".join(en for en, _ in prev["sentences"][:8])
        avoid = (f"\n注意：这个场景上一次（第 {prev['day']} 天）已经讲过下面这些句子，"
                 f"今天必须换新的，不要重复：\n{old}\n")

    return f"""你是一位教中国工程师日常英语口语的私教。
学员背景：38 岁，暖通与楼宇自控工程师，在新加坡参与医院项目。
英语水平：大学四级，读写尚可，但日常场合开不了口、接不上话。
他需要的是能立刻用出来的口语，不是考试英语。

今天的场景：{scene['scene_en']}（{scene['scene_zh']}）
今天的侧重角度：{scene['focus']}
这是该场景第 {scene['round']} 次学习。{avoid}
请只围绕上面这个侧重角度展开，不要泛泛地讲整个场景。

全文用纯文本，禁止使用任何 Markdown 符号。
正文每一行都必须以「行标记 + 竖线」开头，不得有例外。行标记含义：
  T| 小标题行
  D| 对话行，格式为  D|说话人|英文句
  H| 需要重点记住的英文句
  C| 中文行
  W| 单词行，格式为  W|单词|音标|中文    音标要带前后斜杠
  N| 语法点名称行
  P| 说明段落行
  X| 错误示范行，以 ✗ 开头
  V| 正确示范行，以 ✓ 开头

严格按以下八节输出，每节标记原样写出、单独占一行：

[S1]今日场景
先一行 T| 写场景英文名和中文名。
然后 4 轮对话，每轮两行：D|说话人|英文句，紧跟 C|该句中文。
说话人用具体身份（如 Staff、Waiter、Colleague）和 You。
对话要像真人在说话，短句为主，不要教科书腔。

[S2]高频表达
7 句这个角度下最常用的话，每句两行：H|英文，紧跟 C|中文。
必须是学员本人会说出口的句子，不是别人对他说的。
句式简单，避免复杂从句。

[S3]场景词汇
8 个这个角度下的高频词或短语，每个一行 W| 。

[S4]语法小知识
从上面这些句子里挑一个真实出现过的语法点，不要凭空另起。
先一行 N| 写语法点名称。
再一到两行 P| 用中文讲清楚它是什么、为什么中国人容易弄错。
再给两组对照，每组两行：X|✗ 错误说法（中文简短说明），V|✓ 正确说法（中文简短说明）。
最后一行 P| 给一句能直接照做的口诀。

以上四节合计控制在 700 字以内。
下面四节是给语音朗读用的，不计入字数，必须完整输出、不可省略。
朗读稿必须是纯文本：不要音标、不要括号注释、不要序号编号、不要标记符号，
句子之间用句号分隔，让语音停顿自然。

[A1]
朗读第一节。把对话的英文部分完整读一遍，中文不读。

[A2]
朗读第二节。7 句英文每句读两遍，便于跟读。

[A3]
朗读第三节。每个词按「英文词。英文词。中文意思。」读。

[A4]
朗读第四节。把两组对照里的正确说法各读两遍，错误说法不读。

只输出卡片内容本身，不要任何开场白或结束语。"""


# ---------------- 渲染 ----------------

def render_line(line):
    code, _, rest = line.partition("|")
    code = code.strip()
    f = [p.strip() for p in rest.split("|")]

    if code == "W" and len(f) >= 3:
        return (f'<p style="margin:12px 0 2px">'
                f'<span class="w" style="{CSS["word"]}">{esc(f[0])}</span>&nbsp;'
                f'<span style="{CSS["phon"]}">{esc(f[1])}</span>&nbsp;'
                f'<span style="{CSS["wzh"]}">{esc(f[2])}</span></p>')
    if code == "D" and len(f) >= 2:
        return (f'<p style="margin:6px 0 0">'
                f'<span style="{CSS["spk"]}">{esc(f[0])}:&nbsp;</span>'
                f'<span class="d" style="{CSS["dlg"]}">{esc(f[1])}</span></p>')
    if code == "H":
        return f'<p class="k" style="margin:10px 0 2px;{CSS["key"]}">{esc(f[0])}</p>'
    if code == "C":
        return f'<p style="margin:2px 0;{CSS["zh"]}">{esc(f[0])}</p>'
    if code == "T":
        return f'<p style="margin:2px 0;{CSS["topic"]}">{esc(f[0])}</p>'
    if code == "N":
        return (f'<p style="margin:10px 0 4px;font-size:16px;font-weight:700">'
                f'{esc(f[0])}</p>')
    if code == "X":
        return (f'<p class="w" style="margin:4px 0;font-size:15px;'
                f'color:{ACCENT["word"]}">{esc(f[0])}</p>')
    if code == "V":
        return (f'<p class="g" style="margin:4px 0;font-size:15px;'
                f'font-weight:700;color:{GREEN}">{esc(f[0])}</p>')
    if code in ("P", "U", "Q", "K"):
        return f'<p style="margin:6px 0;{CSS["note"]}">{esc(f[0])}</p>'
    return f'<p style="margin:4px 0;{CSS["note"]}">{esc(line)}</p>'


def build_html(sections, paths):
    out = [STYLE_BLOCK]
    for i, (head, body) in enumerate(sections):
        out.append(f'<p style="{CSS["head"]}">【{esc(head)}】</p>')
        p = paths[i] if i < len(paths) else None
        if p:
            out.append(
                f'<audio controls preload="none" style="width:100%;height:34px"'
                f' src="{audio_url(p)}">你的浏览器不支持音频播放</audio>')
        for line in body.splitlines():
            if line.strip():
                out.append(render_line(line.strip()))
    return "".join(out)


# ---------------- 主流程 ----------------

def do_generate(day_n):
    scene = load_scene(day_n)
    print(f"Day {day_n + 1}｜{scene['scene_zh']} 第 {scene['round']} 次"
          f"｜{scene['focus']}")

    prev = load_previous(scene["scene_zh"], day_n)
    print("[往期] " + (f"找到第 {prev['day']} 天的记录" if prev else "无，本次是首次"))

    raw = call_ai(build_prompt(scene, prev))
    print(f"[AI] 返回 {len(raw)} 字")
    if not raw.strip():
        raise SystemExit("[错误] AI 返回内容为空，请检查 API 状态")

    ai_sections, scripts = parse_sections(raw)
    print(f"[切分] {len(ai_sections)} 个小节：" +
          " / ".join(f"{h}({len(b.splitlines())}行)" for h, b in ai_sections))

    save_history(day_n, scene, ai_sections)

    review = build_review(prev)
    if review:
        sections = [(review[0], review[1])] + ai_sections
        scripts = [review[2]] + scripts
    else:
        sections = ai_sections

    paths = [make_audio(scripts[i] if i < len(scripts) else "", day_n, i + 1)
             for i in range(len(sections))]
    cleanup_audio()

    with open(CARD_CACHE, "w", encoding="utf-8") as f:
        json.dump({"day_n": day_n, "scene": scene,
                   "sections": sections, "paths": paths},
                  f, ensure_ascii=False)
    return scene, sections, paths


def do_send(day_n, scene, sections, paths):
    title = f"日常英语 Day {day_n + 1}｜{scene['scene_zh']}"
    print(f"[发送] {len(sections)} 个小节，{sum(1 for p in paths if p)} 段音频")
    if not sections:
        raise SystemExit("[错误] 待发送内容为空，中止（不发空消息）")
    card = build_html(sections, paths)

    if DRY_RUN:
        print("=" * 40)
        print(title)
        print(card)
        print("=" * 40)
        print("(DRY_RUN 模式，未推送微信)")
        return

    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        raise SystemExit("缺少环境变量 PUSHPLUS_TOKEN")
    push_wechat(title, card, token)


def main():
    # 用法：
    #   python push_life.py            本地测试，生成+推送一条龙
    #   python push_life.py generate   只生成内容和音频（工作流第 1 步）
    #   python push_life.py send       读取已生成的内容并推送（工作流第 3 步）
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    day_n = resolve_day()
    if day_n is None:
        return

    if mode == "send":
        if not os.path.exists(CARD_CACHE):
            raise SystemExit(f"找不到 {CARD_CACHE}，请先运行 generate")
        with open(CARD_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        do_send(d["day_n"], d["scene"], d["sections"], d["paths"])
        return

    scene, sections, paths = do_generate(day_n)
    if mode != "generate":
        do_send(day_n, scene, sections, paths)


if __name__ == "__main__":
    sys.exit(main())
