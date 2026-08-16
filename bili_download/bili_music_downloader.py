#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站音乐搜索下载器
==================

输入歌曲名和歌手 → 在B站搜索 → 智能匹配最像 MV / 演唱会现场 / 播放厅的视频
（要求包含那首歌的完整音频）→ 用 yt-dlp 提取音频保存为 MP3。

依赖
----
    pip install requests yt-dlp
    # 系统需安装 ffmpeg（音频转码用）：winget install ffmpeg 或 scoop install ffmpeg

用法
----
    python bili_music_downloader.py "晴天" "周杰伦"
    python bili_music_downloader.py                 # 交互式输入
    python bili_music_downloader.py "晴天" "周杰伦" -y   # 跳过确认，直接下载第1名
    python bili_music_downloader.py "晴天" "周杰伦" -o D:/music   # 指定输出目录

说明
----
    · 无需B站账号。脚本会自动完成"设备激活"（buvid/bili_ticket）绕过游客搜索限制，
      激活结果缓存在 ~/.bili_music_cookies.json（约2天有效，减少重复请求）。
    · B站对搜索接口有 IP 级风控：短时间高频搜索会返回空结果（脚本会自动重试并
      切换备用通道），若仍失败，等几分钟再试即可。
    · 装了 ffmpeg 输出 MP3；没装则保存原始音频（m4a，音质更好且可直接播放）。
