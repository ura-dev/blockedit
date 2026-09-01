"""方式A(全文書き直し→スワップ)をDRYなしで測り直す。

前スレッドの sr_experiment.py から arm A だけを引き継ぎ、
  - サンプラー設定をCLIから切り替えられるようにした(DRYの有無が主眼)
  - 不一致だったときに「どこがどう違うか」を行単位で出すようにした
    (残差0.9995 / 0.9785 の正体を確定するため)
  - 温度を上げて独立サンプルを取れるようにした(温度0は決定論的で試行を重ねても増えない)

  uv run python rewrite_swap.py --trials 3 --temp 0.3 --sampler nodry
"""

import argparse
import difflib
import http.client
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# strip_fence は実運用ランナーと共有する。ここからも再exportして、既存の
# `from rewrite_swap import strip_fence` を壊さない
import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
from blocks import strip_fence  # noqa: F401

from _targets import DOC, INJECTIONS, ROOT  # noqa: F401  他のベンチからも再export

BASE_URL = "http://127.0.0.1:5001/v1"
OUT_DIR = Path(__file__).resolve().parent / "results"

PROMPT_A = """以下のMarkdown文書から、`[表示テキスト](パス)` 形式のリンクを
表示テキストだけに置き換えてください。それ以外の箇所は一切変更しないでください。
変更後の全文を出力してください。説明は不要です。"""

# プロンプトと本文の区切り。`---` はMarkdownの水平線でもあるので、モデルが本文の一部と
# 見て写して返す。区切りを空行にするだけで消えるので、条件として明示的に持つ。
SEPS = {"rule": "\n\n---\n", "blank": "\n\n"}

SAMPLERS = {
    # linebatch からの流用(汚染源そのもの)。対照として残す
    "dry": {"rep_pen": 1.03, "dry_multiplier": 0.8, "dry_base": 1.75,
            "dry_allowed_length": 12, "dry_sequence_breakers": []},
    # DRYだけ切る。前スレッドで3/3を出した条件
    "nodry": {"rep_pen": 1.03, "dry_multiplier": 0.0},
    # 素の貪欲
    "bare": {"rep_pen": 1.0, "dry_multiplier": 0.0},
}


def build_corpus():
    pristine = DOC.read_text(encoding="utf-8")
    injected = pristine
    for old, new in INJECTIONS:
        n = injected.count(old)
        if n != 1:
            sys.exit(f"注入アンカーが一意でない({n}件): {old}")
        injected = injected.replace(old, new, 1)
    assert injected != pristine
    return pristine, injected


def call(messages, max_tokens, temperature, sampler, timeout=1800, grammar=None,
         banned_tokens=None):
    payload = {
        "model": "koboldcpp",
        "messages": messages,
        "temperature": temperature,
        "top_p": 1,
        "max_tokens": max_tokens,
        **sampler,
    }
    # KoboldCppはGBNFをリクエスト単位で受け、/v1/chat/completions でも効く。
    # プロンプトを変えないので、既存の測定と接頭辞が一致したまま比較できる。
    # **grammar を立てると投機的デコードが止まる**(v1.117.1 L6451)。同じ拘束を
    # banned_tokens で渡せば MTP が生きたまま残る。
    if grammar:
        payload["grammar"] = grammar
    if banned_tokens:
        payload["banned_tokens"] = list(banned_tokens)
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except (http.client.IncompleteRead, json.JSONDecodeError):
            if attempt:
                raise
            time.sleep(2)
    ch = data["choices"][0]
    return ch["message"]["content"], ch.get("finish_reason"), data.get("usage", {})


def soft_normalize(text):
    """「実質同じ」と言い切れる揺れだけを潰す(残差の正体を切り分けるため)。"""
    t = unicodedata.normalize("NFKC", text)
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def classify(result, pristine):
    """残差を分類し、差分の実体を返す。"""
    if result.rstrip("\n") == pristine.rstrip("\n"):
        return "exact", []
    if soft_normalize(result) == soft_normalize(pristine):
        return "whitespace_only", []

    a = pristine.splitlines()
    b = result.splitlines()
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "tag": tag,
            "orig_lines": f"{i1 + 1}-{i2}",
            "orig": a[i1:i2],
            "new": b[j1:j2],
        })
    # 全ハンクが「行内の軽微な揺れ」に収まるか
    inline = all(
        h["tag"] == "replace"
        and len(h["orig"]) == len(h["new"])
        and all(soft_normalize(o) == soft_normalize(n)
                for o, n in zip(h["orig"], h["new"]))
        for h in hunks
    )
    lost = sum(len(h["orig"]) for h in hunks if h["tag"] == "delete")
    kind = ("inline_noise" if inline
            else "content_loss" if lost else "content_change")
    return kind, hunks


def similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def trial(pristine, injected, idx, temperature, sampler_name, raw_dir, tag=""):
    msgs = [{"role": "user", "content": PROMPT_A + "\n\n---\n" + injected}]
    t0 = time.monotonic()
    raw, finish, usage = call(msgs, max_tokens=12000, temperature=temperature,
                              sampler=SAMPLERS[sampler_name])
    dt = time.monotonic() - t0
    result = strip_fence(raw)
    kind, hunks = classify(result, pristine)

    raw_path = raw_dir / f"{sampler_name}_t{temperature}{tag}_{idx}.md"
    raw_path.write_text(raw, encoding="utf-8")

    return {
        "trial": idx, "arm": "A", "sampler": sampler_name, "temp": temperature,
        "ok": kind == "exact",
        "kind": kind,
        "seconds": round(dt, 1),
        "finish_reason": finish,
        "out_tokens": usage.get("completion_tokens"),
        "similarity": round(similarity(result, pristine), 5),
        "links_left": result.count("]("),
        "bytes": len(result.encode("utf-8")),
        "bytes_expected": len(pristine.rstrip("\n").encode("utf-8")),
        "hunks": hunks,
        "raw_file": raw_path.name,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.0)
    ap.add_argument("--sampler", choices=list(SAMPLERS), default="nodry")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    pristine, injected = build_corpus()
    raw_dir = OUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"armA_{args.sampler}_t{args.temp}{args.tag}_{ts}.json"

    records = []
    for i in range(1, args.trials + 1):
        try:
            rec = trial(pristine, injected, i, args.temp, args.sampler, raw_dir,
                        args.tag)
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            rec = {"trial": i, "ok": False, "kind": "error", "error": repr(e)}
        records.append(rec)
        out.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"  #{i} {rec.get('kind')} sim={rec.get('similarity')} "
              f"links_left={rec.get('links_left')} "
              f"bytes={rec.get('bytes')}/{rec.get('bytes_expected')} "
              f"tok={rec.get('out_tokens')}/{rec.get('finish_reason')} "
              f"{rec.get('seconds')}s", flush=True)
        for h in (rec.get("hunks") or [])[:6]:
            print(f"      [{h['tag']} @L{h['orig_lines']}]", flush=True)
            for line in h["orig"][:3]:
                print(f"        - {line[:100]}", flush=True)
            for line in h["new"][:3]:
                print(f"        + {line[:100]}", flush=True)

    n_ok = sum(1 for r in records if r.get("ok"))
    kinds = {}
    for r in records:
        kinds[r.get("kind")] = kinds.get(r.get("kind"), 0) + 1
    print(f"[armA {args.sampler} temp={args.temp}] exact {n_ok}/{len(records)}  "
          f"{kinds}  -> {out}")


if __name__ == "__main__":
    main()
