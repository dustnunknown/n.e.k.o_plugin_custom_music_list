"""
网易云音乐歌单导出插件

功能：
- 扫码登录网易云音乐
- 查看收藏和创建的所有歌单（支持分页、搜索）
- 导出歌单内所有歌曲名和作者信息到 txt 文件（以歌单名命名）
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import struct
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, List, Optional
from urllib.parse import ParseResult, urlencode, urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    get_plugin_logger,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

_BASE = "https://music.163.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _mp4_walk_boxes(data: bytes, end_offset: int):
    """遍历 [0, end_offset) 范围内的所有 MP4 box，yield (box_type_4bytes, body_data, abs_pos)。
    处理 32-bit/64-bit size、size=0（扩展到末尾）。"""
    pos = 0
    while pos + 8 <= end_offset and pos + 8 <= len(data):
        sz = struct.unpack(">I", data[pos:pos + 4])[0]
        tp = data[pos + 4:pos + 8]
        if sz == 0:
            sz = end_offset - pos
        elif sz == 1:
            if pos + 16 > len(data):
                return
            sz = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        if sz < 8:
            return
        body = data[pos + 8:pos + sz]
        yield (tp, body, pos)
        pos += sz


def _mp4_duration_sane(sec: float, file_bytes: int) -> bool:
    """校验解析出的时长是否合理：必须为正，且对应码率不低于 12kbps
    （防止 sidx/moof 在 mdat 音频数据里误匹配导致时长上亿秒）。"""
    if not sec or sec <= 0:
        return False
    if file_bytes <= 0:
        return sec < 86400  # 无体积信息时上限 24h
    min_bitrate = 12000  # 12kbps 下限，已非常宽松
    max_sec = file_bytes * 8 / min_bitrate
    return sec <= max_sec


def _normalize_match_text(text: str) -> str:
    """用于音频文件名模糊匹配的文本标准化：去空格、去常见标点、转小写。"""
    if not text:
        return ""
    s = re.sub(r"[\s\u3000\-_—–\-·•・,，。.!?！?、/\\:：;；'\"`()()【】《》\[\]\{\}]+", "", text)
    return s.lower().strip()

# ── weapi 加密常量 ──────────────────────────────────────
_WEAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
_WEAPI_IV = b"0102030405060708"
_WEAPI_PUB_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b72515"
    "2b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbd"
    "a92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cf"
    "e4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
_WEAPI_PUB_EXP = int("010001", 16)


def _aes_cbc_encrypt(text: str, key: bytes) -> str:
    cipher = Cipher(algorithms.AES(key), modes.CBC(_WEAPI_IV), backend=default_backend())
    encryptor = cipher.encryptor()
    data = text.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len]) * pad_len
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")


def _rsa_encrypt(random_key: str) -> str:
    text_int = int(random_key[::-1].encode("utf-8").hex(), 16)
    return format(pow(text_int, _WEAPI_PUB_EXP, _WEAPI_PUB_MODULUS), "0>256x")


def _weapi_encrypt(data: dict) -> dict:
    """将请求数据加密为 weapi 格式 (params + encSecKey)"""
    sec_key = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16))
    text = json.dumps(data, separators=(",", ":"))
    params = _aes_cbc_encrypt(_aes_cbc_encrypt(text, _WEAPI_PRESET_KEY), sec_key.encode("utf-8"))
    return {"params": params, "encSecKey": _rsa_encrypt(sec_key)}


def _headers(cookie: Optional[str] = None) -> dict:
    h = {"User-Agent": _UA, "Referer": f"{_BASE}/"}
    if cookie:
        h["Cookie"] = cookie
    return h


def _sanitize_filename(name: str) -> str:
    """清理 Windows 文件名非法字符"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".")
    return cleaned or "playlist"




class _QQApiError(Exception):
    """QQ音乐接口业务错误，携带 code 与提示，便于向上暴露真实原因。"""

    def __init__(self, code, msg: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"code={code} {msg}".strip())


@staticmethod
def _write_txt(path: Path, name: str, songs: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"歌单名称: {name}",
        f"歌曲总数: {len(songs)}",
        f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 40,
        "",
    ]
    for idx, s in enumerate(songs, 1):
        artist = s["artists"] or "未知"
        lines.append(f"{idx}. {s['name']} - {artist}")
    path.write_text("\n".join(lines), encoding="utf-8")


