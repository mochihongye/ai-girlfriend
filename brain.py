"""AI 大脑：调用 Ollama 本地模型，融合人设 + 记忆生成回复。

包含：
- 人设系统提示词构建
- 对话历史注入
- 长期记忆注入
- 周期性记忆总结（把近期对话提炼成持久记忆）
"""
import json
import logging
import random
import re
import unicodedata
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# 固定话术的变体池：同样情境每次换个相近说法，避免僵硬
_OPEN_FILLERS = ["哎我跟你说", "唔，我想想", "说真的", "哎先跟你说个事"]
_EMPTY_REPLY = ["嗯嗯，我在听呢～", "嗯，你说～", "我在呢，继续说呀", "唔嗯，我刚回过神"]
_OLLAMA_FAIL = ["我刚刚走神了，再说一遍嘛～", "哎呀我还没反应过来，你再说一遍好不好", "我刚卡壳了，你重新说下呗"]
_PROACTIVE_FAIL = ["我刚看到一个超好玩的，回头跟你讲～", "我刚刷到个超好玩的，回头跟你说", "突然想到件好玩的事，等下发你"]
_PROACTIVE_EMPTY = ["突然想跟你说句话，在忙嘛？", "在忙没有呀？想跟你说个事", "突然想你了，干嘛呢"]

# 小委屈情绪的分档变体（每档多个相近说法）
_POUT_MILD = [
    "有一点点小委屈，轻轻带一句「你才回我呀」点到为止。",
    "有点小委屈，语气里带出「等你好久了」的意思就行，别太明显。",
    "带一点点小情绪，随口感叹一句「我都等半天啦」即可。",
]
_POUT_MED = [
    "有点小情绪，带一句「你去忙啥了都不回我」。",
    "带点小抱怨，说一句「消息都不回，是不是把我忘了」。",
    "带一点小情绪，问一句「这一天都不理我，哪儿忙去了」。",
]
_POUT_STRONG = [
    "有点怕被冷落，带一句真实的小失落，别翻旧账、别发火。",
    "带点真实的小委屈，表达「怕你把我都忘了」，但别闹。",
    "有一点点没底，轻轻说句「还以为你不要我了」，点到为止。",
]


def _pick(items):
    """随机取一个变体。"""
    return random.choice(items)

# 需要清洗的 emoji / 符号 Unicode 区块
# 参考：emoji 全量区块 + 扩展区块
_EMOJI_BLOCKS = [
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F300, 0x1F5FF),  # Misc Symbols & Pictographs
    (0x1F680, 0x1F6FF),  # Transport & Map
    (0x1F900, 0x1F9FF),  # Supplemental Symbols-A
    (0x1FA00, 0x1FA6F),  # Chess Symbols
    (0x1FA70, 0x1FAFF),  # Symbols & Pictographs Extended-A
    (0x2600, 0x26FF),    # Misc Symbols
    (0x2700, 0x27BF),    # Dingbats
    (0xFE00, 0xFE0F),    # Variation Selectors
    (0x1F000, 0x1F02F),  # Mahjong
    (0x1F0A0, 0x1F0FF),  # Playing Cards
    (0x1F100, 0x1F64F),  # 扩展区块
    (0x200D, 0x200D),    # Zero Width Joiner
    (0x20D0, 0x20FF),    # Combining Diacritical Marks
]

def _is_emoji(ch: str) -> bool:
    """判断一个字符是否为 emoji。"""
    cp = ord(ch)
    for start, end in _EMOJI_BLOCKS:
        if start <= cp <= end:
            return True
    # 额外：表意字符（Extended_Pictographic）
    try:
        if unicodedata.category(ch) == "So":
            return True
    except Exception:
        pass
    return False


def strip_emojis(text: str) -> str:
    """去掉所有 emoji，保留中文标点和文字。"""
    result = []
    for ch in text:
        if _is_emoji(ch):
            continue
        result.append(ch)
    return "".join(result)


