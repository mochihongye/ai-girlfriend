"""语音合成（TTS）：文字 -> 带自然停顿的 MP3 / SILK。

修复说明：
- 整段文字一次性交给 edge-tts 合成（而不是按句切开）——避免出现"一句话中间停"
  和"段尾拉长音"的问题
- 标点停顿通过 ffmpeg 往 mp3 对应标点位置插入静音实现：用"切出前 N 字符对应
  音频 + 插入静音 + 拼回剩余"太复杂，退而用更稳的策略：
    * 朗读时先一次性生成完整 mp3
    * 再把"句号/感叹号/问号"之后的短暂停顿：通过在 pygame 播放层按预估时长
      做 pause 来实现（而不是真"切分音频再播放"），更可靠、不引入读错
- 加了多音字纠正（读错音问题）
- 清洗 emoji + Markdown
"""
import asyncio
import logging
import os
import re
import tempfile
import uuid

import edge_tts

import utils_audio

logger = logging.getLogger(__name__)

# =========================================================
# 1. 清洗：emoji / Markdown 碎片
# =========================================================
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001F000-\U0001FFFF"
    "\U00002600-\U000027BF"
    "\U0001F100-\U0001F1FF"
    "\U000025A0-\U000025FF"
    "\U00002B00-\U00002BFF"
    "\U0001FA00-\U0001FA6F"
    "\U0000200D"
    "\U0000FE00-\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)
_NOISE_RE = re.compile(r"[`*_#~\[\]]+")


def clean_for_tts(text: str) -> str:
    """朗读前的文本清洗：去掉表情和装饰符号。"""
    text = (text or "").strip()
    if not text:
        return ""
    text = _EMOJI_RE.sub("", text)
    text = _NOISE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# =========================================================
# 2. 多音字纠正（解决"读错读音"）
# 按常见口语中容易读错的词做一个轻量替换：
#   把"词/字"替换成同音字，让 edge-tts 读对。
#   比如 "还hai"（副词）读错成 huan → 写成 "还[害]" 的谐音
# =========================================================
_POLYPHONE_FIXES = [
    # -------- 副词/连词：还（hai/huan）--------
    # "还" 做副词读 hái —— 它后面接形容词/副词/能愿动词/还是等
    (r"还(?=[有在是好能会要不了吗没去来对都也])", "孩"),
    # "还有、还是、还没、还不、还好、还在、还能、还会、还要" 均被上条覆盖
    # 动词"还"（归回、偿还）读 huán：还给、还手、还债、还价、还账、归还
    (r"还给", "环给"),
    (r"归还", "归环"),
    (r"还手", "环手"),
    (r"还债", "环债"),
    (r"还价", "环价"),
    # -------- 为（wèi/wéi）--------
    (r"为什么", "喂什么"),
    (r"为了", "喂了"),
    (r"因为", "因喂"),
    (r"为你", "喂你"),
    (r"为我", "喂我"),
    (r"为他", "喂他"),
    (r"为啥", "喂啥"),
    (r"为何", "喂何"),
    # -------- 相（xiāng/xiàng）--------
    (r"相信", "香信"),
    (r"相同", "香同"),
    (r"互相", "互香"),
    (r"相爱", "香爱"),
    (r"照相", "照象"),
    (r"相片", "象片"),
    (r"相机", "象机"),
    (r"真相", "真象"),
    # -------- 长（cháng/zhǎng）--------
    (r"长大", "掌大"),
    (r"成长", "成掌"),
    (r"长高", "掌高"),
    (r"长胖", "掌胖"),
    (r"长辈", "掌辈"),
    (r"长短", "常短"),
    (r"长期", "常期"),
    (r"长度", "常度"),
    # -------- 重（zhòng/chóng）--------
    (r"重点", "种点"),
    (r"重要", "种要"),
    (r"体重", "体众"),
    (r"重量", "众量"),
    (r"严重", "严众"),
    (r"重要性", "种要性"),
    # "重新、重复、重来" 本来 chóng 其实音准通常没问题
    # -------- 行（xíng/háng）--------
    (r"不行", "不形"),
    (r"就行", "就形"),
    (r"人行道", "人形道"),
    (r"自行车", "自行"[0] + "形车"),
    (r"一行", "一杭"),
    (r"银行", "银杭"),
    # -------- 了（le/liǎo）--------
    (r"了解", "廖解"),
    (r"了不起", "廖不起"),
    (r"了结", "廖结"),
    (r"明了", "明廖"),
    # "…就行"、"…不行" 已经覆盖，但 "就行啦" 这种尾部 "行啦" 也补一条
    (r"行(?=[啦吗嘛呀哦])", "形"),
    # -------- 着（zhe/zháo）--------
    (r"着急", "昭集"),
    (r"着凉", "昭凉"),
    (r"着火", "昭火"),
    (r"着迷", "昭迷"),
    # -------- 倒（dǎo/dào）--------
    (r"摔倒", "摔岛"),
    (r"倒霉", "岛霉"),
    (r"倒垃圾", "岛垃圾"),
    # -------- 朝（zhāo/cháo）--------
    (r"朝阳", "招阳"),   # 早晨的太阳
    (r"朝向", "潮向"),
    (r"朝着", "潮着"),
    # -------- 好（hǎo/hào）--------
    (r"爱好", "爱浩"),
    (r"好奇", "浩奇"),
    (r"好客", "浩客"),
    # -------- 便（biàn/pián）--------
    (r"方便", "方变"),
    (r"随便", "随变"),
    (r"便利", "变利"),
    # -------- 都（dōu/dū）--------
    (r"都是", "兜是"),
    (r"都有", "兜有"),
    (r"都会", "兜会"),
    (r"全都", "全兜"),
    (r"首都", "首独"),
    # -------- 要（yào/yāo）--------
    (r"要是", "耀是"),
    (r"要求", "妖求"),
    (r"要点", "耀点"),
    # -------- 差（chà/chā/cī/chāi）--------
    (r"差不多", "叉不多"),
    (r"差别", "插别"),
    (r"出差", "出拆"),
]