@staticmethod
def _parse_txt(path: Path) -> dict:
    """解析导出的 txt 文件，返回 {playlist_name, songs: [{name, artists, path}]}"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    playlist_name = path.stem
    songs: List[dict] = []
    for line in lines:
        line = line.strip()
        # 匹配 "序号. 歌名 - 歌手" 格式
        m = re.match(r"^(\d+)\.\s*(.+?)\s*-\s*(.+)$", line)
        if m:
            songs.append(
                {"name": m.group(2).strip(), "artists": m.group(3).strip(), "path": ""}
            )
        else:
            # 尝试匹配 "歌单名称: xxx"
            m2 = re.match(r"^歌单名称:\s*(.+)$", line)
            if m2:
                playlist_name = m2.group(1).strip()
    return {"playlist_name": playlist_name, "songs": songs}


@staticmethod
def _parse_http_url(url: str) -> ParseResult | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if not str(parsed.hostname or "").strip():
        return None
    try:
        parsed.port
    except ValueError:
        return None
    return parsed


@staticmethod
def _default_port_for_scheme(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


@staticmethod
def _strip_data_uri(data_uri: str) -> str:
    """去掉 data:xxx;base64, 前缀"""
    idx = data_uri.find(",")
    if idx >= 0 and data_uri[:5].lower().startswith("data:"):
        return data_uri[idx + 1:]
    return data_uri


@staticmethod
def _mp4_duration_parse(path: Path) -> Optional[float]:
    """多策略 MP4/M4A 时长解析（处理 DASH/fragmented MP4）:
    1) moov/mvhd 时长（普通 MP4，用电影时标）
    2) sidx（segment index）时长（DASH，用媒体时标）
    3) moof/traf/tfdt + trun 累加（fragmented MP4，用媒体时标 mdhd）
    注意：B站下载的音频是 fragmented MP4，mvhd.timescale 常为 0/电影时标(1000)，
    但 tfdt/trun 数值用的是「媒体时标 mdhd」(如 44100/48000)。
    若误用电影时标去除，会放大几十倍 → 几千几万秒。故必须区分两者。"""
    try:
        total_size = path.stat().st_size
        with open(path, "rb") as f:
            # fragmented MP4 需扫描全部 moof 才能拿到最大 tfdt，读取整个文件
            # （音频文件通常 < 256MB，这里设安全上限）
            data = f.read(min(total_size, 256 * 1024 * 1024))
        if len(data) < 16:
            return None

        # 分别取电影时标(mvhd) 与 媒体时标(mdhd)
        movie_ts = 1000
        media_ts = 44100
        for tp, body, _pos in _mp4_walk_boxes(data, len(data)):
            if tp == b"moov":
                for st, sb, _sp in _mp4_walk_boxes(body, len(body)):
                    if st == b"mvhd" and len(sb) >= 20:
                        ver = sb[0]
                        if ver == 0:
                            ts = struct.unpack(">I", sb[12:16])[0]
                        else:
                            ts = struct.unpack(">I", sb[20:24])[0]
                        if ts > 0:
                            movie_ts = ts
                    elif st == b"trak":
                        for st2, sb2, _sp2 in _mp4_walk_boxes(sb, len(sb)):
                            if st2 == b"mdia":
                                for st3, sb3, _sp3 in _mp4_walk_boxes(sb2, len(sb2)):
                                    if st3 == b"mdhd" and len(sb3) >= 20:
                                        ver = sb3[0]
                                        if ver == 0:
                                            ts = struct.unpack(">I", sb3[12:16])[0]
                                        else:
                                            ts = struct.unpack(">I", sb3[20:24])[0]
                                        if ts > 0:
                                            media_ts = ts

        # ── 策略 1: mvhd（用电影时标）──
        for tp, body, _pos in _mp4_walk_boxes(data, len(data)):
            if tp == b"moov":
                for st, sb, _sp in _mp4_walk_boxes(body, len(body)):
                    if st == b"mvhd" and len(sb) >= 20:
                        ver = sb[0]
                        if ver == 0:
                            dur = struct.unpack(">I", sb[16:20])[0]
                        else:
                            dur = struct.unpack(">Q", sb[24:32])[0]
                        if movie_ts > 0 and dur > 0:
                            cand = dur / movie_ts
                            if _mp4_duration_sane(cand, total_size):
                                return cand

        # ── 策略 2: sidx（用媒体时标）──
        for tp, body, _pos in _mp4_walk_boxes(data, len(data)):
            if tp == b"sidx" and len(body) >= 28:
                ver = body[0]
                ts = struct.unpack(">I", body[12:16])[0]
                total = 0
                if ver == 0:
                    ept = struct.unpack(">I", body[16:20])[0]
                    count = struct.unpack(">H", body[26:28])[0]
                    total = ept
                    for i in range(min(count, 10000)):
                        base = 28 + i * 12
                        if base + 8 > len(body):
                            break
                        total += struct.unpack(">I", body[base + 4:base + 8])[0]
                else:
                    ept = struct.unpack(">Q", body[16:24])[0]
                    count = struct.unpack(">H", body[34:36])[0]
                    total = ept
                    for i in range(min(count, 10000)):
                        base = 36 + i * 12
                        if base + 8 > len(body):
                            break
                        total += struct.unpack(">I", body[base + 4:base + 8])[0]
                if media_ts > 0 and ts > 0 and total > 0:
                    # sidx 时长用的是 sidx 自己的 timescale(ts)，不是媒体时标
                    cand = total / ts
                    if _mp4_duration_sane(cand, total_size):
                        return cand

        # ── 策略 3: moof/traf/tfdt + trun (fragmented MP4，用媒体时标) ──
        max_end_time = 0
        for tp, body, _pos in _mp4_walk_boxes(data, len(data)):
            if tp != b"moof":
                continue
            for st, sb, _sp in _mp4_walk_boxes(body, len(body)):
                if st != b"traf":
                    continue
                tfdt_base = 0
                sample_dur_sum = 0
                # 找出 traf 中的 tfhd 拿 default_sample_duration
                default_dur = 0
                for it, ib, _ip in _mp4_walk_boxes(sb, len(sb)):
                    if it == b"tfhd" and len(ib) >= 8:
                        tf_flags = struct.unpack(">I", ib[0:4])[0] & 0xFFFFFF
                        off = 8  # fullbox(4) + track_ID(4)
                        if tf_flags & 0x01:  # base_data_offset_present
                            off += 8
                        if tf_flags & 0x02:  # sample_description_index_present
                            off += 4
                        if tf_flags & 0x04:  # default_sample_duration_present
                            if off + 4 <= len(ib):
                                default_dur = struct.unpack(">I", ib[off:off + 4])[0]
                            off += 4
                        if tf_flags & 0x08:  # default_sample_flags_present
                            off += 4
                        if tf_flags & 0x10:  # default_sample_size_present
                            off += 4
                        break
                for it, ib, _ip in _mp4_walk_boxes(sb, len(sb)):
                    if it == b"tfdt" and len(ib) >= 8:
                        ver = ib[0]
                        if ver == 0:
                            tfdt_base = struct.unpack(">I", ib[4:8])[0]
                        else:
                            tfdt_base = struct.unpack(">Q", ib[4:12])[0]
                    elif it == b"trun" and len(ib) >= 8:
                        flags = struct.unpack(">I", ib[0:4])[0] & 0xFFFFFF
                        sample_count = struct.unpack(">I", ib[4:8])[0]
                        off2 = 8
                        if flags & 0x001:  # data_offset_present
                            off2 += 4
                        if flags & 0x004:  # first_sample_flags_present
                            off2 += 4
                        has_dur = bool(flags & 0x100)
                        per_sample_size = (4 if has_dur else 0) + (4 if (flags & 0x200) else 0) + \
                                          (4 if (flags & 0x400) else 0) + (4 if (flags & 0x800) else 0)
                        if has_dur and per_sample_size > 0:
                            for _i in range(sample_count):
                                if off2 + 4 > len(ib):
                                    break
                                sd = struct.unpack(">I", ib[off2:off2 + 4])[0]
                                sample_dur_sum += sd if sd > 0 else default_dur
                                off2 += per_sample_size
                        elif default_dur > 0:
                            sample_dur_sum += sample_count * default_dur
                end_time = tfdt_base + sample_dur_sum
                if end_time > max_end_time:
                    max_end_time = end_time
        if max_end_time > 0:
            cand = max_end_time / media_ts
            if _mp4_duration_sane(cand, total_size):
                return cand
        return None
    except Exception:
        return None


@staticmethod
def _mp3_duration_estimate(path: Path) -> Optional[float]:
    """简化版 mp3 时长估算：找第一个帧头，根据位率/采样率估算。"""
    try:
        with open(path, "rb") as f:
            data = f.read(min(int(path.stat().st_size), 128 * 1024))
        # 跳过 ID3v2 头
        offset = 0
        if len(data) >= 10 and data[:3] == b"ID3":
            size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
            offset = 10 + size
        # 找帧头 0xFFFB / 0xFFFA / 0xFFF3 / 0xFFF2
        bitrate_table_v1_l1 = [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0]
        bitrate_table_v1_l2 = [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0]
        bitrate_table_v1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
        bitrate_table_v2_l1 = [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0]
        bitrate_table_v2_l23 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
        sr_table = [44100, 48000, 32000, 0]
        sr_table_v2 = [22050, 24000, 16000, 0]
        for i in range(offset, max(offset, len(data) - 4)):
            b = data[i:i+4]
            if len(b) < 4:
                break
            if b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
                continue
            version = (b[1] >> 3) & 0x03        # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
            layer = (b[1] >> 1) & 0x03           # 3=Layer I, 2=Layer II, 1=Layer III
            bitrate_idx = (b[2] >> 4) & 0x0F
            sr_idx = (b[2] >> 2) & 0x03
            padding = (b[2] >> 1) & 0x01
            is_v1 = version == 3
            if layer == 3:
                table = bitrate_table_v1_l1 if is_v1 else bitrate_table_v2_l1
                samples_per_frame = 384
            elif layer == 2:
                table = bitrate_table_v1_l2 if is_v1 else bitrate_table_v2_l23
                samples_per_frame = 1152
            elif layer == 1:
                table = bitrate_table_v1_l3 if is_v1 else bitrate_table_v2_l23
                samples_per_frame = 1152
            else:
                continue
            br = table[bitrate_idx]
            if br == 0:
                continue
            sr = (sr_table if is_v1 else sr_table_v2)[sr_idx]
            if sr == 0:
                continue
            # kbps * 1000 → bps
            bitrate_bps = br * 1000.0
            # 估算帧长（Layer I 4字节，其他 1 字节 padding）
            if layer == 3:
                frame_len = int((12 * bitrate_bps // sr + 4 * padding) * 4)
            else:
                frame_len = int(144 * bitrate_bps // sr + padding)
            if frame_len <= 0:
                continue
            total_bytes = path.stat().st_size
            approx_frames = max(1, total_bytes // max(1, frame_len))
            total_samples = approx_frames * samples_per_frame
            dur = total_samples / float(sr)
            if dur > 1.0:
                return dur
            break
        return None
    except Exception:
        return None


def _qq_extract_json(raw: str) -> dict:
    """QQ音乐部分接口返回 jsonp（callback 包裹），这里兼容纯 json 与 jsonp。"""
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}


@neko_plugin
class CustomMusicListPlugin(NekoPluginBase):
    """网易云音乐歌单导出插件"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = get_plugin_logger(__name__)
        self._cookie: Optional[str] = None
        self._uid: Optional[int] = None
        self._nickname: Optional[str] = None
        self._playlists_cache: Optional[List[dict]] = None
        # 播放列表（顺序播放）
        self._playlist: List[dict] = []
        # 播放任务锁：防止并发顺序播放
        self._playlist_play_lock = asyncio.Lock()
        self._playlist_playing: bool = False
        # 顺序播放：当前索引（用户手动下一首）
        self._playlist_cursor: int = 0
        # 批量下载任务状态（供查询进度）
        self._download_status: dict = {
            "running": False,
            "total": 0,
            "done": 0,
            "current_title": "",
            "current_artist": "",
            "current_index": 0,
            "current_stage": "",  # "parsing" / "downloading" / "done"
            "failed": [],  # [{"title", "artist", "song_id", "reason"}]
            "success": [],  # [{"title", "artist", "song_id", "stored_name", "size_kb", "source"}]
            "started_at": None,
            "finished_at": None,
            # 实时同步的 songs：每下完一首就更新这里，前端轮询拿到后立刻填回 UI
            "songs": None,        # list or None：和 _songs 同样格式，带 stored_name/path
            "source": "",         # 本次任务来源：netease / bili / auto
            "save_filename": "",  # 完成后自动保存用（可空）
            "playlist_name": "",
            "message": "",        # 启动信息或完成信息
            "task_error": "",     # 任务整体异常时记录
        }
        # 保存后台批量下载任务的引用（防止被 gc）
        self._download_bg_task: Optional[asyncio.Task] = None
        self._download_lock = asyncio.Lock()
        # ── 自动下一首（时长计时）状态 ──
        # Neko 没有音乐播放结束回调，所以用"时长计时 + asyncio.sleep + cancel"实现：
        #   - playlist_autoplay_on: 是否开启"播完自动跳下一首"模式（用户可开关）
        #   - _autoplay_cancel_event: set() 就立刻取消当前 sleep 调度（用户暂停/切歌时用）
        #   - _autoplay_paused_event: set() 表示暂停计时；clear() 表示恢复计时
        #   - _autoplay_song_started_at: 当首播放起始时间（monotonic）
        #   - _autoplay_remaining_sec: 暂停时剩余秒数
        #   - _autoplay_current_song: { title, artist, duration_sec }
        self._playlist_autoplay_on: bool = True  # 默认开启，用户可关
        self._autoplay_cancel_event: Optional[asyncio.Event] = None
        self._autoplay_paused_event: Optional[asyncio.Event] = None
        self._autoplay_song_started_at: float = 0.0
        self._autoplay_remaining_sec: float = 0.0
        self._autoplay_current_song: Optional[dict] = None
        self._autoplay_lock = asyncio.Lock()
        # 保存当前调度器的 task 引用，cancel 时等它完全退出，防止 finally 清空新调度器状态
        self._autoplay_task: Optional[asyncio.Task] = None
        # 播放模式：sequential=顺序(默认) / loop_one=单曲循环 / loop_all=列表循环 / random=随机
        self._playlist_mode: str = "sequential"
        # 标记：本次 playlist_play 是被 autoplay scheduler 自动调用的（防止 prev 的 bug）
        self._autoplay_in_auto_advance: bool = False
        # B 站下载任务状态（进度查询用）
        self._bili_download_status: dict = {
            "running": False, "total": 0, "done": 0,
            "current_title": "", "current_artist": "",
            "current_index": 0, "current_stage": "",
            "failed": [], "success": [],
            "started_at": None, "finished_at": None,
        }
        self._bili_download_lock = asyncio.Lock()
        # 切歌锁：防止"调度器自动 next"和"用户手点 next"同时执行导致跳过一首
        self._playlist_advance_lock = asyncio.Lock()
        # 下载取消标志：用户点"取消下载"后置 True，worker 每首歌开始前检查
        self._download_cancel_flag: bool = False

    # ==================== 生命周期 ====================
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._load_session()
        try:
            self.register_static_ui("static")
            self.set_list_actions(
                [
                    {
                        "id": "open_ui",
                        "kind": "ui",
                        "target": f"/plugin/{self.plugin_id}/ui/",
                        "open_in": "new_tab",
                    }
                ]
            )
            self.logger.info("静态 UI 已注册")
        except Exception as e:
            self.logger.warning(f"注册静态 UI 失败: {e}")
        return Ok({"status": "ready", "logged_in": self._cookie is not None})

    # ==================== 会话持久化 ====================
    def _cookie_file(self) -> Path:
        """cookie 持久化文件路径（在插件目录下）。
        Neko SDK 的 self.store 在某些版本重启后可能丢失，所以额外用文件持久化。
        """
        return Path(self.config_dir) / "netease_cookie.json"

    async def _load_session(self):
        # 1) 先尝试从文件加载（最可靠）
        try:
            fp = self._cookie_file()
            if fp.is_file():
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("cookie"):
                    self._cookie = data["cookie"]
                    self._uid = data.get("uid")
                    self._nickname = data.get("nickname")
                    self.logger.info(
                        f"已从文件加载网易云会话: uid={self._uid}, nickname={self._nickname}"
                    )
                    return
        except Exception as e:
            self.logger.warning(f"从文件加载会话失败: {e}")
        # 2) 回退到 SDK store
        try:
            data = await self.store.get("netease_session", {})
            if isinstance(data, dict) and data.get("cookie"):
                self._cookie = data["cookie"]
                self._uid = data.get("uid")
                self._nickname = data.get("nickname")
                self.logger.info(
                    f"已从 store 加载网易云会话: uid={self._uid}, nickname={self._nickname}"
                )
                # 顺便把文件也写一份（迁移）
                await self._save_session(self._cookie, self._uid, self._nickname)
        except Exception as e:
            self.logger.warning(f"加载会话失败: {e}")

    async def _save_session(
        self,
        cookie: str,
        uid: Optional[int] = None,
        nickname: Optional[str] = None,
    ):
        self._cookie = cookie
        self._uid = uid
        self._nickname = nickname
        # 双写：文件 + SDK store，文件是兜底
        try:
            fp = self._cookie_file()
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(
                    {"cookie": cookie, "uid": uid, "nickname": nickname},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            self.logger.warning(f"写入 cookie 文件失败: {e}")
        try:
            await self.store.set(
                "netease_session",
                {"cookie": cookie, "uid": uid, "nickname": nickname},
            )
        except Exception as e:
            self.logger.warning(f"写入 store 失败: {e}")

    def _require_login(self):
        if not self._cookie:
            return Err(SdkError("未登录，请先调用 start_browser_login 打开浏览器登录，再用 login_with_cookie 填入 MUSIC_U cookie 完成登录"))
        return None

    # ==================== HTTP 工具 (weapi 加密) ====================
    async def _weapi(
        self, path: str, data: Optional[dict] = None, cookie: Optional[str] = None
    ) -> httpx.Response:
        """发起 weapi 加密 POST 请求"""
        ck = cookie if cookie is not None else self._cookie
        payload = data or {}
        payload["timestamp"] = str(int(time.time() * 1000))
        encrypted = _weapi_encrypt(payload)
        headers = _headers(ck)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            return await client.post(
                f"{_BASE}/weapi{path}", data=encrypted, headers=headers
            )

    # ==================== 浏览器登录（自动打开 + 引导） ====================
    @plugin_entry(
        id="start_browser_login",
        name="浏览器登录（推荐）",
        description="自动打开网易云音乐官网登录页。登录后请按返回的步骤复制 MUSIC_U cookie，"
        "再调用 Cookie 登录（login_with_cookie）把 cookie 填进去完成登录。",
        kind="action",
    )
    async def start_browser_login(self, **_):
        import webbrowser

        login_url = f"{_BASE}/login"
        try:
            webbrowser.open(login_url)
            opened = True
        except Exception:
            opened = False

        steps = [
            "第1步：在弹出的浏览器中完成登录（扫码 / 手机号登录均可）",
            "第2步：登录成功后，在当前页面按 F12 打开开发者工具",
            "第3步：切换到 Application（应用程序）标签，左侧展开 Cookies → https://music.163.com",
            "第4步：找到名为 MUSIC_U 的 Cookie，复制它的 Value 整段内容",
            "第5步：回到本插件，调用 Cookie 登录（login_with_cookie），把复制的内容粘贴进去即可",
        ]
        alternative = (
            "如果浏览器没自动打开，请手动访问以下地址，然后按上述步骤操作：\n"
            "  →  https://music.163.com/login"
        )
        return Ok(
            {
                "opened": opened,
                "login_url": login_url,
                "steps": steps,
                "alternative": alternative,
                "message": (
                    "浏览器已打开，请按以下 5 步完成登录：\n  • "
                    + "\n  • ".join(steps)
                    + ("\n\n" + alternative if not opened else "")
                    + "\n\n复制到 MUSIC_U 后，请调用 Cookie 登录（login_with_cookie）入口并粘贴完成登录。"
                ),
            }
        )

    # ==================== Cookie 登录（推荐） ====================
    @plugin_entry(
        id="login_with_cookie",
        name="Cookie 登录",
        description="使用 MUSIC_U cookie 登录网易云音乐（推荐，扫码登录已被网易云风控限制）。"
        "获取方法：浏览器打开 music.163.com 登录后，F12 → Application → Cookies → 复制 MUSIC_U 的值。"
        "也可直接粘贴完整的 Cookie 字符串。",
        kind="action",
    )
    async def login_with_cookie(
        self,
        music_u: Annotated[str, "MUSIC_U cookie 值或完整 Cookie 字符串"],
        **_,
    ):
        try:
            music_u = music_u.strip()
            if not music_u:
                return Err(SdkError("cookie 不能为空"))
            # 如果传入的已经是完整 cookie 字符串，直接使用；否则拼成 MUSIC_U=xxx
            if "=" in music_u and ("MUSIC_U=" in music_u or ";" in music_u):
                cookie = music_u
            else:
                cookie = f"MUSIC_U={music_u}"
            self._cookie = cookie
            # 验证 cookie 是否有效
            account = await self._fetch_account()
            if not account or not account.get("uid"):
                self._cookie = ""
                return Err(
                    SdkError(
                        "Cookie 无效或已过期，请重新获取 MUSIC_U cookie。"
                        "方法：浏览器打开 music.163.com 登录 → F12 → Application → Cookies → MUSIC_U"
                    )
                )
            self._uid = str(account["uid"])
            self._nickname = account.get("nickname") or ""
            # 持久化
            await self._save_session(cookie, self._uid, self._nickname)
            self.logger.info(f"Cookie 登录成功: uid={self._uid}, nickname={self._nickname}")
            return Ok(
                {
                    "success": True,
                    "uid": self._uid,
                    "nickname": self._nickname,
                    "message": f"登录成功！欢迎，{self._nickname}",
                }
            )
        except Exception as e:
            self._cookie = ""
            return Err(SdkError(f"Cookie 登录失败: {e}"))

    async def _fetch_account(self) -> Optional[dict]:
        """获取当前登录账号信息（uid、昵称）"""
        try:
            resp = await self._weapi("/nuser/account/get", {})
            data = resp.json()
            account = data.get("account") or {}
            profile = data.get("profile") or {}
            uid = profile.get("userId") or account.get("id")
            nickname = profile.get("nickname") or account.get("userName")
            return {"uid": uid, "nickname": nickname}
        except Exception as e:
            self.logger.warning(f"获取账号信息失败: {e}")
            return None

    # ==================== 登录状态 ====================
    @plugin_entry(
        id="get_login_status",
        name="获取登录状态",
        description="检查当前是否已登录网易云音乐。",
    )
    async def get_login_status(self, **_):
        return Ok(
            {
                "logged_in": self._cookie is not None,
                "uid": self._uid,
                "nickname": self._nickname,
            }
        )

    @plugin_entry(
        id="logout",
        name="登出",
        description="清除保存的网易云音乐登录凭据。",
        kind="action",
    )
    async def logout(self, **_):
        self._cookie = None
        self._uid = None
        self._nickname = None
        self._playlists_cache = None
        await self.store.delete("netease_session")
        return Ok({"success": True, "message": "已登出"})

    # ==================== 歌单列表 ====================
    async def _fetch_all_playlists(self) -> List[dict]:
        """获取用户所有歌单（创建+收藏），自动翻页"""
        if not self._uid:
            info = await self._fetch_account()
            if info and info.get("uid"):
                self._uid = info["uid"]
                if info.get("nickname"):
                    self._nickname = info["nickname"]
                await self._save_session(self._cookie, self._uid, self._nickname)
            else:
                raise RuntimeError("无法获取用户 uid，请重新登录")
        all_playlists: List[dict] = []
        offset = 0
        limit = 30
        while True:
            resp = await self._weapi(
                "/user/playlist",
                {
                    "uid": self._uid,
                    "offset": offset,
                    "limit": limit,
                    "includeVideo": True,
                },
            )
            data = resp.json()
            playlists = data.get("playlist") or []
            all_playlists.extend(playlists)
            if not data.get("more") or len(playlists) < limit:
                break
            offset += limit
        return all_playlists

    @plugin_entry(
        id="list_playlists",
        name="获取歌单列表",
        description="获取网易云音乐收藏和创建的所有歌单，支持分页和关键词搜索。"
        "返回歌单 id、名称、歌曲数、创建者等信息。",
    )
    async def list_playlists(
        self,
        offset: Annotated[int, "分页偏移量，默认 0"] = 0,
        limit: Annotated[int, "每页数量，默认 20"] = 20,
        keyword: Annotated[str, "搜索关键词（按歌单名过滤），默认为空"] = "",
        refresh: Annotated[bool, "是否强制刷新缓存重新拉取"] = False,
        **_,
    ):
        err = self._require_login()
        if err:
            return err
        try:
            if refresh or self._playlists_cache is None:
                self._playlists_cache = await self._fetch_all_playlists()
                self.logger.info(f"已获取 {len(self._playlists_cache)} 个歌单")
            playlists = self._playlists_cache
            # 关键词过滤
            if keyword:
                kw = keyword.lower()
                filtered = [
                    p for p in playlists if kw in (p.get("name") or "").lower()
                ]
            else:
                filtered = playlists
            # 分页
            page = filtered[offset : offset + limit]
            items = []
            for p in page:
                creator = (p.get("creator") or {}).get("nickname") or ""
                items.append(
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "track_count": p.get("trackCount", 0),
                        "play_count": p.get("playCount", 0),
                        "creator": creator,
                        "bookmarked": bool(p.get("subscribed")),
                    }
                )
            return Ok(
                {
                    "total": len(playlists),
                    "filtered": len(filtered),
                    "offset": offset,
                    "limit": limit,
                    "keyword": keyword,
                    "items": items,
                }
            )
        except Exception as e:
            return Err(SdkError(f"获取歌单列表失败: {e}"))

    # ==================== 导出歌单 ====================
    async def _fetch_playlist_tracks(
        self, playlist_id: int
    ) -> tuple[str, List[dict]]:
        """获取歌单内所有歌曲（歌名+歌手），返回 (歌单名称, 歌曲列表)"""
        # 1. 获取歌单详情（含全部 trackIds）
        resp = await self._weapi(
            "/v6/playlist/detail", {"id": playlist_id, "n": 1000, "s": 8}
        )
        data = resp.json()
        playlist = data.get("playlist") or {}
        name = playlist.get("name") or str(playlist_id)
        track_ids = [
            t["id"]
            for t in (playlist.get("trackIds") or [])
            if t.get("id") is not None
        ]
        # playlist.tracks 已含部分歌曲完整信息，建立索引
        tracks_map: dict[int, dict] = {}
        for t in (playlist.get("tracks") or []):
            if t.get("id") is not None:
                tracks_map[t["id"]] = t
        # 2. 批量获取缺失歌曲详情
        missing_ids = [tid for tid in track_ids if tid not in tracks_map]
        if missing_ids:
            batch_size = 500
            for i in range(0, len(missing_ids), batch_size):
                batch = missing_ids[i : i + batch_size]
                c_param = json.dumps([{"id": x} for x in batch])
                resp = await self._weapi(
                    "/v3/song/detail", {"c": c_param}
                )
                sdata = resp.json()
                for s in (sdata.get("songs") or []):
                    if s.get("id") is not None:
                        tracks_map[s["id"]] = s
        # 3. 按 trackIds 顺序组装结果
        songs: List[dict] = []
        for tid in track_ids:
            s = tracks_map.get(tid)
            if not s:
                continue
            # 兼容 ar/artists 两种字段名
            artists_list = s.get("ar") or s.get("artists") or []
            artists = "/".join(
                ar.get("name", "")
                for ar in artists_list
                if ar.get("name")
            )
            songs.append({
                "name": s.get("name", ""),
                "artists": artists,
                "song_id": str(s.get("id") or ""),  # 保存网易云歌曲 ID
            })
        return name, songs

    @plugin_entry(
        id="export_playlist",
        name="导出歌单到 txt",
        description="导出指定网易云歌单的所有歌曲名、作者、歌曲 ID 信息到 txt 和 json 文件，"
        "文件以歌单名称命名。需先调用 list_playlists 获取歌单 id。",
        kind="action",
    )
    async def export_playlist(
        self,
        playlist_id: Annotated[str, "要导出的歌单 id（从 list_playlists 获取）"],
        **_,
    ):
        err = self._require_login()
        if err:
            return err
        try:
            name, songs = await self._fetch_playlist_tracks(int(str(playlist_id).strip()))
            if not songs:
                return Err(SdkError(f"歌单「{name}」中没有歌曲，或获取失败"))
            filename = _sanitize_filename(name) + ".txt"
            out_path = self.data_path(filename)
            json_path = self._json_path_for_txt(filename)

            # ── 合并逻辑：保留手动添加的歌曲 & 已下载的音源路径 ──
            # 读取旧 JSON（如果存在），提取：
            #   1. 手动添加的歌曲（manual=True）——重新导出后仍然保留
            #   2. 已下载音源路径（按 song_id 匹配）——避免重新下载
            existing_manual_songs: List[dict] = []
            existing_path_map: dict = {}  # song_id -> {path, stored_name}
            if json_path.is_file():
                try:
                    old_data = json.loads(json_path.read_text(encoding="utf-8"))
                    for s in old_data.get("songs", []):
                        if s.get("manual"):
                            existing_manual_songs.append(s)
                        sid = str(s.get("song_id") or "").strip()
                        if sid:
                            p = str(s.get("stored_name") or s.get("path") or "").strip()
                            if p:
                                existing_path_map[sid] = p
                except Exception:
                    pass  # 旧文件解析失败则忽略，正常导出

            # 构建新的歌曲列表：API 返回的歌曲 + 保留的旧音源路径
            json_songs: List[dict] = []
            for s in songs:
                sid = str(s.get("song_id") or "")
                # 如果之前已下载过，保留音源路径
                saved_path = existing_path_map.get(sid, "")
                p = str(s.get("stored_name") or s.get("path") or "").strip() or saved_path
                json_songs.append({
                    "name": s.get("name", ""),
                    "artists": s.get("artists", ""),
                    "song_id": sid,
                    "path": p,
                    "stored_name": p,
                })

            # 追加手动添加的歌曲（排在最后）
            manual_count = 0
            for ms in existing_manual_songs:
                ms = dict(ms)
                ms.setdefault("manual", True)
                ms.setdefault("song_id", "")
                p = str(ms.get("stored_name") or ms.get("path") or "").strip()
                ms["path"] = p
                ms["stored_name"] = p
                json_songs.append(ms)
                manual_count += 1

            total_count = len(json_songs)
            await asyncio.to_thread(_write_txt, out_path, name, json_songs)
            json_path.write_text(
                json.dumps(
                    {"playlist_name": name, "songs": json_songs},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.logger.info(
                f"已导出歌单「{name}」共 {total_count} 首"
                f"（API {len(songs)} 首 + 手动保留 {manual_count} 首）"
                f"到 {out_path} 和 {json_path}"
            )
            return Ok(
                {
                    "success": True,
                    "playlist_name": name,
                    "song_count": total_count,
                    "api_count": len(songs),
                    "manual_preserved": manual_count,
                    "file_path": str(out_path),
                    "json_path": str(json_path),
                }
            )
        except Exception as e:
            return Err(SdkError(f"导出歌单失败: {e}"))


    # ==================== 歌曲管理（UI 用） ====================

    def _json_path_for_txt(self, txt_filename: str) -> Path:
        """返回与 txt 文件对应的 json 文件路径"""
        stem = Path(txt_filename).stem
        return self.data_path(stem + ".json")

    @plugin_entry(
        id="list_exported_files",
        name="列出已导出文件",
        description="列出已导出的歌单 txt 文件列表，供 UI 选择加载。",
    )
    async def list_exported_files(self, **_):
        try:
            data_dir = self.data_path()
            if not data_dir.is_dir():
                return Ok({"files": []})
            files = sorted(
                (f.name for f in data_dir.glob("*.txt")),
                key=lambda n: n,
                reverse=True,
            )
            return Ok({"files": files})
        except Exception as e:
            return Err(SdkError(f"列出文件失败: {e}"))

    @plugin_entry(
        id="load_song_list",
        name="加载歌曲列表",
        description="读取已导出的 txt 文件，解析歌曲列表。如果存在对应的 json 编辑文件（含音源路径），优先加载 json。",
    )
    async def load_song_list(
        self,
        filename: Annotated[str, "txt 文件名（如 我的歌单.txt）"],
        **_,
    ):
        try:
            filename = _sanitize_filename(Path(filename).name)
            txt_path = self.data_path(filename)
            if not txt_path.is_file():
                return Err(SdkError(f"文件不存在: {filename}"))
            json_path = self._json_path_for_txt(filename)
            if json_path.is_file():
                data = json.loads(json_path.read_text(encoding="utf-8"))
            else:
                data = _parse_txt(txt_path)
            return Ok(
                {
                    "filename": filename,
                    "playlist_name": data.get("playlist_name", ""),
                    "songs": data.get("songs", []),
                }
            )
        except Exception as e:
            return Err(SdkError(f"加载歌曲列表失败: {e}"))

    @plugin_entry(
        id="save_song_list",
        name="保存歌曲列表",
        description="保存编辑后的歌曲列表（含音源路径、排序、增删）到 json 文件，同时更新 txt 文件。",
        kind="action",
    )
    async def save_song_list(
        self,
        filename: Annotated[str, "txt 文件名（如 我的歌单.txt）"],
        playlist_name: Annotated[str, "歌单名称"] = "",
        songs: Annotated[str, "歌曲列表 JSON 字符串，每项含 name/artists/path 或 stored_name"] = "[]",
        **_,
    ):
        try:
            filename = _sanitize_filename(Path(filename).name)
            song_list = json.loads(songs) if isinstance(songs, str) else songs
            if not isinstance(song_list, list):
                return Err(SdkError("songs 参数必须是列表"))
            # 字段名统一：保证 path 与 stored_name 一致，避免 LLM 调用时字段不匹配
            normalized: List[dict] = []
            for s in song_list:
                if not isinstance(s, dict):
                    continue
                s = dict(s)
                p = str(s.get("stored_name") or s.get("path") or "").strip()
                s["path"] = p
                s["stored_name"] = p
                normalized.append(s)
            name = playlist_name or Path(filename).stem
            # 保存 JSON（含音源路径等完整信息）
            json_path = self._json_path_for_txt(filename)
            json_path.write_text(
                json.dumps(
                    {"playlist_name": name, "songs": normalized},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            # 同时更新 txt 文件
            txt_path = self.data_path(filename)
            await asyncio.to_thread(_write_txt, txt_path, name, normalized)
            self.logger.info(f"已保存歌曲列表到 {json_path} 和 {txt_path}")
            return Ok(
                {
                    "success": True,
                    "filename": filename,
                    "song_count": len(normalized),
                }
            )
        except Exception as e:
            return Err(SdkError(f"保存歌曲列表失败: {e}"))

    # ==================== 音频上传与播放（推送到 N.E.K.O） ====================
    def _uploads_dir(self) -> Path:
        """返回静态 UI 下的 uploads 目录（可被 N.E.K.O 访问）"""
        d = self.config_dir / "static" / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _build_ui_file_url(self, stored_filename: str) -> str:
        return f"/plugin/{self.plugin_id}/ui/uploads/{stored_filename}"

    def _build_song_url(self, stored_filename: str) -> str:
        """构造可被 N.E.K.O 访问的音频完整 URL"""
        relative = self._build_ui_file_url(stored_filename)
        return self._to_absolute_url(relative)

    def _resolve_public_origin(self) -> str:
        """解析插件服务器的公开 origin（参考 music_pusher）"""
        for env_key in ("NEKO_PLUGIN_SERVER_ORIGIN", "NEKO_USER_PLUGIN_SERVER_ORIGIN", "NEKO_SERVER_ORIGIN"):
            val = str(os.getenv(env_key, "")).strip().rstrip("/")
            if val.startswith("http://") or val.startswith("https://"):
                return val
        try:
            port = int(os.getenv("NEKO_USER_PLUGIN_SERVER_PORT", "").strip())
            if 1 <= port <= 65535:
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
        return "http://127.0.0.1:48916"

    def _to_absolute_url(self, maybe_relative: str) -> str:
        url = str(maybe_relative or "").strip()
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = f"/{url}"
        return f"{self._resolve_public_origin()}{url}"



    def _matches_public_origin(self, parsed: ParseResult) -> bool:
        public_origin = _parse_http_url(self._resolve_public_origin())
        if public_origin is None:
            return False
        return (
            parsed.scheme == public_origin.scheme
            and parsed.hostname == public_origin.hostname
            and (parsed.port or _default_port_for_scheme(parsed.scheme))
            == (public_origin.port or _default_port_for_scheme(public_origin.scheme))
        )

    def _is_plugin_upload_url(self, url: str) -> bool:
        """检查 URL 是否是当前插件上传的本地音频 URL"""
        parsed = _parse_http_url(url)
        if parsed is None:
            return False
        upload_prefix = f"/plugin/{self.plugin_id}/ui/uploads/"
        path_str = str(parsed.path or "")
        if not path_str.startswith(upload_prefix):
            return False
        filename = path_str.removeprefix(upload_prefix).strip()
        if not filename or "/" in filename:
            return False
        return self._matches_public_origin(parsed)

    def _music_allowlist_domains_for_url(self, url: str) -> list[str]:
        """根据 URL 解析需要加入白名单的域名列表"""
        parsed = _parse_http_url(url)
        if parsed is None:
            return []
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            return []
        if self._is_plugin_upload_url(url) and host in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            return ["127.0.0.1", "localhost", "::1"]
        if self._is_plugin_upload_url(url):
            return [host]
        if ":" in host:
            return []
        return [host]


    @plugin_entry(
        id="upload_song_file",
        name="上传音频文件",
        description="上传音频文件到插件 uploads 目录，返回存储文件名。供 UI 导入音源路径使用。",
        kind="action",
    )
    async def upload_song_file(
        self,
        audio_base64: Annotated[str, "音频文件的 base64 data URI"],
        filename: Annotated[str, "原始文件名（用于获取扩展名）"] = "",
        **_,
    ):
        try:
            encoded = _strip_data_uri(audio_base64.strip())
            if not encoded:
                return Err(SdkError("音频数据为空"))
            binary = base64.b64decode(encoded, validate=True)
            # 生成安全文件名
            ext = Path(filename).suffix.lower() if filename else ".mp3"
            if ext not in (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma"):
                ext = ".mp3"
            stored_name = f"{uuid.uuid4().hex[:12]}{ext}"
            dest = self._uploads_dir() / stored_name
            await asyncio.to_thread(lambda: dest.write_bytes(binary))
            self.logger.info(f"已上传音频文件: {filename} -> {stored_name} ({len(binary)} bytes)")
            return Ok(
                {
                    "success": True,
                    "stored_name": stored_name,
                    "size": len(binary),
                }
            )
        except Exception as e:
            return Err(SdkError(f"上传音频文件失败: {e}"))

    def _resolve_target_lanlan(self, kwargs: dict) -> str | None:
        """解析目标 lanlan 名称（参考 music_pusher）"""
        # 1. 显式参数
        explicit = kwargs.get("target_lanlan")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        # 2. _ctx 中的 lanlan_name
        ctx_obj = kwargs.get("_ctx")
        if isinstance(ctx_obj, dict):
            lanlan_name = ctx_obj.get("lanlan_name")
            if isinstance(lanlan_name, str) and lanlan_name.strip():
                return lanlan_name.strip()
        # 3. ctx 属性
        current_lanlan = getattr(self.ctx, "_current_lanlan", None)
        if isinstance(current_lanlan, str) and current_lanlan.strip():
            return current_lanlan.strip()
        # 4. 环境变量
        for env_key in ("NEKO_TARGET_LANLAN", "NEKO_LANLAN_NAME", "NEKO_HER_NAME"):
            val = str(os.getenv(env_key, "")).strip()
            if val:
                return val
        # 5. 默认值
        return "NEKO"

    @plugin_entry(
        id="play_song",
        name="播放歌曲",
        description="将指定歌曲推送到 N.E.K.O 对话界面播放。需要歌曲已导入音源路径（uploaded 文件名）。注意：在对话里点歌请优先使用 LLM 工具 play_song_by_name（无需传 stored_name），不要直接调用此入口。",
        kind="action",
    )
    async def play_song(
        self,
        title: Annotated[str, "歌曲名"],
        artist: Annotated[str, "歌手"] = "",
        stored_name: Annotated[str, "音频文件存储名（upload_song_file 返回的 stored_name）"] = "",
        path: Annotated[str, "同 stored_name，兼容字段"] = "",
        audio_path: Annotated[str, "同 stored_name，兼容字段"] = "",
        **kwargs,
    ):
        try:
            # 兼容多种字段名
            final_stored = str(stored_name or path or audio_path or kwargs.get("path") or kwargs.get("audio_path") or "").strip()
            if not final_stored:
                # 如果所有参数都空，尝试在本地保存的所有 JSON 中，按 title/artist 搜索一首匹配的来播放（兜底）
                data_dir = Path(self.data_path(""))
                json_files = sorted(data_dir.glob("*.json"))
                best_score = 0
                best_stored = ""
                best_title_out = title
                best_artist_out = artist or "未知艺术家"
                needle_t = (title or "").strip().lower()
                needle_a = (artist or "").strip().lower()
                for jf in json_files:
                    try:
                        data = json.loads(jf.read_text(encoding="utf-8"))
                        songs = data.get("songs") or []
                        for s in songs:
                            s_title = str(s.get("name") or s.get("title") or "").lower()
                            s_artist = str(s.get("artist") or s.get("artists") or "").lower()
                            s_stored = str(s.get("stored_name") or s.get("path") or "").strip()
                            if not s_stored:
                                continue
                            if not (self._uploads_dir() / s_stored).is_file():
                                continue
                            score = 0
                            if needle_t and (needle_t == s_title or needle_t in s_title):
                                score += 100 if needle_t == s_title else 50
                            # title 也可能其实是歌手名（LLM 常见误传），匹配歌手
                            if needle_t and needle_t in s_artist:
                                score += 40
                            if needle_a and (needle_a in s_artist):
                                score += 40
                            if score > best_score:
                                best_score = score
                                best_stored = s_stored
                                best_title_out = s.get("name") or s.get("title") or title
                                best_artist_out = s.get("artist") or s.get("artists") or artist or "未知艺术家"
                    except Exception:
                        continue
                if best_stored and best_score > 0:
                    final_stored = best_stored
                    title = best_title_out
                    artist = best_artist_out
                    self.logger.info(f"play_song: stored_name 为空，自动在本地列表匹配到: {title} - {artist} -> {final_stored} (score={best_score})")
            if not final_stored:
                return Err(SdkError("该歌曲未导入音源路径，请先导入。请在插件控制面板的「歌曲管理」中导入音频文件并点击💾保存，之后就可以正常播放。若在对话中请用自然语言描述歌名，系统会自动用 play_song_by_name 工具查找匹配。"))
            stored_name = final_stored
            # 检查文件是否存在
            file_path = self._uploads_dir() / stored_name
            if not file_path.is_file():
                return Err(SdkError(f"音频文件不存在: {stored_name}，请重新导入"))
            # 构造 URL
            url = self._build_song_url(stored_name)
            # 解析需要加入白名单的域名
            domains = self._music_allowlist_domains_for_url(url)
            if not domains:
                return Err(SdkError(f"音频 URL 无法安全加入播放白名单: {url}"))
            # 解析目标 lanlan
            target_lanlan = self._resolve_target_lanlan(kwargs)
            self.logger.info(f"推送播放: {title} - {artist}, url={url}, domains={domains}, target_lanlan={target_lanlan}")
            event_id = f"song_{uuid.uuid4().hex[:8]}"
            source_tag = str(self.plugin_id or "custom_music_list")

            # 第一步：添加域名白名单（与 music_pusher 保持相同的旧版 message_type 格式，兼容性最好）
            self.ctx.push_message(
                source=source_tag,
                message_type="music_allowlist_add",
                description=f"Allow music host: {domains[0]}",
                priority=7,
                metadata={"domains": list(domains), "event_id": event_id},
                target_lanlan=target_lanlan,
            )
            # 第二步：推送播放
            self.ctx.push_message(
                source=source_tag,
                message_type="music_play_url",
                description=f"🎵 {title} [{artist}]",
                priority=9,
                metadata={
                    "url": url,
                    "name": title or "自定义歌曲",
                    "artist": artist or "未知艺术家",
                    "event_id": event_id,
                },
                target_lanlan=target_lanlan,
            )
            return Ok(
                {
                    "success": True,
                    "title": title,
                    "artist": artist,
                    "url": url,
                    "allowlist_domains": list(domains),
                    "message": f"已推送「{title}」到 N.E.K.O 播放",
                }
            )
        except Exception as e:
            return Err(SdkError(f"播放歌曲失败: {e}"))

    # ==================== 播放列表（顺序播放） ====================
    @plugin_entry(
        id="playlist_get",
        name="获取播放列表",
        description="获取当前顺序播放列表的所有歌曲。",
        kind="action",
    )
    async def playlist_get(self, **_):
        """返回当前播放列表"""
        return Ok({
            "songs": [dict(s) for s in self._playlist],
            "count": len(self._playlist),
            "is_playing": self._playlist_playing,
        })

    @plugin_entry(
        id="playlist_add",
        name="添加到播放列表",
        description="将一首歌曲添加到顺序播放列表末尾。",
        kind="action",
    )
    async def playlist_add(
        self,
        title: Annotated[str, "歌曲名"],
        artist: Annotated[str, "歌手（可选，留空填未知）"] = "",
        stored_name: Annotated[str, "音频文件存储名（upload_song_file 返回），如果是本地音频"] = "",
        song_id: Annotated[str, "网易云歌曲ID，如果是网易云在线音源"] = "",
        **_,
    ):
        if not title.strip():
            return Err(SdkError("歌曲名不能为空"))
        item = {
            "id": f"pl_{uuid.uuid4().hex[:8]}",
            "title": title.strip(),
            "artist": artist.strip() or "未知艺术家",
            "stored_name": stored_name.strip() or "",
            "song_id": song_id.strip() or "",
        }
        self._playlist.append(item)
        self.logger.info(f"已添加到播放列表: {item['title']} - {item['artist']}  (总数: {len(self._playlist)})")
        return Ok({"success": True, "item": item, "count": len(self._playlist)})

    @plugin_entry(
        id="playlist_add_all",
        name="批量添加到播放列表",
        description="把多首歌曲（列表 JSON）一次性全部添加到播放列表末尾，只加入有音频文件 stored_name 的那些。",
        kind="action",
    )
    async def playlist_add_all(
        self,
        songs: Annotated[str, "歌曲列表 JSON 字符串，每项含 name/artists/stored_name/song_id"],
        replace: Annotated[bool, "是否先清空播放列表再加入（True/False，默认 False=追加）"] = False,
        **_,
    ):
        try:
            song_list = json.loads(songs) if isinstance(songs, str) else songs
            if not isinstance(song_list, list):
                return Err(SdkError("songs 参数必须是列表 JSON"))
        except Exception as e:
            return Err(SdkError(f"songs JSON 解析失败: {e}"))
        added_count = 0
        skipped_no_audio = 0
        skipped_dup = 0
        if replace:
            self._playlist.clear()
        existing_keys = {
            (s.get("title", "").strip(), s.get("artist", "").strip(), (s.get("stored_name") or s.get("song_id") or "").strip())
            for s in self._playlist
        }
        for s in song_list:
            if not isinstance(s, dict):
                continue
            title = str(s.get("name") or s.get("title") or "").strip()
            artist = str(s.get("artists") or s.get("artist") or "未知艺术家").strip()
            stored = str(s.get("stored_name") or s.get("path") or "").strip()
            song_id = str(s.get("song_id") or "").strip()
            if not title:
                continue
            # 过滤：只有有 stored_name 的才加入列表，否则播放不了
            if not stored:
                skipped_no_audio += 1
                continue
            # 去重
            key = (title, artist, stored or song_id)
            if key in existing_keys:
                skipped_dup += 1
                continue
            existing_keys.add(key)
            self._playlist.append({
                "id": f"pl_{uuid.uuid4().hex[:8]}",
                "title": title,
                "artist": artist or "未知艺术家",
                "stored_name": stored,
                "song_id": song_id,
            })
            added_count += 1
        self.logger.info(f"批量加入播放列表: 新增 {added_count}，跳过(无音频) {skipped_no_audio}，跳过(重复) {skipped_dup}，总数 {len(self._playlist)}")
        return Ok({
            "success": True,
            "added_count": added_count,
            "skipped_no_audio": skipped_no_audio,
            "skipped_dup": skipped_dup,
            "total": len(self._playlist),
            "message": (
                f"播放列表新增 {added_count} 首"
                + (f"（跳过 {skipped_no_audio} 首没有本地音频的）" if skipped_no_audio else "")
                + (f"（跳过 {skipped_dup} 首重复的）" if skipped_dup else "")
                + f"，当前共 {len(self._playlist)} 首"
            ),
        })

    @plugin_entry(
        id="playlist_remove",
        name="从播放列表移除",
        description="按 id 移除播放列表中的一首歌。",
        kind="action",
    )
    async def playlist_remove(
        self,
        item_id: Annotated[str, "要移除的歌曲 id（playlist_get 返回的 id 字段）"],
        **_,
    ):
        for i, s in enumerate(self._playlist):
            if s.get("id") == item_id:
                removed = self._playlist.pop(i)
                self.logger.info(f"已从播放列表移除: {removed.get('title')}  (剩余: {len(self._playlist)})")
                return Ok({"success": True, "removed": removed, "count": len(self._playlist)})
        return Err(SdkError(f"播放列表中未找到 id={item_id} 的歌曲"))

    @plugin_entry(
        id="playlist_move",
        name="移动播放列表歌曲顺序",
        description="调整播放列表中歌曲顺序，将歌曲移动到指定位置。",
        kind="action",
    )
    async def playlist_move(
        self,
        item_id: Annotated[str, "要移动的歌曲 id"],
        new_index: Annotated[int, "新的位置（从 0 开始）"],
        **_,
    ):
        idx = None
        for i, s in enumerate(self._playlist):
            if s.get("id") == item_id:
                idx = i
                break
        if idx is None:
            return Err(SdkError(f"播放列表中未找到 id={item_id} 的歌曲"))
        new_index = max(0, min(int(new_index), len(self._playlist) - 1))
        item = self._playlist.pop(idx)
        self._playlist.insert(new_index, item)
        return Ok({"success": True, "item": item, "new_index": new_index, "count": len(self._playlist)})

    @plugin_entry(
        id="playlist_clear",
        name="清空播放列表",
        description="清空当前顺序播放列表。",
        kind="action",
    )
    async def playlist_clear(self, **_):
        count = len(self._playlist)
        self._playlist.clear()
        return Ok({"success": True, "cleared_count": count})

    async def _resolve_url_for_playlist_item(self, item: dict) -> Optional[str]:
        """为播放列表条目解析播放 URL"""
        stored = str(item.get("stored_name") or "").strip()
        if stored:
            fp = self._uploads_dir() / stored
            if fp.is_file():
                return self._build_song_url(stored)
        # 网易云歌曲 ID：这里简化处理，未来可扩展
        return None

    @plugin_entry(
        id="playlist_play",
        name="播放当前索引的歌曲",
        description="播放播放列表中指定索引的歌曲（默认从之前停下的位置继续）。推送这一首到 N.E.K.O 播放。要按顺序一首接一首听，请等当前歌曲播放完后再点「下一首」（或调用 playlist_next），避免一次性全部推送全部播放出来的问题。",
        kind="action",
    )
    async def playlist_play(
        self,
        start_index: Annotated[int, "要播放第几首（从 0 开始，留空/负数=从当前游标位置继续）"] = -1,
        **_,
    ):
        if not self._playlist:
            return Err(SdkError("播放列表为空，请先添加歌曲"))
        # ── 关键：进入 playlist_play 时，先把"上一首的自动调度"取消掉 ──
        # 否则：上一首有调度在跑 → 点"上一首"时调度时间到了 → 调 playlist_next → 把歌切到下一首
        await self._autoplay_cancel_current()

        idx = int(start_index)
        if idx < 0:
            idx = self._playlist_cursor
        idx = max(0, min(idx, len(self._playlist) - 1))
        self._playlist_cursor = idx
        item = self._playlist[idx]
        url = await self._resolve_url_for_playlist_item(item)
        if not url:
            return Err(SdkError(f"第 {idx+1} 首「{item.get('title')}」没有可播放的音源，请先导入音频或下载"))
        domains = self._music_allowlist_domains_for_url(url)
        event_id = f"pl_{uuid.uuid4().hex[:8]}"
        title = item.get("title") or "自定义歌曲"
        artist = item.get("artist") or "未知艺术家"
        target_lanlan = self._resolve_target_lanlan(_)
        source_tag = str(self.plugin_id or "custom_music_list")
        try:
            if domains:
                self.ctx.push_message(
                    source=source_tag,
                    message_type="music_allowlist_add",
                    description=f"Allow host: {domains[0]}",
                    priority=7,
                    metadata={"domains": list(domains), "event_id": event_id},
                    target_lanlan=target_lanlan,
                )
            self.ctx.push_message(
                source=source_tag,
                message_type="music_play_url",
                description=f"🎵 播放列表 [{idx+1}/{len(self._playlist)}] {title} [{artist}]",
                priority=9,
                metadata={
                    "url": url,
                    "name": title,
                    "artist": artist,
                    "event_id": event_id,
                    "playlist_index": idx,
                    "playlist_total": len(self._playlist),
                },
                target_lanlan=target_lanlan,
            )
        except Exception as e:
            return Err(SdkError(f"推送失败: {e}"))

        # ── 如果开启了"自动下一首"，获取音频时长，启动后台计时器 ──
        duration_sec: Optional[float] = None
        duration_source = ""
        autoplay_enabled = self._playlist_autoplay_on
        if autoplay_enabled:
            stored = str(item.get("stored_name") or "").strip()
            if stored:
                duration_sec = self._get_audio_duration_sec(stored)
                if duration_sec is not None:
                    duration_source = "mutagen/mp3_header"
            if duration_sec is None:
                duration_sec = 240.0
                duration_source = "default_4min_unknown_duration"
            # 设置 _autoplay_current_song（注意：上面 cancel 已经清空过了）
            async with self._autoplay_lock:
                self._autoplay_current_song = {
                    "title": title,
                    "artist": artist,
                    "duration_sec": duration_sec,
                    "duration_source": duration_source,
                    "index": idx,
                }
            # 启动调度（放到后台），超时后自动 playlist_next
            kwargs_for_next = dict(_)
            asyncio.create_task(self._autoplay_scheduler_after_song(duration_sec, **kwargs_for_next))

        return Ok({
            "success": True,
            "message": f"正在播放第 {idx+1}/{len(self._playlist)} 首：{title} - {artist}" + (
                f"，{duration_source}估算 {round(duration_sec or 0,0)} 秒后自动跳下一首" if autoplay_enabled else ""
            ),
            "current_index": idx,
            "total": len(self._playlist),
            "title": title,
            "artist": artist,
            "has_prev": idx > 0,
            "has_next": idx < len(self._playlist) - 1,
            "autoplay_on": autoplay_enabled,
            "duration_sec": duration_sec,
            "duration_source": duration_source,
            "mode": self._playlist_mode,
        })

    def _compute_next_index(self, current_idx: int) -> tuple[int, str]:
        """根据当前播放模式，计算下一首的目标索引。返回 (new_idx, reason)。
        - sequential: 顺序播放，最后一首时返回 (-1, 'end') 表示结束
        - loop_one:   单曲循环，返回 (current_idx, 'loop_one')
        - loop_all:   列表循环，最后一首时回到 0
        - random:     随机挑一首（不能是当前首）
        """
        n = len(self._playlist)
        if n == 0:
            return -1, "empty"
        mode = self._playlist_mode or "sequential"
        if mode == "loop_one":
            return current_idx, "loop_one"
        if mode == "loop_all":
            return (current_idx + 1) % n, "loop_all"
        if mode == "random":
            if n == 1:
                return 0, "random_only_one"
            candidates = [i for i in range(n) if i != current_idx]
            return random.choice(candidates), "random"
        # sequential
        nxt = current_idx + 1
        if nxt >= n:
            return -1, "end"
        return nxt, "sequential"

    @plugin_entry(
        id="playlist_next",
        name="播放列表下一首",
        description="播放播放列表中的下一首（游标+1），推送一首到 N.E.K.O。用于替代自动连播，避免 Neko 一次性全部播放完。",
        kind="action",
    )
    async def playlist_next(self, **_):
        if not self._playlist:
            return Err(SdkError("播放列表为空"))
        # ── 切歌锁：防止"调度器自动 next"和"用户手点 next"同时执行导致跳过一首 ──
        async with self._playlist_advance_lock:
            # ── 关键保护：防止调度器和用户同时切歌 / 调度取消后还继续切 ──
            from_auto = bool(self._autoplay_in_auto_advance)
            if from_auto:
                cancelled = (
                    self._autoplay_cancel_event is not None
                    and self._autoplay_cancel_event.is_set()
                )
                if cancelled:
                    self.logger.info("autoplay next 被 cancel，不切歌")
                    return Ok({
                        "success": False,
                        "skipped": True,
                        "message": "调度已取消，跳过自动下一首",
                        "total": len(self._playlist),
                    })
            self._autoplay_in_auto_advance = False
            new_idx, reason = self._compute_next_index(self._playlist_cursor)
            if new_idx < 0 or reason == "end":
                await self._autoplay_cancel_current()
                return Ok({
                    "success": True,
                    "message": "已经是最后一首啦，播放列表播放完毕 ✨",
                    "current_index": self._playlist_cursor,
                    "total": len(self._playlist),
                    "is_end": True,
                    "mode": self._playlist_mode,
                })
            return await self.playlist_play(start_index=new_idx, **_)

    @plugin_entry(
        id="playlist_prev",
        name="播放列表上一首",
        description="播放播放列表中的上一首（游标-1），推送一首到 N.E.K.O。",
        kind="action",
    )
    async def playlist_prev(self, **_):
        if not self._playlist:
            return Err(SdkError("播放列表为空"))
        # ── 切歌锁：和 playlist_next 共用，防止并发切歌 ──
        async with self._playlist_advance_lock:
            self._autoplay_in_auto_advance = False
            if self._playlist_cursor <= 0:
                return Ok({
                    "success": True,
                    "message": "已经是第一首啦",
                    "current_index": 0,
                    "total": len(self._playlist),
                    "is_start": True,
                })
            return await self.playlist_play(start_index=self._playlist_cursor - 1, **_)

    @plugin_entry(
        id="playlist_stop",
        name="停止顺序播放/重置游标",
        description="将播放列表游标重置到第 0 首，方便下次从头播放。不会停止已经推送的当前播放。",
        kind="action",
    )
    async def playlist_stop(self, **_):
        await self._autoplay_cancel_current()  # 取消当前自动下一首调度
        self._playlist_cursor = 0
        self._autoplay_in_auto_advance = False
        return Ok({
            "success": True,
            "message": f"已重置播放列表游标（回到第 1 首），当前列表共 {len(self._playlist)} 首",
            "total": len(self._playlist),
            "mode": self._playlist_mode,
        })

    @plugin_entry(
        id="playlist_set_mode",
        name="设置播放模式",
        description="设置播放列表的播放模式：sequential=顺序（默认）/loop_one=单曲循环/loop_all=列表循环/random=随机。设置后立即生效，影响下一首的切换逻辑。",
        kind="action",
    )
    async def playlist_set_mode(
        self,
        mode: Annotated[str, "播放模式：sequential / loop_one / loop_all / random 之一"] = "sequential",
        **_,
    ):
        m = str(mode or "sequential").strip().lower()
        if m not in ("sequential", "loop_one", "loop_all", "random"):
            return Err(SdkError(f"不支持的播放模式：{mode}（可选：sequential / loop_one / loop_all / random）"))
        old_mode = self._playlist_mode
        self._playlist_mode = m
        mode_label = {
            "sequential": "顺序播放",
            "loop_one": "单曲循环",
            "loop_all": "列表循环",
            "random": "随机播放",
        }.get(m, m)
        self.logger.info(f"播放模式: {old_mode} → {m}")
        return Ok({
            "success": True,
            "old_mode": old_mode,
            "mode": m,
            "mode_label": mode_label,
            "message": f"已切换为「{mode_label}」模式",
        })

    @plugin_entry(
        id="playlist_get_mode",
        name="查询当前播放模式",
        description="获取当前播放列表的播放模式。",
        kind="action",
    )
    async def playlist_get_mode(self, **_):
        m = self._playlist_mode
        mode_label = {
            "sequential": "顺序播放",
            "loop_one": "单曲循环",
            "loop_all": "列表循环",
            "random": "随机播放",
        }.get(m, m)
        return Ok({
            "mode": m,
            "mode_label": mode_label,
            "message": f"当前播放模式：{mode_label}",
        })

    # ────────────── 时长获取 & 自动下一首（计时调度） ──────────────
    def _get_audio_duration_sec(self, stored_name: str) -> Optional[float]:
        """多策略读取音频时长：mutagen → MP3 头估算 → MP4/M4A 二进制解析。
        解决 B站下载的 fragmented MP4 文件 mutagen 返回 0 的问题。"""
        name = (stored_name or "").strip()
        if not name:
            return None
        path = self._uploads_dir() / name
        if not path.is_file():
            return None
        lower = str(path).lower()
        # 方案 A：mutagen（mp3/flac/wav/ogg 等，但 m4a 经常返回 0）
        try:
            import mutagen  # noqa: WPS433
            audio = mutagen.File(str(path))
            if audio is not None and getattr(audio, "info", None) is not None:
                length = getattr(audio.info, "length", None)
                if length is not None and length > 0:
                    return max(1.0, float(length))
        except Exception:
            pass
        # 方案 B：MP4/M4A 二进制解析（专门处理 fragmented MP4）
        if lower.endswith((".m4a", ".mp4")):
            try:
                dur = _mp4_duration_parse(path)
                if dur and dur > 0:
                    return max(1.0, float(dur))
            except Exception:
                pass
        # 方案 C：mp3 头估算法（无需第三方），仅 mp3
        if lower.endswith(".mp3"):
            try:
                dur = _mp3_duration_estimate(path)
                if dur and dur > 0:
                    return max(1.0, float(dur))
            except Exception:
                pass
        return None



    # ── 自动下一首调度器 ──
    async def _autoplay_cancel_current(self, **_):
        """取消当前运行中的自动下一首调度（有就 cancel）。不会抛错。
        关键：清空 self._autoplay_task，这样旧调度器的 finally 检查到
        task 不匹配就不会清空新调度器的状态。"""
        async with self._autoplay_lock:
            self._autoplay_task = None
            if self._autoplay_cancel_event is not None:
                try:
                    self._autoplay_cancel_event.set()
                except Exception:
                    pass
            self._autoplay_cancel_event = None
            self._autoplay_paused_event = None
            self._autoplay_song_started_at = 0.0
            self._autoplay_remaining_sec = 0.0
            self._autoplay_current_song = None
            self._playlist_playing = False

    async def _autoplay_scheduler_after_song(self, duration_sec: float, **kwargs):
        """在后台 asyncio.sleep duration_sec 后，调用 playlist_next 播放下一首。
        支持：cancel（cancel_event.set）、暂停/恢复（paused_event.set = 暂停）。
        关键：用局部变量存 remaining_sec，不用 self 字段，防止被 cancel_current 清空。
        """
        my_task = asyncio.current_task()
        try:
            async with self._autoplay_lock:
                cancel_ev = asyncio.Event()
                paused_ev = asyncio.Event()  # unset=running, set=paused
                self._autoplay_cancel_event = cancel_ev
                self._autoplay_paused_event = paused_ev
                self._autoplay_song_started_at = asyncio.get_event_loop().time()
                self._autoplay_remaining_sec = max(0.0, float(duration_sec))
                self._playlist_playing = True
                # 保存自己的 task 引用，cancel_current 和 finally 都靠这个判断
                self._autoplay_task = my_task
            # 用局部变量存 remaining，不依赖 self（防止被 cancel 清空后误判）
            local_remaining = max(0.0, float(duration_sec))
            try:
                # 阶段式 sleep，0.5 秒精度检查暂停/取消
                while local_remaining > 0.01:
                    if cancel_ev.is_set():
                        return
                    cur_paused = self._autoplay_paused_event
                    if cur_paused is not None and cur_paused is paused_ev and cur_paused.is_set():
                        try:
                            await asyncio.wait_for(asyncio.shield(asyncio.create_task(cur_paused.wait())), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
                        continue
                    step = min(0.5, local_remaining)
                    try:
                        await asyncio.wait_for(asyncio.shield(asyncio.create_task(cancel_ev.wait())), timeout=step)
                    except asyncio.TimeoutError:
                        cur_paused2 = self._autoplay_paused_event
                        if cur_paused2 is None or not cur_paused2.is_set():
                            local_remaining -= step
                            self._autoplay_remaining_sec = local_remaining
                    except Exception:
                        return
                # 时间到，且当前仍开启自动下一首 → 播放下一首
                autoplay_on = self._playlist_autoplay_on
                if not autoplay_on:
                    return
                if cancel_ev.is_set():
                    return
                self.logger.info(f"⏭ 计时到达，调用 playlist_next 播放下一首 (mode={self._playlist_mode})")
                self._autoplay_in_auto_advance = True
                await self.playlist_next(**kwargs)
            finally:
                # ── 关键：只有当当前 task 还是"活跃的"调度器时才清空状态 ──
                # 否则：新调度器已经替换了旧调度器，旧调度器的 finally 不能清空新调度器的状态
                async with self._autoplay_lock:
                    if self._autoplay_task is my_task:
                        self._autoplay_cancel_event = None
                        self._autoplay_paused_event = None
                        self._autoplay_song_started_at = 0.0
                        self._autoplay_remaining_sec = 0.0
                        self._autoplay_current_song = None
                        self._playlist_playing = False
                        self._autoplay_task = None
        except Exception as e:
            self.logger.warning(f"autoplay scheduler 异常: {e}")

    @plugin_entry(
        id="playlist_autoplay_toggle",
        name="开关自动下一首",
        description="开启后：每次播放歌曲时会根据音频时长计时，时间到自动调用「下一首」。关闭：完全手动切歌。",
        kind="action",
    )
    async def playlist_autoplay_toggle(
        self,
        enabled: Annotated[bool, "True=开启自动下一首，False=关闭（保留原本手动）"] = True,
        **_,
    ):
        self._playlist_autoplay_on = bool(enabled)
        if not self._playlist_autoplay_on:
            await self._autoplay_cancel_current()
        return Ok({
            "success": True,
            "autoplay_on": self._playlist_autoplay_on,
            "message": ("已开启自动下一首：播完当前首后（按音频时长）会自动跳下一首。" if self._playlist_autoplay_on else "已关闭自动下一首，改为完全手动点「下一首」。"),
        })

    @plugin_entry(
        id="playlist_autoplay_status",
        name="查询自动下一首状态",
        description="获取自动下一首开关状态、当前歌曲的剩余时间、是否暂停。",
        kind="action",
    )
    async def playlist_autoplay_status(self, **_):
        remaining = 0.0
        total = 0.0
        title = ""
        artist = ""
        paused = False
        scheduler_running = self._autoplay_cancel_event is not None
        # 关键修复：直接读 self._autoplay_current_song 拿 total_sec（不要用局部变量）
        cur = self._autoplay_current_song
        if cur is not None:
            total = float(cur.get("duration_sec") or 0)
            title = cur.get("title") or ""
            artist = cur.get("artist") or ""
        cur_paused_ev = self._autoplay_paused_event
        if cur_paused_ev is not None:
            paused = cur_paused_ev.is_set()
        if paused:
            remaining = max(0.0, self._autoplay_remaining_sec)
        elif scheduler_running and total > 0:
            started = self._autoplay_song_started_at
            if started:
                elapsed = asyncio.get_event_loop().time() - started
                remaining = max(0.0, total - elapsed)
            else:
                remaining = max(0.0, self._autoplay_remaining_sec)
        else:
            remaining = 0.0
        return Ok({
            "autoplay_on": self._playlist_autoplay_on,
            "scheduler_running": scheduler_running,
            "paused": paused,
            "remaining_sec": round(remaining, 1),
            "total_sec": round(total, 1),
            "current_title": title,
            "current_artist": artist,
            "current_index": (cur.get("index") if cur else None),
            "mode": self._playlist_mode,
            "playlist_size": len(self._playlist),
            "message": (
                f"自动下一首：{'开启' if self._playlist_autoplay_on else '关闭'}；"
                + (f"调度中，剩余 {round(remaining,0)} 秒（总 {round(total,0)} 秒）" if scheduler_running else "未启动调度")
                + ("，已暂停" if paused else "")
            ),
        })

    @plugin_entry(
        id="playlist_autoplay_pause_timer",
        name="暂停当前自动计时",
        description="暂停「自动下一首」的计时器，等你处理完再恢复。不会中断已经在播放的声音。",
        kind="action",
    )
    async def playlist_autoplay_pause_timer(self, **_):
        paused_ev = self._autoplay_paused_event
        if paused_ev is None:
            return Err(SdkError("当前没有正在运行的自动调度，无法暂停"))
        # 先把剩余量写回到 remaining（基于 elapsed 近似）
        started = self._autoplay_song_started_at
        if started and self._autoplay_current_song:
            total = float((self._autoplay_current_song or {}).get("duration_sec") or 0)
            if total > 0:
                elapsed = asyncio.get_event_loop().time() - started
                self._autoplay_remaining_sec = max(0.0, total - elapsed)
        paused_ev.set()
        return Ok({
            "success": True,
            "message": f"已暂停自动计时，剩余 {round(self._autoplay_remaining_sec,1)} 秒。调用恢复继续。",
            "remaining_sec": round(self._autoplay_remaining_sec, 1),
        })

    @plugin_entry(
        id="playlist_autoplay_resume_timer",
        name="恢复当前自动计时",
        description="恢复被暂停的自动计时器。",
        kind="action",
    )
    async def playlist_autoplay_resume_timer(self, **_):
        paused_ev = self._autoplay_paused_event
        if paused_ev is None:
            return Err(SdkError("当前没有正在运行的自动调度，无需恢复"))
        if not paused_ev.is_set():
            return Ok({"success": True, "message": "调度本来就在运行"})
        self._autoplay_song_started_at = asyncio.get_event_loop().time()
        paused_ev.clear()
        return Ok({
            "success": True,
            "message": f"已恢复自动计时，剩余 {round(self._autoplay_remaining_sec,1)} 秒。",
            "remaining_sec": round(self._autoplay_remaining_sec, 1),
        })

    # ==================== 音乐下载（根据 song_id） ====================
    async def _daga_parse_song(self, song_id: str, music_type: str = "netease") -> Optional[list]:
        """调用 daga.cc 解析音乐，返回 list of data 项（每一项含 title/author/url 等）。解析失败返回 None。"""
        if not str(song_id or "").strip():
            return None
        post_data = urlencode({
            "input": str(song_id).strip(),
            "filter": "id",
            "type": music_type or "netease",
            "page": 1,
        })
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://daga.cc",
            "Referer": "https://daga.cc/yue/",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
                resp = await client.post("https://daga.cc/yue/", content=post_data, headers=headers)
                resp.raise_for_status()
                result = resp.json()
            if result.get("code") == 200 and result.get("data"):
                data = result.get("data")
                if isinstance(data, list):
                    return data
                return [data]
            return None
        except Exception as e:
            self.logger.warning(f"daga.cc 解析 ID={song_id} 失败: {e}")
            return None

    async def _daga_download_url_to_uploads(
        self,
        url: str,
        title: str,
        author: str,
        ext: str = ".mp3",
        song_id: str = "",
    ) -> Optional[tuple[str, int]]:
        """从 url 下载音频到 uploads 目录，返回 (stored_name, size_bytes)。失败返回 None。
        song_id 用于生成唯一文件名的一部分，防止批量下载串歌。"""
        if not url:
            return None
        safe_title = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", str(title or "song").strip()) or "song"
        safe_author = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", str(author or "unknown").strip()) or "unknown"
        ext = ext if ext.startswith(".") else f".{ext}"
        if ext not in (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma"):
            ext = ".mp3"
        # 唯一命名：16位uuid + song_id摘要 + 标题 + 歌手 + 扩展名
        short_uuid = uuid.uuid4().hex[:16]
        safe_sid = re.sub(r"[^a-zA-Z0-9]", "", str(song_id or ""))[:12] or "nosid"
        stored_name = f"{short_uuid}_{safe_sid}_{safe_title[:30]}_{safe_author[:20]}{ext}"
        dest = self._uploads_dir() / stored_name
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://daga.cc/yue/",
            }
            async with httpx.AsyncClient(timeout=60.0, verify=True) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                f.write(chunk)
            size = dest.stat().st_size
            # 小于 4KB 的基本就是空文件/假链接
            if size < 4096:
                try:
                    dest.unlink()
                except Exception:
                    pass
                return None
            return stored_name, size
        except Exception as e:
            self.logger.warning(f"下载音频失败 url={url[:80]}... : {e}")
            try:
                if dest.is_file():
                    dest.unlink()
            except Exception:
                pass
            return None

    @plugin_entry(
        id="download_status",
        name="查询批量下载进度",
        description="查询当前批量下载歌曲的进度，包括已下载数、总数、当前正在下载的歌曲、成功/失败列表。",
        kind="action",
    )
    async def download_status(self, **_):
        st = dict(self._download_status)
        # 计算进度百分比
        pct = 0
        if st.get("total", 0) > 0:
            pct = round(st["done"] * 100.0 / st["total"], 1)
        st["progress_percent"] = pct
        return Ok(st)

    @plugin_entry(
        id="cancel_download",
        name="取消批量下载",
        description="取消当前正在进行的批量下载任务。下载会在当前歌曲完成后停止，已下载的不会删除。",
        kind="action",
    )
    async def cancel_download(self, **_):
        if not self._download_status.get("running"):
            return Ok({"success": False, "message": "当前没有正在进行的下载任务"})
        self._download_cancel_flag = True
        self.logger.info("用户请求取消批量下载")
        return Ok({"success": True, "message": "已发送取消信号，下载将在当前歌曲完成后停止"})

    @plugin_entry(
        id="download_song_by_id",
        name="根据ID下载单曲",
        description="根据单首歌曲的网易云 song_id 下载音频（调用 daga.cc 解析+下载），保存到 uploads 并返回 stored_name。如果没有可用下载链接会报错。",
        kind="action",
    )
    async def download_song_by_id(
        self,
        song_id: Annotated[str, "网易云歌曲 id"],
        title: Annotated[str, "歌曲名（用于文件名展示）"] = "",
        artist: Annotated[str, "歌手（用于文件名展示）"] = "",
        music_type: Annotated[str, "平台，默认 netease"] = "netease",
        **_,
    ):
        sid = str(song_id or "").strip()
        if not sid:
            return Err(SdkError("song_id 不能为空"))
        parsed = await self._daga_parse_song(sid, music_type or "netease")
        picked = None
        if parsed:
            for it in parsed:
                u = str(it.get("url") or "").strip()
                if u:
                    picked = it
                    break
        if not picked or not str(picked.get("url") or "").strip():
            return Err(SdkError(f"未找到可下载的音源链接（song_id={sid}），请自己手动导入音频文件"))
        t = str(picked.get("title") or title or f"song_{sid}").strip()
        a = str(picked.get("author") or artist or "未知艺术家").strip()
        raw_url = str(picked.get("url") or "")
        # 根据 URL 推断后缀
        ext = ".mp3"
        lower_url = raw_url.lower()
        for e in (".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma", ".mp3"):
            if e in lower_url:
                ext = e
                break
        result = await self._daga_download_url_to_uploads(raw_url, t, a, ext=ext, song_id=sid)
        if not result:
            return Err(SdkError(f"音源链接下载失败（song_id={sid}），文件损坏或太小，请自己手动导入"))
        stored_name, size = result
        size_kb = round(size / 1024.0, 1)
        return Ok({
            "success": True,
            "title": t,
            "artist": a,
            "song_id": sid,
            "stored_name": stored_name,
            "size_kb": size_kb,
            "message": f"已下载「{t}」-「{a}」到本地：{stored_name}（{size_kb} KB）",
        })

    def _build_todo_items_with_reuse(self, song_list: list, require_sid: bool = False) -> tuple:
        """构建批量下载的 todo_items 列表，支持跨歌单复用。
        - require_sid=True: 网易云批量下载必须有 song_id 才加入 todo
        - require_sid=False: B站/自动 批量下载没 song_id 也可加入（按 title 搜）
        返回 (todo_items, reused_count)。
        todo_items 每项: (idx, song_obj, song_id, already_has)
        """
        uploads_dir = self._uploads_dir()
        # 扫 uploads 目录，构建 song_id → stored_name 映射
        existing_sid_map: dict = {}
        try:
            audio_exts = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".ape", ".wma")
            for f in uploads_dir.iterdir():
                if not f.is_file():
                    continue
                if not f.name.lower().endswith(audio_exts):
                    continue
                name = f.name
                m = re.match(r"^[a-f0-9]+_([a-zA-Z0-9]+)_", name)
                if m:
                    extracted_sid = m.group(1)
                    if extracted_sid and extracted_sid != "nosid":
                        existing_sid_map[extracted_sid] = name
        except Exception:
            pass

        todo_items: list = []
        reused_count = 0
        for idx, s in enumerate(song_list):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("song_id") or "").strip()
            if require_sid and not sid:
                continue
            has = False
            # 1) 先看歌曲自身是否已有 stored_name
            for key in ("stored_name", "path"):
                v = str(s.get(key) or "").strip()
                if v and (uploads_dir / v).is_file():
                    has = True
                    break
            # 2) 跨歌单复用：看 uploads 里有没有相同 song_id 的文件
            if not has and sid and sid in existing_sid_map:
                reused_name = existing_sid_map[sid]
                s["stored_name"] = reused_name
                s["path"] = reused_name
                has = True
                reused_count += 1
                self.logger.info(f"跨歌单复用: song_id={sid} → {reused_name}")
            todo_items.append((idx, s, sid, has))
        return todo_items, reused_count

    @plugin_entry(
        id="batch_download_songs",
        name="按ID批量下载歌曲并填入路径",
        description="接收歌曲列表 JSON（每项含 song_id/name/artists），一首首下载；每下载成功一首就把 stored_name/path 填到对应 JSON 项里；完成后返回完整的新 songs 列表（可直接传给 save_song_list 保存）。过程中可用 download_status 查进度。",
        kind="action",
    )
    async def batch_download_songs(
        self,
        songs: Annotated[str, "歌曲列表 JSON 字符串，每项建议含 song_id/name/artists"],
        save_filename: Annotated[str, "全部完成后是否同时保存？填入 txt 文件名即可（如 我的歌单.txt）；留空不自动保存"] = "",
        playlist_name: Annotated[str, "自动保存时用的歌单名称"] = "",
        **_,
    ):
        # ── 后台任务模式：入口只做"启动检查 + 立刻返回"，实际下载放到 create_task 后台跑 ──
        # 这样就不会触发 Neko SDK 30 秒超时；前端靠 download_status 轮询同步进度和 songs
        try:
            song_list = json.loads(songs) if isinstance(songs, str) else songs
            if not isinstance(song_list, list):
                return Err(SdkError("songs 参数必须是列表 JSON"))
        except Exception as e:
            return Err(SdkError(f"songs JSON 解析失败: {e}"))

        # 筛选待下载目标 + 跨歌单复用
        todo_items, reused_count = self._build_todo_items_with_reuse(song_list, require_sid=True)
        total_target = sum(1 for it in todo_items if not it[3])
        if total_target == 0:
            return Ok({
                "success": True,
                "started": False,
                "message": "传入的歌曲要么没有 song_id，要么都已经导入过本地音频啦，无需再下载。",
                "processed": 0,
                "songs": song_list,
            })
        # 抢锁启动
        async with self._download_lock:
            if self._download_status["running"]:
                return Err(SdkError("当前已有批量下载任务在进行，请稍后或等当前完成"))
            st = self._download_status
            st["running"] = True
            st["total"] = total_target
            st["done"] = 0
            st["current_title"] = ""
            st["current_artist"] = ""
            st["current_index"] = 0
            st["current_stage"] = ""
            st["failed"] = []
            st["success"] = []
            st["started_at"] = time.time()
            st["finished_at"] = None
            st["songs"] = list(song_list)
            st["source"] = "netease"
            st["save_filename"] = str(save_filename or "").strip()
            st["playlist_name"] = str(playlist_name or "").strip()
            st["message"] = f"已启动（{total_target} 首待下载）"
            st["task_error"] = ""

        # ── 后台任务：一首首执行，每首失败都 continue 不会卡住 ──
        async def _bg():
            try:
                await self._bg_batch_download_worker("netease", todo_items, **_)
            except Exception as e:
                self.logger.error(f"批量下载后台任务异常: {e}")
                self._download_status["task_error"] = str(e)
                self._download_status["running"] = False
            finally:
                pass

        self._download_bg_task = asyncio.create_task(_bg())
        return Ok({
            "success": True,
            "started": True,
            "message": f"已启动网易云批量下载（{total_target} 首），请在进度面板查看实时进度。",
            "total": total_target,
        })

    async def _bg_batch_download_worker(self, source: str, todo_items: list, **_):
        """通用批量下载后台 worker。
        每首失败都会 try/except 捕获，不会让整体任务崩溃。
        每处理一首就更新 self._download_status["songs"][i] 的 stored_name/path，前端轮询立刻能看到。
        """
        st = self._download_status
        final_songs = list(st["songs"] or [])
        save_fn = str(st.get("save_filename") or "").strip()
        pl_name = str(st.get("playlist_name") or "").strip()
        total_target = int(st.get("total") or 0)
        try:
            processed_counter = 0
            for seq_i, (s_idx, s_obj, sid, already_has) in enumerate(todo_items):
                try:  # 单首 try/except 防止一首失败全部停
                    # ── 取消检查：用户点了"取消下载"就立刻退出循环 ──
                    if self._download_cancel_flag:
                        st["message"] = (
                            f"下载已取消：成功 {len(st['success'])} 首，"
                            f"失败 {len(st['failed'])} 首，"
                            f"剩余 {total_target - processed_counter} 首未下载。"
                        )
                        self.logger.info(st["message"])
                        break
                    # 同步已有音频到 final_songs
                    if already_has:
                        p = str(s_obj.get("stored_name") or s_obj.get("path") or "").strip()
                        s_obj2 = dict(final_songs[s_idx])
                        s_obj2["stored_name"] = p
                        s_obj2["path"] = p
                        final_songs[s_idx] = s_obj2
                        continue
                    processed_counter += 1
                    title = str(s_obj.get("name") or s_obj.get("title") or f"song_{sid or 'unknown'}").strip()
                    artist = str(s_obj.get("artists") or s_obj.get("artist") or "未知艺术家").strip()
                    st["current_title"] = title
                    st["current_artist"] = artist
                    st["current_index"] = processed_counter
                    st["current_stage"] = "parsing"

                    ok_flag = False
                    stored_name = None
                    size_kb = 0
                    err_msg = ""
                    used_source = ""

                    if source == "netease":
                        if not sid:
                            err_msg = "无 song_id"
                        else:
                            parsed = await self._daga_parse_song(sid, "netease")
                            picked = None
                            if parsed:
                                for it in parsed:
                                    if str(it.get("url") or "").strip():
                                        picked = it
                                        break
                            if not picked:
                                err_msg = "无可用音源链接"
                            else:
                                st["current_stage"] = "downloading"
                                raw_url = str(picked.get("url") or "")
                                lower_url = raw_url.lower()
                                ext = ".mp3"
                                for e in (".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma", ".mp3"):
                                    if e in lower_url:
                                        ext = e
                                        break
                                dl_res = await self._daga_download_url_to_uploads(
                                    raw_url, str(picked.get("title") or title),
                                    str(picked.get("author") or artist), ext=ext, song_id=sid,
                                )
                                if not dl_res:
                                    err_msg = "下载失败（文件太小或网络错误）"
                                else:
                                    stored_name, sz = dl_res
                                    size_kb = round(sz / 1024.0, 1)
                                    ok_flag = True
                                    used_source = "netease"

                    elif source == "bili":
                        st["current_stage"] = "downloading"
                        # 批量场景下 B 站风控：每首之间 sleep 2 秒，避免短时间高频搜索被限
                        if processed_counter > 1:
                            await asyncio.sleep(2.0)
                        ok_flag, msg, stored_name = await self._run_bili_download_subprocess(title, artist, self._uploads_dir(), song_id=sid)
                        if ok_flag and stored_name:
                            try:
                                sz = (self._uploads_dir() / stored_name).stat().st_size
                                size_kb = round(sz / 1024.0, 1)
                            except Exception:
                                size_kb = 0
                            used_source = "bili"
                        else:
                            err_msg = msg or "B站下载失败"
                            # 失败后再 sleep 3 秒，给 B 站风控冷却时间
                            await asyncio.sleep(3.0)

                    elif source == "auto":
                        if sid:
                            parsed = await self._daga_parse_song(sid, "netease")
                            picked = None
                            if parsed:
                                for it in parsed:
                                    if str(it.get("url") or "").strip():
                                        picked = it
                                        break
                            if picked:
                                st["current_stage"] = "downloading"
                                raw_url = str(picked.get("url") or "")
                                lower_url = raw_url.lower()
                                ext = ".mp3"
                                for e in (".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma", ".mp3"):
                                    if e in lower_url:
                                        ext = e
                                        break
                                dl_res = await self._daga_download_url_to_uploads(
                                    raw_url, str(picked.get("title") or title),
                                    str(picked.get("author") or artist), ext=ext, song_id=sid,
                                )
                                if dl_res:
                                    stored_name, sz = dl_res
                                    size_kb = round(sz / 1024.0, 1)
                                    ok_flag = True
                                    used_source = "netease"
                        if not ok_flag:
                            st["current_stage"] = "downloading"
                            # 批量场景下 B 站风控：sleep 2 秒
                            if processed_counter > 1:
                                await asyncio.sleep(2.0)
                            ok_flag, msg, stored_name = await self._run_bili_download_subprocess(title, artist, self._uploads_dir(), song_id=sid)
                            if ok_flag and stored_name:
                                try:
                                    sz = (self._uploads_dir() / stored_name).stat().st_size
                                    size_kb = round(sz / 1024.0, 1)
                                except Exception:
                                    size_kb = 0
                                used_source = "bili"
                            else:
                                err_msg = msg or "网易云和B站都失败"
                                # 失败后再 sleep 3 秒
                                await asyncio.sleep(3.0)

                    if ok_flag and stored_name:
                        s_obj2 = dict(final_songs[s_idx])
                        s_obj2["stored_name"] = stored_name
                        s_obj2["path"] = stored_name
                        s_obj2["song_id"] = str(s_obj2.get("song_id") or sid)
                        final_songs[s_idx] = s_obj2
                        # ↓↓ 每下完一首立刻同步 songs，前端就能立刻填回 UI ↓↓
                        st["songs"] = list(final_songs)
                        st["success"].append({
                            "title": title, "artist": artist, "song_id": sid,
                            "stored_name": stored_name, "size_kb": size_kb,
                            "source": used_source or source,
                        })
                        self.logger.info(f"批量下载 [{processed_counter}/{total_target}] OK: {title} - {artist} ({size_kb} KB, source={used_source or source})")
                    else:
                        st["failed"].append({
                            "title": title, "artist": artist, "song_id": sid, "reason": err_msg or "未知错误",
                        })
                        self.logger.warning(f"批量下载 [{processed_counter}/{total_target}] FAIL: {title} - {artist}: {err_msg}")
                    st["done"] = processed_counter
                    # 每首无论成功失败都同步 songs 引用
                    st["songs"] = list(final_songs)
                except Exception as song_e:
                    # 单首异常不影响整体
                    self.logger.warning(f"批量下载单首异常: {song_e}")
                    title = str(s_obj.get("name") or s_obj.get("title") or "").strip() or f"index_{s_idx}"
                    artist = str(s_obj.get("artists") or s_obj.get("artist") or "").strip()
                    st["failed"].append({
                        "title": title, "artist": artist,
                        "song_id": str(s_obj.get("song_id") or "").strip(),
                        "reason": f"单首异常: {song_e}",
                    })
                    processed_counter += 1
                    st["done"] = processed_counter
                    st["songs"] = list(final_songs)
                    continue
            st["current_stage"] = "done"
            st["finished_at"] = time.time()

            # 可选：自动保存
            if save_fn and final_songs:
                try:
                    await self.save_song_list(
                        filename=save_fn,
                        playlist_name=pl_name or Path(save_fn).stem,
                        songs=json.dumps(final_songs, ensure_ascii=False),
                        **_,
                    )
                except Exception as save_e:
                    self.logger.warning(f"批量下载完成，但自动保存失败: {save_e}")
                    st["task_error"] = f"任务完成但保存失败: {save_e}"

            st["message"] = (
                f"批量下载完成：成功 {len(st['success'])} 首，失败 {len(st['failed'])} 首。"
            )
            self.logger.info(st["message"])
        finally:
            st["running"] = False
            self._download_cancel_flag = False
    # 通过子进程调用 bili_download/bili_music_downloader.py（依赖 requests + yt-dlp，
    # 这些库不一定装在 Neko 的 Python 环境里，所以走子进程方式，用项目内置的 venv 解释器）

    def _bili_script_path(self) -> Path:
        """bili_music_downloader.py 脚本路径
        注意：Neko SDK 没有 self.plugin_dir 属性，用 config_dir 代替。
        config_dir 就是插件目录本身（包含 plugin.toml 的目录）。
        """
        return Path(self.config_dir) / "bili_download" / "bili_music_downloader.py"

    def _bili_python_exe(self) -> str:
        """优先用项目隔离 venv 里的 python；失败回退 sys.executable"""
        candidate = r"C:\Users\Dustnunknown\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
        if Path(candidate).is_file():
            return candidate
        return sys.executable

    async def _run_bili_download_subprocess(
        self, song: str, artist: str, out_dir: Path, song_id: str = ""
    ) -> tuple[bool, str, Optional[str]]:
        """调子进程运行 bili_music_downloader.py，下载音频。
        返回 (ok, message, stored_name_if_ok)。
        out_dir 是主 uploads 目录。
        song_id 用于在文件名里加额外区分，防止串歌。
        每次下载用独立临时子目录，杜绝多首串歌。"""
        script = self._bili_script_path()
        if not script.is_file():
            return False, f"B站下载脚本不存在: {script}", None
        py = self._bili_python_exe()

        # ── 核心修复：每次用独立临时目录，下载完后 move 到主目录并唯一命名 ──
        unique_key = uuid.uuid4().hex[:16]
        safe_sid = re.sub(r"[^a-zA-Z0-9]", "", str(song_id or ""))[:12] or "nosid"
        tmp_dir = out_dir / f"_tmp_{unique_key}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 用 -y 跳过交互；-o 指定输出目录（独立临时目录）
            args = [py, "-X", "utf8", str(script), song, artist, "-y", "-o", str(tmp_dir), "-t", "1"]
            # 强制 UTF-8 输出
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            # 用 asyncio.create_subprocess_exec 异步等子进程
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            rc = proc.returncode
            text = ""
            try:
                text = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass
            # ── 关键修复：B 站脚本现在会打印 __OK__ / __FAIL__ 标记 ──
            # 即使 rc=0，如果 stdout 含 __FAIL__ 也判失败（旧脚本 rc=0 但实际失败的兜底）
            if "__FAIL__" in text:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站下载失败: {last_line or '脚本标记 __FAIL__'}", None
            if rc != 0:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站下载失败 (rc={rc}): {last_line or '无输出'}", None

            # 从独立临时目录里取音频文件（绝对不会串歌！）
            await asyncio.sleep(0.15)
            candidates = []
            for ext in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"):
                for f in tmp_dir.glob(f"*{ext}"):
                    if f.is_file():
                        candidates.append(f)
            if not candidates:
                return False, "B站下载完成但临时目录没有音频文件", None

            # 取唯一的一个（临时目录里只会有这一次下载的文件）
            if len(candidates) > 1:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            picked = candidates[0]
            src_ext = picked.suffix.lower()
            # 唯一命名：uuid_短sid_标题_歌手.ext
            safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(song or "song").strip())[:30] or "song"
            safe_author = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(artist or "unknown").strip())[:20] or "unknown"
            stored_name = f"{unique_key}_{safe_sid}_{safe_title}_{safe_author}{src_ext}"
            dest = out_dir / stored_name
            picked.rename(dest)
            return True, f"已下载「{song} - {artist}」到本地: {stored_name}", stored_name
        except asyncio.TimeoutError:
            return False, f"B站下载超时（10 分钟未完成）: {song} - {artist}", None
        except FileNotFoundError as e:
            return False, f"找不到 Python 解释器或脚本: {e}", None
        except Exception as e:
            return False, f"B站下载异常: {e}", None
        finally:
            # 清理临时目录
            try:
                if tmp_dir.is_dir():
                    for f in tmp_dir.iterdir():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    tmp_dir.rmdir()
            except Exception:
                pass

    @plugin_entry(
        id="download_song_from_bili",
        name="B站解析下载单曲",
        description="根据歌曲名+歌手，在B站搜索并下载音源到本地 uploads 目录。返回 stored_name。",
        kind="action",
    )
    async def download_song_from_bili(
        self,
        title: Annotated[str, "歌曲名"],
        artist: Annotated[str, "歌手名（没有可传空字符串）"] = "",
        **_,
    ):
        t = str(title or "").strip()
        a = str(artist or "").strip()
        if not t:
            return Err(SdkError("title 不能为空"))
        uploads = self._uploads_dir()
        uploads.mkdir(parents=True, exist_ok=True)
        ok, msg, stored = await self._run_bili_download_subprocess(t, a, uploads)
        if not ok or not stored:
            return Err(SdkError(msg or "B站下载失败"))
        # 拿文件大小
        try:
            size = (uploads / stored).stat().st_size
            size_kb = round(size / 1024.0, 1)
        except Exception:
            size_kb = 0
        return Ok({
            "success": True,
            "title": t,
            "artist": a,
            "stored_name": stored,
            "size_kb": size_kb,
            "source": "bili",
            "message": f"B站下载成功: {stored} ({size_kb} KB)",
        })

    # ==================== B站手动选择音源 ====================
    async def _run_bili_search_subprocess(
        self, song: str, artist: str
    ) -> tuple[bool, str, list]:
        """调子进程运行 bili_music_downloader.py --search-json，返回 (ok, message, results_list)。
        搜索B站并返回候选视频列表（JSON 格式），不执行下载。"""
        script = self._bili_script_path()
        if not script.is_file():
            return False, f"B站下载脚本不存在: {script}", []
        py = self._bili_python_exe()

        args = [py, "-X", "utf8", str(script), song, artist, "--search-json", "-t", "20"]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            rc = proc.returncode
            text = ""
            try:
                text = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass
            if rc != 0:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站搜索失败 (rc={rc}): {last_line or '无输出'}", []
            # 解析 JSON 输出（脚本最后一行是 JSON）
            lines = text.strip().splitlines()
            json_str = ""
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    json_str = line
                    break
            if not json_str:
                return False, "B站搜索返回的输出中未找到 JSON 数据", []
            data = json.loads(json_str)
            if not data.get("ok"):
                return False, data.get("error", "B站搜索未返回结果"), []
            results = data.get("results", [])
            return True, f"找到 {len(results)} 个候选视频", results
        except asyncio.TimeoutError:
            return False, "B站搜索超时（60秒未完成）", []
        except json.JSONDecodeError as e:
            return False, f"B站搜索结果 JSON 解析失败: {e}", []
        except Exception as e:
            return False, f"B站搜索异常: {e}", []

    @plugin_entry(
        id="bili_search_videos",
        name="B站搜索候选视频",
        description="根据歌曲名+歌手在B站搜索，返回候选视频列表（含BV号、标题、UP主、时长、播放量、评分），供用户手动选择下载。不执行下载。",
        kind="action",
    )
    async def bili_search_videos(
        self,
        title: Annotated[str, "歌曲名"],
        artist: Annotated[str, "歌手名（没有可传空字符串）"] = "",
        **_,
    ):
        t = str(title or "").strip()
        a = str(artist or "").strip()
        if not t:
            return Err(SdkError("title 不能为空"))
        ok, msg, results = await self._run_bili_search_subprocess(t, a)
        if not ok:
            return Err(SdkError(msg or "B站搜索失败"))
        return Ok({
            "success": True,
            "title": t,
            "artist": a,
            "count": len(results),
            "results": results,
            "message": msg,
        })

    async def _run_bili_download_by_bvid_subprocess(
        self, bvid: str, song: str, artist: str, out_dir: Path, song_id: str = ""
    ) -> tuple[bool, str, Optional[str]]:
        """调子进程运行 bili_music_downloader.py --bvid xxx，下载指定 BV 号的音频。
        返回 (ok, message, stored_name_if_ok)。"""
        script = self._bili_script_path()
        if not script.is_file():
            return False, f"B站下载脚本不存在: {script}", None
        py = self._bili_python_exe()

        unique_key = uuid.uuid4().hex[:16]
        safe_sid = re.sub(r"[^a-zA-Z0-9]", "", str(song_id or ""))[:12] or "nosid"
        tmp_dir = out_dir / f"_tmp_{unique_key}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            args = [
                py, "-X", "utf8", str(script),
                song, artist,
                "--bvid", bvid,
                "-o", str(tmp_dir),
            ]
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            rc = proc.returncode
            text = ""
            try:
                text = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass
            if "__FAIL__" in text:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站BV下载失败: {last_line or '脚本标记 __FAIL__'}", None
            if rc != 0:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站BV下载失败 (rc={rc}): {last_line or '无输出'}", None

            await asyncio.sleep(0.15)
            candidates = []
            for ext in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"):
                for f in tmp_dir.glob(f"*{ext}"):
                    if f.is_file():
                        candidates.append(f)
            if not candidates:
                return False, "B站BV下载完成但临时目录没有音频文件", None

            if len(candidates) > 1:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            picked = candidates[0]
            src_ext = picked.suffix.lower()
            safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(song or "song").strip())[:30] or "song"
            safe_author = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(artist or "unknown").strip())[:20] or "unknown"
            stored_name = f"{unique_key}_{safe_sid}_{safe_title}_{safe_author}{src_ext}"
            dest = out_dir / stored_name
            picked.rename(dest)
            return True, f"已下载「{song} - {artist}」(BV: {bvid}) 到本地: {stored_name}", stored_name
        except asyncio.TimeoutError:
            return False, f"B站BV下载超时（10分钟未完成）: {song} - {artist}", None
        except FileNotFoundError as e:
            return False, f"找不到 Python 解释器或脚本: {e}", None
        except Exception as e:
            return False, f"B站BV下载异常: {e}", None
        finally:
            try:
                if tmp_dir.is_dir():
                    for f in tmp_dir.iterdir():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    tmp_dir.rmdir()
            except Exception:
                pass

    @plugin_entry(
        id="bili_download_by_bvid",
        name="B站按BV号下载音源",
        description="根据用户指定的B站视频BV号，下载该视频的音频到本地 uploads 目录。返回 stored_name。用于用户手动选择B站音源后下载。",
        kind="action",
    )
    async def bili_download_by_bvid(
        self,
        bvid: Annotated[str, "B站视频BV号（如 BV1xx411x7xx）或完整B站视频URL"],
        title: Annotated[str, "歌曲名（用于文件命名展示）"] = "",
        artist: Annotated[str, "歌手名（用于文件命名展示）"] = "",
        song_id: Annotated[str, "网易云歌曲ID（用于文件命名区分，可选）"] = "",
        **_,
    ):
        bv = str(bvid or "").strip()
        if not bv:
            return Err(SdkError("bvid 不能为空"))
        t = str(title or "").strip()
        a = str(artist or "").strip()
        sid = str(song_id or "").strip()
        if not t:
            t = bv
        uploads = self._uploads_dir()
        uploads.mkdir(parents=True, exist_ok=True)
        ok, msg, stored = await self._run_bili_download_by_bvid_subprocess(bv, t, a, uploads, song_id=sid)
        if not ok or not stored:
            return Err(SdkError(msg or "B站BV下载失败"))
        try:
            size = (uploads / stored).stat().st_size
            size_kb = round(size / 1024.0, 1)
        except Exception:
            size_kb = 0
        return Ok({
            "success": True,
            "title": t,
            "artist": a,
            "bvid": bv,
            "stored_name": stored,
            "size_kb": size_kb,
            "source": "bili_manual",
            "message": f"B站手动下载成功: {stored} ({size_kb} KB)",
        })

    @plugin_entry(
        id="bili_match_existing_songs",
        name="B站歌单匹配已下载音频",
        description="根据传入的歌曲列表（每项含 title + upper_name），从 uploads 目录扫描已有音频文件，按歌名+UP主做模糊匹配。返回哪些歌已经有匹配音频（直接复用，不用重新下载）。",
        kind="action",
    )
    async def bili_match_existing_songs(
        self,
        songs_json: Annotated[str, "JSON 字符串：[{title, upper_name, bvid}, ...]"] = "[]",
        **_,
    ):
        """匹配已下载的音频：检查 uploads 目录下文件名的 title+upper_name 是否与传入的歌匹配。
        匹配规则：歌名去掉空格、标点后做子串匹配（双向），UP主做模糊子串匹配。"""
        try:
            songs = json.loads(songs_json) if isinstance(songs_json, str) else songs_json
            if not isinstance(songs, list):
                return Err(SdkError("songs_json 必须是列表"))
            uploads = self._uploads_dir()
            if not uploads.is_dir():
                return Ok({"matched": [], "unmatched": songs, "scan_files": 0})
            # 预扫描 uploads 目录
            scan_files = []
            for f in uploads.iterdir():
                if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".ape", ".wma"):
                    scan_files.append(f.stem)  # 文件名不含扩展名
            matched = []
            unmatched = []
            for song in songs:
                if not isinstance(song, dict):
                    unmatched.append(song)
                    continue
                title = _normalize_match_text(str(song.get("title") or "").strip())
                upper = _normalize_match_text(str(song.get("upper_name") or song.get("artists") or "").strip())
                hit_stored = None
                hit_score = 0
                for stem in scan_files:
                    stem_norm = _normalize_match_text(stem)
                    if not title:
                        continue
                    score = 0
                    # 标题必须命中
                    if title in stem_norm or stem_norm in title:
                        score += 50
                    elif len(title) >= 4:
                        # 至少要 4 字符才允许子串重叠
                        shorter = min(len(title), len(stem_norm))
                        overlap = sum(1 for i in range(shorter) if title[i] == stem_norm[i])
                        if overlap >= 4:
                            score += 30
                    # UP主加分
                    if upper and len(upper) >= 2:
                        if upper in stem_norm:
                            score += 30
                        elif any(part in stem_norm for part in (upper,)):
                            score += 15
                    if score >= 60 and score > hit_score:
                        hit_score = score
                        # 还原真实文件名（含扩展名）
                        # uploads_dir 下的实际文件
                        for f in uploads.iterdir():
                            if f.stem == stem:
                                hit_stored = f.name
                                break
                        if hit_stored:
                            break
                if hit_stored:
                    matched.append({
                        "title": song.get("title", ""),
                        "upper_name": song.get("upper_name", song.get("artists", "")),
                        "bvid": song.get("bvid", ""),
                        "stored_name": hit_stored,
                        "matched_by": "name_fuzzy",
                    })
                else:
                    unmatched.append(song)
            return Ok({
                "matched": matched,
                "unmatched": unmatched,
                "scan_files": len(scan_files),
            })
        except Exception as e:
            return Err(SdkError(f"匹配失败: {e}"))

    # ==================== B站收藏夹功能 ====================

    def _bili_cookie_file(self) -> Path:
        """B站 SESSDATA cookie 持久化文件"""
        return Path(self.config_dir) / "bili_cookie.json"

    def _load_bili_sessdata(self) -> str:
        """从文件加载B站 SESSDATA，返回空字符串表示未保存"""
        try:
            fp = self._bili_cookie_file()
            if fp.is_file():
                data = json.loads(fp.read_text(encoding="utf-8"))
                return str(data.get("sessdata", "")).strip()
        except Exception:
            pass
        return ""

    async def _run_bili_fav_subprocess(
        self, mode: str, sessdata: str, media_id: int = 0
    ) -> tuple[bool, str, dict]:
        """调子进程运行 bili_music_downloader.py --fav-lists 或 --fav-videos。
        返回 (ok, message, parsed_json_dict)。"""
        script = self._bili_script_path()
        if not script.is_file():
            return False, f"B站下载脚本不存在: {script}", {}
        py = self._bili_python_exe()

        args = [py, "-X", "utf8", str(script), "--sessdata", sessdata]
        if mode == "fav_lists":
            args.append("--fav-lists")
        elif mode == "fav_videos":
            args.append("--fav-videos")
            args.append(str(media_id))
        else:
            return False, f"未知模式: {mode}", {}

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            rc = proc.returncode
            text = ""
            try:
                text = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                pass
            if rc != 0:
                last_line = (text.strip().splitlines() or [""])[-1]
                return False, f"B站收藏夹接口失败 (rc={rc}): {last_line or '无输出'}", {}
            # 解析 JSON 输出（脚本最后一行是 JSON）
            lines = text.strip().splitlines()
            json_str = ""
            for line in reversed(lines):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    json_str = line
                    break
            if not json_str:
                return False, "B站收藏夹返回的输出中未找到 JSON 数据", {}
            data = json.loads(json_str)
            if not data.get("ok"):
                return False, data.get("error", "B站收藏夹接口返回错误"), {}
            return True, "ok", data
        except asyncio.TimeoutError:
            return False, "B站收藏夹接口超时（60秒未完成）", {}
        except json.JSONDecodeError as e:
            return False, f"B站收藏夹结果 JSON 解析失败: {e}", {}
        except Exception as e:
            return False, f"B站收藏夹接口异常: {e}", {}

    @plugin_entry(
        id="bili_save_cookie",
        name="保存B站Cookie",
        description="保存B站用户登录态 SESSDATA cookie，用于访问收藏夹等功能。",
        kind="action",
    )
    async def bili_save_cookie(
        self,
        sessdata: Annotated[str, "B站 SESSDATA cookie 值"],
        **_,
    ):
        sd = str(sessdata or "").strip()
        if not sd:
            return Err(SdkError("sessdata 不能为空"))
        fp = self._bili_cookie_file()
        fp.write_text(
            json.dumps({"sessdata": sd, "saved_ts": int(time.time())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info("已保存B站 SESSDATA cookie")
        return Ok({"success": True, "message": "B站 Cookie 已保存"})

    @plugin_entry(
        id="bili_cookie_status",
        name="B站Cookie状态",
        description="检查B站 SESSDATA cookie 是否已保存。",
        kind="action",
    )
    async def bili_cookie_status(self, **_):
        sd = self._load_bili_sessdata()
        return Ok({"saved": bool(sd), "has_cookie": bool(sd)})

    @plugin_entry(
        id="bili_get_fav_lists",
        name="获取B站收藏夹列表",
        description="获取当前登录B站用户的所有收藏夹列表（需要先保存 SESSDATA cookie）。",
        kind="action",
    )
    async def bili_get_fav_lists(self, **_):
        sd = self._load_bili_sessdata()
        if not sd:
            return Err(SdkError("未保存B站 Cookie，请先在B站收藏夹页面输入 SESSDATA"))
        ok, msg, data = await self._run_bili_fav_subprocess("fav_lists", sd)
        if not ok:
            return Err(SdkError(msg))
        return Ok({
            "success": True,
            "mid": data.get("mid", ""),
            "name": data.get("name", ""),
            "count": data.get("count", 0),
            "folders": data.get("folders", []),
            "message": f"找到 {data.get('count', 0)} 个收藏夹",
        })

    @plugin_entry(
        id="bili_get_fav_videos",
        name="获取B站收藏夹视频",
        description="获取指定收藏夹内的所有视频列表（需要先保存 SESSDATA cookie）。",
        kind="action",
    )
    async def bili_get_fav_videos(
        self,
        media_id: Annotated[int, "收藏夹 ID"],
        **_,
    ):
        sd = self._load_bili_sessdata()
        if not sd:
            return Err(SdkError("未保存B站 Cookie，请先在B站收藏夹页面输入 SESSDATA"))
        ok, msg, data = await self._run_bili_fav_subprocess("fav_videos", sd, media_id=media_id)
        if not ok:
            return Err(SdkError(msg))
        return Ok({
            "success": True,
            "media_id": data.get("media_id", media_id),
            "count": data.get("count", 0),
            "videos": data.get("videos", []),
            "message": f"找到 {data.get('count', 0)} 个视频",
        })

    @plugin_entry(
        id="download_song_auto",
        name="自动解析下载单曲",
        description="先尝试网易云解析站（需要 song_id）；失败/没有 song_id 时自动尝试B站搜索（按 title+artist）。返回 stored_name。",
        kind="action",
    )
    async def download_song_auto(
        self,
        title: Annotated[str, "歌曲名"],
        artist: Annotated[str, "歌手名"] = "",
        song_id: Annotated[str, "网易云歌曲 ID（有就传，没有可留空）"] = "",
        **_,
    ):
        t = str(title or "").strip()
        a = str(artist or "").strip()
        if not t:
            return Err(SdkError("title 不能为空"))
        sid = str(song_id or "").strip()
        # 1) 先尝试网易云
        if sid:
            try:
                r = await self.download_song_by_id(
                    song_id=sid, title=t, artist=a, music_type="netease",
                )
                if isinstance(r, Ok):
                    v = r.value if hasattr(r, "value") else r
                    v2 = dict(v)
                    v2["source"] = "netease"
                    return Ok(v2)
            except Exception as e:
                self.logger.warning(f"auto: 网易云解析失败 ({sid}): {e}")
        # 2) 回退到 B站
        self.logger.info(f"auto: 回退到 B站搜索: {t} - {a}")
        r2 = await self.download_song_from_bili(title=t, artist=a)
        if isinstance(r2, Ok):
            v = r2.value if hasattr(r2, "value") else r2
            v2 = dict(v)
            v2["source"] = "bili"
            return Ok(v2)
        return Err(SdkError(f"自动解析失败：网易云和B站都没下载成功「{t} - {a}」"))

    @plugin_entry(
        id="batch_download_songs_bili",
        name="B站解析批量下载",
        description="批量从B站解析下载歌曲音源。每首歌按 name+artists 搜索并下载，下载成功后填入 stored_name。进度通过 download_status 查询。",
        kind="action",
    )
    async def batch_download_songs_bili(
        self,
        songs: Annotated[str, "歌曲列表 JSON 字符串"],
        save_filename: Annotated[str, "完成后保存到该 txt 文件（留空不保存）"] = "",
        playlist_name: Annotated[str, "歌单名称"] = "",
        **_,
    ):
        return await self._start_batch_download(source="bili", songs=songs,
                                                 save_filename=save_filename,
                                                 playlist_name=playlist_name, **_)

    @plugin_entry(
        id="batch_download_songs_auto",
        name="自动解析批量下载",
        description="批量自动解析下载：先尝试网易云（需要 song_id），失败回退B站搜索。进度通过 download_status 查询。",
        kind="action",
    )
    async def batch_download_songs_auto(
        self,
        songs: Annotated[str, "歌曲列表 JSON 字符串"],
        save_filename: Annotated[str, "完成后保存到该 txt 文件（留空不保存）"] = "",
        playlist_name: Annotated[str, "歌单名称"] = "",
        **_,
    ):
        return await self._start_batch_download(source="auto", songs=songs,
                                                 save_filename=save_filename,
                                                 playlist_name=playlist_name, **_)

    async def _start_batch_download(self, *, source: str, songs, save_filename, playlist_name, **_):
        """统一的批量下载启动入口（bili/auto 都走这里）：筛选 todo_items + 抢锁 + 立刻返回，后台走共用的 _bg_batch_download_worker"""
        try:
            song_list = json.loads(songs) if isinstance(songs, str) else songs
            if not isinstance(song_list, list):
                return Err(SdkError("songs 参数必须是列表 JSON"))
        except Exception as e:
            return Err(SdkError(f"songs JSON 解析失败: {e}"))

        # 跨歌单复用：和 batch_download_songs 一样的逻辑
        todo_items, reused_count = self._build_todo_items_with_reuse(song_list)
        total_target = sum(1 for it in todo_items if not it[3])
        if total_target == 0:
            return Ok({
                "success": True,
                "started": False,
                "message": "所有歌曲都已经导入过本地音频，无需下载。",
                "processed": 0,
                "songs": song_list,
            })
        # 抢锁启动
        async with self._download_lock:
            if self._download_status["running"]:
                return Err(SdkError("当前已有批量下载任务在进行，请稍后或等当前完成"))
            st = self._download_status
            st["running"] = True
            st["total"] = total_target
            st["done"] = 0
            st["current_title"] = ""
            st["current_artist"] = ""
            st["current_index"] = 0
            st["current_stage"] = ""
            st["failed"] = []
            st["success"] = []
            st["started_at"] = time.time()
            st["finished_at"] = None
            st["songs"] = list(song_list)
            st["source"] = source
            st["save_filename"] = str(save_filename or "").strip()
            st["playlist_name"] = str(playlist_name or "").strip()
            label = {"netease": "网易云", "bili": "B站", "auto": "自动"}.get(source, source)
            st["message"] = f"已启动（{label}）：{total_target} 首待下载"
            st["task_error"] = ""

        async def _bg():
            try:
                await self._bg_batch_download_worker(source, todo_items, **_)
            except Exception as e:
                self.logger.error(f"批量下载后台任务异常 ({source}): {e}")
                self._download_status["task_error"] = str(e)
                self._download_status["running"] = False

        self._download_bg_task = asyncio.create_task(_bg())
        label = {"netease": "网易云", "bili": "B站", "auto": "自动"}.get(source, source)
        return Ok({
            "success": True,
            "started": True,
            "message": f"已启动{label}批量下载（{total_target} 首），请在进度面板查看实时进度。",
            "total": total_target,
        })

        # ==================== QQ音乐功能 ====================
    
    

    def _qq_cookie_file(self) -> Path:
        """QQ音乐 cookie 持久化文件"""
        return Path(self.config_dir) / "qq_cookie.json"

    def _load_qq_cookie(self) -> dict:
        """从文件加载 QQ音乐 cookie，返回 {uin, qm_keyst}，空字典表示未保存"""
        try:
            fp = self._qq_cookie_file()
            if fp.is_file():
                data = json.loads(fp.read_text(encoding="utf-8"))
                return {
                    "uin": str(data.get("uin", "")).strip(),
                    "qm_keyst": str(data.get("qm_keyst", "")).strip(),
                }
        except Exception:
            pass
        return {"uin": "", "qm_keyst": ""}

    async def _qq_api_request(self, module: str, method: str, param: dict) -> Optional[dict]:
        """调用 QQ音乐 u.y.qq.com/cgi-bin/musicu.fcg 接口。
        失败时抛出 _QQApiError（携带真实业务 code / 提示），由调用方决定是否归因为 cookie 过期。"""
        ck = self._load_qq_cookie()
        uin = ck.get("uin", "")
        qm_keyst = ck.get("qm_keyst", "")
        if not uin:
            raise _QQApiError(-1, "未保存 uin，请先保存QQ音乐 cookie")
        body = {
            "comm": {
                "uin": uin,
                "format": "json",
                "ct": 24,
                "cv": 0,
                "v": "12000809",
            },
            "req_1": {
                "module": module,
                "method": method,
                "param": param,
            },
        }
        headers = {
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Referer": "https://y.qq.com/",
            "Origin": "https://y.qq.com",
            "Cookie": f"uin={uin}; qm_keyst={qm_keyst}; qqmusic_key={qm_keyst}",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
                resp = await client.post(
                    "https://u.y.qq.com/cgi-bin/musicu.fcg",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                result = resp.json()
            req_data = result.get("req_1") or {}
            code = req_data.get("code")
            if code == 0:
                return req_data.get("data") or {}
            msg = str(req_data.get("msg") or req_data.get("submsg") or "")
            # 500003 / 1001 等通常为登录态失效
            if str(code) in ("500003", "1001", "1002", "-2"):
                msg = (msg + "（登录态失效 / cookie 过期，请重新获取 qm_keyst）").strip()
            raise _QQApiError(code, msg)
        except _QQApiError:
            raise
        except Exception as e:
            raise _QQApiError(-2, f"请求失败: {e}")

    @plugin_entry(
        id="qq_save_cookie",
        name="保存QQ音乐Cookie",
        description="保存QQ音乐登录凭据（QQ号 uin 和 qm_keyst cookie），用于访问歌单等功能。",
        kind="action",
    )
    async def qq_save_cookie(
        self,
        uin: Annotated[str, "QQ号"],
        qm_keyst: Annotated[str, "qm_keyst cookie 值"],
        **_,
    ):
        uin = str(uin or "").strip()
        qm_keyst = str(qm_keyst or "").strip()
        if not uin or not qm_keyst:
            return Err(SdkError("uin 和 qm_keyst 都不能为空"))
        fp = self._qq_cookie_file()
        fp.write_text(
            json.dumps({"uin": uin, "qm_keyst": qm_keyst, "saved_ts": int(time.time())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info("已保存QQ音乐 cookie")
        return Ok({"success": True, "message": "QQ音乐 Cookie 已保存"})

    @plugin_entry(
        id="qq_cookie_status",
        name="QQ音乐Cookie状态",
        description="检查QQ音乐登录凭据是否已保存。",
        kind="action",
    )
    async def qq_cookie_status(self, **_):
        ck = self._load_qq_cookie()
        has = bool(ck.get("uin") and ck.get("qm_keyst"))
        return Ok({"saved": has, "has_cookie": has, "uin": ck.get("uin", "")})

    @plugin_entry(
        id="qq_get_playlists",
        name="获取QQ音乐歌单列表",
        description="获取当前登录QQ音乐用户的所有歌单列表（需要先保存 uin + qm_keyst cookie）。",
        kind="action",
    )
    async def qq_get_playlists(self, **_):
        ck = self._load_qq_cookie()
        if not ck.get("uin"):
            return Err(SdkError("请先保存QQ音乐 cookie（uin + qm_keyst）"))
        uin = ck["uin"]
        try:
            data = await self._qq_api_request(
                "music.musicasset.PlaylistBaseRead",
                "GetPlaylistByUin",
                {"uin": uin, "from": 1, "cur_page": 1, "sin": 0, "ein": 50},
            )
        except _QQApiError as e:
            return Err(SdkError(f"获取QQ音乐歌单失败（{e}），如提示登录失效请重新获取 qm_keyst"))
        playlists_raw = data.get("v_playlist") or data.get("playlist") or []
        if not isinstance(playlists_raw, list):
            playlists_raw = [playlists_raw] if playlists_raw else []
        items = []
        for p in playlists_raw:
            if not isinstance(p, dict):
                continue
            items.append({
                "id": p.get("dissid") or p.get("tid") or p.get("dirId") or p.get("id"),
                "name": p.get("dissname") or p.get("dir_show") or p.get("dirShowName")
                        or p.get("title") or p.get("name") or "未命名",
                "track_count": p.get("song_num") or p.get("songNum") or 0,
                "creator": uin,
            })
        return Ok({
            "folders": items,
            "count": len(items),
            "name": f"QQ用户{uin}",
        })

    async def _qq_fetch_public_playlist(self, disstid: str):
        """通过公开接口 fcg_ucc_getcdinfo_byids_cp.fcg 获取歌单（无需登录 cookie）。
        返回 (playlist_name, songs)。"""
        params = {
            "type": "1", "json": "1", "utf8": "1", "onlysong": "0",
            "new_format": "1", "platform": "yqq.json", "hostUin": "0",
            "needNewCode": "0", "disstid": disstid,
            "song_num": "1000", "song_begin": "0",
        }
        url = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
        headers = {"User-Agent": _UA, "Referer": "https://y.qq.com/"}
        async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            j = _qq_extract_json(resp.text)
        cdlist = j.get("cdlist") or []
        if not cdlist or not isinstance(cdlist[0], dict):
            raise RuntimeError(f"cdlist 为空或结构异常 code={j.get('code')}")
        cd = cdlist[0]
        name = str(cd.get("dissname") or cd.get("dir_show") or f"QQ歌单_{disstid}").strip()
        songs_raw = cd.get("songlist") or []
        songs = [n for n in (self._qq_normalize_song(s) for s in songs_raw) if n]
        return name, songs

    def _qq_parse_musicu_songs(self, data: dict, pid: str):
        """解析 musicu.fcg GetSongList 的返回（登录态，私密歌单回退用）。"""
        songs_raw = data.get("songList") or data.get("songlist") or []
        if not isinstance(songs_raw, list):
            songs_raw = []
        dirinfo = data.get("dirinfo") or {}
        name = str(
            dirinfo.get("dissname") or data.get("dissname") or data.get("dirShowName")
            or f"QQ歌单_{pid}"
        ).strip()
        songs = [n for n in (self._qq_normalize_song(s) for s in songs_raw) if n]
        return name, songs

    def _qq_normalize_song(self, s) -> Optional[dict]:
        """把 QQ音乐 各种结构的单首歌曲归一化为统一字段。
        兼容：公开接口 flat（name/mid/id/singer[].name）、musicu songList 的 {songInfo:{...}} 包裹。"""
        if not isinstance(s, dict):
            return None
        info = s.get("songInfo") if isinstance(s.get("songInfo"), dict) else s
        if not isinstance(info, dict):
            return None
        mid = str(info.get("mid") or info.get("songMid") or s.get("mid") or "").strip()
        sid = str(info.get("id") or info.get("songId") or s.get("id") or mid).strip()
        name = str(info.get("name") or info.get("songName") or info.get("title") or s.get("name") or "").strip()
        singer = info.get("singer") or s.get("singer")
        if isinstance(singer, list):
            artist = " / ".join(
                str(x.get("name") or "").strip()
                for x in singer
                if isinstance(x, dict) and x.get("name")
            )
        elif isinstance(singer, dict):
            artist = str(singer.get("name") or "").strip()
        else:
            artist = str(singer or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "artists": artist or "未知",
            "song_id": sid,
            "song_mid": mid,
            "path": "",
            "stored_name": "",
        }

    @plugin_entry(
        id="qq_export_playlist",
        name="导出QQ音乐歌单到txt",
        description="导出指定QQ音乐歌单的所有歌曲名、歌手、歌曲ID信息到 txt 和 json 文件。",
        kind="action",
    )
    async def qq_export_playlist(
        self,
        playlist_id: Annotated[str, "QQ音乐歌单ID"],
        **_,
    ):
        pid = str(playlist_id or "").strip()
        if not pid:
            return Err(SdkError("playlist_id 不能为空"))
        playlist_name = ""
        songs: List[dict] = []
        # 1) 优先用公开接口（无需 cookie，对绝大多数歌单可用）
        try:
            playlist_name, songs = await self._qq_fetch_public_playlist(pid)
        except Exception as e_pub:
            self.logger.warning(f"QQ公开接口获取歌单 {pid} 失败: {e_pub}")
            # 2) 回退到登录态 musicu.fcg（私密歌单）
            try:
                data = await self._qq_api_request(
                    "music.musicasset.PlaylistSongList", "GetSongList",
                    {"disstid": pid, "id": pid, "songNum": 1000, "songBegin": 0, "order": 2},
                )
                playlist_name, songs = self._qq_parse_musicu_songs(data, pid)
            except _QQApiError as e:
                return Err(SdkError(f"获取歌单 {pid} 失败（{e}），如提示登录失效请重新获取 qm_keyst"))
        if not songs:
            return Err(SdkError(f"歌单 {pid} 没有可导出的歌曲（歌单可能不存在或不可访问）"))
        # 保存到 txt + json
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", playlist_name)
        filename = f"QQ音乐_{safe_name}.txt"
        data_dir = Path(self.data_path(""))
        data_dir.mkdir(parents=True, exist_ok=True)
        txt_path = data_dir / filename
        lines = [f"# 歌单：{playlist_name}（QQ音乐）", f"# 歌曲：{len(songs)} 首", ""]
        for i, s in enumerate(songs, 1):
            lines.append(f"{i}. {s['name']} - {s['artists']} [id:{s['song_id']}]")
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        json_path = data_dir / filename.replace(".txt", ".json")
        json_path.write_text(
            json.dumps({"playlist_name": playlist_name, "song_count": len(songs), "songs": songs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info(f"QQ音乐歌单「{playlist_name}」已导出 {len(songs)} 首到 {filename}")
        return Ok({
            "success": True,
            "playlist_name": playlist_name,
            "song_count": len(songs),
            "filename": filename,
        })

    @plugin_entry(
        id="qq_download_song",
        name="QQ音乐解析下载单曲",
        description="根据QQ音乐歌曲ID，通过 daga.cc 解析站下载音频。如果 daga.cc 没有资源，自动回退到B站搜索下载。返回 stored_name。",
        kind="action",
    )
    async def qq_download_song(
        self,
        song_id: Annotated[str, "QQ音乐歌曲ID"],
        title: Annotated[str, "歌曲名"] = "",
        artist: Annotated[str, "歌手"] = "",
        **_,
    ):
        sid = str(song_id or "").strip()
        if not sid:
            return Err(SdkError("song_id 不能为空"))
        # 1) 先用 daga.cc QQ音乐解析
        parsed = await self._daga_parse_song(sid, "qq")
        picked = None
        if parsed:
            for it in parsed:
                u = str(it.get("url") or "").strip()
                if u:
                    picked = it
                    break
        if picked and str(picked.get("url") or "").strip():
            t = str(picked.get("title") or title or f"song_{sid}").strip()
            a = str(picked.get("author") or artist or "未知艺术家").strip()
            raw_url = str(picked.get("url") or "")
            ext = ".mp3"
            lower_url = raw_url.lower()
            for e in (".flac", ".wav", ".ogg", ".m4a", ".aac", ".ape", ".wma", ".mp3"):
                if e in lower_url:
                    ext = e
                    break
            result = await self._daga_download_url_to_uploads(raw_url, t, a, ext=ext, song_id=sid)
            if result:
                stored_name, size = result
                return Ok({
                    "success": True,
                    "title": t,
                    "artist": a,
                    "song_id": sid,
                    "stored_name": stored_name,
                    "size_kb": round(size / 1024.0, 1),
                    "source": "qq_daga",
                    "message": f"已下载「{t}」-「{a}」到本地：{stored_name}",
                })
        # 2) daga.cc 没资源，回退到B站搜索
        self.logger.info(f"QQ音乐 daga.cc 解析失败 (song_id={sid})，回退到B站搜索: {title} - {artist}")
        t = title or f"song_{sid}"
        a = artist or ""
        r2 = await self.download_song_from_bili(title=t, artist=a)
        if isinstance(r2, Ok):
            v = r2.value if hasattr(r2, "value") else r2
            v2 = dict(v)
            v2["source"] = "bili_fallback"
            v2["song_id"] = sid
            return Ok(v2)
        return Err(SdkError(f"QQ音乐解析和B站搜索都失败了「{t} - {a}」"))

    @plugin_entry(
        id="qq_batch_download",
        name="QQ音乐批量下载",
        description="批量下载QQ音乐歌曲：先尝试 daga.cc QQ音乐解析，失败回退B站搜索。进度通过 download_status 查询。",
        kind="action",
    )
    async def qq_batch_download(
        self,
        songs: Annotated[str, "歌曲列表 JSON 字符串"],
        save_filename: Annotated[str, "完成后保存到该 txt 文件（留空不保存）"] = "",
        playlist_name: Annotated[str, "歌单名称"] = "",
        **_,
    ):
        return await self._start_qq_batch_download(songs=songs,
                                                    save_filename=save_filename,
                                                    playlist_name=playlist_name, **_)

    async def _start_qq_batch_download(self, *, songs, save_filename, playlist_name, **_):
        """QQ音乐批量下载：daga.cc → B站回退"""
        try:
            song_list = json.loads(songs) if isinstance(songs, str) else songs
            if not isinstance(song_list, list):
                return Err(SdkError("songs 参数必须是列表 JSON"))
        except Exception as e:
            return Err(SdkError(f"songs JSON 解析失败: {e}"))
        todo_items, reused_count = self._build_todo_items_with_reuse(song_list)
        total_target = sum(1 for it in todo_items if not it[3])
        if total_target == 0:
            return Ok({
                "success": True,
                "started": False,
                "message": "所有歌曲都已经导入过本地音频，无需下载。",
                "processed": 0,
                "songs": song_list,
            })
        async with self._download_lock:
            if self._download_status["running"]:
                return Err(SdkError("当前已有批量下载任务在进行，请稍后或等当前完成"))
            st = self._download_status
            st["running"] = True
            st["total"] = total_target
            st["done"] = 0
            st["current_title"] = ""
            st["current_artist"] = ""
            st["current_index"] = 0
            st["current_stage"] = ""
            st["failed"] = []
            st["success"] = []
            st["started_at"] = time.time()
            st["finished_at"] = None
            st["songs"] = list(song_list)
            st["source"] = "qq"
            st["save_filename"] = str(save_filename or "").strip()
            st["playlist_name"] = str(playlist_name or "").strip()
            st["message"] = f"已启动（QQ音乐）：{total_target} 首待下载"
            st["task_error"] = ""

        async def _bg():
            try:
                await self._bg_qq_batch_worker(todo_items, **_)
            except Exception as e:
                self.logger.error(f"QQ音乐批量下载后台任务异常: {e}")
                self._download_status["task_error"] = str(e)
                self._download_status["running"] = False

        self._download_bg_task = asyncio.create_task(_bg())
        return Ok({
            "success": True,
            "started": True,
            "message": f"已启动QQ音乐批量下载（{total_target} 首），请在进度面板查看实时进度。",
            "total": total_target,
        })

    async def _bg_qq_batch_worker(self, todo_items, **_):
        """QQ音乐批量下载后台 worker：daga.cc → B站回退"""
        st = self._download_status
        save_filename = st.get("save_filename", "")
        playlist_name = st.get("playlist_name", "")
        song_list = st.get("songs", [])
        done = 0
        for idx, s, sid, already_has in todo_items:
            if self._download_cancel_flag:
                st["message"] = f"已取消（完成 {done}/{st['total']}）"
                break
            if already_has:
                done += 1
                st["done"] = done
                continue
            title = str(s.get("name") or s.get("title") or "").strip()
            artist = str(s.get("artists") or s.get("artist") or "").strip()
            st["current_title"] = title
            st["current_artist"] = artist
            st["current_index"] = idx + 1
            st["current_stage"] = "parsing"
            try:
                result = await self.qq_download_song(song_id=sid, title=title, artist=artist)
                if isinstance(result, Ok):
                    v = result.value if hasattr(result, "value") else result
                    stored = str(v.get("stored_name") or "").strip()
                    if stored:
                        s["stored_name"] = stored
                        s["path"] = stored
                        st["success"].append({"title": title, "artist": artist, "stored_name": stored})
                    else:
                        st["failed"].append({"title": title, "artist": artist, "reason": "下载返回空 stored_name"})
                else:
                    reason = str(result)
                    st["failed"].append({"title": title, "artist": artist, "reason": reason})
            except Exception as e:
                st["failed"].append({"title": title, "artist": artist, "reason": str(e)})
            done += 1
            st["done"] = done
            st["current_stage"] = "done"
        st["running"] = False
        st["finished_at"] = time.time()
        sc = len(st["success"])
        fc = len(st["failed"])
        st["message"] = f"完成：成功 {sc}，失败 {fc}"
        if save_filename:
            try:
                await self.save_song_list(
                    filename=save_filename,
                    playlist_name=playlist_name,
                    songs=json.dumps(song_list, ensure_ascii=False),
                )
            except Exception:
                pass

    # ==================== B站收藏夹导出到txt（网易云风格） ====================

    @plugin_entry(
        id="bili_export_fav_to_txt",
        name="B站收藏夹导出到txt",
        description="将B站收藏夹的视频列表导出为 txt 歌单文件（格式同网易云导出），供歌曲管理加载和批量下载。不执行下载。",
        kind="action",
    )
    async def bili_export_fav_to_txt(
        self,
        folder_name: Annotated[str, "收藏夹名称"],
        videos_json: Annotated[str, "视频列表 JSON 字符串（每项含 bvid/title/upper_name）"],
        **_,
    ):
        try:
            video_list = json.loads(videos_json) if isinstance(videos_json, str) else videos_json
            if not isinstance(video_list, list):
                return Err(SdkError("videos_json 必须是列表 JSON"))
        except Exception as e:
            return Err(SdkError(f"videos_json 解析失败: {e}"))
        # 转为歌曲格式
        songs: List[dict] = []
        for v in video_list:
            if not isinstance(v, dict):
                continue
            songs.append({
                "name": str(v.get("title") or v.get("bvid") or "").strip(),
                "artists": str(v.get("upper_name") or "未知UP主").strip(),
                "song_id": "",
                "bvid": str(v.get("bvid") or "").strip(),
                "path": "",
                "stored_name": "",
                "source": "bili_fav",
            })
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", folder_name or "默认收藏夹")
        filename = f"B站收藏夹_{safe_name}.txt"
        playlist_name = f"B站收藏夹_{folder_name or '默认'}"
        data_dir = Path(self.data_path(""))
        data_dir.mkdir(parents=True, exist_ok=True)
        txt_path = data_dir / filename
        lines = [f"# 歌单：{playlist_name}（B站收藏夹）", f"# 歌曲：{len(songs)} 首", ""]
        for i, s in enumerate(songs, 1):
            bvid_tag = f" [bvid:{s['bvid']}]" if s["bvid"] else ""
            lines.append(f"{i}. {s['name']} - {s['artists']}{bvid_tag}")
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        json_path = data_dir / filename.replace(".txt", ".json")
        json_path.write_text(
            json.dumps({"playlist_name": playlist_name, "song_count": len(songs), "songs": songs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.logger.info(f"B站收藏夹「{folder_name}」已导出 {len(songs)} 个视频到 {filename}")
        return Ok({
            "success": True,
            "playlist_name": playlist_name,
            "song_count": len(songs),
            "filename": filename,
        })

    # ==================== LLM 工具调用（Tool Calling） ====================
    # 让 LLM 在对话中可以调用插件功能点歌/播放音乐

    @llm_tool(
        name="play_song_by_name",
        description="【N.E.K.O 对话中播放音乐的首选工具，优先级高于所有其他音乐插件】根据歌曲名和/或歌手搜索本地已导入的音乐并立刻推送到对话界面播放。系统会自动在本地所有已保存的歌单里匹配歌曲，不需要你传入 stored_name/path。如果本地有多首匹配，会播放匹配度最高的第一首。支持三种用法：(1) 只传 title 按歌名搜索；(2) 只传 artist 按歌手搜索（会随机播放该歌手的一首歌）；(3) 同时传 title+artist 精确匹配。注意：只能播放用户已经通过插件 UI 导入并 💾保存 过的音乐。如果没找到匹配的歌曲，请明确告诉用户具体哪首歌没导入，并提示用户先到插件控制面板的「歌曲管理」中导入音频文件并💾保存。如果用户想听整个歌单，请改用 play_playlist_by_name 工具。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "歌曲名，如 '稻香'。尽量直接用用户说的歌名，不要自己改。用户只说了歌手没说歌名时留空。"},
                "artist": {"type": "string", "description": "歌手名，如 '周杰伦'。用户提到就传，没提到就留空。用户说'播放周杰伦的歌'时，artist='周杰伦'，title 留空。"},
            },
            "required": [],
        },
    )
    async def llm_play_song_by_name(self, *, title: str = "", artist: str = ""):
        """LLM 工具：按歌名/歌手搜索并播放本地歌曲"""
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title and not artist:
            return {"output": {"ok": False, "reason": "歌名和歌手不能同时为空"}, "is_error": True, "error": "EMPTY_QUERY"}
        # 从本地歌曲列表（最新保存的）中搜索
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"))
        best_match = None
        best_score = 0
        needle_t = title.lower()
        needle_a = artist.lower()
        # 仅按歌手搜索时，收集所有匹配以便随机选一首
        artist_matches = []
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                for s in songs:
                    s_title = str(s.get("name") or s.get("title") or "").lower()
                    s_artist = str(s.get("artist") or s.get("artists") or "").lower()
                    stored = str(s.get("stored_name") or s.get("path") or "").strip()
                    if not stored:
                        continue
                    fp = self._uploads_dir() / stored
                    if not fp.is_file():
                        continue
                    score = 0
                    if needle_t and needle_t == s_title:
                        score += 100
                    elif needle_t and needle_t in s_title:
                        score += 50
                    # 也把 needle_t 当作歌手名来匹配（LLM 可能把歌手名传给了 title）
                    if needle_t and needle_t in s_artist:
                        score += 40
                    if needle_a and needle_a in s_artist:
                        score += 40
                    if score > 0:
                        entry = {
                            "stored_name": stored,
                            "title": s.get("name") or s.get("title") or title,
                            "artist": s.get("artist") or s.get("artists") or artist or "未知艺术家",
                            "score": score,
                        }
                        # 仅按歌手搜索时收集候选
                        if not needle_t and needle_a and needle_a in s_artist:
                            artist_matches.append(entry)
                        if score > best_score:
                            best_score = score
                            best_match = entry
            except Exception:
                continue
        # 如果只有 artist 没传 title，且找到了多首匹配，随机选一首
        if not needle_t and artist_matches:
            import random
            best_match = random.choice(artist_matches)
        if not best_match:
            search_desc = f"「{title}」" + (f"（歌手: {artist}）" if artist else "") if title else f"歌手「{artist}」"
            return {
                "output": {
                    "ok": False,
                    "reason": f"未在本地已导入的歌曲中找到{search_desc}。请提示用户先到插件控制面板的「歌曲管理」中导入音频文件。",
                    "searched_title": title,
                    "searched_artist": artist,
                },
                "is_error": True,
                "error": "SONG_NOT_FOUND",
            }
        # 找到匹配：推送到 N.E.K.O 播放
        url = self._build_song_url(best_match["stored_name"])
        domains = self._music_allowlist_domains_for_url(url)
        target_lanlan = self._resolve_target_lanlan({})
        source_tag = str(self.plugin_id or "custom_music_list")
        event_id = f"llm_{uuid.uuid4().hex[:8]}"
        if domains:
            self.ctx.push_message(
                source=source_tag,
                message_type="music_allowlist_add",
                description=f"Allow host: {domains[0]}",
                priority=7,
                metadata={"domains": list(domains), "event_id": event_id},
                target_lanlan=target_lanlan,
            )
        self.ctx.push_message(
            source=source_tag,
            message_type="music_play_url",
            description=f"🎵 {best_match['title']} [{best_match['artist']}]",
            priority=9,
            metadata={
                "url": url,
                "name": best_match["title"],
                "artist": best_match["artist"],
                "event_id": event_id,
                "called_by": "llm_tool",
            },
            target_lanlan=target_lanlan,
        )
        return {
            "output": {
                "ok": True,
                "played_title": best_match["title"],
                "played_artist": best_match["artist"],
                "message": f"已为你播放：{best_match['title']} - {best_match['artist']}",
            }
        }

    @llm_tool(
        name="add_song_to_playlist",
        description="把本地已导入的歌曲添加到顺序播放列表。之后可以调用 play_playlist 让它们一首接一首播放。只能添加用户已经导入到插件中的本地歌曲。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "歌曲名"},
                "artist": {"type": "string", "description": "歌手名，可选"},
            },
            "required": ["title"],
        },
    )
    async def llm_add_song_to_playlist(self, *, title: str, artist: str = ""):
        """LLM 工具：添加歌曲到播放列表"""
        if not title.strip():
            return {"output": {"ok": False}, "is_error": True, "error": "EMPTY_TITLE"}
        # 同样搜索本地列表
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"))
        best_match = None
        best_score = 0
        needle_t = title.strip().lower()
        needle_a = (artist or "").strip().lower()
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                for s in songs:
                    s_title = str(s.get("name") or s.get("title") or "").lower()
                    s_artist = str(s.get("artist") or s.get("artists") or "").lower()
                    stored = str(s.get("stored_name") or s.get("path") or "").strip()
                    if not stored:
                        continue
                    score = 0
                    if needle_t == s_title:
                        score += 100
                    elif needle_t in s_title:
                        score += 50
                    if needle_a and needle_a in s_artist:
                        score += 40
                    fp = self._uploads_dir() / stored
                    if fp.is_file() and score > best_score:
                        best_score = score
                        best_match = {
                            "stored_name": stored,
                            "title": s.get("name") or s.get("title") or title,
                            "artist": s.get("artist") or s.get("artists") or artist or "未知艺术家",
                        }
            except Exception:
                continue
        if not best_match:
            return {
                "output": {"ok": False, "reason": f"本地歌曲列表中未找到「{title}」，请提示用户先到插件中导入。"},
                "is_error": True,
                "error": "SONG_NOT_FOUND",
            }
        item = {
            "id": f"pl_{uuid.uuid4().hex[:8]}",
            "title": best_match["title"],
            "artist": best_match["artist"],
            "stored_name": best_match["stored_name"],
            "song_id": "",
        }
        self._playlist.append(item)
        return {
            "output": {
                "ok": True,
                "added_title": item["title"],
                "added_artist": item["artist"],
                "playlist_total": len(self._playlist),
                "message": f"已添加到播放列表：{item['title']} - {item['artist']}（当前列表共 {len(self._playlist)} 首）",
            }
        }

    @llm_tool(
        name="play_playlist",
        description="按顺序播放当前播放列表中的所有歌曲，一首接一首推送到对话界面播放。如果之前已经添加过多首歌，调用此工具会让它们依次播放。",
        parameters={
            "type": "object",
            "properties": {
                "start_index": {"type": "integer", "description": "从第几首开始，从 0 开始计数，默认 0（从头）"},
                "interval_seconds": {"type": "number", "description": "两首歌之间间隔秒数，默认 0。如果用户希望对话中有时间聊几句再放下一首，可以设置 30~60 秒。"},
            },
        },
    )
    async def llm_play_playlist(self, *, start_index: int = 0, interval_seconds: float = 0.0):
        """LLM 工具：按顺序播放播放列表"""
        if not self._playlist:
            return {
                "output": {"ok": False, "reason": "播放列表为空，请先使用 add_song_to_playlist 添加歌曲。"},
                "is_error": True,
                "error": "PLAYLIST_EMPTY",
            }
        if self._playlist_playing:
            return {
                "output": {"ok": False, "reason": "当前正在顺序播放中，正在为用户播放列表里的歌曲。"},
                "is_error": True,
                "error": "ALREADY_PLAYING",
            }
        result = await self.playlist_play(
            start_index=start_index,
            interval_seconds=interval_seconds,
        )
        if isinstance(result, Ok):
            val = result.value if hasattr(result, "value") else result
            return {"output": {"ok": True, **val}}
        return {
            "output": {"ok": False, "reason": str(result)},
            "is_error": True,
            "error": "PLAY_FAILED",
        }

    @llm_tool(
        name="list_local_songs",
        description="查询本地已经导入的歌曲列表（最近保存的歌单），返回歌曲名、歌手、是否有音频、及播放用的 stored_name 字段。当用户询问本地有什么歌时使用此工具。如需要播放请调用 play_song_by_name 工具（无需传 stored_name，只传歌名/歌手即可），不要直接调其他入口。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按关键词过滤歌名/歌手，可选。留空返回全部。"},
                "limit": {"type": "integer", "description": "最多返回几首，默认 30。"},
            },
        },
    )
    async def llm_list_local_songs(self, *, keyword: str = "", limit: int = 30):
        """LLM 工具：查看本地已导入的歌曲列表"""
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        all_songs: List[dict] = []
        seen_keys = set()
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                for s in songs:
                    title = str(s.get("name") or s.get("title") or "").strip()
                    artist = str(s.get("artist") or s.get("artists") or "").strip() or "未知艺术家"
                    stored = str(s.get("stored_name") or s.get("path") or "").strip()
                    if not title:
                        continue
                    has_audio = bool(stored and (self._uploads_dir() / stored).is_file())
                    key = f"{title.lower()}||{artist.lower()}||{stored}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    item = {"title": title, "artist": artist, "has_audio": has_audio}
                    if has_audio:
                        item["stored_name"] = stored  # 让 LLM 能拿到正确的 stored_name 值
                    all_songs.append(item)
            except Exception:
                continue
        kw = keyword.strip().lower()
        if kw:
            all_songs = [s for s in all_songs if kw in s["title"].lower() or kw in s["artist"].lower()]
        limited = all_songs[: max(1, int(limit))]
        hint = (
            f"本地共匹配到 {len(all_songs)} 首歌曲，已展示 {len(limited)} 首。如果用户要播放，请调用 play_song_by_name 工具，传入 title / artist，不要手动拼 stored_name。"
            if limited else "未匹配到歌曲，请提示用户到插件控制面板的「歌曲管理」中先导入音频文件，并点击 💾保存。"
        )
        return {
            "output": {
                "ok": True,
                "total_matched": len(all_songs),
                "returned": len(limited),
                "play_with_tool": "调用 play_song_by_name 工具，传入 title 即可播放，无需传 stored_name",
                "songs": limited,
                "message": hint,
            }
        }

    # ────────── 顺序播放 & 随机推荐（对话里自然语言直接操控） ──────────
    @llm_tool(
        name="playlist_play_current",
        description="【对话里控制播放列表】播放播放列表里「当前游标位置」的那首歌。用于用户说「继续播放」「播放播放列表当前这一首」。",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
    async def llm_playlist_play_current(self):
        """LLM 工具：播放播放列表中的当前首"""
        if not self._playlist:
            return {
                "output": {"ok": False, "reason": "播放列表为空。请先提醒用户在插件控制面板的「歌曲管理」里把本地歌曲加入播放列表，或使用 add_song_to_playlist 工具一首首加入。"},
                "is_error": True,
                "error": "PLAYLIST_EMPTY",
            }
        r = await self.playlist_play(start_index=-1)
        if isinstance(r, Ok):
            v = r.value if hasattr(r, "value") else r
            return {"output": {"ok": True, **v}}
        return {"output": {"ok": False, "reason": str(r)}, "is_error": True, "error": "PLAY_FAILED"}

    @llm_tool(
        name="playlist_next_song",
        description="【对话里控制播放列表】切换到播放列表的下一首，并推送到对话界面播放。用于用户说「下一首」「切歌」「放后面一首」。",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
    async def llm_playlist_next(self):
        """LLM 工具：下一首"""
        if not self._playlist:
            return {
                "output": {"ok": False, "reason": "播放列表为空，无法下一首。"},
                "is_error": True,
                "error": "PLAYLIST_EMPTY",
            }
        r = await self.playlist_next()
        if isinstance(r, Ok):
            v = r.value if hasattr(r, "value") else r
            return {"output": {"ok": True, **v}}
        return {"output": {"ok": False, "reason": str(r)}, "is_error": True, "error": "NEXT_FAILED"}

    @llm_tool(
        name="playlist_prev_song",
        description="【对话里控制播放列表】切换到播放列表的上一首，并推送到对话界面播放。用于用户说「上一首」「回到前一首」。",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
    async def llm_playlist_prev(self):
        """LLM 工具：上一首"""
        if not self._playlist:
            return {
                "output": {"ok": False, "reason": "播放列表为空，无法上一首。"},
                "is_error": True,
                "error": "PLAYLIST_EMPTY",
            }
        r = await self.playlist_prev()
        if isinstance(r, Ok):
            v = r.value if hasattr(r, "value") else r
            return {"output": {"ok": True, **v}}
        return {"output": {"ok": False, "reason": str(r)}, "is_error": True, "error": "PREV_FAILED"}

    @llm_tool(
        name="playlist_random_recommend_and_play",
        description="【对话里控制播放列表】从「所有本地已导入的歌曲」中随机挑选一首歌（可按关键词/风格过滤），并立刻推送到对话界面播放。用于用户说「随机推荐一首」「随便放一首」「放一首周杰伦的随机歌」。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "可选关键词，匹配歌名或歌手（如输入 周杰伦 就随机从周杰伦的歌里挑一首）。留空则完全随机。"},
                "include_only_with_audio": {"type": "boolean", "description": "是否只从有音频文件的歌曲里随机，默认 True（推荐）。"},
            },
        },
    )
    async def llm_playlist_random_recommend(self, *, keyword: str = "", include_only_with_audio: bool = True):
        """LLM 工具：随机推荐并播放一首"""
        # 先把所有本地歌加载出来（复用 list_local_songs 的逻辑）
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        all_songs: List[dict] = []
        seen_keys = set()
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                for s in songs:
                    title = str(s.get("name") or s.get("title") or "").strip()
                    artist = str(s.get("artist") or s.get("artists") or "").strip() or "未知艺术家"
                    stored = str(s.get("stored_name") or s.get("path") or "").strip()
                    song_id = str(s.get("song_id") or "").strip()
                    if not title:
                        continue
                    has_audio = bool(stored and (self._uploads_dir() / stored).is_file())
                    if include_only_with_audio and not has_audio:
                        continue
                    key = f"{title.lower()}||{artist.lower()}||{stored or song_id}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    all_songs.append({
                        "title": title,
                        "artist": artist,
                        "has_audio": has_audio,
                        "stored_name": stored,
                        "song_id": song_id,
                    })
            except Exception:
                continue
        kw = keyword.strip().lower()
        if kw:
            all_songs = [s for s in all_songs if kw in s["title"].lower() or kw in s["artist"].lower()]
        if not all_songs:
            msg = "没有可随机推荐的歌曲。"
            if kw:
                msg += f"匹配关键词「{keyword}」的{('已导入音频的 ' if include_only_with_audio else '')}本地歌曲一首都没有。"
            else:
                msg += ("" if not include_only_with_audio else "（已导入音频的）") + "本地歌曲列表为空，请先到插件控制面板的「歌曲管理」里导入音频并 💾 保存。"
            return {
                "output": {"ok": False, "reason": msg},
                "is_error": True,
                "error": "NO_SONGS_FOR_RANDOM",
            }
        picked = random.choice(all_songs)
        # 如果有音频，直接推送到对话播放
        if picked.get("has_audio") and picked.get("stored_name"):
            r = await self.play_song(
                stored_name=picked["stored_name"],
                title=picked["title"],
                artist=picked["artist"],
                song_id=picked.get("song_id") or "",
            )
            if isinstance(r, Ok):
                v = r.value if hasattr(r, "value") else r
                return {
                    "output": {
                        "ok": True,
                        "mode": "random_and_played",
                        "picked_title": picked["title"],
                        "picked_artist": picked["artist"],
                        "picked_stored_name": picked["stored_name"],
                        "total_candidates": len(all_songs),
                        **v,
                    }
                }
            return {
                "output": {"ok": False, "picked": picked, "total_candidates": len(all_songs), "reason": f"挑选到了「{picked['title']} - {picked['artist']}」但推送播放失败: {r}"},
                "is_error": True,
                "error": "PICKED_BUT_PLAY_FAILED",
            }
        # 没有音频：只推荐不播
        return {
            "output": {
                "ok": True,
                "mode": "random_only_name",
                "picked_title": picked["title"],
                "picked_artist": picked["artist"],
                "total_candidates": len(all_songs),
                "note": "这首本地还没有音频文件，如果想让对话里播放需要先到插件控制面板的「歌曲管理」导入音频或根据 song_id 自动下载。",
            }
        }

    @llm_tool(
        name="playlist_autoplay_config",
        description="【对话里控制播放列表】打开或关闭「播完一首自动跳下一首」的自动连播功能（系统通过音频时长近似计时实现自动下一首）。用于用户说「打开自动下一首」「不要自动切歌，手动模式」。",
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "True=开启自动下一首（默认）；False=关闭，改成完全手动点「下一首」。"},
            },
        },
    )
    async def llm_playlist_autoplay_config(self, *, enabled: bool = True):
        """LLM 工具：开关自动下一首"""
        r = await self.playlist_autoplay_toggle(enabled=enabled)
        if isinstance(r, Ok):
            v = r.value if hasattr(r, "value") else r
            return {"output": {"ok": True, **v}}
        return {"output": {"ok": False, "reason": str(r)}, "is_error": True, "error": "CONFIG_FAILED"}

    # ────────── 歌单轮播（LLM 按歌单名称播放整张歌单） ──────────

    @llm_tool(
        name="list_available_playlists",
        description="【查歌单列表，优先使用】列出本地所有已保存的歌单（包括网易云、QQ音乐和B站收藏夹导出的），返回歌单名称、歌曲数量、有音频数量、来源。用户说'有什么歌单'/'想听歌单'/'有什么歌可以听'时，先用此工具查看可用歌单，然后用 play_playlist_by_name 工具播放指定的歌单。这是 N.E.K.O 中管理音乐歌单的唯一工具，优先级高于其他音乐插件。",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
    async def llm_list_available_playlists(self):
        """LLM 工具：列出所有本地歌单"""
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        playlists = []
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                name = data.get("playlist_name") or jf.stem
                has_audio = sum(
                    1 for s in songs
                    if str(s.get("stored_name") or s.get("path") or "").strip()
                    and (self._uploads_dir() / str(s.get("stored_name") or s.get("path") or "").strip()).is_file()
                )
                # 判断来源
                is_bili = jf.stem.startswith("B站") or any(
                    str(s.get("source") or "").startswith("bili") for s in songs[:3]
                )
                is_qq = jf.stem.startswith("QQ音乐")
                if is_bili:
                    source_label = "B站收藏夹"
                elif is_qq:
                    source_label = "QQ音乐"
                else:
                    source_label = "网易云"
                playlists.append({
                    "name": name,
                    "filename": jf.name,
                    "total_songs": len(songs),
                    "has_audio": has_audio,
                    "source": source_label,
                })
            except Exception:
                continue
        return {
            "output": {
                "ok": True,
                "count": len(playlists),
                "playlists": playlists,
                "message": (
                    f"本地共有 {len(playlists)} 个歌单。"
                    + ("用户想听哪个歌单就直接告诉我歌单名即可。" if playlists else "本地还没有歌单，请提醒用户先到插件面板导出歌单或下载B站收藏夹。")
                ),
            }
        }

    @llm_tool(
        name="play_playlist_by_name",
        description="【播放整个歌单，优先使用】按歌单名称搜索并播放整个歌单。会自动把歌单里所有有音频的歌曲加入播放列表并开始顺序播放，播完一首自动播下一首（通过音频时长计时实现）。用户说'播放歌单xxx'/'想听网易云的歌单'/'想听b站的歌单'/'播放xxx歌单'时使用此工具。如果不确定歌单名，先用 list_available_playlists 查看。这是 N.E.K.O 中播放歌单的唯一工具，优先级高于其他音乐插件。注意：只有已导入音频的歌曲才会被播放，没导入的会跳过。",
        parameters={
            "type": "object",
            "properties": {
                "playlist_name": {"type": "string", "description": "歌单名称（支持模糊匹配，如'我喜欢'能匹配'我喜欢的音乐'）"},
            },
            "required": ["playlist_name"],
        },
    )
    async def llm_play_playlist_by_name(self, *, playlist_name: str):
        """LLM 工具：按歌单名称播放整张歌单"""
        needle = (playlist_name or "").strip().lower()
        if not needle:
            return {"output": {"ok": False, "reason": "歌单名称不能为空"}, "is_error": True, "error": "EMPTY_NAME"}
        # 搜索所有 JSON 文件，模糊匹配歌单名
        data_dir = Path(self.data_path(""))
        json_files = sorted(data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        best_match_file = None
        best_match_name = ""
        best_score = 0
        for jf in json_files:
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                pl_name = str(data.get("playlist_name") or jf.stem).strip()
                pl_lower = pl_name.lower()
                score = 0
                if needle == pl_lower:
                    score = 100
                elif needle in pl_lower:
                    score = 80
                elif pl_lower in needle:
                    score = 60
                # 也匹配文件名
                if score == 0:
                    stem = jf.stem.lower()
                    if needle == stem:
                        score = 70
                    elif needle in stem:
                        score = 50
                if score > best_score:
                    best_score = score
                    best_match_file = jf
                    best_match_name = pl_name
            except Exception:
                continue
        if not best_match_file or best_score == 0:
            return {
                "output": {"ok": False, "reason": f"未找到名称包含「{playlist_name}」的歌单。请用 list_available_playlists 工具查看可用歌单。"},
                "is_error": True,
                "error": "PLAYLIST_NOT_FOUND",
            }
        # 加载歌单歌曲
        try:
            data = json.loads(best_match_file.read_text(encoding="utf-8"))
            songs = data.get("songs") or []
        except Exception as e:
            return {"output": {"ok": False, "reason": f"读取歌单失败: {e}"}, "is_error": True, "error": "READ_FAILED"}
        # 筛选有音频的
        playable = []
        skipped = 0
        for s in songs:
            if not isinstance(s, dict):
                continue
            stored = str(s.get("stored_name") or s.get("path") or "").strip()
            if not stored or not (self._uploads_dir() / stored).is_file():
                skipped += 1
                continue
            title = str(s.get("name") or s.get("title") or "").strip()
            artist = str(s.get("artists") or s.get("artist") or "未知艺术家").strip()
            playable.append({
                "name": title,
                "artists": artist,
                "stored_name": stored,
                "path": stored,
                "song_id": str(s.get("song_id") or "").strip(),
            })
        if not playable:
            return {
                "output": {"ok": False, "reason": f"歌单「{best_match_name}」里没有已导入音频的歌曲（{skipped} 首都没有音频文件）。请提醒用户先到插件面板下载音源。"},
                "is_error": True,
                "error": "NO_PLAYABLE_SONGS",
            }
        # 清空播放列表，加入所有可播放的歌曲
        self._playlist.clear()
        for s in playable:
            self._playlist.append({
                "id": f"pl_{uuid.uuid4().hex[:8]}",
                "title": s["name"],
                "artist": s["artists"],
                "stored_name": s["stored_name"],
                "song_id": s.get("song_id", ""),
            })
        # 确保自动下一首开启
        if not self._playlist_autoplay_on:
            self._playlist_autoplay_on = True
        # 开始播放第一首
        self._playlist_cursor = 0
        r = await self.playlist_play(start_index=0)
        if isinstance(r, Ok):
            v = r.value if hasattr(r, "value") else r
            return {
                "output": {
                    "ok": True,
                    "playlist_name": best_match_name,
                    "total_songs": len(songs),
                    "playable_count": len(playable),
                    "skipped_no_audio": skipped,
                    "now_playing": v.get("title", ""),
                    "now_playing_artist": v.get("artist", ""),
                    "autoplay_on": True,
                    "message": f"已开始播放歌单「{best_match_name}」：共 {len(songs)} 首，其中 {len(playable)} 首有音频{'（跳过 ' + str(skipped) + ' 首无音频）' if skipped else ''}。正在播放第 1 首：{v.get('title', '')} - {v.get('artist', '')}。播完会自动播放下一首。",
                }
            }
        return {"output": {"ok": False, "reason": f"歌单已加载但播放失败: {r}"}, "is_error": True, "error": "PLAY_FAILED"}
