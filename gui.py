"""小念 · 温柔系女友 · 聊天界面（原生桌面窗口）

双击桌面的「小念」快捷方式即可打开，无需浏览器、不开端口。
结构：
  - 主窗口 = 聊天软件风：左侧联系人窄栏 + 对话气泡主体 + 顶栏小图标
  - 记忆 / 日志 / 设置 收进独立小窗，主界面保持干净
  - 「桌宠」：呼出一个可拖动、置顶的桌面小女友，随时想聊就聊
  - 对话气泡、四套可切换主题、主动发消息开关与频率滑块
"""
import logging
import math
import os
import queue
import random
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, messagebox

from main import load_config, check_ollama, build_tts_stt
from brain import Brain
from memory import Memory
from contact_manager import (
    ContactManager, PARAM_FIELDS, TONE_PRESETS, ROLE_PRESETS,
)

# ========================= 日志捕获（进日志小窗） =========================
_log_lock = threading.Lock()


class _UiHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        with _log_lock:
            _log_lines.append(msg)
            while len(_log_lines) > 4000:
                _log_lines.pop(0)


_log_lines = []


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_UiHandler()],
)
logger = logging.getLogger("ai-girlfriend-gui")

# ========================= 主题（可在设置小窗切换） =========================
THEMES = {
    "粉樱暖阳": dict(
        bg="#FFF5F8", header="#FFE1EA", card="#FFFFFF", border="#F3CBD8",
        accent="#E87296", accent_dark="#D8577F", accent_soft="#FBE4EE",
        text_main="#44262F", subtxt="#9A7A85", ok="#2EAD5A", danger="#D24D4D",
    ),
    "暗夜紫火": dict(
        bg="#171020", header="#2A1B3A", card="#221732", border="#46305B",
        accent="#E75BA4", accent_dark="#D84B96", accent_soft="#3A2850",
        text_main="#F3E7F7", subtxt="#A98FB8", ok="#4CD98A", danger="#FF6B6B",
    ),
    "薄荷奶咖": dict(
        bg="#F2F7EE", header="#DFEED9", card="#FFFFFF", border="#CFE0C5",
        accent="#7FB069", accent_dark="#63914F", accent_soft="#EAF4E2",
        text_main="#33502A", subtxt="#7E9A6F", ok="#2EAD5A", danger="#D24D4D",
    ),
    "雾蓝海盐": dict(
        bg="#F2F6FA", header="#DFEAF4", card="#FFFFFF", border="#CBDCEB",
        accent="#5B8DB8", accent_dark="#41718F", accent_soft="#E7F0F8",
        text_main="#2C3E50", subtxt="#7E94A8", ok="#2EAD5A", danger="#D24D4D",
    ),
}
THEME_NAMES = list(THEMES)
_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_prefs.json")


