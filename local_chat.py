"""本地聊天界面：命令行直接与 AI 女友对话，无需微信。

功能：
- 文字输入 / 文字输出
- 语音输入（按 R 录制一段，自动识别成文字）
- 语音回复（TTS 自动朗读她的回答）
"""
import logging
import os
import tempfile
import time
import wave

logger = logging.getLogger("local")


# ---------- 录音功能：仅 Windows，用系统自带 API ----------
class Recorder:
    def __init__(self):
        self._ok = False
        try:
            import pyaudio  # noqa: F401

            self._ok = True
        except ImportError:
            logger.warning("未安装 pyaudio，语音输入不可用（pip install pyaudio）。")
        self._format_map = None

    def ok(self) -> bool:
        return self._ok

    def record_wav(self, seconds: int = 5) -> str:
        """按秒录音，返回 wav 文件路径。按 Ctrl+C 提前结束。"""
        if not self._ok:
            raise RuntimeError("pyaudio 未安装")
        import pyaudio

        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000

        p = pyaudio.PyAudio()
        stream = p.open(
            format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK
        )
        frames = []
        print(f"🎙 开始录音 ({seconds}s，按 Ctrl+C 结束)…", end="", flush=True)
        try:
            for _ in range(int(RATE / CHUNK * seconds)):
                data = stream.read(CHUNK)
                frames.append(data)
        except KeyboardInterrupt:
            pass
        print(" 完成")
        stream.stop_stream()
        stream.close()
        p.terminate()

        path = os.path.join(tempfile.mkdtemp(prefix="rec_"), "input.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(p.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(b"".join(frames))
        return path


# ---------- TTS 播放：pygame 播放 mp3 ----------
class Speaker:
    def __init__(self):
        self._have_mp3 = False
        self._have_player = False
        try:
            import pygame

            self._pygame = pygame
            self._have_player = True
        except ImportError:
            logger.warning("未安装 pygame，TTS 播放不可用（pip install pygame）。")

    def ok(self) -> bool:
        return self._have_player

    def play_mp3(self, mp3_path: str):
        """播放 mp3 文件，完成后返回。"""
        self._pygame.mixer.init()
        self._pygame.mixer.music.load(mp3_path)
        self._pygame.mixer.music.play()
        while self._pygame.mixer.music.get_busy():
            time.sleep(0.1)
        self._pygame.mixer.music.unload()


# ---------- CLI 主流程 ----------
HELP = """
命令：
  任意文字  →  直接对话
  :r 或 :录音  →  开始录音（语音输入）
  :v 或 :语音开关  →  切换是否用 TTS 朗读回复
  :m 或 :记忆  →  查看当前长期记忆
  :h 或 :?    →  显示帮助
  :q 或 :退出  →  退出
"""


def run_local_chat(brain, config: dict, tts=None, stt=None):
    p = config["persona"]
    voice_cfg = config["voice"]
    voice_enabled = voice_cfg.get("enabled", False) and bool(tts)
    use_tts_reply = voice_enabled and voice_cfg.get("reply_with_voice", False)

    rec = Recorder()
    sp = Speaker()

    if voice_enabled and not sp.ok():
        logger.warning("TTS 模块已就绪，但播放不可用（缺少 pygame），语音将只生成不播放。")

    print(f"\n{'='*50}")
    print(f"💖 {p['name']} 已上线")
    print(f"  她：{p['role']}，称你为「{p.get('user_name','你')}」")
    print(f"  模式：本地聊天  |  TTS朗读回复：{'开' if use_tts_reply else '关'}  |  语音输入：{'支持' if rec.ok() else '不支持'}")
    print(f"{'='*50}")
    print(HELP)

    while True:
        try:
            line = input(f"\n{p.get('user_name','你')} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        # 命令
        if line == ":q" or line == ":exit" or line == ":quit" or line == ":退出":
            break
        if line == ":h" or line == ":?" or line == ":help":
            print(HELP)
            continue
        if line == ":v" or line == ":语音开关":
            use_tts_reply = not use_tts_reply
            print(f"  → TTS 朗读回复：{'开' if use_tts_reply else '关'}")
            continue
        if line == ":m" or line == ":记忆":
            mems = brain.memory.get_memories("local_user", 50)
            print(f"\n  📝 长期记忆（共 {len(mems)} 条）：")
            if mems:
                for i, m in enumerate(mems, 1):
                    print(f"     {i}. {m}")
            else:
                print("     （暂无）")
            continue
        if line == ":r" or line == ":录音":
            if not rec.ok():
                print("  ✗ 语音输入不可用，请先 pip install pyaudio")
                continue
            if not stt:
                print("  ✗ STT 未初始化，无法语音识别")
                continue
            try:
                wav = rec.record_wav(seconds=6)
            except Exception as e:
                print(f"  ✗ 录音失败: {e}")
                continue
            try:
                text = stt.transcribe_wav(wav)
            except Exception as e:
                print(f"  ✗ 识别失败: {e}")
                continue
            if not text:
                print("  ✗ 没听清楚，请重新录音")
                continue
            print(f"  👂 识别结果：{text}")
            line = text

        # 生成回复
        answer = brain.reply(line, "local_user")
        print(f"\n{p['name']} > {answer}")

        # TTS 朗读：表情自动清洗 + 多音字纠正 + 整段一次合成（语气连贯不中断）
        if use_tts_reply and tts:
            try:
                mp3_path = tts.synthesize_to_mp3(answer)
                if sp.ok():
                    print("  🔊 (正在朗读…)", end="", flush=True)
                    sp.play_mp3(mp3_path)
                    print("  ✓")
            except Exception as e:
                logger.warning("朗读失败: %s", e)

    print(f"\n💖 {p['name']}：{p.get('user_name','你')}先忙，有空再来找我～")
