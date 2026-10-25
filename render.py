#!/usr/bin/env python3
"""
Free Shorts renderer (no paid APIs).

Hindi voice  : edge-tts (Microsoft Edge voices, no key)
AI visuals   : Pollinations (no key)  -> falls back to colour gradients if unavailable
Animation    : FFmpeg Ken Burns zoom + animated scene transitions + animated captions
Output       : out/short.mp4  (1080x1920, 30 fps) and out/meta.json

Input        : JSON in env PAYLOAD (or a file path as argv[1])
  {"title": "...", "topic": "...",
   "script": "part1 | part2 | part3 | part4",      (Hindi narration, parts split by |)
   "headings": "h1 | h2 | h3 | h4",                  (short on-screen headings)
   "visuals": "english prompt 1 | ... | 4"}          (optional, English image prompts)
Set MOCK=1 to test the FFmpeg pipeline offline (sine tones + gradient images).
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

W, H, FPS = 1080, 1920, 30
XF = 0.5                                   # transition length (seconds)
VOICE = os.getenv("VOICE", "hi-IN-SwaraNeural")
RATE = os.getenv("RATE", "+10%")           # slightly faster speech = faster pacing
SHOTS = max(1, int(os.getenv("SHOTS_PER_SCENE", "2")))
MOCK = os.getenv("MOCK") == "1"
FONT = os.getenv("CAPTION_FONT", "Noto Sans Devanagari")
OUT, TMP = Path("out"), Path("tmp")

TRANSITIONS = ["slideleft", "circleopen", "wipeup", "zoomin",
               "slideright", "fadeblack", "radial", "wipedown"]
PALETTE = [("#0B1D51", "#7A1F5C"), ("#7A1F5C", "#0F5C4D"),
           ("#0F5C4D", "#8A3B12"), ("#8A3B12", "#0B1D51")]


def run(cmd):
    print("+", " ".join(str(c) for c in cmd)[:200], flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def load_payload():
    raw = os.getenv("PAYLOAD")
    if not raw and len(sys.argv) > 1:
        raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    if not raw:
        sys.exit("No payload: set PAYLOAD or pass a JSON file path.")
    data = json.loads(raw)
    parts = [p.strip() for p in data["script"].split("|") if p.strip()]
    heads = [p.strip() for p in data.get("headings", "").split("|")]
    heads += [""] * (len(parts) - len(heads))
    visuals = [p.strip() for p in data.get("visuals", "").split("|") if p.strip()]
    return data, parts, heads, visuals


# ---------- voice ----------
async def _tts(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))


def make_voice(text, path, idx):
    if MOCK:
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"sine=frequency={300 + 80 * idx}:duration={5 + idx}", "-q:a", "5", path])
        return
    for attempt in range(3):
        try:
            asyncio.run(_tts(text, path))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return
        except Exception as e:  # network hiccup
            print("TTS retry:", e)
        time.sleep(3)
    sys.exit("Voice generation failed.")


# ---------- visuals ----------
def gradient(path, c1, c2):
    from PIL import Image, ImageColor
    g = Image.linear_gradient("L").resize((720, 1280))
    a = Image.new("RGB", (720, 1280), ImageColor.getrgb(c1))
    b = Image.new("RGB", (720, 1280), ImageColor.getrgb(c2))
    Image.composite(b, a, g).save(path, quality=92)


def fetch_image(prompt, path, seed):
    if MOCK:
        return False
    import requests
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           f"?width=720&height=1280&seed={seed}&nologo=true&model=flux")
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=120)
            if (r.status_code == 200 and len(r.content) > 10000
                    and r.headers.get("content-type", "").startswith("image")):
                Path(path).write_bytes(r.content)
                return True
            print("image status", r.status_code)
        except Exception as e:
            print("image retry:", e)
        time.sleep(8 * (attempt + 1))
    return False


def ken_burns(img, dur, idx, out):
    frames = int(round((dur + XF) * FPS))
    z = "min(1.0+0.0012*on,1.25)" if idx % 2 == 0 else "max(1.25-0.0012*on,1.0)"
    vf = (f"scale=2160:-1,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", img, "-vf", vf,
         "-frames:v", frames, "-r", FPS, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", out])


# ---------- captions ----------
def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(t):
    return t.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_ass(scenes, path):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Cap,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,8,0,2,60,60,520,1",
        f"Style: Head,{FONT},84,&H0000E1FF,&H0000E1FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,0,8,60,60,230,1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for s in scenes:
        t0, t1 = s["start"], s["start"] + s["dur"]
        if s["head"]:
            lines.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Head,,0,0,0,,"
                         f"{{\\fad(300,300)}}{clean(s['head'])}")
        words = clean(s["text"]).split()
        chunks = [" ".join(words[i:i + 3]) for i in range(0, len(words), 3)] or [""]
        total = sum(len(c) for c in chunks) or 1
        t = t0
        for c in chunks:
            d = s["dur"] * len(c) / total
            lines.append(f"Dialogue: 1,{ass_time(t)},{ass_time(t + d)},Cap,,0,0,0,,"
                         f"{{\\fscx85\\fscy85\\t(0,120,\\fscx100\\fscy100)}}{c}")
            t += d
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- main ----------
def main():
    data, parts, heads, visuals = load_payload()
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    topic = data.get("topic", "")

    scenes, clips, shot_durs = [], [], []
    cursor = 0.0
    for i, text in enumerate(parts):
        voice = TMP / f"voice_{i}.mp3"
        make_voice(text, voice, i)
        d_scene = duration(voice)
        scenes.append({"text": text, "head": heads[i], "start": cursor,
                       "dur": d_scene, "voice": voice})
        cursor += d_scene

        base = visuals[i] if i < len(visuals) else (topic or heads[i] or "abstract background")
        for j in range(SHOTS):
            n = len(clips)
            img = TMP / f"img_{n}.jpg"
            style = "wide establishing shot" if j % 2 == 0 else "dramatic close-up detail"
            prompt = (f"{base}, {style}, vertical 9:16 cinematic illustration, vibrant colors, "
                      "dramatic lighting, highly detailed, no text, no watermark")[:380]
            if not fetch_image(prompt, img, random.randint(1, 10**6)):
                gradient(img, *PALETTE[(i + j) % len(PALETTE)])
            d = d_scene / SHOTS
            clip = TMP / f"clip_{n}.mp4"
            ken_burns(str(img), d, n, str(clip))
            clips.append(clip)
            shot_durs.append(d)

    # animated transitions between every shot
    n = len(clips)
    if n == 1:
        video = clips[0]
    else:
        inputs, filt, prev, offset = [], "", "0:v", 0.0
        for c in clips:
            inputs += ["-i", c]
        for k in range(1, n):
            offset += shot_durs[k - 1]
            tr = TRANSITIONS[(k - 1) % len(TRANSITIONS)]
            filt += f"[{prev}][{k}:v]xfade=transition={tr}:duration={XF}:offset={offset:.3f}[v{k}];"
            prev = f"v{k}"
        video = TMP / "joined.mp4"
        run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", filt.rstrip(";"),
             "-map", f"[{prev}]", "-r", FPS, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", video])

    # voice track
    lst = TMP / "voices.txt"
    lst.write_text("".join(f"file '{s['voice'].resolve()}'\n" for s in scenes))
    audio = TMP / "voice.m4a"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c:a", "aac", "-b:a", "160k", audio])

    # captions + final mux
    ass = TMP / "subs.ass"
    build_ass(scenes, ass)
    final = OUT / "short.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", audio,
         "-vf", f"ass={ass}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest",
         "-movflags", "+faststart", final])

    meta = {"title": data.get("title", ""), "duration": round(duration(final), 2),
            "scenes": len(parts), "shots": n}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("DONE", meta)


if __name__ == "__main__":
    main()
