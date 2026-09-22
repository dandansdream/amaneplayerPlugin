"""本机文件播放源.

把条目已索引的本机视频文件直接作为播放流交给主机: 主机负责校验路径属于该条目、
以 ``Accept-Ranges`` 输出正文, 并在客户端断开后停止读取. 本插件不做转码, 因此
浏览器无法解码的格式 (avi / wmv / rmvb / ts 等) 会被列为不可播并说明原因.

同目录下与视频同名的 ``.srt`` / ``.vtt`` 会作为字幕轨道列出 (srt 转成 WebVTT).
"""

from __future__ import annotations

from pathlib import Path

try:  # 打包版同样提供这两个入口, 优先用作者 SDK 路径
    from amane.plugin import (  # type: ignore[import-not-found]
        EmptyPluginConfig,
        FilePlaybackTarget,
        PlaybackOffer,
        PlaybackPlugin,
        PlaybackProvider,
        PlaybackQuery,
        PluginContext,
        SourceCapability,
        SourceDescriptor,
        SubtitleTrack,
    )
except ImportError:  # pragma: no cover
    from amane.plugins.api import (  # type: ignore[no-redef]
        EmptyPluginConfig,
        FilePlaybackTarget,
        PlaybackOffer,
        PlaybackPlugin,
        PlaybackProvider,
        PlaybackQuery,
        PluginContext,
        SubtitleTrack,
    )
    from amane.plugins.models import (  # type: ignore[no-redef]
        PLUGIN_API_VERSION,
        SourceCapability,
        SourceDescriptor,
    )

SOURCE_ID = "nas.local"

#: 浏览器可直接解码的容器. 其余格式一律标为不可播, 避免点了只有声音或黑屏.
CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
}

#: 主机不解码, 但值得告诉用户为什么不能播.
KNOWN_UNPLAYABLE: dict[str, str] = {
    ".avi": "浏览器无法解码 AVI",
    ".wmv": "浏览器无法解码 WMV",
    ".rmvb": "浏览器无法解码 RMVB",
    ".rm": "浏览器无法解码 RM",
    ".flv": "浏览器无法解码 FLV",
    ".ts": "浏览器无法解码 TS",
    ".m2ts": "浏览器无法解码 M2TS",
    ".mts": "浏览器无法解码 M2TS",
    ".mpg": "浏览器无法解码 MPEG",
    ".mpeg": "浏览器无法解码 MPEG",
    ".iso": "ISO 镜像需要挂载后播放",
}

SUBTITLE_SUFFIXES = (".srt", ".vtt")
RESOLVE_TTL_SECONDS = 600.0


def _content_type(path: Path) -> str | None:
    return CONTENT_TYPES.get(path.suffix.casefold())


def _stream_key(file_id: int) -> str:
    """流的标识: 只进路径, 因此用文件 id 而不是文件名."""
    return f"f{file_id}"


def _file_by_key(query: PlaybackQuery, key: str | None):
    if key is None:
        return None
    for item in query.files:
        if _stream_key(item.id) == key:
            return item
    return None


def _subtitle_tracks(video: Path) -> tuple[SubtitleTrack, ...]:
    tracks: list[SubtitleTrack] = []
    for index, suffix in enumerate(SUBTITLE_SUFFIXES):
        candidate = video.with_suffix(suffix)
        if not candidate.is_file():
            continue
        tracks.append(
            SubtitleTrack(
                id=f"s{index}",
                label=candidate.name,
                language="zh" if suffix == ".srt" else None,
            )
        )
    return tuple(tracks)


def _srt_to_vtt(text: str) -> str:
    """把 SRT 转成 WebVTT: 只改时间戳分隔符并补文件头, 不解析样式标签."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if "-->" in stripped and "," in stripped:
            before, _, after = stripped.partition("-->")
            lines.append(f"{before.strip().replace(',', '.')} --> {after.strip().replace(',', '.')}")
        else:
            lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines)


class LocalFileProvider(PlaybackProvider):
    """把条目索引到的本机文件逐条列为可播流."""

    async def probe(self, query: PlaybackQuery) -> tuple[PlaybackOffer, ...]:
        if not query.files:
            return ()
        offers: list[PlaybackOffer] = []
        for item in query.files:
            path = Path(item.path)
            content_type = _content_type(path)
            name = path.name
            if content_type is None:
                offers.append(
                    PlaybackOffer(
                        key=_stream_key(item.id),
                        name=name,
                        content_type="video/mp4",
                        seekable=False,
                        unavailable=KNOWN_UNPLAYABLE.get(path.suffix.casefold(), "浏览器无法解码此格式"),
                    )
                )
                continue
            unavailable: str | None = None
            if not path.is_file():
                unavailable = "文件不在磁盘上"
            elif item.size is not None and item.size <= 0:
                unavailable = "文件为空"
            offers.append(
                PlaybackOffer(
                    key=_stream_key(item.id),
                    name=name,
                    content_type=content_type,
                    seekable=True,
                    unavailable=unavailable,
                    subtitles=_subtitle_tracks(path) if unavailable is None else (),
                )
            )
        return tuple(offers)

    async def resolve(self, query: PlaybackQuery) -> FilePlaybackTarget | None:
        """返回选中那条流对应的本机文件; 没指定时挑第一条可播的."""
        selected = _file_by_key(query, query.selected_key)
        if selected is None:
            for item in query.files:
                if _content_type(Path(item.path)) is not None:
                    selected = item
                    break
        if selected is None:
            return None
        path = Path(selected.path)
        content_type = _content_type(path) or "video/mp4"
        return FilePlaybackTarget(path=path, content_type=content_type, cache_ttl=RESOLVE_TTL_SECONDS)

    async def subtitle(self, query: PlaybackQuery, track_id: str) -> str | None:
        target = _file_by_key(query, query.selected_key)
        if target is None:
            for item in query.files:
                if _content_type(Path(item.path)) is not None:
                    target = item
                    break
        if target is None:
            return None
        video = Path(target.path)
        for index, suffix in enumerate(SUBTITLE_SUFFIXES):
            if f"s{index}" != track_id:
                continue
            candidate = video.with_suffix(suffix)
            if not candidate.is_file():
                return None
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return text if suffix == ".vtt" else _srt_to_vtt(text)
        return None


class Plugin(PlaybackPlugin):
    """本机文件播放源插件."""

    config_model = EmptyPluginConfig

    @classmethod
    def descriptor(cls) -> SourceDescriptor:
        return SourceDescriptor(
            id=SOURCE_ID,
            name="本机文件",
            version="0.1.0",
            api_version="1",
            capabilities=frozenset({SourceCapability.PLAYBACK}),
        )

    def build_playback(self, context: PluginContext, config: EmptyPluginConfig) -> PlaybackProvider:
        return LocalFileProvider()
