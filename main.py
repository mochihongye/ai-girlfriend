"""我的 AI 女友 · 主入口

运行模式：
  python main.py             # （默认）本地聊天：命令行直接对话 + 语音朗读 + 语音输入
  python main.py wechat      # 微信机器人：基于桌面自动化（pywinauto），不依赖微信版本
  python main.py --help      # 帮助
"""
import argparse
import logging
import os
import sys

import yaml

from brain import Brain
from memory import Memory

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT, encoding="utf-8")
logger = logging.getLogger("ai-girlfriend")


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        logger.error("找不到配置文件 %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_ollama(config: dict) -> bool:
    import requests

    host = config["brain"]["host"].rstrip("/")
    try:
        r = requests.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        logger.info("Ollama 在线，已装模型: %s", ", ".join(models) if models else "（无）")
        if config["brain"]["model"] not in models:
            logger.warning(
                "模型 %s 未找到，请运行: ollama pull %s",
                config["brain"]["model"],
                config["brain"]["model"],
            )
        return True
    except Exception as e:
        logger.error("连不上 Ollama (%s)：%s", host, e)
        logger.error("请确认 Ollama 已安装并运行（后台托盘有图标）。")
        return False


def build_tts_stt(config: dict):
    tts = stt = None
    if config["voice"].get("enabled"):
        import utils_audio

        if not utils_audio.voice_ok():
            logger.warning("语音依赖不完整（缺 pysilk 或 ffmpeg），本次以纯文字模式运行。")
        else:
            try:
                from voice_tts import TTS

                tts = TTS(config)
                logger.info("TTS 就绪")
            except Exception as e:
                logger.warning("TTS 初始化失败: %s", e)
            try:
                from voice_stt import STT

                stt = STT(config)
                logger.info("STT 就绪")
            except Exception as e:
                logger.warning("STT 初始化失败: %s", e)
    return tts, stt


def run_local(config: dict):
    """本地聊天模式：命令行直接对话，无需微信。"""
    memory = Memory(config["memory"]["db_path"])
    brain = Brain(config, memory)

    if not check_ollama(config):
        logger.error("Ollama 不可用，退出。")
        sys.exit(1)

    tts, stt = build_tts_stt(config)

    # 本地聊天界面（pyaudio / pygame 缺失时自动降级）
    try:
        from local_chat import run_local_chat

        run_local_chat(brain, config, tts, stt)
    except KeyboardInterrupt:
        logger.info("再见～")


def run_wechat(config: dict):
    """微信机器人模式：基于 pywinauto 桌面自动化，不依赖微信版本。"""
    memory = Memory(config["memory"]["db_path"])
    brain = Brain(config, memory)

    if not check_ollama(config):
        logger.error("Ollama 不可用，退出。")
        sys.exit(1)

    tts, stt = build_tts_stt(config)

    # 新版：基于 pywinauto 桌面自动化（替换原 WeChatFerry 方案）
    from wechat_auto_bot import WeChatAutoBot

    bot = WeChatAutoBot(config, brain, tts, stt)
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("再见～")


def main():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="我的 AI 女友：默认运行本地聊天。加 `wechat` 用桌面自动化接入 PC 微信。",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="local",
        choices=["local", "wechat"],
        help="运行模式：local（默认，本地聊天） / wechat（PC 微信自动化）",
    )
    args = parser.parse_args()

    config = load_config()

    if args.mode == "wechat":
        run_wechat(config)
    else:
        run_local(config)


if __name__ == "__main__":
    main()