"""

import argparse
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

try:
    import requests
except ImportError:
    print("缺少 requests，请先安装:  pip install requests")
    sys.exit(1)

# ============================================================
#  WBI 签名 —— B站 API 参数签名算法
# ============================================================

_MIXIN_TABS = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 36, 25, 6, 22, 7, 20, 59, 16, 24, 44, 57, 51, 4, 17, 21,
    52, 30, 40, 34, 55, 48, 11, 26, 0, 54, 56, 1, 60, 61, 62, 63,
]


def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_TABS if i < len(orig))[:32]


def _enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """对请求参数进行 WBI 签名，返回带 wts / w_rid 的参数 dict"""
    mixin = _get_mixin_key(img_key + sub_key)
    p = dict(params)
    p["wts"] = round(time.time())
    bad = set("!'()*-._")
    cleaned = {k: "".join(c for c in str(v) if c not in bad) for k, v in p.items()}
    query = "&".join(f"{k}={v}" for k, v in sorted(cleaned.items()))
    cleaned["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return cleaned


# ============================================================
#  下载器
# ============================================================

class BiliMusicDownloader:
    BASE = "https://api.bilibili.com"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        })
        self._img_key = None
        self._sub_key = None
        self._cookie_file = os.path.join(
            os.path.expanduser("~"), ".bili_music_cookies.json")
        if not self._load_cookies():
            self._activate()

    # ---------- 风控激活（2024年后搜索接口必需） ----------

    def _save_cookies(self):
        try:
            data = {c.name: c.value for c in self.s.cookies}
            data["_saved_ts"] = int(time.time())
            with open(self._cookie_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load_cookies(self) -> bool:
        """加载之前保存的设备 cookie（bili_ticket 有效期约3天），失败则重新激活"""
        try:
            if not os.path.exists(self._cookie_file):
                return False
            with open(self._cookie_file, encoding="utf-8") as f:
                data = json.load(f)
            saved = data.pop("_saved_ts", 0)
            if time.time() - saved > 2 * 24 * 3600:  # 超过2天重新激活
                return False
            for k, v in data.items():
                self.s.cookies.set(k, v, domain=".bilibili.com")
            return "bili_ticket" in data
        except Exception:
            return False

    def _activate(self):
        """
        无登录状态下，B站搜索接口需要一套"设备指纹" cookie 才会返回真实结果，
        否则返回 code=0 但 result 为空。流程:
          1. 访问首页拿 buvid3 / b_nut
          2. finger/spi 拿 buvid3 / buvid4
          3. GenWebTicket（HMAC-SHA256 签名）拿 bili_ticket
        """
        try:
            # 首页 cookie
            self.s.get("https://www.bilibili.com", timeout=10)
            # buvid3 / buvid4
            spi = self.s.get(
                f"{self.BASE}/x/frontend/finger/spi", timeout=10
            ).json()["data"]
            self.s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
            self.s.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
            self.s.cookies.set(
                "_uuid", f"{uuid.uuid4()}infoc", domain=".bilibili.com")
            # bili_ticket
            ts = int(time.time())
            hexsign = hmac.new(
                b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256
            ).hexdigest()
            tk = self.s.post(
                f"{self.BASE}/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                params={"key_id": "ec02", "hexsign": hexsign,
                        "context[ts]": ts, "csrf": ""},
                timeout=10,
            ).json()
            if tk.get("code") == 0 and tk.get("data", {}).get("ticket"):
                self.s.cookies.set(
                    "bili_ticket", tk["data"]["ticket"], domain=".bilibili.com")
                # 顺带缓存 wbi keys，省一次 nav 请求
                nav = tk["data"].get("nav", {})
                if nav.get("img"):
                    self._img_key = nav["img"].rsplit("/", 1)[1].split(".")[0]
                    self._sub_key = nav["sub"].rsplit("/", 1)[1].split(".")[0]
                self._save_cookies()
        except Exception as e:
            print(f"  ⚠ 设备激活失败（继续尝试）: {e}")

    # ---------- WBI keys ----------

    def _ensure_wbi_keys(self):
        if self._img_key:
            return
        r = self.s.get(f"{self.BASE}/x/web-interface/nav", timeout=10)
        d = r.json()["data"]["wbi_img"]
        self._img_key = d["img_url"].rsplit("/", 1)[1].split(".")[0]
        self._sub_key = d["sub_url"].rsplit("/", 1)[1].split(".")[0]

    # ---------- 搜索 ----------

    @staticmethod
    def _strip_html(text: str) -> str:
        """去掉搜索结果里的 <em class="keyword"> 高亮标签"""
        return text.replace('<em class="keyword">', "").replace("</em>", "").strip()

    @staticmethod
    def _parse_duration(s) -> int:
        """把 '3:45' / '1:03:45' 之类的时长字符串转成秒数"""
        try:
            parts = [int(x) for x in str(s).split(":")]
        except ValueError:
            return 0
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0

    def search(self, keyword: str, page: int = 1) -> list:
        """在B站搜索视频，返回原始结果列表（带重试 + bilisearch 备用通道）"""
        for attempt in range(2):
            results = self._search_once(keyword, page)
            if results:
                return results
            if attempt == 0:
                print("  ⚠ 结果为空（可能被风控），重新激活设备后重试...")
                time.sleep(3)
                self._img_key = None
                self._sub_key = None
                self._activate()
        # 备用通道：yt-dlp 的 bilisearch
        print("  ⚠ 官方搜索接口无结果，尝试 yt-dlp 备用通道...")
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True,
                                   "skip_download": True}) as ydl:
                info = ydl.extract_info(f"bilisearch20:{keyword}", download=False)
            entries = info.get("entries") or []
            out = []
            for e in entries:
                if not e or not e.get("id"):
                    continue
                vid = str(e["id"])
                if not vid.startswith("BV"):       # bilisearch 有时返回数字AV号
                    vid = f"av{vid}"
                dur = e.get("duration") or 0
                out.append({
                    "bvid": vid,
                    "title": e.get("title", "") or vid,
                    "description": "",
                    "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "0:0",
                    "play": e.get("view_count") or 0,
                    "typeid": 3,          # 视为音乐分区
                    "video_badge": "",
                    "author": e.get("uploader") or "",
                    "tag": "",
                })
            if out:
                return out
        except Exception:
            pass
        return []

    def _search_once(self, keyword: str, page: int) -> list:
        self._ensure_wbi_keys()
        params = _enc_wbi(
            {"search_type": "video", "keyword": keyword,
             "page": page, "page_size": 20},
            self._img_key, self._sub_key,
        )
        url = f"{self.BASE}/x/web-interface/wbi/search/type"
        r = self.s.get(url, params=params, timeout=15)
        j = r.json()
        if j.get("code") != 0:
            print(f"  搜索接口报错: {j.get('message', '?')} (code={j.get('code')})")
            return []
        return j.get("data", {}).get("result") or []

    # ---------- 评分 ----------

    def rank(self, results: list, song: str, artist: str) -> list:
        """
        对搜索结果打分排序，分数最高的最可能是「完整的 MV / 现场版 / 音乐播放视频」。

        评分项（大致权重）:
          +30 标题含歌曲名          +20 标题含歌手名
          +15 标题/简介含 MV/官方/4K +10 徽章带 MV
           +8 现场/演唱会关键词      +15 时长在 1.5~10 分钟（正常歌曲长度）
          +10 属于音乐分区           ≤+15 播放量（对数）
          -25 标题含 片段/翻唱/remix/混剪 等负面词
          -20 时长 < 1 分钟          -10 时长 > 10 分钟（可能是合集）
        """
        sl, al = song.lower(), artist.lower()
        mv_kw = ["mv", "官方", "official", "4k", "修复", "remaster", "高清", "完整版"]
        live_kw = ["live", "现场", "演唱会", "tour", "音乐会", "演奏厅", "播放厅"]
        bad_kw = ["片段", "cut", "剪辑", "preview", "预告", "翻唱",
                  "cover", "remix", "混剪", "amv", "mad", "试听", "短版", "串烧"]

        ranked = []
        for item in results:
            title = self._strip_html(item.get("title", ""))
            desc = self._strip_html(item.get("description", ""))
            badge = item.get("video_badge", "") or ""
            dur_str = item.get("duration", "0:0")
            dur = self._parse_duration(dur_str)
            play = item.get("play", 0) or 0
            tid = item.get("typeid", 0)
            author = item.get("author", "") or ""
            tlow, dlow = title.lower(), desc.lower()

            score = 0.0

            # 歌曲名命中
            if sl in tlow:
                score += 30
            elif sl in dlow:
                score += 8
            # 歌手名命中
            if al in tlow:
                score += 20
            elif al in author.lower():
                score += 10
            elif al in dlow:
                score += 5
            # MV / 官方
            if any(k in tlow or k in dlow for k in mv_kw):
                score += 15
            if "mv" in badge.lower():
                score += 10
            # 现场 / 演唱会
            if any(k in tlow or k in dlow for k in live_kw):
                score += 8
            # 负面关键词
            if any(k in tlow for k in bad_kw):
                score -= 25
            # 时长
            if 90 <= dur <= 600:
                score += 15
            elif dur > 600:
                score -= 10
            elif dur < 60:
                score -= 20
            # 音乐分区（typeid==3）
            if tid == 3:
                score += 10
            # 播放量
            if play > 0:
                score += min(math.log10(play + 1) * 3, 15)

            ranked.append({
                "bvid": item.get("bvid", ""),
                "title": title,
                "author": author,
                "dur": dur_str,
                "dur_s": dur,
                "play": play,
                "score": round(score, 1),
                "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    # ---------- 收藏夹 ----------

    def set_sessdata(self, sessdata: str):
        """设置用户登录态 SESSDATA cookie（用于访问收藏夹等需要登录的接口）"""
        self.s.cookies.set("SESSDATA", sessdata.strip(), domain=".bilibili.com")

    def get_user_mid(self) -> dict:
        """通过 nav 接口获取当前登录用户信息，返回 {mid, name, is_login}"""
        r = self.s.get(f"{self.BASE}/x/web-interface/nav", timeout=10)
        j = r.json()
        if j.get("code") != 0:
            return {"is_login": False, "mid": "", "name": "", "error": j.get("message", "?")}
        data = j.get("data", {})
        mid = str(data.get("mid", ""))
        name = data.get("name", "")
        is_login = data.get("isLogin", False)
        return {"is_login": is_login, "mid": mid, "name": name}

    def get_fav_folders(self, mid: str) -> list:
        """获取用户创建的收藏夹列表，返回 [{id, title, media_count, cover}]"""
        if not mid:
            return []
        r = self.s.get(
            f"{self.BASE}/x/v3/fav/folder/created/list-all",
            params={"up_mid": mid, "type": 2},
            timeout=15,
        )
        j = r.json()
        if j.get("code") != 0:
            print(f"  收藏夹接口报错: {j.get('message', '?')} (code={j.get('code')})")
            return []
        folders = j.get("data", {}).get("list", []) or []
        out = []
        for f in folders:
            out.append({
                "id": f.get("id", 0),
                "title": f.get("title", ""),
                "media_count": f.get("media_count", 0),
                "cover": f.get("cover", ""),
                "fav_time": f.get("fav_time", 0),
            })
        return out

    def get_fav_videos(self, media_id: int, page_size: int = 20) -> list:
        """获取收藏夹内所有视频（自动翻页），返回 [{bvid, title, upper_name, duration, cover, fav_time}]"""
        all_videos = []
        pn = 1
        while True:
            r = self.s.get(
                f"{self.BASE}/x/v3/fav/resource/list",
                params={
                    "media_id": media_id,
                    "pn": pn,
                    "ps": page_size,
                    "order": "mtime",
                    "platform": "web",
                },
                timeout=15,
            )
            j = r.json()
            if j.get("code") != 0:
                print(f"  收藏夹视频接口报错: {j.get('message', '?')} (code={j.get('code')})")
                break
            data = j.get("data", {})
            medias = data.get("medias", []) or []
            if not medias:
                break
            for m in medias:
                bvid = m.get("bvid", "")
                if not bvid:
                    continue
                upper = m.get("upper", {}) or {}
                cnt = m.get("cnt_info", {}) or {}
                all_videos.append({
                    "bvid": bvid,
                    "title": m.get("title", "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                    "upper_name": upper.get("name", ""),
                    "upper_mid": upper.get("mid", 0),
                    "duration": m.get("duration", 0),
                    "cover": m.get("cover", ""),
                    "play": cnt.get("play", 0),
                    "danmaku": cnt.get("danmaku", 0),
                    "fav_time": m.get("fav_time", 0),
                    "link": f"https://www.bilibili.com/video/{bvid}",
                })
            total = data.get("info", {}).get("media_count", 0)
            if len(all_videos) >= total or len(medias) < page_size:
                break
            pn += 1
            time.sleep(0.3)  # 避免请求过快
        return all_videos

    # ---------- 下载 ----------

    def download(self, video_url: str, out_dir: str = ".",
                 name_hint: str = "") -> bool:
        """提取音频：优先用 yt-dlp Python 模块，失败则回退到命令行"""
        os.makedirs(out_dir, exist_ok=True)
        base = _safe_name(name_hint) if name_hint else "%(title)s"
        tmpl = os.path.join(out_dir, base + ".%(ext)s")
        have_ffmpeg = shutil.which("ffmpeg") is not None
        if not have_ffmpeg:
            print("  ⚠ 未检测到 ffmpeg，无法转 MP3，将保存原始音频（m4a）")

        common = {
            "format": "bestaudio/best",
            "outtmpl": tmpl,
            "noplaylist": True,
        }

        # 方式一：yt-dlp Python 模块
        try:
            import yt_dlp
            opts = dict(common)
            if have_ffmpeg:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }]
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([video_url])
                    return True
            except yt_dlp.utils.DownloadError as e:
                print(f"  ✘ 下载出错: {str(e).splitlines()[0]}")
                return False
        except ImportError:
            pass

        # 方式二：yt-dlp 命令行
        cmd = ["yt-dlp", "-o", tmpl, "--no-playlist",
               "--format", "bestaudio/best", video_url]
        if have_ffmpeg:
            cmd[1:1] = ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        print(f"  $ {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd)
            return r.returncode == 0
        except FileNotFoundError:
            print("  未找到 yt-dlp，请安装:  pip install yt-dlp")
            return False


# ============================================================
#  工具函数
# ============================================================

def _check_yt_dlp() -> bool:
    """yt-dlp 模块或命令行任一可用即可"""
    if importlib.util.find_spec("yt_dlp") is not None:
        return True
    try:
        subprocess.run(["yt-dlp", "--version"],
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


def _safe_name(name: str, maxlen: int = 40) -> str:
    """生成安全的文件名前缀"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name[:maxlen] if name else "audio"


