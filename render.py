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
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

W, H, FPS = 1080, 1920, 30
XF = 0.45                                  # transition length (seconds)
VOICE = os.getenv("VOICE", "hi-IN-SwaraNeural")
RATE = os.getenv("RATE", "+10%")           # slightly faster speech = faster pacing
MAX_SCENES = max(1, int(os.getenv("MAX_SCENES", "4")))
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
    if len(parts) > MAX_SCENES:                       # AI sometimes writes 6-7 parts: merge to 4
        q, r = divmod(len(parts), MAX_SCENES)
        sizes = [q + (1 if i < r else 0) for i in range(MAX_SCENES)]
        mp, mh, at = [], [], 0
        for sz in sizes:
            mp.append(" ".join(parts[at:at + sz]))
            mh.append(heads[at])
            at += sz
        parts, heads = mp, mh
    visuals = [p.strip() for p in data.get("visuals", "").split("|") if p.strip()]
    return data, parts, heads, visuals


# ---------- voice ----------
async def _tts(text, path):
    import edge_tts
    try:
        com = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    except TypeError:                      # older edge-tts without the boundary option
        com = edge_tts.Communicate(text, VOICE, rate=RATE)
    words = []
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7))
    return words


def make_voice(text, path, idx):
    """Returns word (start, end) times in seconds, or [] when the voice gives none."""
    if MOCK:
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"sine=frequency={300 + 80 * idx}:duration={5 + idx}", "-q:a", "5", path])
        return []
    for attempt in range(3):
        try:
            words = asyncio.run(asyncio.wait_for(_tts(text, path), 90))
            if Path(path).exists() and Path(path).stat().st_size > 1000:
                return words
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


STYLES = ["cinematic digital illustration, teal and orange colour palette, soft volumetric light",
          "vibrant flat vector illustration, bold shapes, purple and gold colour palette",
          "semi-realistic 3D render, warm sunset colour palette, shallow depth of field",
          "dramatic neon cyberpunk illustration, magenta and cyan colour palette",
          "clean modern isometric illustration, bright blue and white colour palette"]
STYLE = STYLES[0]
SEEN = set()
STATS = {"ok": 0, "fail": 0, "clips": 0}
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


PIXABAY = os.getenv("PIXABAY_API_KEY", "")
USED_CLIPS = set()
STOP = set("a an the of in on at to with and or for from by is are over under into near "
           "vivid scene shot view angle close up wide camera cinematic family friendly "
           "image showing clearly different people person".split())


def keywords(prompt):
    return [w for w in re.findall(r"[a-z]+", prompt.lower()) if w not in STOP and len(w) > 2]


def fetch_clip(prompt, path):
    """Real stock footage from Pixabay (free key). Returns True if a clip was saved."""
    if MOCK or not PIXABAY:
        return False
    import requests
    kw = keywords(prompt)
    tries = [" ".join(kw[:3]), " ".join(kw[:2]), kw[0] if kw else "", "technology", "city"]
    for q in dict.fromkeys(x for x in tries if x):
        try:
            r = requests.get("https://pixabay.com/api/videos/", timeout=40,
                             params={"key": PIXABAY, "q": q, "per_page": 20, "safesearch": "true"})
            if r.status_code != 200:
                print("pixabay status", r.status_code, r.text[:120], flush=True)
                continue
            hits = [h for h in r.json().get("hits", []) if h["id"] not in USED_CLIPS]
            if not hits:
                continue
            h = random.choice(hits[:8])
            v = h["videos"]
            url = (v.get("large") or {}).get("url") or (v.get("medium") or {}).get("url")
            if not url:
                continue
            data = requests.get(url, timeout=180).content
            if len(data) < 50000:
                continue
            Path(path).write_bytes(data)
            USED_CLIPS.add(h["id"])
            STATS["clips"] += 1
            print(f"pixabay clip '{q}' id={h['id']}", flush=True)
            return True
        except Exception as e:
            print("pixabay retry:", e, flush=True)
    return False


def stock_clip(src, dur, out):
    """Cut/loop a stock clip to the shot length, fill 9:16 by centre-crop."""
    run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", src,
         "-t", f"{dur + XF:.3f}", "-vf",
         f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},format=yuv420p",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out])


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


