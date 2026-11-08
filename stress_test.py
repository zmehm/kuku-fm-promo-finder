"""
stress_test.py - Stress tests for the Kuku FM promo finder. Place in the repo root
(next to run_batch.py) and run from there.

    python stress_test.py parser                 # 1. new-input / messy-input parser test (offline)
    python stress_test.py integrity              # 2. timestamp integrity of every promo (offline)
    python stress_test.py adversarial            # 5. hand-built clips vs your quality checker (offline)
    python stress_test.py stability --episodes 1,2,3,4,5   # 3. rerun stability (API calls, pennies)
    python stress_test.py judge --n 10 --repeats 3         # 4. judge noise + discrimination (API calls)
    python stress_test.py recall-make            # 6. builds a sheet of 15 clips for YOU to read
    python stress_test.py recall-score           #    compares your findings with the checker's flags
    python stress_test.py fallback               # 7. --no-llm and bad-model behaviour (API calls)
    python stress_test.py all                    # parser + integrity + adversarial

Results are merged into outputs/stress_test/stress_results.json.
Nothing here edits your pipeline outputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config
from src.clips import (EPS, clip_blob, clip_elements, element_intensity, fmt_ts,
                       render_clip_text, sorted_elements, speakers_in_clip)
from src.schemas import ElementType, ParsedEpisode, PromoCandidate, PromoSegment, PromoType

OUT = config.OUTPUT_DIR / "stress_test"
RESULTS = OUT / "stress_results.json"
FAILURE_CODES = ["context_dependency", "weak_hook", "resolved_cliffhanger", "pronoun_ambiguity",
                 "ad_policy_risk", "hallucinated_claim", "redundancy", "timestamp_imprecision",
                 "genre_mismatch"]


# ── helpers ──────────────────────────────────────────────────────────────
def record(name: str, summary: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    data[name] = summary
    RESULTS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_json(path: Path):
    if not path.exists():
        sys.exit(f"Missing {path}. Run run_batch.py first.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_episodes() -> dict[int, ParsedEpisode]:
    eps: dict[int, ParsedEpisode] = {}
    for p in sorted(config.PARSED_DIR.glob("*.json")):
        try:
            ep = ParsedEpisode(**json.loads(p.read_text(encoding="utf-8")))
            eps[ep.episode_number] = ep
        except Exception:
            pass
    if not eps:
        sys.exit("No parsed episodes in outputs/parsed. Run run_batch.py first.")
    return eps


def cs(x: float) -> int:
    return int(round(x * 100))


def windows(els, min_d=20.0, max_d=60.0):
    """Yield (i, j) element-index windows whose duration is within [min_d, max_d]."""
    for i in range(len(els)):
        for j in range(i, len(els)):
            d = els[j].end_seconds - els[i].start_seconds
            if d > max_d:
                break
            if d >= min_d:
                yield i, j
                break


def make_promo(ep: ParsedEpisode, start: float, end: float, hook: str, cliff: str, pid: str) -> PromoCandidate:
    seg = PromoSegment(episode=ep.episode_number, start_time=fmt_ts(start), end_time=fmt_ts(end),
                       start_seconds=start, end_seconds=end)
    return PromoCandidate(
        promo_id=pid, promo_type=PromoType.SINGLE_EPISODE, episodes=[ep.episode_number],
        segments=[seg], hook=hook[:300].ljust(10, "."), cliffhanger=cliff[:300].ljust(10, "."),
        why_it_works="Synthetic stress-test clip used to validate the checker.",
        characters_in_clip=speakers_in_clip(ep, start, end))


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


# ── 1. parser / new-input test ───────────────────────────────────────────
def t_parser() -> None:
    from src.parser import parse_episode
    files = sorted(config.SCRIPTS_DIR.glob("episode-*.txt"))
    if not files:
        sys.exit(f"No episode-*.txt in {config.SCRIPTS_DIR}. Set SCRIPTS_DIR in .env.")
    src = files[0]
    raw = src.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    base_n = parse_episode(src).total_elements
    rng = random.Random(1)
    lines = text.splitlines()
    shuffled = lines[:]
    rng.shuffle(shuffled)
    names = {"Juliana": "Elara", "JULIANA": "ELARA", "Gerald": "Doran", "GERALD": "DORAN",
             "Alexander": "Kael", "ALEXANDER": "KAEL", "Clarissa": "Mira", "CLARISSA": "MIRA"}
    renamed = text
    for a, b in names.items():
        renamed = renamed.replace(a, b)
    muts: dict[str, tuple[bytes, bool]] = {   # name -> (bytes, should_match_baseline)
        "empty_file": (b"", False),
        "header_only": ("\n".join(lines[:5]).encode(), False),
        "truncated_half": (text[: len(text) // 2].encode(), False),
        "duplicated_content": ((text + "\n" + text).encode(), False),
        "shuffled_lines": ("\n".join(shuffled).encode(), False),
        "renamed_characters": (renamed.encode(), True),
        "timestamp_trailing_zero_dropped": (re.sub(
            r"(\d{2}):(\d{2})\.(\d{2})",
            lambda m: f"{m[1]}:{m[2]}.{m[3].rstrip('0') or '0'}", text).encode(), True),
        "timestamp_comma_decimal": (re.sub(r"(\d{2}):(\d{2})\.(\d{2})", r"\1:\2,\3", text).encode(), True),
        "latin1_encoding": (text.replace("Juliana", "Julià").encode("latin-1", "replace"), True),
    }
    rows = []
    print(f"Baseline: {src.name} -> {base_n} elements\n")
    with tempfile.TemporaryDirectory() as td:
        for name, (data, should_match) in muts.items():
            d = Path(td) / name
            d.mkdir()
            f = d / src.name
            f.write_bytes(data)
            try:
                ep = parse_episode(f)
                n = ep.total_elements
                inverted = sum(1 for e in ep.get_all_elements() if e.end_seconds < e.start_seconds)
                if n == 0:
                    res, note = "FAIL", "SILENT: parsed 0 elements without raising"
                elif should_match and n < 0.9 * base_n:
                    res, note = "BREAKS", f"parsed {n}/{base_n} elements (format assumption; document it)"
                else:
                    res, note = "PASS", f"parsed {n} elements" + (f", {inverted} inverted timestamps" if inverted else "")
            except Exception as e:  # noqa: BLE001
                msg = str(e).strip()
                res = "PASS" if msg else "FAIL"
                note = f"raised {type(e).__name__}: {msg[:90]}" if msg else f"raised {type(e).__name__} with no message"
            rows.append({"case": name, "result": res, "note": note})
            print(f"  [{res:6}] {name:34} {note}")
    bad = [r for r in rows if r["result"] != "PASS"]
    print(f"\nParser stress: {len(rows) - len(bad)}/{len(rows)} PASS. Write down every BREAKS/FAIL row: that is your 'where it breaks' answer.")
    record("parser", {"cases": rows, "passed": len(rows) - len(bad), "total": len(rows)})


# ── 2. timestamp integrity ───────────────────────────────────────────────
def t_integrity() -> None:
    eps = load_episodes()
    promos: dict[str, dict] = {}
    for label, path in (("candidates", config.PROMOS_DIR / "promo_candidates.json"),
                        ("ranked", config.RANKED_DIR / "ranked_promos.json")):
        if not path.exists():
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            p = row["promo"] if "promo" in row else row
            promos[f"{label}:{p['promo_id']}"] = p
    if not promos:
        sys.exit("No promo files found.")
    violations, micro = [], []
    boundary_cache: dict[int, tuple[set, set]] = {}
    for key, p in promos.items():
        total = sum(s["end_seconds"] - s["start_seconds"] for s in p["segments"])
        if not (20 - 0.05 <= total <= 60 + 0.05):
            violations.append(f"{key}: total {total:.1f}s outside 20-60s")
        for s in p["segments"]:
            ep = eps.get(s["episode"])
            if ep is None:
                violations.append(f"{key}: episode {s['episode']} not parsed")
                continue
            if ep.episode_number not in boundary_cache:
                els = sorted_elements(ep)
                boundary_cache[ep.episode_number] = ({cs(e.start_seconds) for e in els},
                                                      {cs(e.end_seconds) for e in els})
            starts, ends = boundary_cache[ep.episode_number]
            if cs(s["start_seconds"]) not in starts:
                violations.append(f"{key}: Ep{s['episode']} start {s['start_time']} is not an element start")
            if cs(s["end_seconds"]) not in ends:
                violations.append(f"{key}: Ep{s['episode']} end {s['end_time']} is not an element end")
            if s["end_seconds"] <= s["start_seconds"]:
                violations.append(f"{key}: Ep{s['episode']} end <= start")
            if s["end_seconds"] > ep.total_duration_sec + 0.02:
                violations.append(f"{key}: Ep{s['episode']} ends after episode ({ep.total_duration_sec:.1f}s)")
            if fmt_ts(s["start_seconds"]) != s["start_time"] or fmt_ts(s["end_seconds"]) != s["end_time"]:
                violations.append(f"{key}: timestamp strings disagree with seconds")
            if s["end_seconds"] - s["start_seconds"] < 3.0:
                micro.append(f"{key}: segment under 3s")
    n = len(promos)
    bad_keys = {v.split(":")[0] + ":" + v.split(":")[1] for v in violations}
    print(f"Checked {n} promos (candidates + ranked).")
    print(f"Fully valid: {n - len(bad_keys)}/{n}. Micro-segments (<3s, warning only): {len(micro)}")
    for v in violations[:15]:
        print("  VIOLATION", v)
    record("integrity", {"promos_checked": n, "valid": n - len(bad_keys),
                         "violations": violations[:50], "micro_segments": len(micro)})


# ── 5. adversarial clips vs your checker ─────────────────────────────────
def run_checker(promo: PromoCandidate, eps: dict[int, ParsedEpisode]):
    """Call every validate*/check* function in src.quality_checker that accepts (promo, episodes)."""
    import src.quality_checker as qc
    found: set[str] = set()
    called, errors = [], []
    for name in dir(qc):
        if name.startswith("_") or not (name.startswith("validate") or name.startswith("check")):
            continue
        fn = getattr(qc, name)
        if not callable(fn):
            continue
        for args in ((promo, eps), (promo, eps[promo.episodes[0]])):
            try:
                res = fn(*args)
            except TypeError:
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
                break
            called.append(name)
            for f in (res if isinstance(res, (list, tuple)) else [res]):
                ft = getattr(f, "failure_type", None)
                if ft is not None:
                    found.add(getattr(ft, "value", str(ft)))
            break
    return found, sorted(set(called)), errors


def build_cases(eps: dict[int, ParsedEpisode]) -> dict[str, tuple[PromoCandidate, str | None, bool]]:
    """name -> (promo, expected_flag, expect_flag_present). expect False = flag must be ABSENT."""
    cases: dict = {}
    hard = getattr(config, "AD_POLICY_HARD", config.AD_POLICY_FLAGGED_WORDS)
    hard_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in hard) + r")\b", re.I)
    for en in sorted(eps):
        ep = eps[en]
        els = sorted_elements(ep)
        for i, j in windows(els):
            s, e = els[i].start_seconds, els[j].end_seconds
            first_dlg = next((x for x in els[i:j + 1] if x.element_type == ElementType.DIALOGUE), None)
            blob = " ".join(x.content for x in els[i:j + 1])
            text = lambda x: f'"{x.content}"'  # noqa: E731
            # 1. strong opening
            if "strong_hook" not in cases and element_intensity(els[i]) >= 8 and \
                    els[i].element_type == ElementType.DIALOGUE:
                cases["strong_hook"] = (make_promo(ep, s, e, f"Opens on: {text(els[i])}",
                                                   f"Ends on: {text(els[j])}", "STRESS-OK"), "weak_hook", False)
            # 2. slow hook
            if "slow_hook" not in cases and first_dlg and els[i].element_type != ElementType.DIALOGUE \
                    and first_dlg.start_seconds - s > 4.5:
                cases["slow_hook"] = (make_promo(ep, s, e, f"Opens on: {text(first_dlg)}",
                                                 f"Ends on: {text(els[j])}", "STRESS-SLOW"), "weak_hook", True)
            # 4. ad policy
            if "ad_policy" not in cases and hard_re.search(blob):
                cases["ad_policy"] = (make_promo(ep, s, e, f"Opens on: {els[i].content}",
                                                 f"Ends on: {els[j].content}", "STRESS-POLICY"), "ad_policy_risk", True)
            # 5. hallucinated claim: quote a line outside the clip
            if "hallucinated_claim" not in cases:
                outside = [x for x in els if x.element_type == ElementType.DIALOGUE and len(x.content) > 25
                           and (x.end_seconds < s - 1 or x.start_seconds > e + 1)]
                if outside:
                    cases["hallucinated_claim"] = (make_promo(
                        ep, s, e, f'Opens on: "{outside[0].content}"', f"Ends on: {els[j].content}",
                        "STRESS-HALLU"), "hallucinated_claim", True)
        # 3. resolved cliffhanger: clip keeps running 8s+ past the peak line
        if "resolved_cliffhanger" not in cases:
            peaks = [k for k, x in enumerate(els) if x.element_type == ElementType.DIALOGUE
                     and element_intensity(x) >= 8 and len(x.content) > 20]
            for p in peaks:
                j = next((k for k in range(p, len(els)) if els[k].end_seconds >= els[p].end_seconds + 8), None)
                if j is None:
                    continue
                i = p
                while i > 0 and els[j].end_seconds - els[i - 1].start_seconds <= 45:
                    i -= 1
                d = els[j].end_seconds - els[i].start_seconds
                if 20 <= d <= 60:
                    cases["resolved_cliffhanger"] = (make_promo(
                        ep, els[i].start_seconds, els[j].end_seconds, f"Opens on: {els[i].content}",
                        f'Ends on: "{els[p].content}"', "STRESS-RESOLVED"), "resolved_cliffhanger", True)
                    break
        if len(cases) >= 5:
            break
    return cases


def t_adversarial() -> None:
    eps = load_episodes()
    cases = build_cases(eps)
    if not cases:
        sys.exit("Could not build any adversarial cases from the parsed episodes.")
    rows, caught, expected_total = [], 0, 0
    all_called: set[str] = set()
    for name, (promo, flag, present) in cases.items():
        found, called, errors = run_checker(promo, eps)
        all_called.update(called)
        ok = (flag in found) if present else (flag not in found)
        if present:
            expected_total += 1
            caught += int(ok)
        rows.append({"case": name, "expected": ("flag " if present else "NO ") + str(flag),
                     "found": sorted(found), "result": verdict(ok), "errors": errors})
        print(f"  [{verdict(ok)}] {name:22} expected {'flag' if present else 'NO'} {flag:22} found {sorted(found)}")
    missing = {"strong_hook", "slow_hook", "ad_policy", "hallucinated_claim", "resolved_cliffhanger"} - set(cases)
    if missing:
        print(f"  (could not build: {sorted(missing)} - none matched in the parsed data)")
    print(f"\nKnown failures caught: {caught}/{expected_total}. Checker functions called: {sorted(all_called) or 'NONE'}")
    if not all_called:
        print("  WARNING: no src.quality_checker function accepted (promo, episodes). Paste quality_checker.py "
              "and adjust run_checker().")
    print("Report misses honestly: 'missed X' is a better row than hiding it.")
    record("adversarial", {"caught": caught, "expected": expected_total, "cases": rows,
                           "could_not_build": sorted(missing)})


# ── 3. rerun stability ───────────────────────────────────────────────────
def run_batch_to(outdir: Path, episodes: str, extra: list[str]):
    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(outdir)
    cmd = [sys.executable, "run_batch.py", "--episodes", episodes, *extra]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def iou(a, b) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def t_stability(episodes: str) -> None:
    dirs = [OUT / "runA", OUT / "runB"]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"Running pipeline fresh (no cache) on episodes {episodes} -> {d} ...")
        r = run_batch_to(d, episodes, ["--no-cache", "--no-baselines", "--top-n", "10"])
        if r.returncode != 0:
            sys.exit(f"run_batch failed:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    A = json.loads((dirs[0] / "ranked" / "ranked_promos.json").read_text(encoding="utf-8"))
    B = json.loads((dirs[1] / "ranked" / "ranked_promos.json").read_text(encoding="utf-8"))

    def by_ep(rows):
        out: dict[int, list] = {}
        for r in rows:
            seg = r["promo"]["segments"][0]
            out.setdefault(r["promo"]["episodes"][0], []).append((seg["start_seconds"], seg["end_seconds"]))
        return out

    a, b = by_ep(A), by_ep(B)
    stable, total = 0, 0
    for ep, segs in sorted(a.items()):
        total += 1
        hit = any(iou(segs[0], s) >= 0.5 for s in b.get(ep, []))
        stable += int(hit)
        print(f"  Ep{ep}: top pick {'STABLE' if hit else 'CHANGED'}")
    print(f"\nTop-pick stability: {stable}/{total} (IoU>=0.5 on first segment). Low = LLM picks are noisy; say so.")
    record("stability", {"episodes": episodes, "stable": stable, "total": total})


# ── 4. judge noise + discrimination ──────────────────────────────────────
RUBRIC = ("You are judging a 20-60s promo clip cut from a vertical microdrama screenplay, as a stranger "
          "who has never seen the show, scrolling TikTok. Score each from 1 to 10 (integers): "
          "hook_strength (does the first 3 seconds stop a scroll), cliffhanger_quality (ends on unresolved "
          "tension), standalone_coherence (understandable cold), emotional_impact, duration_fitness "
          "(20-60s, best 25-45s). Return ONLY JSON with exactly those five integer keys.\n\nCLIP:\n")
_client = None


def judge(promo: PromoCandidate, eps, temp: float) -> float:
    global _client
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    err: Exception | None = None
    for attempt in range(4):
        try:
            r = _client.models.generate_content(
                model=config.GEMINI_JUDGE_MODEL, contents=RUBRIC + render_clip_text(promo, eps),
                config=types.GenerateContentConfig(temperature=temp, response_mime_type="application/json"))
            d = json.loads(r.text)
            w = config.SCORING_WEIGHTS
            return round(sum(w[k] * float(d[k]) for k in w), 2)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"judge failed: {err}")


def t_judge(n: int, repeats: int, temp: float) -> None:
    if not config.GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY not set.")
    eps = load_episodes()
    ranked = load_json(config.RANKED_DIR / "ranked_promos.json")[:n]
    good = [PromoCandidate(**r["promo"]) for r in ranked]
    spreads, first_scores = [], []
    print(f"Repeat test: {len(good)} clips x {repeats} runs at temperature {temp} (note: this standalone "
          f"rubric is a proxy for your pipeline judge prompt)")
    for p in good:
        sc = [judge(p, eps, temp) for _ in range(repeats)]
        first_scores.append(sc[0])
        spreads.append(max(sc) - min(sc))
        print(f"  {p.promo_id}: {sc}  spread {spreads[-1]:.2f}")
    # discrimination: lowest-intensity windows
    flat = []
    for en in sorted(eps)[:5]:
        ep = eps[en]
        els = sorted_elements(ep)
        best = None
        for i, j in windows(els):
            avg = sum(element_intensity(x) for x in els[i:j + 1]) / (j - i + 1)
            if best is None or avg < best[0]:
                best = (avg, i, j)
        if best:
            _, i, j = best
            flat.append(make_promo(ep, els[i].start_seconds, els[j].end_seconds,
                                   "Flat window placeholder", "Flat window placeholder", f"FLAT-{en}"))
    flat_scores = [judge(p, eps, temp) for p in flat]
    gm = sum(first_scores) / len(first_scores)
    fm = sum(flat_scores) / len(flat_scores) if flat_scores else float("nan")
    print(f"\nMean score, shortlisted clips: {gm:.2f} | flat low-intensity clips: {fm:.2f} | gap {gm - fm:.2f}")
    print(f"Max repeat spread: {max(spreads):.2f} | mean spread: {sum(spreads) / len(spreads):.2f}")
    ok = (gm - fm) >= 1.0
    print(f"Discrimination check (gap >= 1.0): {verdict(ok)}. If FAIL, the judge cannot separate good from "
          "bad and every benchmark rests on it. Say so.")
    record("judge", {"model": config.GEMINI_JUDGE_MODEL, "temperature": temp, "repeats": repeats,
                     "max_spread": max(spreads), "mean_spread": sum(spreads) / len(spreads),
                     "good_mean": gm, "flat_mean": fm, "gap": gm - fm, "discrimination_pass": ok})


# ── 6. checker recall (manual) ───────────────────────────────────────────
def t_recall_make(n: int) -> None:
    eps = load_episodes()
    ranked = load_json(config.RANKED_DIR / "ranked_promos.json")
    rng = random.Random(11)
    pick = rng.sample(ranked, min(n, len(ranked)))
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "recall_sheet.csv", "w", newline="", encoding="utf-8") as fs, \
            open(OUT / "recall_key.csv", "w", newline="", encoding="utf-8") as fk:
        ws, wk = csv.writer(fs), csv.writer(fk)
        ws.writerow(["item_id", "clip_text", "problems_you_see"])
        wk.writerow(["item_id", "promo_id"])
        for i, r in enumerate(pick, 1):
            iid = f"R{i:02d}"
            ws.writerow([iid, render_clip_text(PromoCandidate(**r["promo"]), eps), ""])
            wk.writerow([iid, r["promo"]["promo_id"]])
    print(f"Wrote {OUT / 'recall_sheet.csv'} ({len(pick)} clips).")
    print("Read each clip as a cold viewer. In 'problems_you_see' write codes separated by ';' from:")
    print("  " + ", ".join(FAILURE_CODES))
    print("Leave blank if you see no problem. Do NOT look at the flags first. Then run: recall-score")


def t_recall_score() -> None:
    key = {r["item_id"]: r["promo_id"] for r in csv.DictReader(open(OUT / "recall_key.csv", encoding="utf-8"))}
    ranked = load_json(config.RANKED_DIR / "ranked_promos.json")
    flagged: dict[str, set[str]] = {}
    qpath = config.QUALITY_DIR / "quality_report.json"
    q = json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    for f in q.get("flags", []):
        flagged.setdefault(f["promo_id"], set()).add(f["failure_type"])
    for r in ranked:   # fallback: REVIEW tokens in posting_rationale
        for code in re.findall(r"(\w+) \((?:high|medium|low)\)", r.get("posting_rationale", "")):
            flagged.setdefault(r["promo"]["promo_id"], set()).add(code)
    mine_total = hits = extra = 0
    with open(OUT / "recall_sheet.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mine = {c.strip() for c in (row.get("problems_you_see") or "").split(";") if c.strip()}
            theirs = flagged.get(key.get(row["item_id"], ""), set())
            mine_total += len(mine)
            hits += len(mine & theirs)
            extra += len(theirs - mine)
            print(f"  {row['item_id']}: you {sorted(mine)} | checker {sorted(theirs)}")
    recall = hits / mine_total if mine_total else None
    print(f"\nProblems you found: {mine_total} | checker also flagged: {hits} | "
          f"flags you did not agree with: {extra}")
    print(f"Recall: {recall:.0%}" if recall is not None else "You listed no problems; recall undefined.")
    print("Caveat: single reader, 15 clips. Report it as a rough number.")
    record("recall", {"mine": mine_total, "caught": hits, "recall": recall, "unconfirmed_flags": extra})


# ── 7. fallback / failure behaviour ──────────────────────────────────────
def t_fallback() -> None:
    results = {}
    d = OUT / "fallback_nollm"
    d.mkdir(parents=True, exist_ok=True)
    r = run_batch_to(d, "1,2", ["--no-llm", "--no-baselines"])
    man = d / "run_manifest.json"
    ok = r.returncode == 0 and man.exists() and json.loads(man.read_text(encoding="utf-8")).get("mode") != "llm"
    lim = ""
    qp = d / "quality_report" / "quality_report.json"
    if qp.exists():
        lim = " ".join(json.loads(qp.read_text(encoding="utf-8")).get("honest_limitations", []))
    labelled = "HEURISTIC" in lim.upper()
    print(f"  [{verdict(ok and labelled)}] --no-llm: exit {r.returncode}, manifest mode not 'llm': {ok}, "
          f"report labels heuristic mode: {labelled}")
    results["no_llm"] = ok and labelled

    d2 = OUT / "fallback_badmodel"
    d2.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"OUTPUT_DIR": str(d2), "GEMINI_MODEL": "not-a-real-model-xyz",
                "GEMINI_JUDGE_MODEL": "not-a-real-model-xyz"})
    r2 = subprocess.run([sys.executable, "run_batch.py", "--episodes", "1", "--no-baselines",
                         "--no-critique", "--no-cache"], env=env, capture_output=True, text=True)
    man2 = d2 / "run_manifest.json"
    if r2.returncode != 0:
        res, note = True, f"failed loudly (exit {r2.returncode})"
    elif man2.exists():
        m = json.loads(man2.read_text(encoding="utf-8"))
        fb = m.get("fallback_to_heuristic", {})
        labelled = any(fb.get(k) for k in fb)
        res = labelled
        note = "fell back and recorded it in the manifest" if labelled else \
            "SILENT: bad model, exit 0, no fallback recorded"
    else:
        res, note = False, "no manifest written"
    print(f"  [{verdict(res)}] bad model name: {note}")
    results["bad_model"] = res
    record("fallback", results)


# ── entry point ──────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("parser")
    sub.add_parser("integrity")
    sub.add_parser("adversarial")
    sub.add_parser("all")
    s = sub.add_parser("stability")
    s.add_argument("--episodes", default="1,2,3,4,5")
    j = sub.add_parser("judge")
    j.add_argument("--n", type=int, default=10)
    j.add_argument("--repeats", type=int, default=3)
    j.add_argument("--temp", type=float, default=0.0)
    rm = sub.add_parser("recall-make")
    rm.add_argument("--n", type=int, default=15)
    sub.add_parser("recall-score")
    sub.add_parser("fallback")
    a = ap.parse_args()
    if a.cmd == "parser":
        t_parser()
    elif a.cmd == "integrity":
        t_integrity()
    elif a.cmd == "adversarial":
        t_adversarial()
    elif a.cmd == "stability":
        t_stability(a.episodes)
    elif a.cmd == "judge":
        t_judge(a.n, a.repeats, a.temp)
    elif a.cmd == "recall-make":
        t_recall_make(a.n)
    elif a.cmd == "recall-score":
        t_recall_score()
    elif a.cmd == "fallback":
        t_fallback()
    elif a.cmd == "all":
        t_parser()
        t_integrity()
        t_adversarial()


if __name__ == "__main__":
    main()
