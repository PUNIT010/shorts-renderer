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
XF = 0.45                                  # transition length (seconds)
VOICE = os.getenv("VOICE", "hi-IN-SwaraNeural")
RATE = os.getenv("RATE", "+10%")           # slightly faster speech = faster pacing
SHOTS = max(1, int(os.getenv("SHOTS_PER_SCENE", "3")))
MOCK = os.getenv("MOCK") == "1"
FONT = os.getenv("CAPTION_FONT", "Noto Sans Devanagari")
T0 = time.time()
BUDGET = int(os.getenv("IMAGE_BUDGET", "2700"))  # seconds allowed for AI images (45 min)
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
    if raw.lstrip().startswith("{"):
        data = json.loads(raw)
    else:  # compact format: title###topic###script###headings###visuals
        f = [x.strip() for x in raw.split("###")] + [""] * 5
        data = {"title": f[0], "topic": f[1], "script": f[2],
                "headings": f[3], "visuals": f[4]}
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
            asyncio.run(asyncio.wait_for(_tts(text, path), 90))
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


def rand_pair(n):
    r = random.Random(n * 7919)
    col = lambda: "#%02x%02x%02x" % (r.randint(10, 150), r.randint(10, 150), r.randint(10, 150))
    return col(), col()


SEEN = set()
STATS = {"ok": 0, "fail": 0}
ANGLES = ["wide establishing shot", "close-up detail shot", "dramatic low angle shot",
          "top-down aerial view", "side view with depth", "macro close-up"]


def fetch_image(prompt, path, seed):
    """Patient fetch: Pollinations rate-limits anonymous users, so retry with back-off."""
    if MOCK:
        return False
    import hashlib
    import requests
    from PIL import Image
    models = ["flux", "turbo", "flux"]
    for attempt in range(7):
        if time.time() - T0 > BUDGET:
            print("image budget used up", flush=True)
            break
        model = models[attempt % len(models)]
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
               f"?width=768&height=1344&seed={seed + attempt * 101}&nologo=true&model={model}")
        try:
            r = requests.get(url, timeout=150)
            ok = (r.status_code == 200 and len(r.content) > 15000
                  and r.headers.get("content-type", "").startswith("image"))
            if ok:
                h = hashlib.md5(r.content).hexdigest()
                if h in SEEN:       # same picture again = placeholder / cache, not a new image
                    print("duplicate image, retrying", flush=True)
                else:
                    SEEN.add(h)
                    Path(path).write_bytes(r.content)
                    im = Image.open(path).convert("RGB")
                    w, hh = im.size
                    im.crop((0, 0, w, int(hh * 0.92))).save(path, quality=93)  # drop corner watermark
                    STATS["ok"] += 1
                    time.sleep(4)    # be polite to the free service
                    return True
            else:
                print("image status", r.status_code, flush=True)
        except Exception as e:
            print("image retry:", e, flush=True)
        time.sleep(min(10 + attempt * 10, 60))
    STATS["fail"] += 1
    return False


def motion(idx, frames):
    """Different camera move for every shot: zoom in/out and slow pans."""
    k = idx % 6
    zin = f"min(1.0+0.0016*on,1.28)"
    zout = f"max(1.28-0.0016*on,1.0)"
    if k == 0:
        return zin, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if k == 1:
        return "1.22", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    if k == 2:
        return zout, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if k == 3:
        return "1.22", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"
    if k == 4:
        return "1.22", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames}"
    return "1.22", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{frames})"


def ken_burns(img, dur, idx, out, flip=False):
    frames = int(round((dur + XF) * FPS))
    z, x, y = motion(idx, frames)
    vf = (("hflip," if flip else "") +
          "scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
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

        scene_imgs = []
        for j in range(SHOTS):
            n = len(clips)
            k = i * SHOTS + j
            base = (visuals[k] if k < len(visuals)
                    else f"{visuals[i] if i < len(visuals) else (topic or heads[i] or 'abstract scene')}, "
                         f"{ANGLES[(k + j) % len(ANGLES)]}")
            prompt = (f"{base}, vertical 9:16 cinematic illustration, vibrant colors, "
                      "dramatic lighting, highly detailed, no text, no letters, no watermark")[:380]
            img = TMP / f"img_{k}.jpg"
            if fetch_image(prompt, img, random.randint(1, 10**6)):
                scene_imgs.append(img)
            elif scene_imgs:
                img = scene_imgs[-1]          # reuse previous picture of this scene, new camera move
            else:
                gradient(img, *rand_pair(k))
            d = d_scene / SHOTS
            clip = TMP / f"clip_{n}.mp4"
            ken_burns(str(img), d, n, str(clip), flip=(img in scene_imgs and j % 2 == 1))
            clips.append(clip)
            shot_durs.append(d)
            print(f"shot {n + 1}/{len(parts) * SHOTS} done, images ok={STATS['ok']} "
                  f"fail={STATS['fail']} t={int(time.time() - T0)}s", flush=True)

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
            "scenes": len(parts), "shots": n,
            "images_ok": STATS["ok"], "images_failed": STATS["fail"]}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("DONE", meta)


if __name__ == "__main__":
    main()
