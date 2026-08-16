#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daga.cc 音乐解析工具
=====================
从 https://daga.cc/yue/?id={歌曲ID}&type={平台} 解析音乐下载链接。

原理：
  网页源代码中不包含音乐链接，链接由 JavaScript (music.js) 通过
  AJAX POST 请求动态获取后注入 DOM。本脚本直接模拟该 AJAX 请求，
  无需启动浏览器即可拿到 JSON 结果中的 url 字段。

用法：
  python music_parser.py                        # 交互模式
  python music_parser.py 1489958235             # 单首歌（默认 netease）
  python music_parser.py 1489958235 qq          # 指定平台
  python music_parser.py 1489958235 28815250    # 多首歌（默认 netease）
  python music_parser.py 1489958235 netease --download   # 下载 mp3
"""

import argparse
import json
import os
import time
from urllib.parse import urlencode

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    import urllib.error
    import urllib.request

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://daga.cc/yue/"
API_URL = "https://daga.cc/yue/"

SUPPORTED_TYPES = [
    "netease", "qq", "kugou", "kuwo", "xiami", "baidu",
    "1ting", "migu", "lizhi", "qingting", "ximalaya",
    "kg", "5singyc", "5singfc"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://daga.cc",
    "Referer": "https://daga.cc/yue/",
}


# ============================================================
# 核心请求函数
# ============================================================
def parse_music(song_id, music_type="netease", filter_type="id", page=1):
    """
    向 daga.cc 发送 AJAX POST 请求，解析音乐下载链接。

    参数:
        song_id:    歌曲 ID（或歌曲名称，此时 filter_type 应为 "name"）
        music_type: 音乐平台类型，如 netease / qq / kugou 等
        filter_type: 搜索方式，"id"=按ID搜索, "name"=按名称搜索
        page:       页码（用于"载入更多"）

    返回:
        dict: 包含解析结果的字典，失败返回 None
    """
    post_data = urlencode({
        "input": str(song_id),
        "filter": filter_type,
        "type": music_type,
        "page": page,
    })

    try:
        if HAS_REQUESTS:
            resp = requests.post(
                API_URL,
                data=post_data,
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        else:
            # urllib 回退
            req = urllib.request.Request(
                API_URL,
                data=post_data.encode("utf-8"),
                headers=HEADERS,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

        if result.get("code") == 200 and result.get("data"):
            return result["data"]
        else:
            error_msg = result.get("error", "未知错误")
            print(f"  [!] 解析失败: {error_msg}")
            return None

    except Exception as e:
        print(f"  [!] 请求出错: {e}")
        return None


def extract_music_url(data_item):
    """从单个解析结果中提取音乐下载链接。"""
    url = data_item.get("url", "")
    if url and ("m701." in url or "m801." in url or "music.126.net" in url
                or ".mp3" in url or "m701" in url or "m801" in url):
        return url
    # 如果没有匹配到特定模式，返回原始 url
    return url if url else None


def download_mp3(url, filepath, title="unknown", author="unknown"):
    """下载 MP3 文件到本地。"""
    try:
        safe_name = f"{title}-{author}".replace("/", "_").replace("\\", "_")
        if not filepath:
            filepath = f"{safe_name}.mp3"

        print(f"  [*] 正在下载: {filepath}")

        if HAS_REQUESTS:
            resp = requests.get(url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://daga.cc/yue/",
            }, stream=True, timeout=60)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        else:
            req = urllib.request.Request(url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://daga.cc/yue/",
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(filepath, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

        size = os.path.getsize(filepath)
        size_str = f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size / 1024:.1f} KB"
        print(f"  [+] 下载完成: {filepath} ({size_str})")
        return filepath

    except Exception as e:
        print(f"  [!] 下载失败: {e}")
        return None


# ============================================================
# 输出 / 导出
# ============================================================
def save_links_to_file(results, output_file="music_links.txt"):
    """将解析结果导出到文本文件。"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("daga.cc 音乐解析结果\n")
        f.write(f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")
        for i, r in enumerate(results, 1):
            f.write(f"[{i}] {r['title']} - {r['author']}\n")
            f.write(f"    平台: {r.get('type', 'N/A')}\n")
            f.write(f"    歌曲ID: {r.get('songid', 'N/A')}\n")
            f.write(f"    原始链接: {r.get('link', 'N/A')}\n")
            f.write(f"    下载链接: {r.get('url', 'N/A')}\n")
            f.write(f"    封面: {r.get('pic', 'N/A')}\n")
            f.write("\n")
    print(f"\n[*] 链接已导出到: {os.path.abspath(output_file)}")


def save_links_to_json(results, output_file="music_links.json"):
    """将解析结果导出为 JSON 文件。"""
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[*] JSON 已导出到: {os.path.abspath(output_file)}")


def print_result(data_item, index=1):
    """格式化打印单个解析结果。"""
    title = data_item.get("title", "未知")
    author = data_item.get("author", "未知")
    url = data_item.get("url", "")
    link = data_item.get("link", "")
    songid = data_item.get("songid", "")
    pic = data_item.get("pic", "")

    print(f"\n{'='*60}")
    print(f"  [{index}] {title} - {author}")
    print(f"{'='*60}")
    print(f"  歌曲ID   : {songid}")
    print(f"  原始链接 : {link}")
    print(f"  下载链接 : {url}")
    print(f"  封面图片 : {pic}")
    print(f"{'='*60}")


# ============================================================
# 主程序
# ============================================================
def interactive_mode():
    """交互模式。"""
    print("=" * 60)
    print("  daga.cc 音乐解析工具")
    print("=" * 60)
    print(f"  支持平台: {', '.join(SUPPORTED_TYPES)}")
    print(f"  依赖库  : {'requests' if HAS_REQUESTS else 'urllib (内置)'}")
    print("=" * 60)

    while True:
        print()
        user_input = input("请输入歌曲 ID 或名称 (输入 q 退出): ").strip()
        if user_input.lower() in ("q", "quit", "exit"):
            print("再见！")
            break
        if not user_input:
            continue

        # 判断是 ID 还是名称
        if user_input.isdigit():
            filter_type = "id"
        else:
            filter_type = "name"

        print("\n请选择平台 (直接回车默认 netease):")
        for i, t in enumerate(SUPPORTED_TYPES):
            print(f"  {i+1}. {t}", end="")
            if (i + 1) % 5 == 0:
                print()
        print()
        type_input = input(f"选择 [1-{len(SUPPORTED_TYPES)}] 或直接输入名称: ").strip().lower()

        if type_input.isdigit() and 1 <= int(type_input) <= len(SUPPORTED_TYPES):
            music_type = SUPPORTED_TYPES[int(type_input) - 1]
        elif type_input in SUPPORTED_TYPES:
            music_type = type_input
        else:
            music_type = "netease"

        print(f"\n[*] 正在解析: {user_input} ({filter_type}) | 平台: {music_type} ...")

        results = parse_music(user_input, music_type, filter_type=filter_type)
        if not results:
            print("[!] 未找到结果，请尝试切换平台或检查 ID")
            continue

        all_results = list(results)

        # 第一页结果
        for i, item in enumerate(results, 1):
            print_result(item, i)

        # 是否有更多
        if len(results) >= 10:
            more = input("\n[*] 结果可能有更多，是否加载下一页？(y/N): ").strip().lower()
            page = 2
            while more == "y":
                print(f"\n[*] 正在加载第 {page} 页...")
                more_results = parse_music(user_input, music_type, filter_type=filter_type, page=page)
                if not more_results:
                    print("[!] 没有更多结果了")
                    break
                for i, item in enumerate(more_results, len(all_results) + 1):
                    print_result(item, i)
                all_results.extend(more_results)
                page += 1
                if len(more_results) < 10:
                    break
                more = input("\n[*] 是否继续加载下一页？(y/N): ").strip().lower()

        # 导出
        save = input("\n[*] 是否导出链接到文件？(y/N): ").strip().lower()
        if save == "y":
            fmt = input("  格式: 1=txt  2=json  3=两者都要 (默认1): ").strip() or "1"
            if fmt in ("1", "3"):
                save_links_to_file(all_results)
            if fmt in ("2", "3"):
                save_links_to_json(all_results)

        # 下载
        dl = input("\n[*] 是否下载 MP3？输入序号下载对应歌曲，或 a 下载全部 (回车跳过): ").strip()
        if dl:
            if dl.lower() == "a":
                for item in all_results:
                    url = item.get("url", "")
                    if url:
                        download_mp3(url, "", item.get("title", ""), item.get("author", ""))
            elif dl.isdigit() and 1 <= int(dl) <= len(all_results):
                item = all_results[int(dl) - 1]
                url = item.get("url", "")
                if url:
                    download_mp3(url, "", item.get("title", ""), item.get("author", ""))


def parse_url(url_str):
    """从 daga.cc URL 中提取 id 和 type。"""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url_str)
    params = parse_qs(parsed.query)
    song_id = params.get("id", [None])[0]
    music_type = params.get("type", ["netease"])[0]
    name = params.get("name", [None])[0]
    return song_id or name, music_type