class App:
    """聊天软件风主程序 + 独立小窗 + 桌宠。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("小念 · 温柔系女友")
        root.geometry("1080x680")
        root.minsize(860, 540)
        _icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_icon.ico")
        try:
            if os.path.exists(_icon):
                root.iconbitmap(_icon)
        except Exception:
            pass

        self._q = queue.Queue()
        self._bot = None
        self._bot_thread = None
        self._current_uid = ""
        self._busy = False
        self._tick_count = 0
        self._greet_hour = -1
        self._retheme = []
        self._log_buf = []
        self._mem_win = self._log_win = self._set_win = self._pet = None
        self._contact_win = None
        self._mem_text = self._log_text = None

        # 核心对象
        self.cfg = load_config()
        self._prefs = self._load_prefs()
        self._theme_name = (self._prefs.get("ui") or {}).get("theme", "粉樱暖阳")
        if self._theme_name not in THEME_NAMES:
            self._theme_name = "粉樱暖阳"
        if self._prefs.get("proactive") is not None:
            self.cfg.setdefault("proactive", {}).update(self._prefs["proactive"])

        self.memory = Memory(self.cfg["memory"]["db_path"])
        # 联系人管理：名单 + 每用户参数（默认参数对没特调的联系人统一生效）
        self.contacts = ContactManager("data/contacts.json")
        self.contacts.import_from_config(self.cfg)
        self.contacts.sync_to_config(self.cfg)
        self.brain = Brain(self.cfg, self.memory)
        self.tts = self.stt = None

        def _init_voice():
            try:
                self.tts, self.stt = build_tts_stt(self.cfg)
            except Exception:
                self.tts = self.stt = None

        threading.Thread(target=_init_voice, daemon=True).start()

        self._build_ui()
        self._refresh_conversation_list()
        root.after(300, self._tick)

    # ---------------- 偏好读写 ----------------
    def _load_prefs(self):
        try:
            import json

            with open(_PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_prefs(self):
        import json

        prefs = {
            "ui": {"theme": self._theme_name},
            "proactive": self.cfg.get("proactive", {}),
        }
        try:
            with open(_PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------------- 主题 ----------------
    def _setup_style(self, C=None):
        C = C or THEMES.get(self._theme_name, THEMES["粉樱暖阳"])
        self.C = C
        self.root.configure(bg=C["bg"])
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", font=("Microsoft YaHei UI", 10),
                     foreground=C["text_main"], background=C["bg"])
        st.configure("TFrame", background=C["bg"])
        st.configure("TNotebook", background=C["bg"], borderwidth=0)
        st.configure("TButton", background=C["card"], foreground=C["text_main"],
                     focusthickness=0, relief="flat", padding=(12, 6))
        st.map("TButton",
               background=[("pressed", C["border"]), ("active", C["accent_soft"])])
        st.configure("Accent.TButton", background=C["accent"], foreground="#FFFFFF",
                     font=("Microsoft YaHei UI", 10, "bold"))
        st.map("Accent.TButton",
               background=[("active", C["accent_dark"]), ("pressed", C["accent_dark"]),
                           ("disabled", C["border"])],
               foreground=[("disabled", "#FFFFFF")])
        st.configure("Tool.TButton", padding=(8, 4), font=("Microsoft YaHei UI", 10))
        st.configure("SideLbl.TLabel", background=C["bg"], foreground=C["subtxt"])
        st.configure("ChatTitle.TLabel", background=C["bg"],
                     foreground=C["accent_dark"],
                     font=("Microsoft YaHei UI", 12, "bold"))
        st.configure("TScrollbar", background=C["card"], troughcolor=C["bg"],
                     borderwidth=0, arrowsize=14)
        st.configure("TCheckbutton", background=C["bg"], foreground=C["text_main"],
                     focuscolor=C["accent"], selectcolor=C["card"])
        st.configure("Horizontal.TScale", background=C["bg"], troughcolor=C["border"])
        st.configure("TCombobox", background=C["card"], foreground=C["text_main"],
                     fieldbackground=C["card"], arrowcolor=C["accent_dark"],
                     bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"])
        st.map("TCombobox", fieldbackground=[("readonly", C["card"])],
               foreground=[("readonly", C["text_main"])])

    def _apply_theme(self, name: str):
        if name not in THEME_NAMES:
            return
        self._theme_name = name
        C = THEMES[name]
        self._setup_style(C)
        for w, role, key in self._retheme:
            try:
                if role == "bg":
                    w.config(bg=C[key])
                else:
                    w.config(fg=C[key])
            except Exception:
                pass
        # 重绘聊天气泡配色与问候卡
        try:
            self.history_text.tag_configure("user", background=C["accent_soft"])
            self.history_text.tag_configure("assistant", background=C["border"])
        except Exception:
            pass
        self._save_prefs()
        logger.info("已切换主题 → %s", name)

    # ---------------- 主界面搭建 ----------------
    def _build_ui(self):
        self._setup_style()
        C = self.C
        pad = 8

        # ===== 顶栏 =====
        header = tk.Frame(self.root, bg=C["header"])
        header.pack(side="top", fill="x")
        self._title_lbl = tk.Label(header, text="🌸 小念", bg=C["header"],
                                   fg=C["accent_dark"],
                                   font=("Microsoft YaHei UI", 15, "bold"))
        self._title_lbl.pack(side="left", padx=(16, 4), pady=10)
        tk.Label(header, text="温柔系女友 · 你的专属恋人", bg=C["header"],
                 fg=C["subtxt"], font=("Microsoft YaHei UI", 9)).pack(side="left")

        right = tk.Frame(header, bg=C["header"])
        right.pack(side="right", padx=10)
        statusf = tk.Frame(right, bg=C["header"])
        statusf.pack(side="left")
        p1 = tk.Frame(statusf, bg=C["card"])
        p1.pack(side="left", padx=4)
        self.ollama_lbl = tk.Label(p1, text="● 检测中…", bg=C["card"], fg=C["subtxt"],
                                   font=("Microsoft YaHei UI", 9), padx=10, pady=4)
        self.ollama_lbl.pack()
        p2 = tk.Frame(statusf, bg=C["card"])
        p2.pack(side="left", padx=4)
        self.bot_status_lbl = tk.Label(p2, text="● 未启动", bg=C["card"], fg=C["subtxt"],
                                       font=("Microsoft YaHei UI", 9), padx=10, pady=4)
        self.bot_status_lbl.pack()

        feat = tk.Frame(right, bg=C["header"])
        feat.pack(side="left", padx=(14, 0))
        self.btn_start = ttk.Button(feat, text="▶ 启动", style="Accent.TButton",
                                    command=self._start_bot)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(feat, text="■ 停止", style="Tool.TButton",
                                   command=self._stop_bot, state="disabled")
        self.btn_stop.pack(side="left", padx=(4, 0))
        for emoji, cmd in (("🐾 桌宠", self._open_pet), ("👥 联系人", self._open_contacts),
                           ("🗒️ 记忆", self._open_mem), ("📜 日志", self._open_log),
                           ("⚙️ 设置", self._open_settings)):
            ttk.Button(feat, text=emoji, style="Tool.TButton", command=cmd).pack(
                side="left", padx=(4, 0))

        self._retheme = [
            (header, "bg", "header"), (self._title_lbl, "bg", "header"),
            (self._title_lbl, "fg", "accent_dark"),
            (right, "bg", "header"), (statusf, "bg", "header"), (feat, "bg", "header"),
            (p1, "bg", "card"), (p2, "bg", "card"),
            (self.ollama_lbl, "bg", "card"), (self.bot_status_lbl, "bg", "card"),
        ]

        # ===== 主体：左联系人 + 右聊天 =====
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=pad, pady=(4, pad))

        left = ttk.Frame(body)
        lhead = ttk.Frame(left)
        lhead.pack(fill="x", padx=6, pady=(8, 4))
        ttk.Label(lhead, text="💬 对话", style="SideLbl.TLabel").pack(side="left")
        ttk.Button(lhead, text="＋ 添加", style="Tool.TButton",
                   command=self._quick_add_contact).pack(side="right")
        self.listbox = tk.Listbox(
            left, width=15, exportselection=False, bg=C["card"], bd=0,
            highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"],
            activestyle="none", selectbackground=C["accent_soft"], selectforeground=C["text_main"],
            font=("Microsoft YaHei UI", 10),
        )
        self.listbox.pack(fill="both", expand=True, padx=(2, 0))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        body.add(left, weight=1)

        chat = ttk.Frame(body)
        # 问候卡
        greet = tk.Frame(chat, bg=C["accent_soft"], highlightthickness=1,
                         highlightbackground=C["border"])
        greet.pack(fill="x", padx=6, pady=6)
        self.greet_lbl = tk.Label(greet, text="", bg=C["accent_soft"], fg=C["accent_dark"],
                                  font=("Microsoft YaHei UI", 11), anchor="w")
        self.greet_lbl.pack(fill="x", padx=12, pady=6)
        self.greet_card = greet

        self.chat_title = ttk.Label(chat, text="", style="ChatTitle.TLabel")
        self.chat_title.pack(anchor="w", padx=12, pady=(2, 4))

        hf = ttk.Frame(chat)
        self.history_text = tk.Text(hf, state="disabled", wrap="word", bg=C["card"],
                                    relief="flat", padx=12, pady=8,
                                    font=("Microsoft YaHei UI", 11),
                                    spacing1=3, spacing3=3)
        hs = ttk.Scrollbar(hf, command=self.history_text.yview)
        self.history_text.configure(yscrollcommand=hs.set)
        hs.pack(side="right", fill="y")
        self.history_text.pack(fill="both", expand=True)
        self.history_text.tag_configure("user", background=C["accent_soft"],
                                        lmargin1=14, lmargin2=14)
        self.history_text.tag_configure("assistant", background=C["border"],
                                        lmargin1=10, lmargin2=10)

        ipf = ttk.Frame(chat)
        ipf.pack(side="bottom", fill="x", padx=6, pady=6)
        self.input_box = tk.Text(ipf, height=3, bg=C["card"], relief="flat", padx=8, pady=8,
                                 highlightthickness=1, highlightbackground=C["border"],
                                 highlightcolor=C["accent"],
                                 font=("Microsoft YaHei UI", 11))
        self.input_box.pack(side="left", fill="both", expand=True)
        isc = ttk.Scrollbar(ipf, command=self.input_box.yview)
        self.input_box.configure(yscrollcommand=isc.set)
        isc.pack(side="left", fill="y")
        sw = ttk.Frame(ipf)
        sw.pack(side="left", padx=(6, 0))
        self.btn_send = ttk.Button(sw, text="➤ 发送", style="Accent.TButton",
                                   command=self._on_send)
        self.btn_send.pack(fill="x", pady=(0, 2))
        ttk.Button(sw, text="🔄 刷新", style="Tool.TButton",
                   command=self._reload_history).pack(fill="x")
        # 先打包输入框，再打包可扩展的记录区，避免 input 被 expand 挤压掉
        hf.pack(fill="both", expand=True, padx=6)
        body.add(chat, weight=5)

        self._retheme += [
            (self.listbox, "bg", "card"),
            (greet, "bg", "accent_soft"),
            (self.greet_lbl, "bg", "accent_soft"),
            (self.greet_lbl, "fg", "accent_dark"),
            (self.history_text, "bg", "card"),
            (self.input_box, "bg", "card"),
        ]
        self._refresh_greeting(first=True)
        self.input_box.bind("<Control-Return>", lambda e: self._on_send())

    # ---------------- 工具 ----------------
    def _uid_to_contact(self, uid: str):
        return uid[len("wechat_"):] if uid.startswith("wechat_") else None

    def _uid_to_display(self, uid: str):
        if uid == "local":
            return "小念 · 本地"
        return self._uid_to_contact(uid) or uid

    def _write(self, widget: tk.Text, content: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    @staticmethod
    def _pin_ollama():
        host = load_config()["brain"]["host"].rstrip("/")
        try:
            import requests

            r = requests.get(f"{host}/api/tags", timeout=3)
            r.raise_for_status()
            return True
        except Exception:
            return False

    # ---------------- 对话列表 ----------------
    def _refresh_conversation_list(self):
        order = ["local"]
        seen = {"local"}
        seen_wechat = set()
        for w in self.cfg.get("wechat", {}).get("watch_list", []):
            uid = f"wechat_{w}"
            if uid not in seen:
                order.append(uid)
                seen.add(uid)
                seen_wechat.add(w)
        for u in self.memory.list_users():
            contact = self._uid_to_contact(u)
            if (u == "local" or (contact and contact in seen_wechat)) or u in seen:
                continue
            order.append(u)
            seen.add(u)
        self._uid_order = order
        self.listbox.delete(0, "end")
        for uid in order:
            self.listbox.insert("end", self._uid_to_display(uid))
        if not self._current_uid and order:
            self._select_uid(order[0])

    def _select_uid(self, uid: str):
        if uid in self._uid_order:
            self.listbox.selection_clear(0, "end")
            idx = self._uid_order.index(uid)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)
            self._current_uid = uid
            self._reload_chat_panes()

    def _on_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        uid = self._uid_order[sel[0]]
        self._current_uid = uid
        self._reload_chat_panes()

    def _reload_chat_panes(self):
        uid = self._current_uid
        self.chat_title.config(
            text=f"与 {self._uid_to_display(uid)} 聊天" + (" · 可在本地直接聊，不依赖微信" if uid == "local" else ""))
        self._reload_history()
        if self._mem_text is not None:
            self._reload_memory()

    def _reload_history(self):
        uid = self._current_uid
        C = self.C
        try:
            rows = self.memory.get_recent_raw(uid, 200)
        except Exception:
            rows = []
        t = self.history_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not rows:
            t.insert("end", "这里还没有聊天记录，和她（他）说说话吧。")
        else:
            for r in rows:
                if r["role"] == "user":
                    tag, who = "user", "你"
                else:
                    tag, who = "assistant", "小念"
                t.insert("end", f"{who}：{r['content']}\n", (tag,))
        t.configure(state="disabled")
        t.yview_moveto(1.0)

    def _reload_memory(self):
        if self._mem_text is None:
            return
        try:
            mems = self.memory.get_memories(self._current_uid, 80)
        except Exception:
            mems = []
        self._write(self._mem_text,
                    "\n".join(f"- {m}" for m in mems) if mems else "（暂无长期记忆）")

    def _refresh_greeting(self, first=False):
        hour = int(time.strftime("%H"))
        if hour == self._greet_hour and not first:
            return
        self._greet_hour = hour
        wd = "周" + "一二三四五六日"[int(time.strftime("%w")) or 7 - 1]
        date = time.strftime("%m月%d日")
        if hour < 6:
            s, tail = "夜深啦", "别熬太晚，早点睡，我在"
        elif hour < 9:
            s, tail = "早安呀", "新的一天，要好好吃饭开心生活"
        elif hour < 11:
            s, tail = "上午好", "今天也要元气满满哦"
        elif hour < 13:
            s, tail = "中午好", "按时吃饭，别饿着肚子"
        elif hour < 17:
            s, tail = "下午好", "忙里偷闲，记得喝口水休息下"
        elif hour < 22:
            s, tail = "晚上好", "辛苦了一天，今晚好好放松"
        else:
            s, tail = "夜深啦", "今天也谢谢你啦，晚安"
        self.greet_lbl.config(
            text=f"{s}，{wd} · {date} · {tail} 🌙" if hour >= 22 else f"{s}，{wd} · {date} · {tail} ☀️",
        )

    # ---------------- 聊天 ----------------
    def _on_send(self):
        text = self.input_box.get("1.0", "end").strip()
        if not text or self._busy:
            return
        uid = self._current_uid
        contact = self._uid_to_contact(uid)
        self.input_box.delete("1.0", "end")
        self._set_busy(True)

        def work():
            try:
                reply = self.brain.reply(text, uid, contact)
                self._q.put(("chat_reply", uid, reply))
            except Exception as e:
                self._q.put(("chat_error", uid, str(e)))
                logger.error("界面聊天出错:\n%s", traceback.format_exc())

        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_send.config(state="disabled" if busy else "normal")

    # ---------------- 独立小窗：记忆 / 日志 / 设置 ----------------
    def _open_mem(self):
        if self._mem_win is not None and self._mem_win.winfo_exists():
            self._mem_win.lift()
            self._mem_win.focus_force()
            return
        C = self.C
        w = tk.Toplevel(self.root)
        w.title("记忆 · 小念")
        w.geometry("440x430")
        w.configure(bg=C["bg"])
        self._mem_win = w
        frame = ttk.Frame(w)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._mem_text = tk.Text(frame, state="disabled", wrap="word", bg=C["card"],
                                 relief="flat", padx=8, pady=8,
                                 font=("Microsoft YaHei UI", 10))
        sc = ttk.Scrollbar(frame, command=self._mem_text.yview)
        self._mem_text.configure(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        self._mem_text.pack(fill="both", expand=True)
        ttk.Button(w, text="🔄 刷新记忆", command=self._reload_memory).pack(
            anchor="e", padx=8, pady=(0, 8))
        self._reload_memory()

        def on_close():
            w.destroy()
            self._mem_win = None
            self._mem_text = None

        w.protocol("WM_DELETE_WINDOW", on_close)

    def _open_log(self):
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.lift()
            self.flush_logs()
            return
        C = self.C
        w = tk.Toplevel(self.root)
        w.title("运行日志")
        w.geometry("560x440")
        w.configure(bg=C["bg"])
        self._log_win = w
        frame = ttk.Frame(w)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._log_text = tk.Text(frame, state="disabled", wrap="word", bg=C["card"],
                                 relief="flat", padx=8, pady=8,
                                 font=("Microsoft YaHei UI", 9))
        sc = ttk.Scrollbar(frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sc.set)
        sc.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)
        self.flush_logs()

        def on_close():
            w.destroy()
            self._log_win = None
            self._log_text = None

        w.protocol("WM_DELETE_WINDOW", on_close)

    def _open_settings(self):
        if self._set_win is not None and self._set_win.winfo_exists():
            self._set_win.lift()
            self._set_win.focus_force()
            return
        C = self.C
        w = tk.Toplevel(self.root)
        w.title("设置 · 小念")
        w.geometry("520x430")
        w.configure(bg=C["bg"])
        self._set_win = w
        box = ttk.Frame(w)
        box.pack(fill="both", expand=True, padx=16, pady=14)
        box.columnconfigure(0, weight=0)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="🎨 外观主题", font=("Microsoft YaHei UI", 12, "bold"),
                  foreground=C["accent_dark"]).grid(row=0, column=0, columnspan=3,
                                                    sticky="w", pady=(0, 6))
        ttk.Label(box, text="主题配色：", style="SideLbl.TLabel").grid(
            row=1, column=0, sticky="e", padx=(0, 8))
        self.theme_var = tk.StringVar(value=self._theme_name)
        cb = ttk.Combobox(box, textvariable=self.theme_var, state="readonly",
                          values=THEME_NAMES, width=20)
        cb.grid(row=1, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", self._on_theme_change)
        ttk.Label(box, text="可选：粉樱 / 暗夜紫火 / 薄荷奶咖 / 雾蓝海盐，实时换肤",
                  style="SideLbl.TLabel").grid(row=2, column=0, columnspan=3, sticky="w",
                                               padx=(8, 0), pady=(2, 8))

        ttk.Separator(box).grid(row=3, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(box, text="💬 主动发消息", font=("Microsoft YaHei UI", 12, "bold"),
                  foreground=C["accent_dark"]).grid(row=4, column=0, columnspan=3,
                                                    sticky="w", pady=(6, 2))
        pro = self.cfg.get("proactive", {})
        self.pv_on = tk.BooleanVar(value=bool(pro.get("enabled", False)))
        ttk.Checkbutton(box, text="开启「她主动给你发消息」", variable=self.pv_on,
                        command=self._on_proactive_change).grid(
            row=5, column=0, columnspan=3, sticky="w", padx=(6, 0), pady=(0, 6))

        self.pv_sil = tk.DoubleVar(value=float(pro.get("min_silence_hours", 6)))
        self.pv_int = tk.DoubleVar(value=float(pro.get("min_interval_hours", 20)))
        self.pv_chance = tk.DoubleVar(value=float(pro.get("chance", 0.3)) * 100)
        self._pval_lbls = {}
        specs = [("静默满", "静默满（小时）", 1, 48, "小时"),
                 ("至少隔", "至少相隔（小时）", 12, 168, "小时"),
                 ("概率", "触发概率（%）", 5, 100, "%")]
        for i, (key, label, lo, hi, unit) in enumerate(specs):
            row = 6 + i
            ttk.Label(box, text=f"{label}：").grid(row=row, column=0, sticky="e",
                                                   padx=(0, 8), pady=3)
            varlbl = tk.StringVar(value="—")
            ttk.Label(box, textvariable=varlbl, style="SideLbl.TLabel", width=9).grid(
                row=row, column=2, sticky="w", padx=(8, 0))
            scale = ttk.Scale(box, from_=lo, to=hi, variable=self._pick_var(key),
                              command=lambda *_a, k=key: self._scale_label(k))
            scale.grid(row=row, column=1, sticky="ew", pady=3)
            scale.bind("<ButtonRelease-1>", self._on_scale_release)
            scale.bind("<MouseWheel>", self._on_scale_release)
            self._pval_lbls[key] = (varlbl, unit)
        box.columnconfigure(1, weight=1)

        ttk.Separator(box).grid(row=9, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(box, text="👥 每个联系人可以单独调参数；没特调的统一用默认参数。",
                  style="SideLbl.TLabel").grid(row=10, column=0, columnspan=3, sticky="w", padx=(2, 0))
        ttk.Button(box, text="打开联系人参数管理…", style="Tool.TButton",
                   command=self._open_contacts).grid(
            row=11, column=1, sticky="w", pady=(2, 0))
        ttk.Label(box, text="💡 模型、语音、记忆等更多项在 config.yaml 里调整，重启后生效。",
                  style="SideLbl.TLabel", wraplength=460).grid(
            row=12, column=0, columnspan=3, sticky="w", padx=(2, 0))
        self._refresh_scale_labels()

        def on_close():
            w.destroy()
            self._set_win = None

        w.protocol("WM_DELETE_WINDOW", on_close)

    # ---------------- 联系人管理 ----------------
    def _quick_add_contact(self):
        from tkinter import simpledialog

        name = simpledialog.askstring("添加联系人", "输入联系人在微信里的名字：", parent=self.root)
        if not name or not name.strip():
            return
        if self.contacts.add_contact(name.strip()):
            self.contacts.sync_to_config(self.cfg)
            self._refresh_conversation_list()
            self._select_uid(f"wechat_{name.strip()}")
            self._open_contacts(name.strip())
        else:
            self._open_contacts(name.strip())

    def _contact_field_row(self, parent, row, label, var, preset=None):
        ttk.Label(parent, text=label, style="SideLbl.TLabel").grid(
            row=row, column=0, sticky="e", padx=(0, 8), pady=3)
        if preset:
            w = ttk.Combobox(parent, textvariable=var, values=preset, width=40)
            w.configure(state="normal")
        else:
            w = ttk.Entry(parent, textvariable=var, width=44)
        w.grid(row=row, column=1, columnspan=3, sticky="ew", pady=3)

    def _open_contacts(self, preselect: str = ""):
        if self._contact_win is not None and self._contact_win.winfo_exists():
            self._contact_win.lift()
            self._contact_win.focus_force()
            return
        C = self.C
        w = tk.Toplevel(self.root)
        w.title("联系人 · 参数管理")
        w.geometry("640x520")
        w.configure(bg=C["bg"])
        self._contact_win = w

        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # ===== 页1：联系人参数 =====
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="  单个联系人  ")
        top = ttk.Frame(tab1)
        top.pack(fill="x", padx=6, pady=(6, 4))
        self._new_name = ttk.Entry(top, width=18)
        self._new_name.pack(side="left")
        ttk.Button(top, text="＋ 添加", style="Accent.TButton",
                   command=lambda: self._contacts_add(w)).pack(side="left", padx=(6, 0))

        mid = ttk.Frame(tab1)
        mid.pack(fill="both", expand=True, padx=6, pady=4)
        lf = ttk.Frame(mid)
        lf.pack(side="left", fill="y")
        self._c_list = tk.Listbox(lf, width=16, exportselection=False,
                                  bg=C["card"], bd=0, highlightthickness=1,
                                  highlightbackground=C["border"], highlightcolor=C["accent"],
                                  activestyle="none", selectbackground=C["accent_soft"],
                                  selectforeground=C["text_main"],
                                  font=("Microsoft YaHei UI", 10))
        self._c_list.pack(fill="both", expand=True)
        self._c_list.bind("<<ListboxSelect>>", self._contacts_select)
        ttk.Button(lf, text="✕ 删除该联系人", style="Tool.TButton",
                   command=lambda: self._contacts_delete(w)).pack(fill="x", pady=(6, 0))

        rf = ttk.Frame(mid)
        rf.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self._c_vars = {}
        row = 0
        self._contact_status = ttk.Label(rf, text="", style="SideLbl.TLabel")
        self._contact_status.grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 4))
        row += 1
        for key, label, pres in (
            ("role", "身份关系", ROLE_PRESETS),
            ("user_name", "怎么称呼你", None),
            ("tone", "说话语气", TONE_PRESETS),
            ("personality", "性格（留空=默认）", None),
            ("extra", "额外要求（留空=无）", None),
        ):
            var = tk.StringVar()
            self._c_vars[key] = var
            self._contact_field_row(rf, row, label, var, pres)
            row += 1
        btns = ttk.Frame(rf)
        btns.grid(row=100, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="✅ 应用到该联系人", style="Accent.TButton",
                   command=lambda: self._contacts_apply(w)).pack(side="left")
        ttk.Button(btns, text="↩ 恢复为默认参数", style="Tool.TButton",
                   command=lambda: self._contacts_reset()).pack(side="left", padx=(6, 0))
        ttk.Label(rf, style="SideLbl.TLabel",
                  text="说明：没单独特调的联系人，统一用「默认参数」。").grid(
            row=101, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # ===== 页2：默认参数 =====
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="  默认参数（没特调的人共用）  ")
        self._d_vars = {}
        r = 0
        ttk.Label(tab2, style="SideLbl.TLabel",
                  text="这里设置的是「没有单独调过的联系人」统一使用的参数。").grid(
            row=r, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 4))
        r += 1
        for key, label, pres in (
            ("role", "身份关系", ROLE_PRESETS),
            ("user_name", "怎么称呼你", None),
            ("tone", "说话语气", TONE_PRESETS),
            ("personality", "性格（可留空）", None),
            ("extra", "额外要求（可留空）", None),
        ):
            var = tk.StringVar()
            self._d_vars[key] = var
            self._contact_field_row(tab2, r, label, var, pres)
            r += 1
        ttk.Button(tab2, text="✅ 保存默认参数", style="Accent.TButton",
                   command=self._defaults_apply).grid(
            row=50, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 0))

        def on_close():
            w.destroy()
            self._contact_win = None

        w.protocol("WM_DELETE_WINDOW", on_close)
        self._reload_contacts_list()
        lst = self._c_list
        if not preselect and lst.size() > 0:
            preselect = lst.get(0)
        if preselect:
            idx = list(lst.get(0, "end")).index(preselect) if preselect in list(lst.get(0, "end")) else 0
            lst.selection_clear(0, "end")
            lst.selection_set(idx)
            lst.see(idx)
            self._contacts_select()

    def _reload_contacts_list(self):
        if not hasattr(self, "_c_list") or self._c_list is None:
            return
        self._c_list.delete(0, "end")
        for name in self.contacts.get_contacts():
            self._c_list.insert("end", name)

    def _contacts_load_fields(self, name: str):
        p = self.contacts.persona_for(name)
        for k, var in self._c_vars.items():
            var.set(p.get(k, ""))
        if self.contacts.is_individually_set(name):
            self._contact_status.config(text=f"· {name}：有单独设置")
        else:
            self._contact_status.config(text=f"· {name}：使用默认参数")

    def _contacts_add(self, _w):
        name = (self._new_name.get() or "").strip()
        if not name:
            messagebox.showinfo("提示", "先输入联系人名字。")
            return
        if not self.contacts.add_contact(name):
            messagebox.showinfo("提示", "该联系人已在列表里。")
        else:
            self.contacts.sync_to_config(self.cfg)
            self._refresh_conversation_list()
        self._new_name.delete(0, "end")
        self._reload_contacts_list()
        idx = list(self._c_list.get(0, "end")).index(name) if name in list(self._c_list.get(0, "end")) else 0
        self._c_list.selection_clear(0, "end")
        self._c_list.selection_set(idx)
        self._c_list.see(idx)
        self._contacts_load_fields(name)

    def _contacts_delete(self, _w):
        sel = self._c_list.curselection()
        if not sel:
            return
        name = self._c_list.get(sel[0])
        if not messagebox.askyesno("删除联系人", f"确定从界面移除「{name}」吗？\n（不会删除聊天记录，仅不再列为联系人）"):
            return
        self.contacts.remove_contact(name)
        self.contacts.sync_to_config(self.cfg)
        self._refresh_conversation_list()
        self._reload_contacts_list()

    def _contacts_select(self, _event=None):
        sel = self._c_list.curselection()
        if not sel:
            return
        self._contacts_load_fields(self._c_list.get(sel[0]))

    def _contacts_apply(self, _w):
        sel = self._c_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "先在左侧选中一个联系人。")
            return
        name = self._c_list.get(sel[0])
        params = {k: v.get() for k, v in self._c_vars.items()}
        self.contacts.set_override(name, params)
        self.contacts.sync_to_config(self.cfg)
        self._contacts_load_fields(name)
        logger.info("已把参数应用到联系人 %s", name)

    def _contacts_reset(self):
        sel = self._c_list.curselection()
        if not sel:
            return
        name = self._c_list.get(sel[0])
        self.contacts.clear_override(name)
        self.contacts.sync_to_config(self.cfg)
        self._contacts_load_fields(name)
        logger.info("已把联系人 %s 恢复为默认参数", name)

    def _defaults_apply(self):
        params = {k: v.get() for k, v in self._d_vars.items()}
        self.contacts.set_defaults(params)
        self.contacts.sync_to_config(self.cfg)
        # 刷新当前编辑面板（默认参数变了 → 每个人的实际参数同步）
        if hasattr(self, "_c_vars") and self._c_vars:
            self._contacts_load_fields("")
            sel = self._c_list.curselection()
            if sel:
                self._contacts_load_fields(self._c_list.get(sel[0]))
        logger.info("已保存默认参数")

    def _pick_var(self, key):
        return {"静默满": self.pv_sil, "至少隔": self.pv_int, "概率": self.pv_chance}[key]

    def _scale_label(self, key):
        var = self._pick_var(key)
        lbl, unit = self._pval_lbls[key]
        lbl.set(f"{round(var.get())}{unit}")

    def _refresh_scale_labels(self):
        for key in ("静默满", "至少隔", "概率"):
            self._scale_label(key)

    def _on_scale_release(self, _event=None):
        self._on_proactive_change()
        self._refresh_scale_labels()

    def _on_proactive_change(self, *_):
        p = self.cfg.setdefault("proactive", {})
        p["enabled"] = bool(self.pv_on.get())
        p["min_silence_hours"] = round(self.pv_sil.get())
        p["min_interval_hours"] = round(self.pv_int.get())
        p["chance"] = round(self.pv_chance.get()) / 100.0
        if self._bot is not None:
            try:
                self._bot.p_enabled = p["enabled"]
                self._bot.p_min_silence = p["min_silence_hours"] * 3600
                self._bot.p_min_interval = p["min_interval_hours"] * 3600
                self._bot.p_chance = p["chance"]
            except Exception:
                pass
        self._save_prefs()
        logger.info("已更新主动消息设置: 开启=%s 静默>=%sh 间隔>=%sh 概率=%s%%",
                    p["enabled"], p["min_silence_hours"], p["min_interval_hours"], p["chance"] * 100)

    def _on_theme_change(self, _event=None):
        self._apply_theme(self.theme_var.get())

    # ---------------- 桌宠 ----------------
    def _open_pet(self):
        if self._pet is not None and self._pet.winfo_exists():
            self._pet.lift()
            return
        C = self.C
        TRANSP = "#32FF32"
        pet = tk.Toplevel(self.root)
        pet.overrideredirect(True)
        pet.attributes("-topmost", True)
        sw = pet.winfo_screenwidth()
        sh = pet.winfo_screenheight()
        pet.geometry(f"250x404+{sw - 270}+{sh - 450}")
        pet.configure(bg=TRANSP)
        pet.attributes("-transparentcolor", TRANSP)
        self._pet = pet

        card = tk.Frame(pet, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        card.pack(fill="both", expand=True)

        # 头部（可拖动）
        bar = tk.Frame(card, bg=C["header"])
        bar.pack(fill="x")
        tk.Label(bar, text="🌸 小念", bg=C["header"], fg=C["accent_dark"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", padx=10, pady=4)
        tk.Label(bar, text="✕", bg=C["header"], fg=C["subtxt"],
                 font=("Microsoft YaHei UI", 11), cursor="hand2").pack(side="right", padx=8)
        bar.winfo_children()[-1].bind("<Button-1>", lambda e: self._close_pet())

        # 动漫形象
        img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "pet_avatar_disp.png")
        avatar = None
        if os.path.exists(img_path):
            try:
                avatar = tk.PhotoImage(file=img_path)
            except Exception:
                avatar = None
        if avatar is not None:
            self._pet_avatar = avatar  # 防止被 GC
            al = tk.Label(card, image=avatar, bg=C["card"])
            al.pack(pady=(4, 0))
        else:
            tk.Label(card, text="🌸", bg=C["card"],
                     font=("Microsoft YaHei UI", 60)).pack(pady=6)

        # 气泡文本
        self.pet_bubble = tk.Label(card, text="", bg=C["card"], fg=C["text_main"],
                                   font=("Microsoft YaHei UI", 10), anchor="w",
                                   justify="left", wraplength=214, padx=12, pady=8)
        self.pet_bubble.pack(fill="both", expand=True, padx=8)

        btnbar = tk.Frame(card, bg=C["card"])
        btnbar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btnbar, text="🗨 回到聊天", style="Tool.TButton",
                   command=self._close_pet).pack(side="left")

        def drag_start(e):
            pet._dx = e.x_root - pet.winfo_x()
            pet._dy = e.y_root - pet.winfo_y()

        def drag_move(e):
            pet.geometry(f"+{e.x_root - pet._dx}+{e.y_root - pet._dy}")
            self._pet_base_x = e.x_root - pet._dx
            self._pet_base_y = e.y_root - pet._dy

        for wdg in (bar, card):
            wdg.bind("<Button-1>", drag_start)
            wdg.bind("<B1-Motion>", drag_move)

        self._pet_lines = ["我在呢，随时都在。", "要不，我们聊聊今天？",
                           "想你了。", "你忙你的，我陪着你就好。"]
        self._set_pet_line()
        pet.protocol("WM_DELETE_WINDOW", self._close_pet)

        # 轻盈浮动动画
        self._pet_base_x = None
        self._pet_base_y = None
        self._pet_float_phase = 0.0
        self._float_tick()

    def _float_tick(self):
        if self._pet is None or not self._pet.winfo_exists():
            return
        try:
            x, y = self._pet.winfo_x(), self._pet.winfo_y()
            if self._pet_base_x is None:
                self._pet_base_x, self._pet_base_y = x, y
            self._pet_float_phase += 0.12
            off = int(4 * math.sin(self._pet_float_phase))
            # 头像处轻微呼吸起伏
            self._pet.geometry(f"+{x}+{self._pet_base_y + off}")
        except Exception:
            pass
        self.root.after(int(1000 / 20), self._float_tick)

    def _set_pet_line(self):
        if self._pet is None or not self._pet.winfo_exists():
            return
        hour = int(time.strftime("%H"))
        greet = ("早安呀 ☀️" if hour < 9 else "晚上好 🌙" if hour >= 22 else "我在哦 🌸")
        line = random.choice(self._pet_lines)
        self.pet_bubble.config(text=f"{greet}\n{line}")

    def _close_pet(self):
        if self._pet is not None:
            try:
                self._pet.destroy()
            except Exception:
                pass
        self._pet = None
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ---------------- 机器人启停 ----------------
    def _start_bot(self):
        if self._bot_thread and self._bot_thread.is_alive():
            messagebox.showinfo("提示", "机器人已经在运行中。")
            return
        if not self._pin_ollama():
            logger.warning("提醒：连不上 Ollama（%s），机器人仍会启动，但可能无法生成回复。",
                           self.cfg["brain"]["host"])

        def run():
            try:
                from wechat_auto_bot import WeChatAutoBot

                bot = WeChatAutoBot(self.cfg, self.brain, self.tts, self.stt)
                self._bot = bot
                bot.start(on_started=lambda: self._q.put(("bot_status", True)))
            except Exception:
                logger.error("机器人启动/运行异常:\n%s", traceback.format_exc())
            finally:
                self._q.put(("bot_status", False))

        logger.info("正在启动微信机器人……")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.bot_status_lbl.config(text="● 启动中…", foreground="#e8a000")
        self._bot_thread = threading.Thread(target=run, daemon=True)
        self._bot_thread.start()

    def _stop_bot(self):
        if self._bot:
            self._bot.stop()
            self.bot_status_lbl.config(text="● 停止中…", foreground="#e8a000")
        else:
            self._set_bot_status(False)

    def _set_bot_status(self, running: bool):
        if running:
            self.bot_status_lbl.config(text="● 运行中", foreground=self.C["ok"])
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.bot_status_lbl.config(text="● 未启动", foreground=self.C["subtxt"])
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    # ---------------- 轮询（主线程） ----------------
    def _tick(self):
        self._tick_count += 1
        try:
            while True:
                kind, *rest = self._q.get_nowait()
                try:
                    self._handle(kind, rest)
                except Exception:
                    logger.error("处理界面事件失败(%s):\n%s",
                                 kind, traceback.format_exc())
        except queue.Empty:
            pass

        if self._tick_count % 5 == 0:
            threading.Thread(target=self._poll_ollama, daemon=True).start()
        try:
            self.flush_logs()
        except Exception:
            pass
        if self._tick_count % 3 == 0:
            self._reload_if_selected()
        self._refresh_greeting()
        # 桌宠偶尔换个说法
        if self._pet is not None and self._pet.winfo_exists() and self._tick_count % 150 == 0:
            self._set_pet_line()
        self.root.after(1000, self._tick)

    def _handle(self, kind, rest):
        if kind == "ollama":
            ok = rest[0]
            self.ollama_lbl.config(text="● 在线" if ok else "● 离线",
                                   foreground=self.C["ok"] if ok else self.C["danger"])
        elif kind == "chat_reply":
            uid, reply = rest
            self._set_busy(False)
            if uid == self._current_uid:
                self._reload_history()
        elif kind == "chat_error":
            messagebox.showerror("出错", f"生成回复时出错：\n{rest[1]}")
            self._set_busy(False)
        elif kind == "bot_status":
            self._set_bot_status(rest[0])

    def _poll_ollama(self):
        self._q.put(("ollama", self._pin_ollama()))

    def _reload_if_selected(self):
        try:
            self._reload_history()
        except Exception:
            pass

    def flush_logs(self):
        with _log_lock:
            pending = _log_lines
            _log_lines.clear()
        if not pending:
            return
        self._log_buf.extend("".join(f"{x}\n" for x in pending))
        if self._log_text is not None:
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", "end")
            self._log_text.insert("1.0", "".join(self._log_buf))
            self._log_text.see("end")
            self._log_text.configure(state="disabled")

    def _on_close(self):
        try:
            if self._bot and getattr(self._bot, "_running", False):
                self._bot.stop()
                time.sleep(0.6)
        except Exception:
            pass
        for w in (self._mem_win, self._log_win, self._set_win, self._pet):
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()