# ---------- sound ----------
def make_music(dur, out):
    """Soft ambient pad generated in code (no licence needed)."""
    freqs = [110.0, 164.81, 220.0, 261.63]
    ins = []
    for f in freqs:
        ins += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={dur:.2f}"]
    fc = ("[0][1][2][3]amix=inputs=4:normalize=0,tremolo=f=0.3:d=0.4,lowpass=f=900,volume=2.5,"
          f"afade=t=in:d=1.5,afade=t=out:st={max(dur - 1.5, 0):.2f}:d=1.5[a]")
    run(["ffmpeg", "-y", "-loglevel", "error", *ins, "-filter_complex", fc, "-map", "[a]",
         "-c:a", "aac", "-b:a", "128k", out])


def make_sfx(times, dur, out):
    """A short whoosh at every scene change."""
    if not times:
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"anullsrc=r=44100:cl=stereo", "-t", f"{dur:.2f}", "-c:a", "aac", out])
        return
    n = len(times)
    fc = ("anoisesrc=d=0.6:c=pink:a=0.6,highpass=f=500,afade=t=in:d=0.2,afade=t=out:st=0.2:d=0.4,"
          f"volume=0.7,asplit={n}" + "".join(f"[w{i}]" for i in range(n)) + ";")
    for i, t in enumerate(times):
        ms = max(int((t - 0.15) * 1000), 0)
        fc += f"[w{i}]adelay={ms}|{ms}[d{i}];"
    fc += "".join(f"[d{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0,apad=whole_dur={dur:.2f}[a]"
    run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex", fc, "-map", "[a]",
         "-t", f"{dur:.2f}", "-c:a", "aac", "-b:a", "128k", out])


# ---------- captions ----------
def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(t):
    return t.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def word_times(scene):
    """(start, end) per spoken word, relative to the scene. Uses the voice's own timings when they
    match the script word-for-word, otherwise spreads the time by word length."""
    toks = clean(scene["text"]).split()
    w, dur = scene.get("words") or [], scene["dur"]
    if toks and len(w) == len(toks):
        starts = [x[0] for x in w]
        ends = starts[1:] + [dur]
        return toks, list(zip(starts, ends))
    total = sum(len(t) for t in toks) or 1
    out, t = [], 0.0
    for tok in toks:
        d = dur * len(tok) / total
        out.append((t, t + d))
        t += d
    return toks, out


def build_ass(scenes, path, total):
    hi, lo = "{\\c&H00E1FF&}", "{\\c&HFFFFFF&}"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Cap,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,8,0,2,60,60,520,1",
        f"Style: Head,{FONT},84,&H0000E1FF,&H0000E1FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,0,8,60,60,230,1",
        f"Style: Hook,{FONT},120,&H0000E1FF,&H0000E1FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,9,0,8,50,50,200,1",
        f"Style: End,{FONT},100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,9,0,5,60,60,0,1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for s in scenes:
        t0 = s["start"]
        t1 = t0 + s["dur"]
        if s["head"]:
            first = s["idx"] == 0
            end = min(t0 + 2.6, t1) if first else t1       # opening hook: big, first 2.6 s only
            fx = "{\\fad(150,300)\\fscx70\\fscy70\\t(0,250,\\fscx100\\fscy100)}" if first else "{\\fad(300,300)}"
            lines.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(end)},{'Hook' if first else 'Head'},,0,0,0,,"
                         f"{fx}{clean(s['head'])}")
        toks, times = word_times(s)
        for c0 in range(0, len(toks), 3):
            group = list(range(c0, min(c0 + 3, len(toks))))
            for pos, j in enumerate(group):
                a_, b_ = times[j]
                text = " ".join((hi + toks[k] + lo) if k == j else toks[k] for k in group)
                pop = "{\\fscx85\\fscy85\\t(0,120,\\fscx100\\fscy100)}" if pos == 0 else ""
                lines.append(f"Dialogue: 1,{ass_time(t0 + a_)},{ass_time(t0 + b_)},Cap,,0,0,0,,{pop}{text}")
    lines.append(f"Dialogue: 2,{ass_time(max(total - 2.3, 0))},{ass_time(total)},End,,0,0,0,,"
                 "{\\fad(300,200)}फॉलो करें, रोज़ नया सीखें")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- main ----------