def main():
    epilog_text = (
        "示例:\n"
        "  python music_parser.py                                          # 交互模式\n"
        "  python music_parser.py 1489958235                               # 解析单首 (默认 netease)\n"
        '  python music_parser.py "https://daga.cc/yue/?id=1489958235&type=netease"  # 直接粘贴URL\n'
        "  python music_parser.py 1489958235 28815250                      # 批量解析\n"
        "  python music_parser.py 1489958235 --download                    # 解析并下载\n"
        '  python music_parser.py "你别忘 泠鸢yousa"                        # 按名称搜索\n'
        "  python music_parser.py 1489958235 -t qq                         # 指定平台\n"
        "  python music_parser.py 1489958235 -o result.txt                 # 导出到指定文件\n"
        "  python music_parser.py 1489958235 -j                            # 导出为 JSON\n"
        "  python music_parser.py 1489958235 -t name                       # 按名称搜索"
    )
    parser = argparse.ArgumentParser(
        description="daga.cc 音乐解析工具 - 从 daga.cc 解析音乐下载链接",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog_text,
    )
    parser.add_argument("songs", nargs="*", help="歌曲 ID/名称/URL（多个用空格分隔）")
    parser.add_argument("-t", "--type", default="netease",
                        choices=SUPPORTED_TYPES,
                        help="音乐平台 (默认: netease)")
    parser.add_argument("-f", "--filter", default="auto",
                        choices=["auto", "id", "name"],
                        help="搜索方式: auto=自动判断, id=按ID, name=按名称 (默认: auto)")
    parser.add_argument("-o", "--output", help="导出链接到指定文本文件")
    parser.add_argument("-j", "--json", action="store_true",
                        help="同时导出为 JSON 格式")
    parser.add_argument("-d", "--download", action="store_true",
                        help="下载 MP3 文件到当前目录")
    parser.add_argument("--download-dir", default=".",
                        help="MP3 下载目录 (默认: 当前目录)")

    args = parser.parse_args()

    # 无参数 -> 交互模式
    if not args.songs:
        interactive_mode()
        return

    # 处理输入：支持 URL、ID、名称
    # 如果输入的是完整 URL，从 URL 中提取 id 和 type
    parsed_songs = []
    url_type_override = None
    for s in args.songs:
        if s.startswith("http://") or s.startswith("https://"):
            song_id, url_type = parse_url(s)
            if song_id:
                parsed_songs.append(song_id)
                url_type_override = url_type  # URL 中指定的平台优先
            else:
                print(f"[!] 无法从 URL 解析歌曲ID: {s}")
        else:
            parsed_songs.append(s)

    # URL 中的 type 优先于命令行 -t
    if url_type_override and url_type_override in SUPPORTED_TYPES:
        args.type = url_type_override

    # 命令行模式
    print("=" * 60)
    print("  daga.cc 音乐解析工具")
    print("=" * 60)
    print(f"  依赖库: {'requests' if HAS_REQUESTS else 'urllib (内置)'}")
    print(f"  平台  : {args.type}")
    print(f"  歌曲  : {', '.join(parsed_songs)}")
    print("=" * 60)

    all_results = []

    for song in parsed_songs:
        # 自动判断搜索方式
        if args.filter == "auto":
            filter_type = "id" if song.isdigit() else "name"
        else:
            filter_type = args.filter

        print(f"\n[*] 正在解析: {song} ({filter_type}) ...")

        results = parse_music(song, args.type, filter_type=filter_type)
        if not results:
            print("  [!] 未找到结果")
            continue

        for i, item in enumerate(results, 1):
            print_result(item, i)
            all_results.append(item)

        # 下载
        if args.download:
            for item in results:
                url = item.get("url", "")
                if url:
                    title = item.get("title", "unknown")
                    author = item.get("author", "unknown")
                    safe_name = f"{title}-{author}".replace("/", "_").replace("\\", "_")
                    filepath = os.path.join(args.download_dir, f"{safe_name}.mp3")
                    download_mp3(url, filepath, title, author)

    # 导出
    if args.output:
        save_links_to_file(all_results, args.output)
    elif all_results:
        save_links_to_file(all_results)

    if args.json:
        json_file = args.output.replace(".txt", ".json") if args.output else "music_links.json"
        save_links_to_json(all_results, json_file)

    print(f"\n[*] 共解析 {len(all_results)} 首歌曲")
    print("[*] 完成！")


if __name__ == "__main__":
    main()
