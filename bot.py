#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안치롱 뮤직봇 — 디스코드 유튜브 음악 봇

- 유튜브 링크 또는 검색어로 곡을 재생합니다.
- 곡이 끝나면 대기열의 다음 곡을 자동으로 재생합니다.
- 토큰은 같은 폴더의 .env 파일(DISCORD_TOKEN=...)에서만 읽습니다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("musicbot")


# ---------------------------------------------------------------- FFmpeg
def find_ffmpeg() -> str:
    """FFmpeg 실행 파일 경로를 찾는다. (bin/ffmpeg.exe > PATH 순)"""
    env_path = (os.getenv("FFMPEG_PATH") or "").strip().strip('"')
    if env_path and Path(env_path).exists():
        return env_path
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = BASE_DIR / "bin" / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = find_ffmpeg()

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn"


# ---------------------------------------------------------------- yt-dlp
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,          # 재생목록 대신 곡 하나만
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "default_search": "ytsearch1",
    "cachedir": False,
    "source_address": "0.0.0.0",
}

URL_RE = re.compile(r"^(https?://|www\.)", re.IGNORECASE)

# 유튜브가 "로봇인지 확인" 하며 막을 때 쓰는 브라우저 쿠키 파일(선택 사항)
COOKIE_FILE = BASE_DIR / "cookies.txt"


def build_query(text: str) -> str:
    """입력이 링크면 그대로, 아니면 유튜브 검색어로 바꾼다."""
    text = text.strip()
    if URL_RE.match(text):
        return text
    if re.match(r"^(m\.)?(youtube\.com|youtu\.be)/", text, re.IGNORECASE):
        return "https://" + text
    return f"ytsearch1:{text}"


def friendly_error(exc: Exception) -> str:
    """yt-dlp 오류를 한국어 안내로 바꾼다."""
    msg = str(exc)
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return (
            "유튜브가 '로봇인지 확인' 하며 막았어요. "
            "이 폴더에 cookies.txt 를 넣어 주세요 (README 의 '유튜브가 봇으로 의심할 때' 참고)."
        )
    if "Private video" in msg:
        return "비공개 영상이에요."
    if "age" in msg.lower() and "restrict" in msg.lower():
        return "연령 제한 영상이라 재생할 수 없어요."
    if "Video unavailable" in msg or "unavailable" in msg.lower():
        return "재생할 수 없는 영상이에요(삭제·지역 제한 등)."
    return msg.splitlines()[0][:200]


