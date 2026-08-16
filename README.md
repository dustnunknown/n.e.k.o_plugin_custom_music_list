# 音乐歌单管理（custom_music_list）

一个面向 [N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O) 的插件：把网易云音乐、QQ音乐、B站的歌单/收藏夹导出为文本，统一管理已导入的歌曲文件，并支持让猫娘用 LLM 工具直接点歌、轮播。三平台的播放列表互通。

## 功能特性

- **网易云音乐**
  - MUSIC_U cookie 登录（扫码已被网易云风控，改为 cookie 方式）
  - 查看「我喜欢的音乐」与所有收藏/创建的歌单（支持分页、搜索）
  - 一键导出歌单内全部歌曲名 + 作者到 `txt`（按歌单名命名）
- **QQ音乐**
  - 通过公开接口读取歌单，导出歌单曲目到 `txt` / `json`
- **B站（哔哩哔哩）**
  - 下载收藏夹 / 音频区的音乐到本地，自动解析时长
- **歌曲管理 & 播放列表**
  - 统一管理已导入的歌曲文件（来源、时长、路径）
  - 生成播放列表，支持在对话中让猫娘播放
- **LLM 点歌轮播**
  - `@llm_tool` 暴露 `play_song_by_name` 等工具，猫娘可按歌名直接点歌
  - 三平台播放列表互通，可用于日常轮播陪伴

## 安装

### 方式一（推荐）：通过release的custom_music_list.neko-plugin文件

在neko插件界面导入custom_music_list.neko-plugin文件，打开插件，若播放存在问题则打开neko目录→resources→bin→static→jukebox，在这个路径下用release里提供的music_ui.js替换原来的文件，以修复nekov0.9.0不能正常播放本地音乐的问题（官方说后续的版本会修复了），然后重启neko，手动关闭再开启插件就能用了。


### 方式二：从release下载压缩包到本地插件目录

若你是从steam下载的，且.neko-plugin文件导入失败，则在release中下载压缩包，解压到neko的plugins目录（C:\Users\ Yourname \AppData\Local\N.E.K.O\plugins）（解压出来一定是个文件夹！），然后参考方式一的替换music_ui.js方法进行替换。


## 使用

1. 打开插件面板，先 **启动** 插件（默认 `auto_start = false`，需手动启动）。
2. **网易云**：用「Cookie 登录」填入 `MUSIC_U` cookie 完成登录；随后可对歌单执行「导出 txt」。
3. **QQ音乐 / B站**：按面板提示操作；B站下载需要对应账号的 cookie（自动持久化到 `data/` 下的 `bili_cookie.json`）。
4. **点歌**：在对话里直接说「播放 xxx」，猫娘会调用 LLM 工具从已导入歌曲中匹配并推送到播放界面。

> 登录态（cookie / session）仅保存在插件本地 `data/` 目录。

## 配置（plugin.toml）

| 段 | 字段 | 说明 |
| --- | --- | --- |
| `[plugin_runtime]` | `enabled` | 是否启用插件 |
| `[plugin_runtime]` | `auto_start` | N.E.K.O 启动时是否自动运行（默认关闭，手动启动） |
| `[plugin.store]` | `enabled` | 是否启用持久化键值存储（`self.store`） |

## 目录结构

```
custom_music_list/
├── plugin.toml              # 插件清单（必填）
├── __init__.py              # 插件主逻辑（入口类 CustomMusicListPlugin）
├── bili_download/           # B站音乐下载器
│   └── bili_music_downloader.py
├── music_link/              # 音乐链接解析
│   └── music_parser.py
├── static/                  # 旧版 Web UI（register_static_ui）
│   └── index.html
├── data/                    # 运行时数据（cookie / 导出文件 / 下载歌曲），不提交
└── .github/workflows/       # N.E.K.O 官方 verify / release 流水线
```

## 许可证

本项目随 N.E.K.O 生态发布，具体许可证以仓库 LICENSE 文件为准。