# ============================================================
#  主入口
# ============================================================

def _download_by_bvid(bvid: str, out_dir: str, name_hint: str = "") -> bool:
    """根据 BV 号直接下载音频（不经过搜索），复用 BiliMusicDownloader.download"""
    dl = BiliMusicDownloader()
    # 规范化 BV 号
    bvid = bvid.strip()
    if not bvid:
        print("BV 号不能为空")
        print("__FAIL__")
        sys.exit(1)
    # 支持 URL 形式：https://www.bilibili.com/video/BV1xxxx
    if "bilibili.com" in bvid:
        url = bvid
    else:
        # 确保以 BV 开头
        if not bvid.upper().startswith("BV"):
            bvid = "BV" + bvid
        url = f"https://www.bilibili.com/video/{bvid}"

    if not _check_yt_dlp():
        print("未找到 yt-dlp，请先安装:  pip install yt-dlp")
        print("__FAIL__")
        sys.exit(1)

    print(f"下载 BV 号: {bvid}")
    print(f"  URL: {url}")
    fname = _safe_name(name_hint) if name_hint else "%(title)s"
    if dl.download(url, out_dir, name_hint=fname):
        print(f"\n  ✔ 下载完成! 文件保存在: {os.path.abspath(out_dir)}")
        print("__OK__")
        return True
    else:
        print("__FAIL__")
        sys.exit(1)


