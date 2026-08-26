"""联系人管理：界面里添加联系人 + 每用户可调参数 + 全局默认参数。

设计：
- 联系人名单与参数都存 data/contacts.json（界面可改，重启保留）。
- 「默认参数」对没有单独特调的联系人统一生效；个别联系人可在覆盖里单独调。
- 对外提供一个 ``sync_to_config(config)``：把联系人名单和合并后的参数
  写回 config 的 wechat.watch_list 与 contact_personas，这样大脑和微信机器人
  无需重启即可读取到最新值（原地更新同一个 dict，引用实时可见）。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

# 可在界面调节的参数元信息（字段 -> 中文名）
PARAM_FIELDS = [
    ("role", "身份关系"),
    ("user_name", "怎么称呼你"),
    ("tone", "说话语气"),
    ("personality", "性格（留空=默认）"),
    ("extra", "额外要求（留空=无）"),
]
FIELD_NAMES = [f[0] for f in PARAM_FIELDS]

# 语气/关系 的可选预设（Combobox 可编辑，也可自定义）
TONE_PRESETS = [
    "简短自然的朋友风，礼貌不局促",
    "活泼俏皮，像聊得来的老友",
    "温柔乖巧，会撒娇",
    "知性温柔，语气平缓",
    "直爽干脆，有啥说啥",
    "高冷克制，话不多",
    "像多年老同学，随意直来直去",
]
ROLE_PRESETS = ["朋友", "女友", "闺蜜", "同学", "家人", "前女友", "普通熟人"]


def _defaults() -> dict:
    return {
        "role": "朋友",
        "user_name": "你",
        "tone": "简短自然的朋友风微信聊天，礼貌不局促",
        "personality": "",
        "extra": "",
    }


class ContactManager:
    def __init__(self, json_path: str = "data/contacts.json"):
        self.path = json_path
        self.data = {
            "defaults": _defaults(),
            "contacts": [],
            "overrides": {},  # {联系人: {field: value}}
        }
        self._load()

    # ---------- 读写 ----------
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                defaults = _defaults()
                defaults.update(d.get("defaults") or {})
                self.data["defaults"] = defaults
                self.data["contacts"] = [str(x) for x in (d.get("contacts") or [])]
                self.data["overrides"] = d.get("overrides") or {}
        except Exception as e:
            logger.warning("读取联系人配置失败(%s): %s", self.path, e)

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存联系人配置失败(%s): %s", self.path, e)

    # ---------- 一键从现有 config 迁移（首次运行时调用） ----------
    def import_from_config(self, config: dict):
        """把 config.contact_personas 的人设作为各联系人的覆盖初始化。"""
        if self.data["contacts"]:
            return
        personas = (config.get("contact_personas") or {})
        contacts = list(personas.keys())
        watch = (config.get("wechat", {}).get("watch_list") or [])
        for w in watch:
            if str(w) not in contacts:
                contacts.append(str(w))
        for i, name in enumerate(contacts):
            p = personas.get(str(name))
            if not isinstance(p, dict):
                continue
            # 保留人设全部字段（含 background；name 是她自己的名字）
            self.data["overrides"][str(name)] = {
                k: str(v) for k, v in p.items() if v is not None and str(v).strip() != ""
            }
        self.data["contacts"] = contacts
        self.save()

    # ---------- 联系人类操作 ----------
    def get_contacts(self) -> list:
        return list(self.data["contacts"])

    def has_contact(self, name: str) -> bool:
        return name in self.data["contacts"]

    def add_contact(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        if name in self.data["contacts"]:
            return False
        self.data["contacts"].append(name)
        self.save()
        logger.info("已添加联系人: %s", name)
        return True

    def remove_contact(self, name: str):
        self.data["contacts"] = [c for c in self.data["contacts"] if c != name]
        self.data["overrides"].pop(name, None)
        self.save()

    # ---------- 参数操作 ----------
    def get_defaults(self) -> dict:
        return dict(self.data["defaults"])

    def set_defaults(self, params: dict):
        for k in FIELD_NAMES:
            if k in params:
                self.data["defaults"][k] = (params[k] or "").strip()
        self.save()

    def persona_for(self, name: str) -> dict:
        """某联系人的实际参数 = 默认参数 + 个人覆盖。"""
        merged = dict(self.data["defaults"])
        merged.update(self.data["overrides"].get(name, {}))
        return merged

    def get_override(self, name: str) -> dict:
        return dict(self.data["overrides"].get(name, {}))

    def set_override(self, name: str, params: dict):
        cur = self.data["overrides"].setdefault(name, {})
        for k in FIELD_NAMES:
            if k in params:
                cur[k] = (params[k] or "").strip()
        # 全空则删除覆盖，回落到默认参数
        if not any(v for v in cur.values()):
            self.data["overrides"].pop(name, None)
        self.save()

    def clear_override(self, name: str):
        self.data["overrides"].pop(name, None)
        self.save()

    def is_individually_set(self, name: str) -> bool:
        return bool(self.data["overrides"].get(name))

    # ---------- 同步进 config（让大脑/机器人实时读到） ----------
    def sync_to_config(self, config: dict):
        """把联系人名单与人设覆盖同步进共享 config（原地更新）。"""
        # 1) contact_personas：为每个联系人生成 默认+覆盖 的合并人设
        personas = config.get("contact_personas")
        if personas is None or not isinstance(personas, dict):
            personas = {}
            config["contact_personas"] = personas
        personas.clear()
        for name in self.data["contacts"]:
            personas[name] = self.persona_for(name)

        # 2) watch_list：名单即监听列表（保留既有顺序，删除的联系人同步移除）
        wc = config.setdefault("wechat", {})
        watch = wc.get("watch_list")
        if not isinstance(watch, list):
            watch = []
        contacts = self.data["contacts"]
        # 保留原有序并仅保留仍在名单中的，再把新加入的追加到末尾
        watch = [c for c in watch if c in contacts] + [c for c in contacts if c not in watch]
        wc["watch_list"] = watch