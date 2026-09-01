"""方式A(全文書き直し→スワップ)を、温度0のまま複数の実docで測る。

温度を上げて独立サンプルを取る案は捨てた。理由は2つ:
  1. KoboldCppはシードを固定して返すので、同一プロンプト・同一温度なら
     温度>0でも出力がバイト一致する(temp0.3の3試行が完全に同一だった)
  2. そもそも実運用は温度0で回す。温度を上げた条件の成功率は出荷しない設定の数字

独立サンプルは入力を変えて取る。実docを1本ずつ対象にすれば、
サンプルが独立になると同時に汎化(サイズ・文体・構造の違い)も同時に測れる。

実docは読み取り専用。書き込みは一切しない。

  uv run python multidoc_swap.py --docs 8
"""

import argparse
import json
import re
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

from rewrite_swap import (PROMPT_A, ROOT, SAMPLERS, SEPS, call, classify,
                          similarity, strip_fence)
import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
from gates import LINK_RE, RESIDUE_RE, gate
from _targets import FAKE_PATHS, TARGETS  # noqa: F401  section_swap からも使う

OUT_DIR = Path(__file__).resolve().parent / "results"

# 注入アンカーの候補。内側のテキストをリンクで包むので、平坦化すると原文に戻る
ANCHOR_RES = [re.compile(r"\*\*([^*\n]{4,40})\*\*"), re.compile(r"「([^」\n]{3,30})」")]


def pick_injections(text, want=3):
    """本文中で一意に現れる語を選び、リンクで包む差し替えペアを作る。"""
    seen, pairs = set(), []
    for rx in ANCHOR_RES:
        for m in rx.finditer(text):
            inner = m.group(1)
            if inner in seen or text.count(m.group(0)) != 1 or "](" in m.group(0):
                continue
            seen.add(inner)
            path = FAKE_PATHS[len(pairs) % len(FAKE_PATHS)]
            pairs.append((m.group(0), m.group(0).replace(inner, f"[{inner}]({path})", 1)))
            if len(pairs) == want:
                return pairs
    return pairs


def run_doc(rel, sampler, raw_dir, sep="rule"):
    doc = ROOT / rel
    pristine = doc.read_text(encoding="utf-8")
    pairs = pick_injections(pristine)
    if len(pairs) < 3:
        return {"doc": rel, "kind": "skipped", "note": f"アンカー{len(pairs)}件"}

    injected = pristine
    for old, new in pairs:
        injected = injected.replace(old, new, 1)

    msgs = [{"role": "user", "content": PROMPT_A + SEPS[sep] + injected}]
    t0 = time.monotonic()
    raw, finish, usage = call(msgs, max_tokens=12000, temperature=0,
                              sampler=SAMPLERS[sampler])
    dt = time.monotonic() - t0
    result = strip_fence(raw)
    kind, hunks = classify(result, pristine)
    accepted, reasons, gstats = gate(injected, result, LINK_RE, RESIDUE_RE)

    name = rel.rsplit("/", 1)[-1].replace(".md", "")
    (raw_dir / f"doc_{name}_{sampler}_{sep}.md").write_text(raw, encoding="utf-8")

    return {
        "doc": rel, "kind": kind, "ok": kind == "exact",
        "gate_accepted": accepted, "gate_reasons": reasons[:3], "gate": gstats,
        "seconds": round(dt, 1), "finish_reason": finish,
        "out_tokens": usage.get("completion_tokens"),
        "similarity": round(similarity(result, pristine), 5),
        "links_left": result.count("]("),
        "bytes": len(result.encode("utf-8")),
        "bytes_expected": len(pristine.rstrip("\n").encode("utf-8")),
        "hunks": hunks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=len(TARGETS))
    ap.add_argument("--sampler", choices=list(SAMPLERS), default="nodry")
    ap.add_argument("--sep", choices=list(SEPS), default="rule",
                    help="プロンプトと本文の区切り。rule=`---` / blank=空行のみ")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    raw_dir = OUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"multidoc_{args.sampler}_{args.sep}_{ts}.json"

    records = []
    for rel in TARGETS[:args.docs]:
        try:
            rec = run_doc(rel, args.sampler, raw_dir, args.sep)
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            rec = {"doc": rel, "kind": "error", "ok": False, "error": repr(e)}
        records.append(rec)
        out.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"{rec['kind']:14} gate={str(rec.get('gate_accepted')):5} "
              f"{rec['doc'].rsplit('/', 1)[-1]:32} "
              f"sim={rec.get('similarity')} "
              f"bytes={rec.get('bytes')}/{rec.get('bytes_expected')} "
              f"links={rec.get('links_left')} "
              f"tok={rec.get('out_tokens')}/{rec.get('finish_reason')} "
              f"{rec.get('seconds')}s", flush=True)
        for h in (rec.get("hunks") or [])[:3]:
            print(f"    [{h['tag']} @L{h['orig_lines']}] -{len(h['orig'])}行 "
                  f"+{len(h['new'])}行", flush=True)
            for line in h["orig"][:2]:
                print(f"      - {line[:88]}", flush=True)
            for line in h["new"][:2]:
                print(f"      + {line[:88]}", flush=True)
        for r in rec.get("gate_reasons") or []:
            print(f"    ! {r}", flush=True)

    ok = sum(1 for r in records if r.get("ok"))
    leaked = [r["doc"] for r in records if not r.get("ok") and r.get("gate_accepted")]
    false_rej = [r["doc"] for r in records if r.get("ok") and not r.get("gate_accepted")]
    print(f"\n完全一致 {ok}/{len(records)}   ゲート素通りの破損 {len(leaked)}   "
          f"誤って弾いた正解 {len(false_rej)}   -> {out}")


if __name__ == "__main__":
    main()