def _search_and_output_json(song: str, artist: str, top: int = 20):
    """搜索B站并以 JSON 格式输出候选列表（供插件后端解析），不下载"""
    kw = f"{song} {artist}".strip()
    if not kw:
        print(json.dumps({"ok": False, "error": "关键词为空", "results": []}, ensure_ascii=False))
        return

    dl = BiliMusicDownloader()
    results = dl.search(kw)
    if not results:
        print(json.dumps({"ok": False, "error": "未找到搜索结果", "results": []}, ensure_ascii=False))
        return

    ranked = dl.rank(results, song, artist)
    show_n = min(top, len(ranked))
    out_results = []
    for v in ranked[:show_n]:
        out_results.append({
            "bvid": v.get("bvid", ""),
            "title": v.get("title", ""),
            "author": v.get("author", ""),
            "duration": v.get("dur", ""),
            "duration_sec": v.get("dur_s", 0),
            "play": v.get("play", 0),
            "score": v.get("score", 0),
            "url": v.get("url", ""),
        })
    print(json.dumps({"ok": True, "count": len(out_results), "results": out_results}, ensure_ascii=False))


def _fetch_fav_lists_json(sessdata: str):
    """获取B站用户收藏夹列表，输出 JSON（供插件后端解析）"""
    dl = BiliMusicDownloader()
    dl.set_sessdata(sessdata)
    user = dl.get_user_mid()
    if not user.get("is_login"):
        print(json.dumps({"ok": False, "error": f"SESSDATA 无效或未登录: {user.get('error', '')}", "folders": []}, ensure_ascii=False))
        return
    mid = user.get("mid", "")
    name = user.get("name", "")
    folders = dl.get_fav_folders(mid)
    print(json.dumps({"ok": True, "mid": mid, "name": name, "count": len(folders), "folders": folders}, ensure_ascii=False))


