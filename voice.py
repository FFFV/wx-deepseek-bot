# -*- coding: utf-8 -*-
"""把文字变成语音，并播到指定的声卡上。

两条腿各解决一个问题：

* **合成** —— 按顺序试四种引擎，哪个能出声用哪个（结果会写进日志）：
  1. ``edge``：在线 edge-tts（晓晓等），音色最好，需要 ``pip install edge-tts``
  2. ``winrt``：Windows 自带语音（OneCore，64 位）
  3. ``powershell``：.NET System.Speech（另一套封装）
  4. ``sapi``：SAPI5 的 SpFileStream（最老的路子）
  全失败才报错，不会让机器人卡住——发不出语音就退回打字。
* **播放** —— winmm 的 ``waveOut``。``winsound`` 只能播到默认设备，而微信发语音条
  **只认麦克风**，所以得把声音灌进虚拟声卡的播放端（VB-Cable 的 ``CABLE Input``），
  再从它的录音端（``CABLE Output``）当麦克风喂给微信。``waveOut`` 可以点名播到
  某一台设备，不用改系统默认设备（改了会把你正在听的歌/会议声音一起劫走）。

命令行自检::

    python voice.py --list                 # 列出现在能播的声卡
    python voice.py --test "你好呀"        # 合成 + 播到 CABLE Input
    python voice.py --test "你好呀" --device 扬声器 --save x.wav
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import wave
from ctypes import wintypes
from typing import List, Optional, Tuple

WAVE_FORMAT_PCM = 1
WHDR_DONE = 0x00000001
WAVE_MAPPER = -1
MAXPNAMELEN = 32

_winmm = ctypes.WinDLL("winmm", use_last_error=True)


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


_winmm.waveOutGetNumDevs.restype = wintypes.UINT
_winmm.waveOutGetDevCapsW.argtypes = [
    ctypes.c_size_t, ctypes.POINTER(WAVEOUTCAPS), wintypes.UINT]
_winmm.waveOutGetDevCapsW.restype = wintypes.UINT
_winmm.waveOutOpen.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
    ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
_winmm.waveOutOpen.restype = wintypes.UINT
_winmm.waveOutPrepareHeader.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
_winmm.waveOutPrepareHeader.restype = wintypes.UINT
_winmm.waveOutWrite.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
_winmm.waveOutWrite.restype = wintypes.UINT
_winmm.waveOutUnprepareHeader.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
_winmm.waveOutUnprepareHeader.restype = wintypes.UINT
_winmm.waveOutClose.argtypes = [wintypes.HANDLE]
_winmm.waveOutClose.restype = wintypes.UINT


class VoiceError(RuntimeError):
    """合成/播放失败（调用方据此退回发文字）。"""


# 按住说话的快捷键（微信电脑版：按住右 Alt 说话，松开发送）
# 值是 (虚拟键码, 扫描码, 是否扩展键)。扫描码不能省：右 Alt 是扩展键，只给
# VK 不给扫描码的话，靠扫描码判断 AltGr 的程序会当成左 Alt，微信就不认。
_VK = {
    "alt_r": (0xA5, 0x38, True),
    "alt_l": (0xA4, 0x38, False),
    "ctrl_r": (0xA3, 0x1D, True),
    "ctrl_l": (0xA2, 0x1D, False),
    "shift_r": (0xA1, 0x36, False),
    "shift_l": (0xA0, 0x2A, False),
    "space": (0x20, 0x39, False),
}

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def vk_for(name: str):
    """快捷键名 → (vk, 扫描码, 扩展键)；不认识返回 None。"""
    return _VK.get((name or "").strip().lower())


def _send_key(vk: int, scan: int, extended: bool, up: bool) -> None:
    user32 = ctypes.windll.user32
    flags = KEYEVENTF_KEYUP if up else 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD,
                u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, None)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def key_down(key) -> None:
    _send_key(key[0], key[1], key[2], up=False)


def key_up(key) -> None:
    _send_key(key[0], key[1], key[2], up=True)


def _check(rc: int, what: str) -> None:
    if rc:
        raise VoiceError("%s 失败：MMRESULT=0x%X" % (what, rc))


# ────────────────────────────── 播放 ──────────────────────────────


def list_outputs() -> List[Tuple[int, str]]:
    """[(设备号, 名字)]，设备号可以直接喂给 :func:`play_wav`。"""
    out = []
    for i in range(_winmm.waveOutGetNumDevs()):
        caps = WAVEOUTCAPS()
        if _winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname))
    return out


def find_output(name: str) -> Optional[int]:
    """按名字子串找设备号；``name`` 为空时返回 None（= 默认设备）。

    优先精确匹配，避免 "CABLE Input" 撞上别的带 CABLE 字样的虚拟设备。
    """
    name = (name or "").strip()
    if not name:
        return None
    names = list_outputs()
    for idx, n in names:
        if n.lower() == name.lower():
            return idx
    for idx, n in names:
        if name.lower() in n.lower():
            return idx
    return None


def play_wav(path: str, device: Optional[int] = None, timeout: float = 60.0) -> float:
    """把 wav 播完再返回（阻塞），返回音频时长（秒）。

    ``device=None`` 表示播到系统默认设备。播放失败抛 :class:`VoiceError`。
    """
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
        duration = w.getnframes() / float(rate or 1)

    wfx = WAVEFORMATEX(WAVE_FORMAT_PCM, nch, rate, rate * nch * sw, nch * sw, sw * 8, 0)
    handle = wintypes.HANDLE()
    rc = _winmm.waveOutOpen(ctypes.byref(handle), WAVE_MAPPER if device is None else device,
                            ctypes.byref(wfx), 0, 0, 0)
    _check(rc, "waveOutOpen（设备 %s）" % ("默认" if device is None else device))

    buf = ctypes.create_string_buffer(raw)
    hdr = WAVEHDR(ctypes.cast(buf, ctypes.c_void_p), len(raw), 0, 0, 0, 0, None, 0)
    try:
        _check(_winmm.waveOutPrepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr)),
               "waveOutPrepareHeader")
        _check(_winmm.waveOutWrite(handle, ctypes.byref(hdr), ctypes.sizeof(hdr)),
               "waveOutWrite")
        t0 = time.time()
        while not (hdr.dwFlags & WHDR_DONE):
            if time.time() - t0 > timeout:
                raise VoiceError("播放超时（%.0fs）" % timeout)
            time.sleep(0.02)
    finally:
        try:
            _winmm.waveOutUnprepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
        except Exception:
            pass
        _winmm.waveOutClose(handle)
    return duration


def hw_output_name(device: Optional[int]) -> str:
    """设备号 → 名字（日志里看得懂）。"""
    if device is None:
        return "默认设备"
    for idx, name in list_outputs():
        if idx == device:
            return name
    return "设备 %s" % device


# ────────────────────────────── 合成 ──────────────────────────────


def _wav_ok(path: str) -> bool:
    """生成的 wav 至少得有半秒钟的声音，别把空文件当成功。"""
    try:
        if os.path.getsize(path) < 4000:
            return False
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 1
            return w.getnframes() / float(rate) > 0.3
    except Exception:
        return False


def _to_wav(src: str, out_path: str) -> str:
    """mp3 → 22kHz 单声道 wav（播放那边只认这个格式）。"""
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", src,
                    "-ar", "22050", "-ac", "1", out_path],
                   check=True, capture_output=True, timeout=60)
    try:
        os.remove(src)
    except OSError:
        pass
    return out_path


def _synth_breeze(text: str, out_path: str, rate: int = 0, pitch: int = 0,
                  voice_name: str = "", breeze_key: str = "",
                  breeze_voice: str = "", **kw) -> str:
    """BreezeBlue 在线合成（Breeze TTS 2，开放权重里排第一，最像真人）。

    需要 ``pip install breeze-blue`` + API key（config 的 voice_breeze_key，
    或环境变量 BREEZE_API_KEY）+ 一个音色 id（voice_breeze_voice）。
    """
    try:
        from breeze_blue import BreezeBlue
    except Exception as exc:
        raise VoiceError("没装 breeze-blue（pip install breeze-blue）：%r" % (exc,)) from None

    key = (breeze_key or os.environ.get("BREEZE_API_KEY", "")).strip()
    if not key:
        raise VoiceError("没配 BreezeBlue 的 API key：config.json 的 voice_breeze_key，"
                         "或环境变量 BREEZE_API_KEY")
    voice = (breeze_voice or os.environ.get("BREEZE_VOICE_ID", "")).strip()
    if not voice:
        raise VoiceError("没配音色 id：config.json 的 voice_breeze_voice（形如 voc_xxx）")

    settings = {}
    if rate:
        settings["speed"] = max(0.5, min(2.0, 1.0 + rate / 100.0))
    kwargs = {"voice_settings": settings} if settings else {}
    try:
        client = BreezeBlue(api_key=key, timeout=60.0)
        audio = client.text_to_speech.convert(
            voice_id=voice, text=text, output_format="mp3", **kwargs)
    except Exception as exc:
        raise VoiceError("BreezeBlue 合成失败：%r" % (exc,)) from None

    mp3 = out_path + ".mp3"
    try:
        audio.save(mp3)
    except Exception:
        with open(mp3, "wb") as f:
            f.write(audio.content)
    if not os.path.exists(mp3) or os.path.getsize(mp3) < 1000:
        raise VoiceError("BreezeBlue 没返回音频")
    return _to_wav(mp3, out_path)


def _synth_edge(text: str, out_path: str, rate: int = 0, pitch: int = 0,
                voice_name: str = "", **kw) -> str:
    """在线合成：edge-tts（微软 Edge 的朗读接口，免费、不用 key）。

    音色比系统自带的好一个时代：`zh-CN-XiaoxiaoNeural`（温柔）、
    `zh-CN-XiaoyiNeural`（元气少女）、`zh-CN-YunxiNeural`（男）。
    需要 ``pip install edge-tts``；没装就直接抛错，让 auto 跳到下一个引擎。
    """
    try:
        import asyncio

        import edge_tts
    except Exception as exc:
        raise VoiceError("没装 edge-tts（pip install edge-tts）：%r" % (exc,)) from None

    voice = (voice_name or "").strip() or "zh-CN-XiaoxiaoNeural"
    kwargs = {}
    if rate:
        kwargs["rate"] = "%+d%%" % int(rate)
    if pitch:
        kwargs["pitch"] = "%+dHz" % int(round(pitch * 1.5))   # edge 的音高用 Hz

    mp3 = out_path + ".mp3"

    async def _run():
        await asyncio.wait_for(
            edge_tts.Communicate(text, voice, **kwargs).save(mp3), timeout=30)

    asyncio.run(_run())
    if not os.path.exists(mp3) or os.path.getsize(mp3) < 1000:
        raise VoiceError("edge-tts 没拿到音频（网络不通？音色名写错了？）")

    # 统一转成 22kHz 单声道 wav，播放那边就不用管格式了
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", mp3,
                    "-ar", "22050", "-ac", "1", out_path],
                   check=True, capture_output=True, timeout=60)
    try:
        os.remove(mp3)
    except OSError:
        pass
    return out_path


def _ssml(text: str, rate: int = 0, pitch: int = 0) -> str:
    """拼 SSML：语速/音高都是用百分比调（正数=更快更亮，像个小姑娘）。"""
    from xml.sax.saxutils import escape

    body = escape(text)
    attrs = []
    if rate:
        attrs.append('rate="%+d%%"' % int(rate))
    if pitch:
        attrs.append('pitch="%+d%%"' % int(pitch))
    if attrs:
        body = "<prosody %s>%s</prosody>" % (" ".join(attrs), body)
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="zh-CN">%s</speak>' % body)


def _synth_winrt(text: str, out_path: str, rate: int = 0, pitch: int = 0,
                 voice_name: str = "", **kw) -> str:
    """Windows 自带语音（OneCore）。64 位原生，音色比 SAPI5 的 DeskTop 版好听。"""
    import asyncio

    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    from winsdk.windows.storage.streams import DataReader

    async def _run() -> bytes:
        synth = SpeechSynthesizer()
        try:
            voices = list(SpeechSynthesizer.all_voices or [])
            want = (voice_name or "").lower()
            picked = None
            if want:
                picked = next((v for v in voices
                               if want in (getattr(v, "display_name", "") or "").lower()), None)
            if picked is None:
                picked = next((v for v in voices
                               if "zh" in (getattr(v, "language", "") or "").lower()), None)
            if picked is not None:
                synth.voice = picked
        except Exception:
            pass
        stream = await synth.synthesize_text_to_stream_async(text)
        size = int(stream.size)
        if size <= 0:
            raise VoiceError("WinRT 合成返回空流")
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        arr = (ctypes.c_ubyte * size)()
        reader.read_bytes(arr)
        return bytes(arr)

    data = asyncio.run(_run())
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


_PS_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$ssml = [IO.File]::ReadAllText('__SSML__', [Text.Encoding]::UTF8)
$plain = [IO.File]::ReadAllText('__TXT__', [Text.Encoding]::UTF8)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$want = '__VOICE__'
if ($want -ne '') {
    foreach ($v in $s.GetInstalledVoices()) {
        if ($v.VoiceInfo.Name -like "*$want*") { try { $s.SelectVoice($v.VoiceInfo.Name) } catch {}; break }
    }
}
$s.SetOutputToWaveFile('__WAV__')
try { $s.SpeakSsml($ssml) } catch { $s.Speak($plain) }
$s.Dispose()
"""


