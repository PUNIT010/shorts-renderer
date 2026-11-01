#!/usr/bin/env python3
"""Quality gate for the script Make returns. Exit 0 = good, 1 = ask Make again."""
import re
import sys

raw = open(sys.argv[1], encoding="utf-8").read()
f = [x.strip() for x in raw.split("###")] + [""] * 8
title, topic, script, heads, visuals = f[:5]
parts = [p for p in script.split("|") if p.strip()]
vis = [v for v in visuals.split("|") if v.strip()]
words = len(script.replace("|", " ").split())
deva = len(re.findall(r"[ऀ-ॿ]", script))
letters = len(re.findall(r"[A-Za-zऀ-ॿ]", script)) or 1
problems = []
if deva / letters < 0.8:
    problems.append("script is not mostly Devanagari Hindi")
if not re.search(r"[ऀ-ॿ]", title):
    problems.append("title is not Hindi")
if not 3 <= len(parts) <= 7:
    problems.append(f"{len(parts)} script parts (need 3-7)")
if not 30 <= words <= 140:
    problems.append(f"{words} words (need 30-140)")
if len(vis) < 4:
    problems.append(f"only {len(vis)} image prompts (need 4+)")
if problems:
    print("QUALITY GATE FAILED:", "; ".join(problems))
    sys.exit(1)
print(f"quality gate ok: {len(parts)} parts, {words} words, {len(vis)} visuals")
