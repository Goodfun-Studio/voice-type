"""Rule-based cleanup for raw speech transcripts.

Deliberately conservative. Every rule either strips a hesitation sound or
collapses a stutter repeat; nothing reorders or rewrites meaning. When a rule
is unsure it leaves the text alone, so the worst case is "raw text kept",
never "sentence changed".
"""
from __future__ import annotations

import re

# Tier A: pure hesitation sounds. Safe to drop anywhere in a sentence.
FILLERS_ALWAYS = "嗯呃唔诶欸"
# Tier B: sounds that can also be meaningful ("唉，算了" / "哎你"). Only dropped
# when they sit next to a pause mark, which is how whisper renders a real pause.
FILLERS_PAUSED = "唉哎"

EN_FILLERS = r"um|uh|erm|hmm|mmm|uhh|umm"

# Onomatopoeia where repetition IS the meaning -> never collapse.
KEEP_REPEATS = set("哈嘿嘻呵嘁")

PRONOUN_STUTTER = set("我你他她它们这那")

# Idiomatic A-B-A-B reduplication that must survive ("一个一个来").
KEEP_ABAB = re.compile(r"^一.")


def _strip_fillers(text: str) -> str:
    # Tier A: optional pause mark straight after, plus any trailing spaces.
    text = re.sub(rf"[{FILLERS_ALWAYS}]+[，,、]?\s*", "", text)
    # Tier B: only when delimited by a pause mark on at least one side.
    text = re.sub(rf"[，,、]\s*[{FILLERS_PAUSED}]+[，,、]?\s*", "，", text)
    text = re.sub(rf"^[{FILLERS_PAUSED}]+[，,、]\s*", "", text)
    # English fillers: as standalone words only.
    text = re.sub(rf"\b(?:{EN_FILLERS})\b[,\s]*", " ", text, flags=re.IGNORECASE)
    return text


def _collapse_stutter(text: str) -> str:
    # A character said 3+ times is always a stutter, except laughter ("哈哈哈").
    text = re.sub(
        r"([\u4e00-\u9fff])\1{2,}",
        lambda m: m.group(0) if m.group(1) in KEEP_REPEATS else m.group(1),
        text,
    )
    # A pronoun said twice is a stutter ("我我" -> "我"). Restricted to this set
    # because AA reduplication is real elsewhere: 慢慢来 / 看看 / 试试 / 好好说.
    text = re.sub(
        r"([\u4e00-\u9fff])\1",
        lambda m: m.group(1) if m.group(1) in PRONOUN_STUTTER else m.group(0),
        text,
    )
    # A 2-char word said twice is a stutter ("这个这个" -> "这个"), unless the
    # doubling is idiomatic ("一个一个来").
    def two_char(m: re.Match) -> str:
        unit = m.group(1)
        if KEEP_ABAB.match(unit) or unit[0] in KEEP_REPEATS:
            return m.group(0)
        return unit

    text = re.sub(r"([\u4e00-\u9fff]{2})\1+", two_char, text)
    # English: "the the" -> "the"
    return re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)


CJK = r"\u4e00-\u9fff\u3400-\u4dbf"


def _full_width_punctuation(text: str) -> str:
    """whisper emits half-width , . ? ! between Chinese words. Convert only
    when the mark actually touches a Chinese character, so decimals (3.14),
    URLs and pure English survive untouched."""
    text = re.sub(rf"([{CJK}]),(?=\s*[{CJK}])", r"\1，", text)
    text = re.sub(rf"([{CJK}]),", r"\1，", text)
    text = re.sub(rf",(?=\s*[{CJK}])", "，", text)
    text = re.sub(rf"([{CJK}])\.(?![0-9])", r"\1。", text)
    text = re.sub(rf"([{CJK}])\?", r"\1？", text)
    text = re.sub(rf"([{CJK}])!", r"\1！", text)
    text = re.sub(rf"([{CJK}]):(?![0-9])", r"\1：", text)
    text = re.sub(rf"([{CJK}]);", r"\1；", text)
    text = re.sub(rf"([{CJK}])\(", r"\1（", text)
    text = re.sub(rf"\)(?=[{CJK}])", "）", text)
    return text


def _tidy(text: str) -> str:
    # whisper likes to put a space after 。 between two Chinese sentences; Chinese
    # text does not use that space. Only collapse when both sides are Chinese,
    # so "OK 我们" keeps its space.
    text = re.sub(rf"(?<=[{CJK}。，！？；：、])\s+(?=[{CJK}])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([，。！？；：、）】」》])", r"\1", text)
    text = re.sub(r"([（【「《])\s+", r"\1", text)
    text = re.sub(r"([，,、；])\1+", r"\1", text)
    text = re.sub(r"^[\s，,、。；;:：]+", "", text)
    return text.strip()