def _synth_powershell(text: str, out_path: str, rate: int = 0, pitch: int = 0,
                      voice_name: str = "", **kw) -> str:
    """借 .NET 的 System.Speech 合成（另一套封装，SAPI 出问题时常常还能用）。

    音色用 SSML 的 prosody 调（语速/音高）；引擎不认 SSML 时退回纯文本朗读。
    """
    txt_path = out_path + ".txt"
    ssml_path = out_path + ".ssml"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(ssml_path, "w", encoding="utf-8") as f:
        f.write(_ssml(text, rate, pitch))
    script = (_PS_SCRIPT
              .replace("__TXT__", txt_path.replace("'", "''"))
              .replace("__SSML__", ssml_path.replace("'", "''"))
              .replace("__VOICE__", (voice_name or "").replace("'", "''"))
              .replace("__WAV__", out_path.replace("'", "''")))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=90)
    finally:
        for p in (txt_path, ssml_path):
            try:
                os.remove(p)
            except OSError:
                pass
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise VoiceError("System.Speech 失败：%s" % (msg.splitlines()[-1][:200] if msg else "rc=%d" % proc.returncode))
    return out_path


def _synth_sapi(text: str, out_path: str, rate: int = 0, pitch: int = 0,
                voice_name: str = "", **kw) -> str:
    """SAPI5 直接写文件（最老的路子）。"""
    try:
        import pywin32_bootstrap  # noqa: F401  pywin32 的 DLL 搜索路径
        import win32com.client as win32
    except Exception as exc:
        raise VoiceError("没装 pywin32：%r" % (exc,)) from None

    try:
        voice = win32.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        want = (voice_name or "").lower()
        for i in range(tokens.Count):
            desc = tokens.Item(i).GetDescription() or ""
            low = desc.lower()
            if (want and want in low) or (not want and ("chinese" in low or "中文" in desc)):
                voice.Voice = tokens.Item(i)
                break
        if rate:
            voice.Rate = int(rate) // 10      # SAPI 的 Rate 是 -10~10，百分比除以 10
        stream = win32.Dispatch("SAPI.SpFileStream")
        stream.Open(out_path, 3, False)      # 3 = SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Speak(_ssml(text, rate, pitch), 8)   # 8 = SVSFIsXML
        stream.Close()
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        raise VoiceError("SAPI 合成失败：%r%s" % (
            exc, "" if hresult is None else "（hresult=0x%08X）" % (hresult & 0xFFFFFFFF))) from None
    return out_path