def _fetch_fav_videos_json(sessdata: str, media_id: int):
    """获取B站收藏夹内所有视频，输出 JSON（供插件后端解析）"""
    dl = BiliMusicDownloader()
    dl.set_sessdata(sessdata)
    user = dl.get_user_mid()
    if not user.get("is_login"):
        print(json.dumps({"ok": False, "error": f"SESSDATA 无效或未登录: {user.get('error', '')}", "videos": []}, ensure_ascii=False))
        return
    videos = dl.get_fav_videos(media_id)
    print(json.dumps({"ok": True, "media_id": media_id, "count": len(videos), "videos": videos}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="B站音乐搜索下载器：搜歌 → 智能匹配 MV/现场 → 提取 MP3")
    ap.add_argument("song", nargs="?", help="歌曲名")
    ap.add_argument("artist", nargs="?", help="歌手名")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="跳过确认，直接下载评分最高的视频")
    ap.add_argument("-o", "--output", default=None, help="输出目录（默认 ./downloads）")
    ap.add_argument("-t", "--top", type=int, default=5, help="展示候选数量（默认5）")
    # ── 新增：搜索 JSON 模式（不下载，只返回候选列表） ──
    ap.add_argument("--search-json", action="store_true",
                    help="搜索模式：输出 JSON 格式的候选列表，不下载（供插件调用）")
    # ── 新增：按 BV 号下载模式 ──
    ap.add_argument("--bvid", default=None,
                    help="直接按 BV 号下载音频（跳过搜索）。如 BV1xx411x7xx 或完整 URL")
    # ── 新增：收藏夹模式 ──
    ap.add_argument("--fav-lists", action="store_true",
                    help="获取B站用户收藏夹列表（需 --sessdata）")
    ap.add_argument("--fav-videos", default=None, type=int,
                    help="获取指定收藏夹ID内的视频列表（需 --sessdata）")
    ap.add_argument("--sessdata", default=None,
                    help="B站用户登录态 SESSDATA cookie 值")
    args = ap.parse_args()

    # ── 模式1：按 BV 号直接下载 ──
    if args.bvid:
        out_dir = args.output or os.path.join(os.getcwd(), "downloads")
        name_hint = ""
        if args.song:
            name_hint = f"{args.song}"
            if args.artist:
                name_hint += f" - {args.artist}"
        _download_by_bvid(args.bvid, out_dir, name_hint=name_hint)
        return

    # ── 模式2：搜索 JSON 模式（不下载） ──
    if args.search_json:
        song = (args.song or "").strip()
        artist = (args.artist or "").strip()
        if not song:
            print(json.dumps({"ok": False, "error": "歌曲名不能为空", "results": []}, ensure_ascii=False))
            return
        _search_and_output_json(song, artist, top=max(args.top, 20))
        return

    # ── 模式2.5：收藏夹列表模式 ──
    if args.fav_lists:
        sessdata = (args.sessdata or "").strip()
        if not sessdata:
            print(json.dumps({"ok": False, "error": "需要 --sessdata 参数", "folders": []}, ensure_ascii=False))
            return
        _fetch_fav_lists_json(sessdata)
        return

    # ── 模式2.6：收藏夹视频列表模式 ──
    if args.fav_videos is not None:
        sessdata = (args.sessdata or "").strip()
        if not sessdata:
            print(json.dumps({"ok": False, "error": "需要 --sessdata 参数", "videos": []}, ensure_ascii=False))
            return
        _fetch_fav_videos_json(sessdata, args.fav_videos)
        return

    # ── 模式3：原有交互式搜索+下载 ──
    print("=" * 56)
    print("            B 站 音 乐 搜 索 下 载 器")
    print("=" * 56)

    song = args.song or input("歌曲名: ").strip()
    artist = args.artist or input("歌手名: ").strip()
    if not song or not artist:
        print("歌曲名和歌手名不能为空")
        return

    if not _check_yt_dlp():
        print("未找到 yt-dlp，请先安装:  pip install yt-dlp")
        return

    kw = f"{song} {artist}"
    dl = BiliMusicDownloader()

    print(f"\n[1/3] 搜索B站: 「{kw}」...")
    results = dl.search(kw)
    if not results:
        print("未找到搜索结果，换个关键词试试（例如只用歌名，或加英文）")
        print("__FAIL__")
        sys.exit(1)
    print(f"      找到 {len(results)} 条结果")

    print(f"\n[2/3] 智能评分，候选 Top {args.top}:")
    ranked = dl.rank(results, song, artist)
    show_n = min(args.top, len(ranked))
    for i, v in enumerate(ranked[:show_n]):
        tag = "   ◀ 自动选中" if i == 0 else ""
        print(f"  [{i + 1}] 评分 {v['score']:+6.1f}{tag}")
        print(f"      {v['title']}")
        print(f"      UP: {v['author']} | 时长 {v['dur']} | 播放 {v['play']:,}")
        print(f"      {v['url']}")
        print()

    # 选择
    idx = 0
    if not args.yes:
        ans = input(f"回车 = 下载 [1]，或输入序号 1-{show_n} 选择其他: ").strip()
        if ans:
            try:
                idx = max(0, min(int(ans) - 1, show_n - 1))
            except ValueError:
                idx = 0

    print("\n[3/3] 下载音频:")
    out_dir = args.output or os.path.join(os.getcwd(), "downloads")

    # 第一个失败自动降级尝试下一个
    candidates = [ranked[idx]] + [v for i, v in enumerate(ranked[:show_n]) if i != idx]
    for cand in candidates:
        print(f"\n  ▶ {cand['title']}  ({cand['dur']}, 播放 {cand['play']:,})")
        print(f"    {cand['url']}")
        fname = f"{song} - {artist}"
        if dl.download(cand["url"], out_dir, name_hint=fname):
            print(f"\n  ✔ 下载完成! 文件保存在: {os.path.abspath(out_dir)}")
            print("__OK__")
            return
        print("  ✘ 该视频下载失败，尝试下一个候选...")

    print("\n所有候选都失败了。可以手动试试:")
    for cand in candidates[:2]:
        print(f"  yt-dlp -x --audio-format mp3 {cand['url']}")
    print("__FAIL__")
    sys.exit(1)


if __name__ == "__main__":
    main()
