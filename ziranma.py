#!/usr/bin/env python3
"""
Zero-dependency Chinese -> Ziranma Shuangpin (自然码双拼) word segmenter & encoder for iOS BLE HID typing.
Uses macOS CoreFoundation `CFStringTokenizer` (ICU word segmentation) + `CFStringTransform` (contextual Han-Latin)
so Chinese text is typed word-by-word and verified against the iOS floating candidate bar before committing.

Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
"""

from __future__ import annotations

import ctypes
import re

_cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


class _CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFStringGetLength.restype = ctypes.c_long
_cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
_cf.CFStringCreateWithSubstring.restype = ctypes.c_void_p
_cf.CFStringCreateWithSubstring.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _CFRange]
_cf.CFStringCreateMutableCopy.restype = ctypes.c_void_p
_cf.CFStringCreateMutableCopy.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
_cf.CFStringTransform.restype = ctypes.c_bool
_cf.CFStringTransform.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringTokenizerCreate.restype = ctypes.c_void_p
_cf.CFStringTokenizerCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _CFRange, ctypes.c_ulong, ctypes.c_void_p]
_cf.CFStringTokenizerAdvanceToNextToken.restype = ctypes.c_ulong
_cf.CFStringTokenizerAdvanceToNextToken.argtypes = [ctypes.c_void_p]
_cf.CFStringTokenizerGetCurrentTokenRange.restype = _CFRange
_cf.CFStringTokenizerGetCurrentTokenRange.argtypes = [ctypes.c_void_p]
_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_UTF8 = 0x08000100
_TRANS_LATIN = _cf.CFStringCreateWithCString(None, b"Han-Latin", _UTF8)
_TRANS_STRIP = _cf.CFStringCreateWithCString(None, b"NFD; [:Nonspacing Mark:] Remove; NFC", _UTF8)

INITIAL_MAP = {
    "zh": "v",
    "ch": "i",
    "sh": "u",
}

FINAL_MAP = {
    "a": "a", "o": "o", "e": "e", "i": "i", "u": "u", "v": "v", "ü": "v",
    "ai": "l", "ei": "z", "ui": "v", "ao": "k", "ou": "b", "iu": "q",
    "ie": "x", "ue": "t", "ve": "t", "üe": "t", "er": "r",
    "an": "j", "en": "f", "in": "n", "un": "p", "vn": "p", "ün": "p",
    "ang": "h", "eng": "g", "ing": "y", "ong": "s", "iong": "s",
    "ia": "w", "ua": "w", "ian": "m", "uan": "r", "üan": "r",
    "iang": "d", "uang": "d", "iao": "c", "uo": "o", "uai": "y",
}

ZERO_INITIAL = {
    "a": "aa", "o": "oo", "e": "ee",
    "ai": "ai", "ei": "ei", "ao": "ao", "ou": "ou",
    "an": "an", "en": "en", "er": "er",
    "ang": "ah", "eng": "eg",
}

# Common polyphone backup syllables if contextual Han-Latin ever differs from iOS IME reading
POLYPHONE_ALTERNATES: dict[str, list[str]] = {
    "重": ["zhong", "chong"],
    "行": ["xing", "hang"],
    "长": ["chang", "zhang"],
    "乐": ["le", "yue"],
    "还": ["hai", "huan"],
    "调": ["tiao", "diao"],
    "率": ["lv", "shuai"],
    "觉": ["jue", "jiao"],
    "数": ["shu", "shuo"],
    "传": ["chuan", "zhuan"],
    "弹": ["tan", "dan"],
    "盛": ["sheng", "cheng"],
    "藏": ["cang", "zang"],
    "省": ["sheng", "xing"],
    "便": ["bian", "pian"],
    "系": ["xi", "ji"],
    "曾": ["ceng", "zeng"],
    "校": ["xiao", "jiao"],
    "参": ["can", "shen", "cen"],
    "差": ["cha", "chai", "ci"],
}

ZH_PUNCT_TO_ASCII = str.maketrans({
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "、": "\\",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "【": "[",
    "】": "]",
})