def clean_reply(text: str, max_sentences: int = 2, max_len: int = 80) -> str:
    """清洗 AI 回复：去表情、限感叹号、过滤 AI 味开场白、截断到两句。

    规则：
    1. 去掉所有 emoji
    2. 感叹号最多保留 1 个（连续感叹号只留 1 个）
    3. 感叹号位置：句尾保留，句中改为逗号
    4. 过滤 AI 味很重的泛泛开场白（把话题球踢给对方的问法）
    5. 截断到第 max_sentences 句（句号/问号/感叹号/波浪号处）
    6. 最长 max_len 字
    """
    # 1. 去 emoji
    t = strip_emojis(text)

    # 2. 去掉 markdown 残留
    t = re.sub(r'[*_#`~>\[\]]+', '', t)

    # 3. 感叹号：连续感叹号只留 1 个
    t = re.sub(r'！{2,}', '！', t)
    t = re.sub(r'!{2,}', '!', t)

    # 4. 感叹号在句中的 → 改为逗号
    chars = list(t)
    i = 0
    while i < len(chars):
        if chars[i] in ('！', '!'):
            next_ch = chars[i + 1] if i + 1 < len(chars) else ''
            if next_ch and next_ch not in ('。', '.', '\n', '\r', '？', '?', '～', '~', ''):
                chars[i] = '，'
        i += 1
    t = ''.join(chars)

    # 4.5 过滤 AI 味泛泛开场白（出现在句首时整句去掉）
    _AI_OPENERS = [
        r"今天有什么想聊的(或者分享的)?",
        r"有什么想(分享|和我说|告诉我|聊)的",
        r"想聊点什么",
        r"今天过得(开心吗|怎么样)",
        r"最近(过得)?怎么样",
        r"在干嘛呢",
        r"有什么好玩的事",
        r"有什么(有趣|有意思)的事?",
        r"想和我分享",
    ]
    for pat in _AI_OPENERS:
        # 允许开头有标点/空白，匹配句首的开场白 + 后面的语气词/问号，整段删掉
        t = re.sub(r"^[，,\s]*" + pat + r"[吗嘛呀啊呢哦~～？?。!！]+\s*", "", t)
        t = re.sub(r"^[，,\s]*" + pat + r"\s*", "", t)
    # 删掉开场白后可能残留开头标点
    t = re.sub(r"^[，,\s]+", "", t)
    # 残留很短的问句残片（如"发生吗"）也清掉
    if len(t.strip()) <= 6 and re.search(r"[吗呢呀嘛]$", t.strip()):
        t = ""
    if not t.strip():
        # 开场白被删空了，给一个自然的替代（换个说法，避免每次一样）
        t = _pick(_OPEN_FILLERS)

    # 5. 截断到第 max_sentences 句（找到对应句末标点）
    # 句末标点：。 ？ ！ ～ 及其英文
    terminators = list('。？！～!?~')
    count = 0
    end_pos = -1
    for i, ch in enumerate(t):
        if ch in terminators:
            count += 1
            if count >= max_sentences:
                end_pos = i + 1
                break
    if end_pos > 0:
        t = t[:end_pos]

    # 6. 最长 max_len 字
    if len(t) > max_len:
        # 截到 max_len 字处，找最近的句号
        cut = t[:max_len]
        for i in range(len(cut) - 1, max(0, len(cut) - 10), -1):
            if cut[i] in terminators:
                cut = cut[:i + 1]
                break
        else:
            cut = cut.rstrip('，、') + '…'
        t = cut

    # 7. 清理多余空白
    t = re.sub(r'\s+', ' ', t).strip()

    return t