def main():
    data, parts, heads, visuals = load_payload()
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    topic = data.get("topic", "")
    global STYLE
    STYLE = STYLES[sum(map(ord, data.get("title", "") or topic)) % len(STYLES)]

    scenes, clips, shot_durs = [], [], []
    cursor = 0.0
    for i, text in enumerate(parts):
        voice = TMP / f"voice_{i}.mp3"
        words = make_voice(text, voice, i)
        d_scene = duration(voice)
        scenes.append({"text": text, "head": heads[i], "start": cursor,
                       "dur": d_scene, "voice": voice, "words": words, "idx": i})
        cursor += d_scene

        scene_imgs = []
        for j in range(SHOTS):
            n = len(clips)
            k = i * SHOTS + j
            base = (visuals[k] if k < len(visuals)
                    else f"{visuals[i] if i < len(visuals) else (topic or heads[i] or 'abstract scene')}, "
                         f"{ANGLES[(k + j) % len(ANGLES)]}")
            d = d_scene / SHOTS
            clip = TMP / f"clip_{n}.mp4"
            src = TMP / f"src_{k}.mp4"
            if fetch_clip(base, src):
                stock_clip(str(src), d, str(clip))
            else:
                prompt = (f"{base}, {STYLE}, vertical 9:16, highly detailed, no text, no letters, "
                          "no words, no logo, no watermark")[:380]
                img = TMP / f"img_{k}.jpg"
                if fetch_image(prompt, img, random.randint(1, 10**6)):
                    scene_imgs.append(img)
                elif scene_imgs:
                    img = scene_imgs[-1]
                else:
                    gradient(img, *rand_pair(k))
                ken_burns(str(img), d, n, str(clip), flip=(img in scene_imgs and j % 2 == 1))
            clips.append(clip)
            shot_durs.append(d)
            print(f"shot {n + 1}/{len(parts) * SHOTS} done, images ok={STATS['ok']} "
                  f"fail={STATS['fail']} clips={STATS['clips']} t={int(time.time() - T0)}s", flush=True)

    # animated transitions between every shot
    n = len(clips)
    trans_times = []
    if n == 1:
        video = clips[0]
    else:
        inputs, filt, prev, offset = [], "", "0:v", 0.0
        for c in clips:
            inputs += ["-i", c]
        for k in range(1, n):
            offset += shot_durs[k - 1]
            trans_times.append(offset)
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
    total = duration(video)
    music, sfx = TMP / "music.m4a", TMP / "sfx.m4a"
    make_music(total + 1, music)
    make_sfx(trans_times, total + 1, sfx)

    # captions + progress bar + audio mix (music ducks under the voice) in one final encode
    ass = TMP / "subs.ass"
    build_ass(scenes, ass, total)
    final = OUT / "short.mp4"
    fc = (f"[0:v]ass={ass}[a1];"
          f"color=c=0xFFE100:s={W}x16:r={FPS}:d={total:.2f}[bar];"
          f"[a1][bar]overlay=x='-W+W*t/{total:.2f}':y=0:shortest=1[v];"
          "[1:a]asplit=2[v1][v2];[2:a]volume=0.35[m];"
          "[m][v2]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=400[md];"
          "[v1][md]amix=inputs=2:normalize=0[vm];[vm][3:a]amix=inputs=2:normalize=0[mix];"
          "[mix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", audio, "-i", music, "-i", sfx,
         "-filter_complex", fc, "-map", "[v]", "-map", "[aout]", "-c:v", "libx264",
         "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-t", f"{total:.2f}", "-movflags", "+faststart", final])

    meta = {"title": data.get("title", ""), "duration": round(duration(final), 2),
            "scenes": len(parts), "shots": n,
            "images_ok": STATS["ok"], "images_failed": STATS["fail"], "stock_clips": STATS["clips"]}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("DONE", meta)


if __name__ == "__main__":
    main()