def extract_sync(query: str) -> dict:
    """yt-dlp 추출(블로킹). 별도 스레드에서 호출한다."""
    options = dict(YDL_OPTIONS)
    if COOKIE_FILE.exists():
        options["cookiefile"] = str(COOKIE_FILE)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(build_query(query), download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(friendly_error(exc)) from exc

    if not info:
        raise RuntimeError("영상 정보를 가져오지 못했어요.")

    if info.get("entries") is not None:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("검색 결과가 없어요. 다른 검색어로 시도해 주세요.")
        info = entries[0]

    if not info.get("url"):
        raise RuntimeError("재생 가능한 오디오 주소를 찾지 못했어요. 다른 영상으로 시도해 주세요.")

    return info


def fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_queue_duration(tracks: list) -> str:
    total = sum(t.duration or 0 for t in tracks)
    return fmt_duration(total) if total else "--:--"


# ---------------------------------------------------------------- 곡 정보
@dataclass
class Track:
    title: str
    webpage_url: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    requester: str = "알 수 없음"
    stream_url: Optional[str] = None
    fetched_at: float = field(default=0.0)


# ---------------------------------------------------------------- 플레이어
class MusicPlayer:
    """길드(서버) 하나당 하나씩 만들어지는 재생 담당자."""

    def __init__(self, bot: "MusicBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self._wake = asyncio.Event()
        self._ended = asyncio.Event()
        self._last_error: Optional[Exception] = None
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ 상태
    @property
    def voice(self) -> Optional[discord.VoiceClient]:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = self.bot.loop.create_task(self._run())

    async def announce(self, text: str) -> None:
        channel = self.text_channel
        if channel is None:
            return
        try:
            await channel.send(text)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("메시지 전송 실패: %s", exc)

    # ------------------------------------------------------------ 대기열
    def enqueue(self, track: Track) -> None:
        self.queue.append(track)
        self._wake.set()

    # ------------------------------------------------------------ 재생 루프
    async def _run(self) -> None:
        while True:
            if not self.queue:
                if self.current is not None:
                    self.current = None
                    await self.announce(
                        "🎵 대기열이 비었어요. 다음 곡을 틀려면 `/재생`으로 추가해 주세요!"
                    )
                await self._wake.wait()
                self._wake.clear()
                continue

            track = self.queue.pop(0)
            self.current = track
            self._last_error = None

            if not await self._start_track(track):
                continue

            await self._ended.wait()
            self._ended.clear()

            if self._last_error is not None:
                await self.announce(
                    f"⚠️ 재생 중 문제가 생겨 다음 곡으로 넘어갈게요. ({self._last_error})"
                )

    async def _start_track(self, track: Track) -> bool:
        vc = self.voice
        if vc is None or not vc.is_connected():
            await self.announce("🔌 음성 채널 연결이 끊겨서 재생을 멈췄어요. `/재생`으로 다시 시작해 주세요.")
            self.queue.clear()
            self.current = None
            return False

        try:
            url = await self._stream_url(track)
        except Exception as exc:  # noqa: BLE001 - 사용자에게 그대로 알려준다
            log.warning("[%s] 준비 실패: %s", track.title, exc)
            await self.announce(f"❌ `{track.title}` 준비에 실패했어요. 다음 곡으로 넘어갑니다. ({exc})")
            return False

        try:
            self._ended.clear()  # 이전 곡에서 남은 신호를 지운다
            source = discord.FFmpegPCMAudio(
                url,
                executable=FFMPEG,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
            vc.play(source, after=self._on_audio_end)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 재생 시작 실패: %s", track.title, exc)
            await self.announce(f"❌ `{track.title}` 재생을 시작하지 못했어요. ({exc})")
            return False

        await self.announce(
            f"▶️ **{track.title}** `{fmt_duration(track.duration)}` — 신청: {track.requester}"
        )
        return True

    async def _stream_url(self, track: Track) -> str:
        # 유튜브 스트림 주소는 몇 시간이 지나면 만료되므로 필요할 때 새로 받는다.
        if track.stream_url and time.time() - track.fetched_at < 1800:
            return track.stream_url
        info = await asyncio.to_thread(extract_sync, track.webpage_url)
        track.stream_url = info["url"]
        track.fetched_at = time.time()
        if not track.duration and info.get("duration"):
            track.duration = info.get("duration")
        return track.stream_url

    def _on_audio_end(self, error: Optional[Exception]) -> None:
        """FFmpeg 재생이 끝났을 때(어느 스레드에서든) 호출된다."""
        if error:
            log.warning("재생 오류: %s", error)
        self._last_error = error
        try:
            self.bot.loop.call_soon_threadsafe(self._ended.set)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 조작
    async def skip(self) -> bool:
        vc = self.voice
        if vc is None or not vc.is_playing() and self.current is None:
            return False
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # after 콜백이 불려 다음 곡으로 넘어간다
            return True
        return False

    async def stop(self) -> None:
        self.queue.clear()
        self.current = None
        vc = self.voice
        if vc is not None:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect(force=True)
        self._ended.set()


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # voice_states, guilds 포함 / message_content 불필요
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, MusicPlayer] = {}

    async def setup_hook(self) -> None:
        # 슬래시 명령어를 서버에 바로 등록(전역 등록은 최대 1시간 걸릴 수 있음)
        self.tree.on_error = self.on_app_command_error  # type: ignore[assignment]

    async def on_ready(self) -> None:
        log.info("로그인 완료: %s (서버 %d개)", self.user, len(self.guilds))
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("'%s' 서버에 명령어 %d개 등록", guild.name, len(synced))
            except discord.HTTPException as exc:
                log.warning("'%s' 서버 명령어 등록 실패: %s", guild.name, exc)
        log.info("준비 완료! 디스코드에서 /재생 을 입력해 보세요.")

    def player(self, guild: discord.Guild) -> MusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(self, guild)
        return self.players[guild.id]

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("명령어 오류", exc_info=error)
        message = f"⚠️ 문제가 발생했어요: {error}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


bot = MusicBot()


# ---------------------------------------------------------------- 공통 헬퍼
async def require_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """명령을 쓴 사람이 음성 채널에 있는지 확인하고, 봇을 그 채널에 연결한다."""
    if interaction.guild is None:
        await interaction.followup.send("서버 안에서만 쓸 수 있는 명령어예요.", ephemeral=True)
        return None

    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        await interaction.followup.send(
            "먼저 음성 채널에 들어간 다음 명령어를 써 주세요.", ephemeral=True
        )
        return None

    channel = member.voice.channel
    player = bot.player(interaction.guild)
    player.text_channel = interaction.channel
    player.start()

    vc = interaction.guild.voice_client
    if vc is None:
        try:
            vc = await channel.connect(self_deaf=True)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "음성 채널에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
            )
            return None
        except discord.ClientException as exc:
            await interaction.followup.send(f"음성 채널 연결 실패: {exc}", ephemeral=True)
            return None
    elif vc.channel != channel:
        await vc.move_to(channel)

    # --- @everyone 이 채널에 Speak 권한이 있는지 확인
    me = interaction.guild.me
    perms = channel.permissions_for(me)
    if not perms.connect or not perms.speak:
        await interaction.followup.send(
            f"`{channel.name}` 채널에서 말하기 권한이 없어요. 채널 설정에서 봇 권한을 확인해 주세요.",
            ephemeral=True,
        )
        return None

    return vc


# ---------------------------------------------------------------- 명령어
@bot.tree.command(name="재생", description="유튜브 링크나 검색어로 곡을 대기열에 추가해요")
@app_commands.describe(검색어="유튜브 링크 또는 곡 제목 / 가수")
async def play(interaction: discord.Interaction, 검색어: str) -> None:
    await interaction.response.defer()
    vc = await require_voice(interaction)
    if vc is None:
        return

    player = bot.player(interaction.guild)
    try:
        info = await asyncio.to_thread(extract_sync, 검색어)
    except Exception as exc:  # noqa: BLE001
        log.warning("검색 실패(%s): %s", 검색어, exc)
        await interaction.followup.send(f"❌ `{검색어}` 를 찾지 못했어요. ({exc})")
        return

    track = Track(
        title=info.get("title") or "제목 없음",
        webpage_url=info.get("webpage_url") or info.get("original_url") or 검색어,
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        requester=getattr(interaction.user, "display_name", interaction.user.name),
        stream_url=info.get("url"),
        fetched_at=time.time(),
    )
    player.enqueue(track)

    if player.current is not None or vc.is_playing() or len(player.queue) > 1:
        position = len(player.queue) + (1 if player.current else 0)
        embed = discord.Embed(
            title="➕ 대기열에 추가했어요",
            description=f"**{track.title}**",
            color=0x5865F2,
        )
        embed.add_field(name="길이", value=fmt_duration(track.duration), inline=True)
        embed.add_field(name="순서", value=f"{position}번째", inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="스킵", description="지금 나오는 곡을 건너뛰고 다음 곡을 틀어요")
async def skip(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 쓸 수 있어요.", ephemeral=True)
        return
    player = bot.player(interaction.guild)
    player.text_channel = interaction.channel
    if await player.skip():
        await interaction.response.send_message("⏭️ 다음 곡으로 넘어갈게요.")
    else:
        await interaction.response.send_message("지금 재생 중인 곡이 없어요.", ephemeral=True)


@bot.tree.command(name="일시정지", description="지금 나오는 곡을 잠깐 멈춰요")
async def pause(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc is not None and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 일시정지했어요. `/재개` 로 다시 재생할 수 있어요.")
    else:
        await interaction.response.send_message("지금 재생 중인 곡이 없어요.", ephemeral=True)


@bot.tree.command(name="재개", description="멈춰 둔 곡을 다시 재생해요")
async def resume(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc is not None and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 다시 재생할게요.")
    else:
        await interaction.response.send_message("멈춰 있는 곡이 없어요.", ephemeral=True)


@bot.tree.command(name="정지", description="재생을 멈추고 대기열을 비우며 음성 채널에서 나가요")
async def stop(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 쓸 수 있어요.", ephemeral=True)
        return
    player = bot.player(interaction.guild)
    if interaction.guild.voice_client is None and not player.queue and player.current is None:
        await interaction.response.send_message("이미 재생 중인 곡이 없어요.", ephemeral=True)
        return
    await player.stop()
    await interaction.response.send_message("⏹️ 재생을 멈추고 음성 채널에서 나왔어요.")


@bot.tree.command(name="대기열", description="대기열에 있는 곡 목록을 보여줘요")
async def queue_cmd(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 쓸 수 있어요.", ephemeral=True)
        return
    player = bot.player(interaction.guild)

    if player.current is None and not player.queue:
        await interaction.response.send_message("대기열이 비어 있어요. `/재생` 으로 곡을 추가해 주세요.", ephemeral=True)
        return

    lines: list[str] = []
    if player.current:
        lines.append(f"**지금 재생 중** · {player.current.title} `{fmt_duration(player.current.duration)}`")

    if player.queue:
        lines.append("")
        lines.append("**다음 곡**")
        for idx, track in enumerate(player.queue[:10], start=1):
            lines.append(f"`{idx}.` {track.title} `{fmt_duration(track.duration)}`")
        if len(player.queue) > 10:
            lines.append(f"… 외 {len(player.queue) - 10}곡")

    embed = discord.Embed(
        title="🎶 대기열",
        description="\n".join(lines),
        color=0x5865F2,
    )
    embed.set_footer(text=f"총 {len(player.queue)}곡 · 남은 재생 시간 {fmt_queue_duration(player.queue)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="현재곡", description="지금 재생 중인 곡을 알려줘요")
async def now_playing(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 쓸 수 있어요.", ephemeral=True)
        return
    player = bot.player(interaction.guild)
    track = player.current

    if track is None:
        await interaction.response.send_message("지금 재생 중인 곡이 없어요. `/재생` 으로 곡을 추가해 주세요.", ephemeral=True)
        return

    embed = discord.Embed(title="🎧 지금 재생 중", description=f"**{track.title}**", color=0x57F287)
    embed.add_field(name="길이", value=fmt_duration(track.duration), inline=True)
    embed.add_field(name="신청", value=track.requester, inline=True)
    embed.add_field(name="대기열", value=f"{len(player.queue)}곡", inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    if track.webpage_url:
        embed.url = track.webpage_url
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------- 실행
def main() -> int:
    if not TOKEN:
        print("\n[!] .env 파일에 DISCORD_TOKEN 이 없습니다.")
        print("    실행하기.bat 를 다시 실행하면 봇 토큰을 입력할 수 있습니다.")
        print("    또는 같은 폴더의 .env 파일에 아래처럼 한 줄을 넣어 주세요:")
        print("        DISCORD_TOKEN=여기에_봇_토큰\n")
        return 1

    if shutil.which(FFMPEG) is None and not Path(FFMPEG).exists():
        print("\n[!] FFmpeg 를 찾지 못했습니다. 소리가 나오지 않습니다.")
        print("    실행하기.bat 를 다시 실행하면 자동으로 설치를 안내합니다.\n")

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        print("\n[!] 봇 토큰이 올바르지 않습니다. Discord Developer Portal > Bot > Reset Token 에서")
        print("    새 토큰을 발급해 .env 파일의 DISCORD_TOKEN 을 바꿔 주세요.\n")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("\n[!] Discord Developer Portal > Bot 에서 필요한 Intent 를 켜 주세요.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
