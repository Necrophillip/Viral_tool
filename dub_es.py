import subprocess
import asyncio
from pathlib import Path
import shutil

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

def _cb(status_cb, msg):
    if status_cb:
        status_cb(msg)

def transcribe_and_translate(audio_path: Path, status_cb=None) -> list[dict]:
    """
    Transcribe con mlx-whisper + traduce cada segmento EN→ES.
    Retorna: [{start, end, text_en, text_es}, ...]
    """
    import mlx_whisper
    from deep_translator import GoogleTranslator

    _cb(status_cb, "🎙 Transcribiendo audio (EN)...")
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo="mlx-community/whisper-base-mlx",
        language="en",
        word_timestamps=True,
    )

    _cb(status_cb, "🌍 Traduciendo segmentos EN→ES...")
    translator = GoogleTranslator(source="en", target="es")
    segments = []

    for seg in result.get("segments", []):
        text_en = seg.get("text", "").strip()
        if not text_en:
            continue
        # Traducción por segmento
        text_es = translator.translate(text_en)
        segments.append({
            "start": seg["start"],
            "end":   seg["end"],
            "text_en": text_en,
            "text_es": text_es
        })

    return segments

def dub_audio(video_path: Path, segments: list[dict],
              voice="es-MX-JorgeNeural", status_cb=None) -> Path:
    """
    Genera TTS en español y lo mezcla sobre el video.
    Retorna: Path al video con audio en español (sin subtítulos).
    Output: dub_<stem>.mp4
    """
    import edge_tts

    _cb(status_cb, "🔊 Generando audio neural en español...")

    stem_parts = video_path.stem.split('_')
    vid_id = stem_parts[-1]

    out_path = video_path.parent / f"dub_{vid_id}.mp4"
    if video_path.stem.startswith("clean_916_"):
        out_path = video_path.parent / f"dub_916_{vid_id}.mp4"

    tts_path = video_path.parent / f"tts_es_{vid_id}.mp3"

    full_text = " ".join(s["text_es"] for s in segments)

    async def _generate_tts():
        comm = edge_tts.Communicate(full_text, voice)
        await comm.save(str(tts_path))

    asyncio.run(_generate_tts())

    _cb(status_cb, "🎬 Mezclando audio con video...")

    cmd = [
        FFMPEG, "-y",
        "-i", video_path.name,
        "-i", tts_path.name,
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        out_path.name
    ]

    subprocess.run(cmd, cwd=str(video_path.parent), capture_output=True, check=True)

    tts_path.unlink(missing_ok=True)

    return out_path

def burn_subtitles(video_path: Path, segments: list[dict],
                   is_916=False, status_cb=None) -> Path:
    """
    Genera .ass con estilo viral y quema los subtítulos en el video.
    Retorna: Path al video con subtítulos quemados.
    Output: sub_<stem>.mp4
    """
    _cb(status_cb, "📝 Generando subtítulos virales...")

    stem_parts = video_path.stem.split('_')
    vid_id = stem_parts[-1]

    out_path = video_path.parent / f"sub_{vid_id}.mp4"
    if video_path.stem.startswith("dub_916_"):
        out_path = video_path.parent / f"sub_dub_916_{vid_id}.mp4"
    elif video_path.stem.startswith("dub_"):
        out_path = video_path.parent / f"sub_dub_{vid_id}.mp4"
    elif video_path.stem.startswith("clean_916_"):
        out_path = video_path.parent / f"sub_916_{vid_id}.mp4"

    subs_path = video_path.parent / f"subs_{vid_id}.ass"

    ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Viral,Arial,{fontsize},&H00FFFFFF,&H00000000,&H80000000,-1,0,1,3,2,{align},{margin}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if is_916:
        fontsize = 55
        res_x = 1080
        res_y = 1920
        align = 2
        margin = 80
    else:
        fontsize = 52
        res_x = 1920
        res_y = 1080
        align = 2
        margin = 60

    header = ASS_HEADER.format(
        res_x=res_x, res_y=res_y, fontsize=fontsize, align=align, margin=margin
    )

    def chunk_for_viral(text: str, max_words: int = 4) -> str:
        """Divide texto en máx 4 palabras/línea, máx 2 líneas, MAYÚSCULAS."""
        words = text.upper().split()
        lines = []
        while words and len(lines) < 2:
            lines.append(" ".join(words[:max_words]))
            words = words[max_words:]
        return r"\N".join(lines)

    def ts(sec: float) -> str:
        h, m = int(sec // 3600), int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    events = []
    for seg in segments:
        start = ts(seg["start"])
        end = ts(seg["end"])
        text = chunk_for_viral(seg["text_es"])
        events.append(f"Dialogue: 0,{start},{end},Viral,,0,0,0,,{text}")

    ass_content = header + "\n".join(events) + "\n"
    subs_path.write_text(ass_content, encoding="utf-8")

    _cb(status_cb, "🔥 Quemando subtítulos en el video...")

    cmd = [
        FFMPEG, "-y",
        "-i", video_path.name,
        "-vf", f"subtitles={subs_path.name}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        out_path.name
    ]

    subprocess.run(cmd, cwd=str(video_path.parent), capture_output=True, check=True)

    subs_path.unlink(missing_ok=True)

    return out_path

def dub_and_subtitle(video_path: Path, out_dir: Path, segments=None, voice="es-MX-JorgeNeural",
                     do_dub=True, do_sub=True, is_916=False, status_cb=None) -> Path:

    if not do_dub and not do_sub:
        return video_path

    stem_parts = video_path.stem.split('_')
    vid_id = stem_parts[-1]

    wav_path = out_dir / f"temp_audio_{vid_id}.wav"
    _cb(status_cb, "🎵 Extrayendo audio...")
    subprocess.run([
        FFMPEG, "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(wav_path)
    ], capture_output=True, check=True)

    try:
        if segments is None:
            segments = transcribe_and_translate(wav_path, status_cb)

        if not segments:
            raise RuntimeError("No speech detected or translation failed")

        current_video = video_path

        if do_dub:
            current_video = dub_audio(current_video, segments, voice=voice, status_cb=status_cb)

        if do_sub:
            current_video = burn_subtitles(current_video, segments, is_916=is_916, status_cb=status_cb)

        if current_video != video_path:
            video_path.unlink(missing_ok=True)

            if do_dub and do_sub:
                inter_video = current_video.parent / current_video.name.replace("sub_", "")
                if inter_video != current_video and inter_video.exists():
                    inter_video.unlink(missing_ok=True)

        return current_video

    finally:
        wav_path.unlink(missing_ok=True)