# 按「好听 → 能出声」排队；engine="auto" 时依次尝试
# edge 需要 pip 装；没装会在 import 那步就失败，跳到下一个
ENGINES = (
    ("breeze", _synth_breeze),
    ("edge", _synth_edge),
    ("winrt", _synth_winrt),
    ("powershell", _synth_powershell),
    ("sapi", _synth_sapi),
)


def speak(text: str, out_dir: str, engine: str = "auto", prefix: str = "v",
          rate: int = 0, pitch: int = 0, voice_name: str = "",
          breeze_key: str = "", breeze_voice: str = "") -> Tuple[str, str]:
    """合成一段语音，返回 ``(wav 路径, 实际用的引擎)``。

    ``engine="auto"`` 时按 :data:`ENGINES` 顺序试；全失败抛 :class:`VoiceError`
    （错误里带上每个引擎的失败原因，方便一眼看出缺什么）。
    ``rate`` / ``pitch`` 是百分比（正数=更快/更亮），``voice_name`` 是音色名的一段。
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("没有可以念的文字")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s_%s.wav" % (prefix, time.strftime("%H%M%S")))

    wanted = (engine or "auto").strip().lower()
    opts = dict(rate=rate, pitch=pitch, voice_name=voice_name,
                breeze_key=breeze_key, breeze_voice=breeze_voice)
    tried = []
    for name, fn in ENGINES:
        if wanted not in ("auto", name):
            continue
        try:
            fn(text, out, **opts)
        except Exception as exc:
            tried.append("%s: %s" % (name, exc))
            continue
        if _wav_ok(out):
            return out, name
        tried.append("%s: 生成的 wav 不完整" % name)
    raise VoiceError("没一种合成方式成功 → " + "；".join(tried or ["没有可用的引擎"]))


# ────────────────────────────── 自检 CLI ──────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if "--list" in args or not args:
        print("可播放的声卡：")
        for idx, name in list_outputs():
            print("  [%d] %s" % (idx, name))
        return 0

    if "--test" in args:
        i = args.index("--test")
        text = args[i + 1] if len(args) > i + 1 else "你好呀，我是小鲸鱼。"
        needle = "CABLE Input"
        if "--device" in args:
            needle = args[args.index("--device") + 1]
        device = find_output(needle)
        if device is None:
            print("没找到声卡「%s」，会播到默认设备。现有设备：" % needle)
            for idx, name in list_outputs():
                print("  [%d] %s" % (idx, name))
        else:
            print("播放到：%s" % hw_output_name(device))

        out_dir = os.path.join(os.path.abspath("."), "_voice")
        t0 = time.time()
        try:
            path, used = speak(text, out_dir)
        except VoiceError as exc:
            print("[合成失败] %s" % exc)
            return 1
        print("合成 ok（%s）：%s（%.0f KB，%.1fs）"
              % (used, path, os.path.getsize(path) / 1024, time.time() - t0))
        if "--save" in args:
            import shutil

            dst = args[args.index("--save") + 1]
            shutil.copyfile(path, dst)
            print("已另存：%s" % dst)
        t0 = time.time()
        try:
            dur = play_wav(path, device)
        except VoiceError as exc:
            print("[播放失败] %s" % exc)
            return 1
        print("播放 ok：音频 %.2fs，实际用了 %.2fs" % (dur, time.time() - t0))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
