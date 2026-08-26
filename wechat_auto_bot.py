"""微信接入（桌面自动化版 · 适配新版微信 4.x）

原理：
- 用 FindWindow 按 Qt 窗口类名快速定位微信主窗口
- 用 pywinauto UIA backend 连接
- 通过坐标过滤定位左侧聊天列表项（排除聊天消息区域的 ListItem）
- 通过类名 mmui::ChatInputField 定位聊天输入框
- 读取消息：扫聊天区域内的 Text 控件，按 Y 坐标排序，从下往上找对方发的

不依赖微信内部协议，新版微信 4.x 也能用。
"""
import ctypes
import logging
import random
import time
import traceback

import psutil
import pywinauto
from pywinauto import Application, keyboard
from ctypes import wintypes

import utils_audio

logger = logging.getLogger(__name__)

# Windows API
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class WeChatAutoBot:
    """新版微信 4.x 自动化机器人。"""

    MAIN_WIN_CLASS = "mmui::MainWindow"
    MAIN_WIN_TITLE = "微信"
    WIN32_WIN_CLASS = "Qt51514QWindowIcon"
    CHAT_CELL_CLASS = "mmui::ChatSessionCell"
    INPUT_FIELD_CLASS = "mmui::ChatInputField"

    # UIA 缓存有效期（秒）
    _CACHE_TTL = 0.5

    def __init__(self, config: dict, brain, tts=None, stt=None):
        self.cfg = config
        self.brain = brain
        self.tts = tts
        self.stt = stt
        wc = config["wechat"]
        self.watch_list = wc.get("watch_list") or ["文件传输助手"]
        if not wc.get("watch_list") and wc.get("listen_target"):
            self.watch_list = [wc["listen_target"]]
        vc = config["voice"]
        self.voice_enabled = vc.get("enabled", False) and utils_audio.voice_ok()
        self.reply_with_voice = (
            vc.get("reply_with_voice", False) and self.voice_enabled and tts is not None
        )
        self.app = None
        self.main_win = None
        self._pid = 0

        # 状态管理
        self._item_baseline = {}
        self._sent_state = {}
        self._current_chat = ""
        self._msg_area_top = 100
        self._msg_area_bottom = 700

        # 主动发消息（低频）配置与状态
        pro = config.get("proactive") or {}
        self.p_enabled = pro.get("enabled", False)
        self.p_min_silence = pro.get("min_silence_hours", 6) * 3600
        self.p_min_interval = pro.get("min_interval_hours", 20) * 3600
        self.p_chance = pro.get("chance", 0.3)
        self.p_only_main = pro.get("only_main", True)
        self.p_targets = pro.get("target") or []
        self._last_our_send = {}    # 联系人 -> 我们上次发消息的时间戳
        self._last_activity = {}    # 联系人 -> 双方上次互动（含收/发）的时间戳

        # 已处理消息去重（防止重复处理同一条）
        self._processed_msgs = {}  # {联系人: "已处理的消息文本"}

        # 启停控制（供 GUI 一键启动/停止）
        self._running = False

        # UIA 缓存
        self._cached_descendants = None
        self._cache_time = 0

    # ---------------- UIA 缓存 ----------------
    def _get_descendants(self, force: bool = False):
        """获取 UIA 后代控件列表，带 500ms 缓存。

        微信 UIA 树有 75+ 元素，每次 descendants() 耗时 0.5-1 秒，
        缓存可大幅减少启动和轮询延迟。
        """
        now = time.time()
        if not force and self._cached_descendants and (now - self._cache_time) < self._CACHE_TTL:
            return self._cached_descendants
        try:
            self._cached_descendants = list(self.main_win.descendants())
            self._cache_time = now
            return self._cached_descendants
        except Exception:
            return []

    def _invalidate_cache(self):
        """强制下次刷新缓存。"""
        self._cached_descendants = None
        self._cache_time = 0

    # ---------------- 启动 ----------------
    def start(self, on_started=None):
        logger.info("正在连接 PC 微信 4.x（桌面自动化模式）……")
        if not self._connect():
            logger.error("连接微信失败，请确认微信已打开并登录")
            return
        logger.info("已连接微信主窗口（PID=%s）", self._pid)
        logger.info("监听列表: %s", ", ".join(self.watch_list))
        logger.info("语音功能: %s | 用语音回复: %s", self.voice_enabled, self.reply_with_voice)

        self._detect_msg_area()
        self._refresh_baseline()
        logger.info("已记录 %d 个聊天列表项的基线文本", len(self._item_baseline))

        # 主动发消息状态初始化（避免启动后立刻触发）
        now = time.time()
        for n in self.watch_list:
            self._last_activity[n] = now
            self._last_our_send[n] = now
        if self.p_enabled:
            logger.info(
                "主动发消息: 开启 | 静默≥%.0fh 开始考虑 | 至少间隔 %.0fh | 概率 %.0f%%",
                self.p_min_silence / 3600, self.p_min_interval / 3600, self.p_chance * 100,
            )

        logger.warning(
            "\n【使用说明】\n"
            "  1. 微信窗口保持打开（可最小化但不能关）\n"
            "  2. 程序扫描左侧列表，发现监听对象发来新消息就回复\n"
            "  3. 监听对象: %s\n"
            "  按 Ctrl+C / 停止按钮 退出",
            " / ".join(self.watch_list),
        )

        self._running = True
        if on_started:
            try:
                on_started()
            except Exception:
                logger.exception("启动回调异常")
        try:
            while self._running:
                try:
                    self._poll_once()
                except KeyboardInterrupt:
                    break
                except Exception:
                    logger.error("轮询出错:\n%s", traceback.format_exc())
                try:
                    self._maybe_proactive()
                except KeyboardInterrupt:
                    break
                except Exception:
                    logger.error("主动发消息出错:\n%s", traceback.format_exc())
                time.sleep(2)
        finally:
            self._running = False
            logger.info("已退出微信监听")

    def stop(self):
        """请求停止监听（优雅退出，最多等一个轮询周期）。"""
        logger.info("正在停止微信监听……")
        self._running = False

    # ---------------- 连接微信 ----------------
    def _connect(self) -> bool:
        hwnd = user32.FindWindowW(self.WIN32_WIN_CLASS, self.MAIN_WIN_TITLE)
        if not hwnd:
            hwnd = self._find_main_window_fallback()
        if not hwnd:
            logger.error("没找到微信主窗口，请确认微信已打开并登录")
            return False
        try:
            self.main_win = pywinauto.Application(backend="uia").connect(handle=hwnd).window()
            self.app = self.main_win.element_info.app
            self._pid = self._get_pid_from_hwnd(hwnd)
            self._bring_to_front()
            return True
        except Exception:
            pass
        return self._connect_by_process()

    def _find_main_window_fallback(self) -> int:
        result = [0]

        def _enum_cb(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == self.MAIN_WIN_TITLE:
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                if "Qt" in cls_buf.value or "mmui" in cls_buf.value:
                    result[0] = hwnd
                    return False
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return result[0]

    @staticmethod
    def _get_pid_from_hwnd(hwnd: int) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _connect_by_process(self) -> bool:
        procs = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                name = (p.info["name"] or "").lower()
                if "weixin.exe" in name or "wechat.exe" in name:
                    procs.append((p.info["pid"], p.info["name"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not procs:
            logger.error("没找到 Weixin.exe / WeChat.exe 进程")
            return False
        for pid, name in procs:
            try:
                app = Application(backend="uia").connect(process=pid, timeout=3)
                wins = app.windows()
                for w in wins:
                    t = w.window_text() or ""
                    if t == self.MAIN_WIN_TITLE:
                        self.app = app
                        self.main_win = w
                        self._pid = pid
                        self._bring_to_front()
                        return True
            except Exception:
                continue
        return False

    def _bring_to_front(self):
        try:
            hwnd = self.main_win.handle
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            self.main_win.set_focus()
        except Exception:
            pass

    # ---------------- 左侧聊天列表 ListItem ----------------
    def _get_chat_list_items(self):
        """获取左侧聊天列表区域的 ListItem（过滤掉聊天消息区域的）。"""
        result = []
        for c in self._get_descendants():
            if getattr(c.element_info, "control_type", "") != "ListItem":
                continue
            try:
                rect = c.rectangle()
                if rect.top < 270 or rect.top > 620:
                    continue
                if rect.left > 360:
                    continue
                result.append(c)
            except Exception:
                continue
        return result

    def _refresh_baseline(self, only: str = None):
        self._invalidate_cache()
        items = self._get_chat_list_items()
        if only is None:
            # 全量刷新（启动/兜底用）
            self._item_baseline = {}
        for item in items:
            try:
                t = item.window_text() or ""
                name = t.split("\n")[0].strip()
                if not name:
                    continue
                if only is not None:
                    # 只更新当次处理的联系人，避免把其他人未处理的新消息顶成"已读"
                    if name == only:
                        self._item_baseline[name] = t
                        self._sent_state[name] = t
                else:
                    self._item_baseline[name] = t
            except Exception:
                continue

    # ---------------- 切换聊天（可靠版） ----------------
    def _switch_to_chat(self, target: str) -> bool:
        """切到指定聊天。

        策略：用 _current_chat 追踪当前所在的聊天。
        如果目标 == _current_chat，说明已经在那，直接返回。
        否则点击切换。不再依赖 is_selected()（Qt 上不可靠）。
        """
        try:
            self._bring_to_front()
            time.sleep(0.3)

            # 如果已经在目标聊天，直接返回
            if self._current_chat == target:
                logger.info("已在 '%s' 聊天中", target)
                return True

            self._invalidate_cache()
            items = self._get_chat_list_items()

            target_item = None
            for item in items:
                try:
                    t = item.window_text() or ""
                    if target in t.split("\n")[0]:
                        target_item = item
                        break
                except Exception:
                    continue

            if not target_item:
                logger.error("左侧列表没找到 '%s'", target)
                return False

            target_item.click_input()
            time.sleep(1.2)
            self._invalidate_cache()
            self._current_chat = target
            logger.info("已切到聊天: %s", target)
            return True
        except Exception as e:
            logger.error("切换聊天失败: %s", e)
            return False

    # ---------------- 探测聊天消息区域 ----------------
    def _detect_msg_area(self):
        try:
            edits = [
                c for c in self._get_descendants()
                if getattr(c.element_info, "control_type", "") == "Edit"
                and c.element_info.class_name == self.INPUT_FIELD_CLASS
            ]
            if edits:
                rect = edits[0].rectangle()
                self._msg_area_bottom = rect.top - 10
                logger.info("消息区域下边界: y=%d", self._msg_area_bottom)
            win_rect = self.main_win.rectangle()
            self._msg_area_top = win_rect.top + 80
        except Exception:
            pass

    # ---------------- 读取当前聊天最后一条消息 ----------------
    def _get_last_msg_text(self) -> str:
        """读取当前聊天区域里最后一条对方发来的消息。

        微信 4.x 把聊天消息暴露为 ListItem（class=mmui::ChatTextItemView），
        而不是 Text 控件。所以这里扫 ListItem 而非 Text。
        """
        try:
            items = [
                c for c in self._get_descendants()
                if getattr(c.element_info, "control_type", "") == "ListItem"
                and c.element_info.class_name == "mmui::ChatTextItemView"
            ]
            msgs = []
            for c in items:
                try:
                    t = c.window_text() or ""
                    t = t.strip()
                    if not t:
                        continue
                    rect = c.rectangle()
                    if rect.top < self._msg_area_top or rect.top > self._msg_area_bottom:
                        continue
                    msgs.append((rect.top, rect.left, t))
                except Exception:
                    continue

            if not msgs:
                logger.debug("_get_last_msg_text: 无 ChatTextItemView (top=%d, bottom=%d)",
                           self._msg_area_top, self._msg_area_bottom)
                return ""

            msgs.sort(key=lambda x: x[0], reverse=True)
            win_rect = self.main_win.rectangle()
            mid_x = (win_rect.left + win_rect.right) // 2
            for y, left, t in msgs:
                if left < mid_x:
                    logger.debug("_get_last_msg_text: 找到对方消息: %s", t[:50])
                    return t
            logger.debug("_get_last_msg_text: 全是靠右消息，取最下一条: %s", msgs[0][2][:50])
            return msgs[0][2]
        except Exception as e:
            logger.debug("_get_last_msg_text 异常: %s", e)
            return ""

    def _collect_user_msgs(self) -> list:
        """读取当前聊天区域里所有【对方（靠左）】的消息文本，按时间正序返回。"""
        try:
            items = [
                c for c in self._get_descendants()
                if getattr(c.element_info, "control_type", "") == "ListItem"
                and c.element_info.class_name == "mmui::ChatTextItemView"
            ]
            rows = []
            for c in items:
                try:
                    t = c.window_text() or ""
                    t = t.strip()
                    if not t:
                        continue
                    rect = c.rectangle()
                    if rect.top < self._msg_area_top or rect.top > self._msg_area_bottom:
                        continue
                    rows.append((rect.top, rect.left, t))
                except Exception:
                    continue
            if not rows:
                return []
            rows.sort(key=lambda r: r[0])  # 正序：往上越靠上越旧，下越新
            win_rect = self.main_win.rectangle()
            mid_x = (win_rect.left + win_rect.right) // 2
            return [t for (_, left, t) in rows if left < mid_x]  # 靠左=对方发的
        except Exception as e:
            logger.debug("_collect_user_msgs 异常: %s", e)
            return []

    def _get_new_user_msgs(self) -> list:
        """返回比上次已处理更新、且属于对方的新增消息（正序）。

        首次（还没有已处理标记）时不重放历史，只取最新一条。
        """
        received = self._collect_user_msgs()
        if not received:
            return []
        last = self._processed_msgs.get(self._current_chat, "")
        if not last:
            return [received[-1]]
        idx = -1
        for i, t in enumerate(received):
            if t == last:
                idx = i
                break
        if idx < 0:
            # 已处理的这条已不在可见列表里（被顶上去了），保守只取最后一条
            return [received[-1]]
        return received[idx + 1:]

    # ---------------- 单次轮询 ----------------
    def _poll_once(self):
        """每 2 秒执行一次：扫描左侧 ListItem 文本变化。

        流程：
        1. 扫 ListItem → 找文本变了的（且在 watch_list 中）
        2. 过滤 AI 自己发的（预览以"我:"开头）
        3. 过滤已处理过的消息（去重）
        4. 切到目标聊天 → 读完整消息 → 调大脑 → 发回复
        5. 发完后存 sent_state + 标记已处理
        """
        try:
            self._bring_to_front()
        except Exception:
            pass

        self._invalidate_cache()
        items = self._get_chat_list_items()

        changed = []
        for item in items:
            try:
                t = item.window_text() or ""
                name = t.split("\n")[0].strip()
                if not name:
                    continue
                if not any(w in name for w in self.watch_list):
                    continue

                # 1. sent_state 对比（AI 发完后的状态）
                sent = self._sent_state.get(name, "")
                if sent:
                    if t == sent:
                        continue
                    lines = t.split("\n")
                    preview = lines[1] if len(lines) > 1 else ""
                    if preview.startswith("我:") or preview.startswith("我："):
                        self._sent_state[name] = t
                        self._item_baseline[name] = t
                        continue
                    changed.append((name, item, t))
                else:
                    # 2. baseline 对比（首次）
                    old = self._item_baseline.get(name, "")
                    if t != old:
                        lines = t.split("\n")
                        preview = lines[1] if len(lines) > 1 else ""
                        if preview.startswith("我:") or preview.startswith("我："):
                            self._item_baseline[name] = t
                            continue
                        changed.append((name, item, t))
            except Exception:
                continue

        if not changed:
            return

        name, item, new_text = changed[0]
        self._last_activity[name] = time.time()

        # 3. 切到正确聊天
        if not self._switch_to_chat(name):
            logger.error("切到 '%s' 失败", name)
            return

        # 4. 读完整消息：收集所有新增的对方消息（不只最后一条）
        new_msgs = self._get_new_user_msgs()
        if not new_msgs:
            fb = self._get_last_msg_text()
            new_msgs = [fb] if fb else []

        if not new_msgs:
            logger.warning("切到 %s 后读不到新增消息，跳过并更新 sent_state 防止循环", name)
            self._sent_state[name] = new_text
            if not self._processed_msgs.get(name):
                self._processed_msgs[name] = "__EMPTY__"
            return

        # 5. 清洗：语音占位 + 跳过自己发的/空消息
        clean = []
        for m in new_msgs:
            m = (m or "").strip()
            if not m:
                continue
            if m.startswith("我:") or m.startswith("我："):
                continue
            if "语音" in m and len(m) < 20:
                m = "[收到一条语音，暂未识别]"
            clean.append(m)
        if not clean:
            self._sent_state[name] = new_text
            return

        # 合并多条为一段，让大脑一次看到全部（能逐条回应多个问题）
        msg = "\n".join(clean)
        last_bubble = clean[-1]
        logger.info("[收到 %d 条] %s: %s", len(clean), name,
                    msg[:80].replace("\n", " / "))

        # 5.5 去重：已处理过的最新一条（且本轮没有真正新增）则跳过
        if len(clean) == 1 and last_bubble == self._processed_msgs.get(name, ""):
            logger.debug("跳过重复消息: %s", last_bubble[:30])
            self._sent_state[name] = new_text
            return

        # 6. 调大脑（传入联系人名，支持专属人设切换）
        try:
            reply = self.brain.reply(msg, f"wechat_{name}", contact=name)
        except Exception as e:
            logger.error("大脑生成回复失败: %s", e)
            reply = random.choice(["嗯…我现在有点反应不过来，稍等一下哦", "哎我得缓一下，等会儿回你哈", "等下哈，我脑子转不过来，一会儿回你"])

        # 6.5 模拟真人打字+思考时间（按对方消息长度分层延迟）
        self._typing_delay(msg)

        # 7. 发送回复（长消息拆成两条，模拟真人逐条发）
        self._send_reply_split(reply)
        self._last_our_send[name] = time.time()
        self._last_activity[name] = time.time()

        # 8. 标记已处理（记最近一条对方消息，方便下次识别"新增了哪些"）+ 更新状态
        self._processed_msgs[name] = last_bubble
        time.sleep(1.5)
        self._refresh_baseline(only=name)
        for it in self._get_chat_list_items():
            try:
                t = it.window_text() or ""
                n = t.split("\n")[0].strip()
                if n == name:
                    self._sent_state[name] = t
                    break
            except Exception:
                continue

    # ---------------- 主动发消息（低频） ----------------
    def _proactive_candidates(self):
        if self.p_targets:
            return [n for n in self.p_targets if n in self.watch_list]
        if self.p_only_main and self.watch_list:
            return [self.watch_list[0]]
        return list(self.watch_list)

    def _maybe_proactive(self):
        """在静默期尝试主动给某个联系人发一条消息（低频 + 概率门控）。"""
        if not self.p_enabled:
            return
        now = time.time()
        for name in self._proactive_candidates():
            if now - self._last_our_send.get(name, now) < self.p_min_interval:
                continue
            if now - self._last_activity.get(name, now) < self.p_min_silence:
                continue
            if random.random() >= self.p_chance:
                continue
            self._proactive_send(name)
            return

    def _proactive_send(self, name: str):
        """切到目标聊天，生成并发送一条主动消息。"""
        logger.info("[主动] 打算给 %s 发一条消息", name)
        self._last_our_send[name] = time.time()  # 先占位，防止本循环重复触发
        self._last_activity[name] = time.time()

        if not self._switch_to_chat(name):
            logger.error("[主动] 切到 '%s' 失败", name)
            return

        # 模拟先想了下怎么发
        time.sleep(random.uniform(1.0, 3.0))

        try:
            reply = self.brain.proactive_reply(f"wechat_{name}", contact=name)
        except Exception as e:
            logger.error("[主动] 生成失败: %s", e)
            return

        if not reply.strip():
            reply = random.choice(["突然想跟你说句话，在忙嘛？", "在忙没有呀？想跟你说个事", "突然想你了，干嘛呢"])

        self._send_reply_split(reply)

        # 更新状态，避免这条被误判为新消息
        time.sleep(1.5)
        self._refresh_baseline(only=name)
        self._processed_msgs[name] = "__PROACTIVE__"
        for it in self._get_chat_list_items():
            try:
                t = it.window_text() or ""
                n = t.split("\n")[0].strip()
                if n == name:
                    self._sent_state[name] = t
                    break
            except Exception:
                continue

    # ---------------- 模拟真人打字延迟 ----------------
    @staticmethod
    def _typing_delay(user_text: str):
        """根据对方消息长度模拟真人打字+思考时间（整体控制在 1.5~8 秒内）。

        另外，从检测到消息到真正发出还包含：轮询等待 + 切换聊天 + 生成回复，
        所以这里刻意压得更短，避免整体拖到 10 秒以上让人觉得隔太久。
        """
        msg_len = len(user_text)
        if msg_len <= 10:
            delay = random.uniform(1.5, 3.2)   # 短消息快回
        elif msg_len <= 30:
            delay = random.uniform(3, 6)       # 正常语速
        else:
            delay = random.uniform(4.5, 7.5)   # 长消息多思考
        # 小概率有点小动作，但只轻微加时间，绝不拖到 10 秒开外
        if random.random() < 0.1:
            delay += random.uniform(1.5, 3)
        delay = min(delay, 8.0)
        logger.info("模拟打字延迟 %.1f 秒", delay)
        time.sleep(delay)

    # ---------------- 长消息拆分成两条发送 ----------------
    def _send_reply_split(self, reply: str):
        """超过 40 字且包含句号则拆分，模拟真人逐条发消息。"""
        if len(reply) > 40 and "。" in reply:
            parts = reply.split("。", 1)
            part1 = parts[0] + "。"
            part2 = parts[1].strip()
            if part1 and part2:
                self._send_text(part1)
                time.sleep(random.uniform(1, 2.5))  # 模拟打第二句的间隔
                self._send_text(part2)
                return
        self._send_text(reply)

    # ---------------- 发送文字 ----------------
    def _send_text(self, text: str) -> bool:
        try:
            self._bring_to_front()
            time.sleep(0.2)
            self._invalidate_cache()
            edits = [
                c for c in self._get_descendants()
                if getattr(c.element_info, "control_type", "") == "Edit"
                and c.element_info.class_name == self.INPUT_FIELD_CLASS
            ]
            if not edits:
                logger.error("找不到输入框")
                return False
            edit = edits[0]
            edit.click_input()
            time.sleep(0.2)
            self._copy_to_clipboard(text)
            keyboard.send_keys("^v")
            time.sleep(0.3)
            keyboard.send_keys("{ENTER}")
            logger.info("[发送] %s", text[:80])
            return True
        except Exception as e:
            logger.error("发送文字失败: %s", e)
            return False

    @staticmethod
    def _copy_to_clipboard(text: str):
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        if not user32.OpenClipboard(0):
            raise RuntimeError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                raise MemoryError("GlobalAlloc failed")
            lock = kernel32.GlobalLock(h)
            if not lock:
                raise MemoryError("GlobalLock failed")
            try:
                ctypes.memmove(lock, data, len(data))
            finally:
                kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
        finally:
            user32.CloseClipboard()
