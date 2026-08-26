"""音频转码工具：微信 SILK <-> wav/pcm 互转（基于 pysilk-mod + ffmpeg）。

依赖：
- pip install pysilk-mod
- pip install imageio-ffmpeg（自带 ffmpeg.exe，无需系统安装）
  也可用系统 PATH 里的 ffmpeg 作为备选。

任一依赖缺失时，voice_ok() 返回 False，主程序会自动降级为纯文字模式。
"""
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

_OK = None
_FFMPEG = None


def _ffmpeg_bin() -> str:
    """返回可用的 ffmpeg 可执行文件路径，找不到返回空串。"""
    global _FFMPEG
    if _FFMPEG is not None:
        return _FFMPEG
    # 优先用 imageio-ffmpeg 自带的 ffmpeg
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _FFMPEG = exe
            return _FFMPEG
    except Exception:
        pass
    # 备选：系统 PATH 里的 ffmpeg
    _FFMPEG = shutil.which("ffmpeg") or ""
    return _FFMPEG


def voice_ok() -> bool:
    """检测语音转码依赖是否齐全。"""
    global _OK
    if _OK is not None:
        return _OK
    try:
        import pysilk  # noqa: F401
    except Exception as e:
        logger.warning("pysilk-mod 未安装，语音功能将降级: %s", e)
        _OK = False
        return _OK
    if not _ffmpeg_bin():
        logger.warning("未找到 ffmpeg（imageio-ffmpeg 或系统 ffmpeg），语音功能将降级")
        _OK = False
        return _OK
    _OK = True
    return _OK


def mp3_to_silk(mp3_path: str, silk_path: str) -> str:
    """mp3 -> silk（用于发送语音）。流程：ffmpeg 解码为 PCM -> pysilk 编码。"""
    import pysilk

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError("没有可用的 ffmpeg")
    # ffmpeg: mp3 -> raw PCM (s16le, mono, 24000Hz) 输出到 stdout
    result = subprocess.run(
        [ffmpeg, "-y", "-i", mp3_path, "-ar", "24000", "-ac", "1", "-f", "s16le", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg 解码失败: " + result.stderr.decode(errors="ignore")[:500]
        )
    pcm = result.stdout
    if not pcm:
        raise RuntimeError("ffmpeg 未输出 PCM 数据")
    silk_bytes = pysilk.encode(pcm, data_rate=24000, sample_rate=24000)
    with open(silk_path, "wb") as f:
        f.write(silk_bytes)
    return silk_path


def silk_to_wav(silk_path: str, wav_path: str) -> str:
    """silk -> wav（用于识别收到的语音）。"""
    import pysilk

    with open(silk_path, "rb") as f:
        silk_bytes = f.read()
    if not silk_bytes:
        raise RuntimeError("空的 silk 文件")
    # WeChat 语音通常为 24000Hz；识别失败可尝试改为 16000
    wav_bytes = pysilk.decode(silk_bytes, to_wav=True, sample_rate=24000)
    with open(wav_path, "wb") as f:
        f.write(wav_bytes)
    return wav_path


def probe_audio(path: str) -> bool:
    """文件是否是可读音频。"""
    if not path or not os.path.exists(path):
        return False
    return os.path.getsize(path) > 0