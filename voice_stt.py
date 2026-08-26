"""语音识别（STT）：收到的微信语音 -> 文字。

流程：silk -> wav -> faster-whisper 识别。
依赖：faster-whisper、ffmpeg、graiax-silkcoder。
"""
import logging
import os

import utils_audio

logger = logging.getLogger(__name__)


class STT:
    def __init__(self, config: dict):
        from faster_whisper import WhisperModel

        model = config["voice"].get("stt_model", "small")
        # CPU + int8，兼容无显卡机器；有 N 卡可改 device="cuda"
        self._model = WhisperModel(model, device="cpu", compute_type="int8")
        self._ready = True

    def transcribe(self, silk_path: str) -> str:
        """silk 文件 -> 文字。失败返回空串。"""
        if not utils_audio.probe_audio(silk_path):
            return ""
        wav_path = silk_path + ".wav"
        try:
            utils_audio.silk_to_wav(silk_path, wav_path)
            return self.transcribe_wav(wav_path)
        finally:
            try:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

    def transcribe_wav(self, wav_path: str) -> str:
        """wav 文件 -> 文字。失败返回空串。"""
        try:
            segments, _ = self._model.transcribe(wav_path, language="zh", vad_filter=True)
            return "".join(s.text for s in segments).strip()
        except Exception as e:
            logger.error("语音识别失败: %s", e)
            return ""