def hanzi_to_pinyin_syllables(zh_chunk: str) -> list[str]:
    """Convert a Chinese word/clause to contextual Pinyin syllables (`ü` preserved as `v`)."""
    if not zh_chunk:
        return []
    s = _cf.CFStringCreateWithCString(None, zh_chunk.encode("utf-8"), _UTF8)
    m = _cf.CFStringCreateMutableCopy(None, 0, s)
    _cf.CFStringTransform(m, None, _TRANS_LATIN, False)
    buf = ctypes.create_string_buffer(max(256, len(zh_chunk) * 16))
    _cf.CFStringGetCString(m, buf, len(buf), _UTF8)
    latin_toned = buf.value.decode("utf-8")
    _cf.CFRelease(m)
    _cf.CFRelease(s)

    for ch in "ǖǘǚǜü":
        latin_toned = latin_toned.replace(ch, "v")

    s2 = _cf.CFStringCreateWithCString(None, latin_toned.encode("utf-8"), _UTF8)
    m2 = _cf.CFStringCreateMutableCopy(None, 0, s2)
    _cf.CFStringTransform(m2, None, _TRANS_STRIP, False)
    _cf.CFStringGetCString(m2, buf, len(buf), _UTF8)
    clean = buf.value.decode("utf-8").lower()
    _cf.CFRelease(m2)
    _cf.CFRelease(s2)
    return [tok for tok in clean.split() if tok.isalpha()]


def syllable_to_ziranma(syl: str) -> str:
    s = syl.lower().strip()
    if s in ZERO_INITIAL:
        return ZERO_INITIAL[s]
    if s[:2] in INITIAL_MAP:
        ini = INITIAL_MAP[s[:2]]
        fin = s[2:]
    elif s[0] in "bpmfdtnlgkhjqxrzcsyw":
        ini = s[0]
        fin = s[1:]
    else:
        return s
    return ini + FINAL_MAP.get(fin, fin)


def segment_chinese_words(zh_run: str) -> list[str]:
    """
    Segment a contiguous Chinese run into natural 1..4 char words via macOS ICU `CFStringTokenizer`.
    - Short proper nouns / App names of 2..3 chars (e.g. '猎聘', '备忘录', '时钟') stay intact first
      so iOS phrase lexicon matches them in 1 shot (with automatic per-char fallback if absent).
    - Adjacent isolated 1-char tokens inside longer sentences (e.g. '猎'+'聘' at start of a clause)
      except common grammatical particles ('的','了','与','和','在','是','把','将','及','或','而')
      are paired into 2-char bigrams for faster phrase matching.
    """
    if len(zh_run) <= 3:
        return [zh_run]

    s = _cf.CFStringCreateWithCString(None, zh_run.encode("utf-8"), _UTF8)
    n = _cf.CFStringGetLength(s)
    tok = _cf.CFStringTokenizerCreate(None, s, _CFRange(0, n), 0, None)
    raw_words: list[str] = []
    pos = 0
    buf = ctypes.create_string_buffer(256)
    while _cf.CFStringTokenizerAdvanceToNextToken(tok) != 0:
        r = _cf.CFStringTokenizerGetCurrentTokenRange(tok)
        if r.location > pos:
            gap_sub = _cf.CFStringCreateWithSubstring(None, s, _CFRange(pos, r.location - pos))
            _cf.CFStringGetCString(gap_sub, buf, 256, _UTF8)
            raw_words.append(buf.value.decode("utf-8"))
            _cf.CFRelease(gap_sub)
        sub = _cf.CFStringCreateWithSubstring(None, s, r)
        _cf.CFStringGetCString(sub, buf, 256, _UTF8)
        w = buf.value.decode("utf-8")
        _cf.CFRelease(sub)
        # Split overly long >4 char compounds into <=4 char chunks
        while len(w) > 4:
            raw_words.append(w[:2])
            w = w[2:]
        if w:
            raw_words.append(w)
        pos = r.location + r.length
    if pos < n:
        tail_sub = _cf.CFStringCreateWithSubstring(None, s, _CFRange(pos, n - pos))
        _cf.CFStringGetCString(tail_sub, buf, 256, _UTF8)
        raw_words.append(buf.value.decode("utf-8"))
        _cf.CFRelease(tail_sub)
    _cf.CFRelease(tok)
    _cf.CFRelease(s)

    particles = set("的了与和在是把将及或而也都就着过向对从给被让于")
    merged: list[str] = []
    i = 0
    while i < len(raw_words):
        cur = raw_words[i]
        if (
            len(cur) == 1
            and cur not in particles
            and i + 1 < len(raw_words)
            and len(raw_words[i + 1]) == 1
            and raw_words[i + 1] not in particles
        ):
            merged.append(cur + raw_words[i + 1])
            i += 2
        else:
            merged.append(cur)
            i += 1
    return merged


