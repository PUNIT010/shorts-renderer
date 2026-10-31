#!/usr/bin/env python3
"""Convert Make's compact reply (title###topic###script###headings###visuals###description###hashtags)
into a PurffleShorts script.json. Usage: make_script.py content.txt script.json"""
import json
import re
import sys
from pathlib import Path

STOP = set("a an the of in on at to with and or for from by is are over under into near vivid scene shot "
           "view angle close up wide camera cinematic family friendly image showing clearly different "
           "people person no text".split())


def kw(prompt):
    words = [w for w in re.findall(r"[a-z]+", prompt.lower()) if w not in STOP and len(w) > 2]
    return " ".join(words[:3]) or "technology"


raw = Path(sys.argv[1]).read_text(encoding="utf-8")
f = [x.strip() for x in raw.split("###")] + [""] * 8
title, topic, script, heads, visuals, desc, tags = f[:7]
parts = [p.strip() for p in script.split("|") if p.strip()]
vis = [v.strip() for v in visuals.split("|") if v.strip()] or [topic or "abstract background"]
heads = [h.strip() for h in heads.split("|") if h.strip()]

# one scene per sentence so the picture changes every few seconds
sentences = []
for p in parts:
    sentences += [s.strip() for s in re.split(r"(?<=[।.!?])\s+", p) if s.strip()]
scenes = []
for i, s in enumerate(sentences):
    v = vis[i % len(vis)]
    scenes.append({"speaker": "A", "narration": s, "search_query": kw(v), "image_prompt": v,
                   "emphasis": [], "emoji": ""})
hashtags = [t for t in tags.split() if t.startswith("#")]
data = {"topic": topic or title, "title": title[:95], "hook_text": (heads[0] if heads else title)[:40],
        "style": "facts", "language": "hi", "category": "education", "scenes": scenes,
        "description": desc, "hashtags": hashtags,
        "tags": [t.lstrip("#") for t in hashtags] or ["shorts"]}
Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(scenes)} scenes written")