def _to_simplified(text: str) -> str:
    """whisper often emits Traditional Chinese for Mandarin; convert if opencc
    is available, otherwise leave the text untouched."""
    try:
        from opencc import OpenCC  # type: ignore
    except Exception:
        return text
    global _CC
    try:
        _CC
    except NameError:
        _CC = OpenCC("t2s")
    return _CC.convert(text)


CJK_ONLY = re.compile(rf"^[{CJK}]+$")


def _pinyin(word: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
    except Exception:
        return ""
    return " ".join(lazy_pinyin(word, style=Style.NORMAL))


def apply_vocabulary(text: str, terms: list[str]) -> str:
    """Restore domain terms whisper mis-hears as more common homophones
    (识鱼 -> 食鱼, 抠图 -> 扣图). Matching is exact-pinyin, same length, and only
    runs when the real term is missing from the text, so a term the engine
    already got right is never touched.

    Terms come from the user's own vocabulary.txt, so a legitimate homophone
    of a listed term will also be rewritten -- that is the intended trade."""
    if not text or not terms:
        return text
    for term in terms:
        if len(term) < 2 or term in text:
            continue
        target = _pinyin(term)
        if not target:
            continue
        size = len(term)
        for start in range(len(text) - size + 1):
            candidate = text[start:start + size]
            if not CJK_ONLY.match(candidate):
                continue
            if _pinyin(candidate) == target:
                text = text[:start] + term + text[start + size:]
                break
    return text


# whisper 在静音/底噪上会背出训练数据里的字幕落款。这些句子足够独特，
# 只有"整段输出就是它"才丢，正常内容一律不碰。
HALLUCINATION_CONTAINS = (
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "请不吝点赞订阅转发打赏支持",
    "明镜与点点栏目",
    "字幕由amaraorg社区提供",
    "字幕志愿者提供",
    "thanksforwatching",
    "subscribetomychannel",
    "likeandsubscribe",
    "ご視聴ありがとうございました",
)
HALLUCINATION_EXACT = ("字幕志愿者", "感谢观看", "感谢您的观看", "谢谢观看", "请订阅")


def normalize_for_match(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]", "", text.lower())


# 短落款（4-5 字）本身也可能是人真说的话，所以只在录音安静时才判为幻觉。
# 实测：幻觉那次的音频 rms 是 0.0037，正常说话 0.0063~0.13。
QUIET_RMS = 0.012


def is_hallucination(text: str, rms: float | None = None) -> bool:
    """True when the transcript is a known whisper stock phrase.

    Long phrases are unambiguous, so they go regardless of level. Short ones
    only count when the recording was quiet, which is the condition that makes
    whisper start reciting its training data."""
    norm = normalize_for_match(text)
    if not norm:
        return False
    if any(phrase in norm for phrase in HALLUCINATION_CONTAINS):
        return True
    if norm in HALLUCINATION_EXACT:
        return rms is not None and rms < QUIET_RMS
    return False


def clean(text: str, simplify: bool = True) -> str:
    """Return the polished transcript."""
    if not text or not text.strip():
        return ""
    out = text.strip()
    out = _strip_fillers(out)
    out = _collapse_stutter(out)
    out = _full_width_punctuation(out)
    out = _tidy(out)
    if simplify:
        out = _to_simplified(out)
    return out.strip()


if __name__ == "__main__":
    cases = [
        "嗯，我今天下午三点开会。",
        "呃我今天我我今天下午三点开会。",
        "那个，然后我们今天今天下午三点开会。",
        "嗯 呃 这个这个方案我觉得可以。",
        "um so I think the the plan works.",
        "唉，算了。",
        "哎你说得对。",
        "今天就先这样吧。",
        "哈哈哈这个太好玩了。",
        "我们一个一个来。",
        "这个事儿得慢慢来。",
        "啊，原来是这样。",
        "，今天天气不错。",
        "我今天下午三点跟产品开个会,然后我们把识鱼那个流程再对一遍。",
        "这个版本是 3.14 还是 2.7?",
        "文档在 example.com 上面,你去看一下!",
        "会议纪要:第一点,第二点;第三点。",
        "OK, let's ship it.",
    ]
    for c in cases:
        print(f"  {c}\n→ {clean(c)}\n")

    print("幻觉过滤:")
    for c, r in [("请不吝点赞订阅转发打赏支持明镜与点点栏目", 0.0037),
                 ("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目", 0.0037),
                 ("Thanks for watching!", 0.0037),
                 ("感谢观看", 0.0037),
                 ("感谢观看", 0.0500),
                 ("谢谢观看", 0.0037),
                 ("我今天下午三点开会，记得请大家点赞订阅转发。", 0.0037)]:
        print(f"  [{'丢' if is_hallucination(c, r) else '留'}] rms={r:.4f}  {c}")

    print("\n词表纠正:")
    for c in ["然后我们把食鱼那个流程再对一遍", "把照片扣图以后放进鱼刚里", "今天晚饭吃鱼"]:
        print(f"  {c}\n→ {apply_vocabulary(c, ['识鱼', '鱼缸', '抠图'])}\n")