def word_to_ziranma_plan(zh_word: str, contextual_syllables: list[str] | None = None) -> dict:
    """
    Build a Ziranma typing plan for a single Chinese word:
      {
        "word": "时钟",
        "code": "uivs",
        "chars": [
          {"char": "时", "codes": ["ui"]},
          {"char": "钟", "codes": ["vs"]},
        ]
      }
    """
    syls = contextual_syllables if (contextual_syllables and len(contextual_syllables) == len(zh_word)) else hanzi_to_pinyin_syllables(zh_word)
    if len(syls) != len(zh_word):
        syls = [hanzi_to_pinyin_syllables(ch)[0] for ch in zh_word]

    char_codes = [syllable_to_ziranma(s) for s in syls]
    chars_plan = []
    for ch, primary_code in zip(zh_word, char_codes):
        codes = [primary_code]
        for alt_syl in POLYPHONE_ALTERNATES.get(ch, []):
            alt_code = syllable_to_ziranma(alt_syl)
            if alt_code not in codes:
                codes.append(alt_code)
        chars_plan.append({"char": ch, "codes": codes})

    return {
        "word": zh_word,
        "code": "".join(char_codes),
        "chars": chars_plan,
    }


def encode_ziranma_plan(text: str) -> list[dict]:
    """
    Tokenize arbitrary mixed Chinese/English/symbol text into closed-loop action items:
      - {"kind": "zh", "word": "猎聘", "code": "lxpn", "chars": [...]}
      - {"kind": "en", "text": "tan"}
      - {"kind": "sym", "text": "(30) = 0.577"}
    """
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]+|[^A-Za-z\u4e00-\u9fff]+", text)
    plan: list[dict] = []
    for tok in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            # Compute contextual syllables across the entire Chinese clause first (accurate polyphones!)
            clause_syls = hanzi_to_pinyin_syllables(tok)
            words = segment_chinese_words(tok)
            use_ctx = len(clause_syls) == len(tok)
            offset = 0
            for w in words:
                sub_syls = clause_syls[offset : offset + len(w)] if use_ctx else None
                offset += len(w)
                item = word_to_ziranma_plan(w, sub_syls)
                item["kind"] = "zh"
                plan.append(item)
        elif re.fullmatch(r"[A-Za-z]+", tok):
            plan.append({"kind": "en", "text": tok})
        else:
            ascii_sym = tok.translate(ZH_PUNCT_TO_ASCII)
            plan.append({"kind": "sym", "text": ascii_sym})
    return plan


def encode_ziranma_segments(text: str) -> list[tuple[str, str]]:
    """Backwards-compatible segment helper returning (kind, code_or_text)."""
    out: list[tuple[str, str]] = []
    for item in encode_ziranma_plan(text):
        if item["kind"] == "zh":
            out.append(("zh", item["code"]))
        else:
            out.append((item["kind"], item["text"]))
    return out


if __name__ == "__main__":
    for sample in [
        "时钟",
        "备忘录",
        "无线局域网",
        "猎聘消息巡检完成，时钟与汇率测试通过！",
        "计算结果: tan(30) = 0.57735",
    ]:
        print(sample)
        for step in encode_ziranma_plan(sample):
            if step["kind"] == "zh":
                print(f"   [zh] {step['word']!r} -> code={step['code']!r}")
            else:
                print(f"   [{step['kind']}] {step['text']!r}")