def _apply_polyphone_fixes(text: str) -> str:
    for pattern, replacement in _POLYPHONE_FIXES:
        try:
            text = re.sub(pattern, replacement, text)
        except re.error:
            continue
    return text


# =========================================================
# 3. 音调 / 停顿参数
# =========================================================
def _map_pitch(style: str, base_pitch: str = "+0Hz") -> str:
    """用户显式覆写就用用户的，否则按风格给一点自然偏移。"""
    if base_pitch != "+0Hz":
        return base_pitch
    if style in ("chat", "cheerful"):
        return "+4Hz"
    if style in ("sad", "whisper"):
        return "-2Hz"
    return base_pitch


# 标点之后应该停留的毫秒（用 pygame 层 sleep 实现，而不是重切合成）
SENTENCE_PAUSE_MS = {
    "。": 180,
    "！": 160,
    "？": 220,
    "!": 140,
    "?": 200,
    "；": 100,
    ";": 90,
    "\n": 260,
}


def estimate_pauses(text: str) -> list:
    """返回 (char_index, pause_ms) 的列表：在哪些字之后要停顿多少毫秒。

    注意：char_index 是"原文本"的位置。edge-tts 合成时不会按字符一一对应时长，
    所以我们用一个粗略的平均字符时长来把"字符位置"映射为"播放时间点"。
    但其实播放端用更稳的方式：**把整段合成 + 播放完一段(按标点)后 sleep，**
    见 `synthesize_with_pauses` 返回值的 pauses 列表（每句末尾停顿毫秒）。
    """
    text = clean_for_tts(text)
    pauses = []
    for i, ch in enumerate(text):
        if ch in SENTENCE_PAUSE_MS:
            pauses.append((i, SENTENCE_PAUSE_MS[ch]))
    return pauses


# =========================================================
# 4. TTS 主类
# =========================================================
class TTS:
    def __init__(self, config: dict):
        v = config["voice"]
        self.voice = v.get("tts_voice", "zh-CN-XiaoxiaoNeural")
        self.rate = v.get("tts_rate", "+0%")
        self.style = v.get("tts_style", "chat")
        self.pitch = _map_pitch(self.style, v.get("tts_pitch", "+0Hz"))
        self._tmp = tempfile.mkdtemp(prefix="tts_")

    # ---------- 内部：edge-tts 生成一段 MP3 ----------
    async def _render_one(self, one_text: str, out_path: str) -> str:
        com = edge_tts.Communicate(
            one_text, self.voice, rate=self.rate, pitch=self.pitch
        )
        await com.save(out_path)
        return out_path

    # ---------- 对外：MP3（单文件 + 停顿点位）----------
    def synthesize_with_pauses(self, text: str):
        """返回 (mp3_path, pauses)。

        - mp3_path: 整段文字一次合成后的 mp3 路径（可直接整体播放）
        - pauses: list[(char_index, pause_ms)]，播放端在字符位置之后 sleep。
          如果播放端不方便按字符精准定位，也可以按 `split_sentences` 的
          句数 + 每句 sleep，误差在可接受范围。
        """
        cleaned = clean_for_tts(text)
        if not cleaned:
            raise ValueError("空文本")
        # 先做多音字纠正（只影响合成，不改用户看到的原文）
        tts_text = _apply_polyphone_fixes(cleaned)

        token = uuid.uuid4().hex[:8]
        mp3_path = os.path.join(self._tmp, f"{token}.mp3")
        asyncio.run(self._render_one(tts_text, mp3_path))
        pauses = estimate_pauses(cleaned)
        return mp3_path, pauses

    # ---------- 对外：简单单 MP3（微信发语音 / 旧兼容）----------
    def synthesize_to_mp3(self, text: str) -> str:
        """合成单 mp3 文件，用于播放 / 转 silk。"""
        mp3, _ = self.synthesize_with_pauses(text)
        return mp3

    # ---------- 对外：SILK 输出（微信发语音用）----------
    def synthesize_to_silk(self, text: str) -> str:
        cleaned = clean_for_tts(text)
        if not cleaned:
            raise ValueError("空文本")
        token = uuid.uuid4().hex[:8]
        mp3_path = os.path.join(self._tmp, f"{token}.mp3")
        asyncio.run(
            self._render_one(_apply_polyphone_fixes(cleaned), mp3_path)
        )
        silk_path = os.path.join(self._tmp, f"{token}.silk")
        utils_audio.mp3_to_silk(mp3_path, silk_path)
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        return silk_path