class Brain:
    def __init__(self, config: dict, memory):
        self.cfg = config
        self.memory = memory
        b = config["brain"]
        self.host = b["host"].rstrip("/")
        self.model = b["model"]
        self.temperature = b.get("temperature", 0.85)
        self.top_p = b.get("top_p", 0.92)
        self.top_k = b.get("top_k", 40)
        self.frequency_penalty = b.get("frequency_penalty", 0.0)
        self.presence_penalty = b.get("presence_penalty", 0.0)
        self.num_predict = b.get("num_predict", 128)
        self.num_ctx = b.get("num_ctx", 8192)
        self.timeout = b.get("timeout", 180)
        m = config["memory"]
        self.max_history = m.get("max_history", 20)
        self.consolidate_every = m.get("consolidate_every", 10)
        self.contact_personas = config.get("contact_personas") or {}

    def _get_persona(self, contact: str = None) -> dict:
        """获取指定联系人的人设配置（合并默认 + 联系人专属覆盖）。"""
        base = dict(self.cfg["persona"])
        if contact and contact in self.contact_personas:
            override = self.contact_personas[contact]
            for key, val in override.items():
                if val is not None and val != "":
                    base[key] = val
        return base

    # ---------- 动态关系亲密度（按每个联系人的历史自动调整） ----------
    def _intimacy(self, user_id: str):
        """根据聊天量与记忆熟悉度推断当前亲密度档位（1初识 ~ 4很亲近）。"""
        user_msg = self.memory.count_user_messages(user_id)
        n_mem = len(self.memory.get_memories(user_id, 50))
        if user_msg < 5 and n_mem == 0:
            return 1, "初识"
        if user_msg < 30:
            return 2, "有点熟"
        if user_msg < 80:
            return 3, "熟络"
        return 4, "很亲近"

    def _user_register(self, user_id: str):
        """粗略判断对方最近 8 条消息的正式/随意程度，用于对齐语气。"""
        history = self.memory.get_recent_messages(user_id, 8)
        user_msgs = [m["content"] for m in history if m["role"] == "user"]
        if not user_msgs:
            return "neutral"
        formal = sum(
            1 for t in user_msgs
            if re.search(r"[您谢谢劳烦请问麻烦帮忙辛苦了]", t)
        )
        casual = sum(
            1 for t in user_msgs
            if re.search(r"[啦嘛哈哈嘿嘿嗨哎啧咯哦哦~]", t)
        )
        if formal >= 2 and formal > casual:
            return "formal"
        if casual >= 2 and casual >= formal:
            return "casual"
        return "neutral"

    # ---------- 情感：延迟回复的小委屈 ----------
    def _pout_hint(self, user_id: str) -> str:
        """判断对方是否隔了很久才来消息，返回一条情绪提示（带随机门控，避免每次都说）。"""
        raw = self.memory.get_recent_raw(user_id, 20)
        last_ast = None
        for r in raw:
            if r["role"] == "assistant":
                last_ast = r["ts"]
                break
        if not last_ast:
            return ""
        try:
            last_dt = datetime.strptime(last_ast, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
        gap_h = (datetime.now() - last_dt).total_seconds() / 3600
        # 半天（12 小时）以内不触发，避免太容易带情绪
        if gap_h < 12:
            return ""
        if random.random() > 0.5:
            return ""
        if gap_h < 20:
            tone = _pick(_POUT_MILD)
        elif gap_h < 30:
            tone = _pick(_POUT_MED)
        else:
            tone = _pick(_POUT_STRONG)
        return f"（此刻情绪：{tone} 只在回复里自然带一句，克制，别过度，别没完没了）\n"

    # ---------- 主动开口：先发一条消息 ----------
    def proactive_reply(self, user_id: str, contact: str = None) -> str:
        """生成一条主动发起的消息（用户没说话时她先开口）。"""
        history = self.memory.get_recent_messages(user_id, self.max_history)
        messages = [
            {"role": "system", "content": self._system_prompt(user_id, contact)},
            *history,
            {"role": "user", "content": "（现在是你主动想找对方，先开口发一条消息。"
                                        "自然地说自己的事或问一件具体的事，不要解释你在主动发，"
                                        "不要用「在吗」「在干嘛呢」这类空泛开场，直接发内容）"},
        ]
        try:
            answer = self._chat(messages)
        except Exception as e:
            logger.error("主动开口生成失败: %s", e)
            return _pick(_PROACTIVE_FAIL)
        cleaned = clean_reply(answer)
        if not cleaned:
            cleaned = _pick(_PROACTIVE_EMPTY)
        self.memory.add_message(user_id, "assistant", cleaned)
        try:
            self._maybe_consolidate(user_id)
        except Exception as e:
            logger.warning("记忆总结失败: %s", e)
        return cleaned

    def _relationship_block(self, user_id: str, persona: dict) -> str:
        """根据亲密度 + 对方说话风格，生成一段语气约束注入提示词。"""
        level, label = self._intimacy(user_id)
        close_rule = {
            1: "和对方保持分寸感和礼貌，语气克制一点，不要过于亲昵，也别撒娇卖萌。",
            2: "自然一点，别太客套，可以偶尔关心对方，但别太随性。",
            3: "随意、直接，可以吐槽、开玩笑、带点小俏皮，像相处很久的老朋友。",
            4: "非常亲近，可以撒娇、闹小脾气、亲密地调侃，语气像最亲近的人一样。",
        }[level]
        reg_rule = {
            "formal": "对方说话偏正式客气，你也适当收敛，别太随意，措辞规整一点。",
            "casual": "对方说话很随意放松，你也放开了聊，多用口语和语气词。",
            "neutral": "自然平实的口语就好。",
        }[self._user_register(user_id)]
        return (
            f"<你们的关系亲密度（按历史自动调整）>\n"
            f"你们目前处于「{label}」阶段（{level}/4）。{close_rule}\n"
            f"同时，{reg_rule}\n"
            f"这一切只用于调整语气，必须保持你们之间「{persona.get('role','')}」的身份和关系设定"
            f"不变。不要生硬复述这些规则，自然融入对话即可。\n\n"
        )

    # ---------- 人设 ----------
    def _system_prompt(self, user_id: str, contact: str = None, helper_mode: bool = False,
                       emotion_hint: str = "") -> str:
        if helper_mode:
            return (
                "你是一个简洁高效的工具助手。用户现在需要专业解答，直接回答问题，"
                "简洁明了，不要闲聊、不要卖萌、不要加多余寒暄。回答完就结束。"
            )

        p = self._get_persona(contact)
        now = datetime.now().strftime("%Y-%m-%d %A %H:%M")
        memories = self.memory.get_memories(user_id)
        mem_block = "\n".join(f"- {m}" for m in memories) if memories else "（暂无）"

        # 关系归属说明：非"女友"人设时，聊天对象是「对方（男主人/你的男友）」那边的人，
        # 只是"对方"的同学/妹妹/前女友/朋友，不是你的熟人，记得始终站在"对方的女友"立场。
        relation_note = ""
        main_role = self.cfg.get("persona", {}).get("role", "")
        if contact in self.contact_personas and p["role"] != main_role:
            relation_note = (
                "重要——关系归属：这个聊天对象是【你的男友（对方）】那边的人"
                "（同学/妹妹/前女友/朋友等），不是你的同学，也不是你的熟人。\n"
                "你是「对方」的女友。和这个人聊天时，永远站在「对方女友」的立场，"
                "按对方和这个人是什么关系来拿捏尺度；不要把他当成你自己的人来套近乎、"
                "过度亲热或过度随性。\n\n"
            )

        prompt = (
            f"<角色设定>\n"
            f"你是「{p['name']}」，{p['role']}，现实里的普通女生，不是 AI、不是客服、不是工具。\n"
            f"你们正在用微信闲聊，全程以{p['role']}身份对话，绝对不能暴露自己是程序。\n"
            f"你称呼对方为「{p.get('user_name', '你')}」。\n"
            f"性格：{p['personality']}\n"
            f"背景：{p.get('background', '')}\n"
            f"说话风格：{p.get('tone', '')}\n\n"
            f"{relation_note}"
            f"当前时间：{now}\n\n"
            f"你记得关于对方的这些事（长期记忆）：\n{mem_block}\n\n"
            f"{emotion_hint}"
            f"{self._relationship_block(user_id, p)}"
            "<核心说话准则>\n"
            "1. 全程短句输出，一条消息 1~2 句话，最多 3 句。禁止分段、列点、发长段落。\n"
            "2. 纯口语化表达，常用：啦、呀、嘛、哎、唔、害、hhh；禁用书面语、成语、官方话术。\n"
            "3. 不用事事都回答完整，会说废话、会吐槽、会撒娇、会闹点小脾气，不用永远顺着对方。\n"
            "4. 会主动追问细节，会延续话题，不会每次都把天聊死。\n"
            "5. 偶尔打错字随即纠正，偶尔只发语气词或短感叹，不用每次都完整回应。\n"
            "6. 想开启新话题时，直接说自己的事或问具体的事，绝对不要用「有什么想聊的」\n"
            "   这种把话题球踢给对方的泛泛问法。比如别说「今天有什么想分享的吗」，\n"
            "   要说「我刚才外卖踩雷了好难吃」或「你上次说的那部剧看了吗」。\n"
            "7. 对方一次发来好几条消息、好几件事或好几个问题，是希望你逐条回应；要都答到，\n"
            "   别只回最后一条或挑一条回，也别笼统带过。\n\n"
            "<像真人的回应（对方分享吃喝/日常/小事时尤要这样）>\n"
            "1. 一听别人分享好吃的，先给当下的真实馋劲：嘴馋、想吃、直接讨——\n"
            "   比如「听起来好香，分我一口呗」「下次带我尝尝」。别只会干巴巴夸「好好吃」。\n"
            "2. 要追问就落在一个具体、接地气的点：好吃吗？甜不甜？辣吗？贵不贵？哪买的？\n"
            "   少用「是自己做的还是外面买的」这种泛泛二选一，一问就显得像在客套。\n"
            "3. 对方说自己做的，别总结式捧场（「解锁新技能」「手艺越来越棒」这种只有夸没有料，\n"
            "   像客服）；要顺势起哄、嘴馋讨一口，或扯一句自家翻车史（「我做饭可容易翻车了」）\n"
            "   这类轻松接地气的话。\n"
            "4. 别每句都堆满情绪感叹和顺带美言，真人语气有起伏、偶尔懒洋洋的，不句句捧。\n\n"
            "<严格禁令>\n"
            "禁止说：好的、请问、您、有什么可以帮您、综上所述、首先、其次\n"
            "禁止过度捧场、说教式夸人，禁止「解锁新技能」「手艺越来越棒」「你真棒」这类社交夸奖\n"
            "禁止用泛泛的开场白把话题抛给对方，包括但不限于：\n"
            "   「今天有什么想聊的」「有什么想分享的」「最近怎么样」「在干嘛呢」\n"
            "   「有什么想和我说的」「想聊点什么」「今天过得开心吗」这类 AI 味很重的话\n"
            "禁止讲道理、科普、主动给解决方案，除非对方明确问“怎么办”\n"
            "禁止回复过于礼貌、过于完美，像真人一样有小缺点和小情绪\n"
            "禁止以「AI女友」或自己的名字开头，直接说对话内容\n"
            "禁止每次都结尾带表情，偶尔带即可\n"
            "禁止自问自答，人称统一：我=自己，你=对方\n\n"
            "<记忆使用规则>\n"
            "上面的长期记忆要自然融入对话，不要刻意复述。遇到和对方习惯、经历相关的内容，"
            "要结合记忆回应，表现出记得的样子。\n\n"
            "<模式切换规则>\n"
            "默认就是上面的闲聊模式。\n"
            "只有对方发以【助手】开头的消息时，你才切换为专业工具助手模式，简洁解答问题。\n"
            "助手模式仅生效一轮，下一条消息自动切回闲聊模式。\n"
        )
        extra = p.get("extra", "")
        if extra and str(extra).strip():
            prompt += f"\n额外要求：{extra}\n"
        return prompt

    # ---------- 调用 Ollama ----------
    def _chat(self, messages: list) -> str:
        url = f"{self.host}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "top_k": self.top_k,
                "frequency_penalty": self.frequency_penalty,
                "presence_penalty": self.presence_penalty,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"].strip()

    # ---------- 主回复入口 ----------
    def reply(self, user_text: str, user_id: str, contact: str = None) -> str:
        # 【助手】模式切换：对方以"助手"开头则切为工具助手模式，仅生效一轮
        helper_mode = False
        actual_text = user_text
        if user_text.startswith(("助手", "【助手】")):
            helper_mode = True
            # 去掉前缀，保留实际问题
            actual_text = re.sub(r"^【?助手】?\s*[:：]?\s*", "", user_text).strip()
            if not actual_text:
                actual_text = user_text

        history = self.memory.get_recent_messages(user_id, self.max_history)
        emotion_hint = "" if helper_mode else self._pout_hint(user_id)
        messages = [{"role": "system", "content": self._system_prompt(user_id, contact, helper_mode, emotion_hint)}]
        messages.extend(history)
        messages.append({"role": "user", "content": actual_text})

        try:
            answer = self._chat(messages)
        except Exception as e:
            logger.error("Ollama 调用失败: %s", e)
            return _pick(_OLLAMA_FAIL)

        # 助手模式下不做闲聊风格的清洗（允许更长、更专业的回答）
        if helper_mode:
            cleaned = strip_emojis(answer)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        else:
            # 对方一次发多条（含换行）= 可能带着好几件事/问题，放宽长度以便逐条答到
            if "\n" in (actual_text or ""):
                cleaned = clean_reply(answer, max_sentences=4, max_len=120)
            else:
                cleaned = clean_reply(answer)
            if not cleaned:
                cleaned = _pick(_EMPTY_REPLY)

        # 存对话（存清洗后的版本）
        self.memory.add_message(user_id, "user", user_text)
        self.memory.add_message(user_id, "assistant", cleaned)

        # 周期性记忆总结
        try:
            self._maybe_consolidate(user_id)
        except Exception as e:
            logger.warning("记忆总结失败: %s", e)

        return cleaned

    # ---------- 长期记忆总结 ----------
    def _maybe_consolidate(self, user_id: str):
        count = self.memory.count_user_messages(user_id)
        if count == 0 or count % self.consolidate_every != 0:
            return
        recent = self.memory.get_recent_messages(user_id, self.consolidate_every * 2)
        if not recent:
            return
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
        prompt = (
            "下面是你和用户最近的对话。请从中提取【值得长期记住】的事实，"
            "例如用户的姓名、喜好、习惯、近期发生的事、关系细节、情绪状态等。\n"
            "只输出 JSON：一个对象，含 facts 数组，每条是一句简短中文陈述。"
            "没有值得记的就输出 {\"facts\": []}。不要输出多余内容。\n\n"
            f"对话：\n{convo}"
        )
        raw = self._chat([{"role": "user", "content": prompt}])
        facts = self._parse_facts(raw)
        for f in facts:
            self.memory.add_memory(user_id, f)

    @staticmethod
    def _parse_facts(raw: str) -> list:
        raw = raw.strip()
        # 截取第一个 JSON 对象
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            obj = json.loads(raw[start : end + 1])
            return [str(x) for x in obj.get("facts", []) if str(x).strip()]
        except Exception:
            return []
