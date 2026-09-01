"""doc全体ではなく、対象を含むセクションだけを書き直して貼り戻す。

方式Aは100バイト変えるために13,000バイト・3000トークンを吐く。見出しでdocを割り、
リンクを含むセクションだけモデルに送れば、生成量が数百トークンに落ちる。

**送らなかったセクションは原文からそのまま貼り戻すので、構造的に壊れようがない。**
全文スワップの失敗3件がすべて編集対象から遠い位置(141行中のL114、L0、L33)で
起きていたが、そこはもうモデルに渡らない。残る失敗は断片の中だけに閉じる。

比較対象は multidoc_swap.py(同じ8本・同じ注入・同じ判定・同じゲート)。
違いは「モデルに何バイト渡し、何バイト吐かせるか」だけになるようにしてある。
プロンプトも PROMPT_A をそのまま使う(断片用に書き換えると変数が2つになる)。

実docは読み取り専用。

  uv run python section_swap.py --tag 26B_mtp
"""

import argparse
import json
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
from blocks import (join_blocks, split_paragraphs, split_sections,
                    split_trailing_blanks, unfence)
from constraints import (alphabet_grammar, confusable_banned, confusable_bans,
                         confusable_grammar, forbidden)
from multidoc_swap import TARGETS, pick_injections
from rewrite_swap import (PROMPT_A, ROOT, SAMPLERS, SEPS, call, classify,
                          similarity)
from gates import LINK_RE, RESIDUE_RE, gate

OUT_DIR = Path(__file__).resolve().parent / "results"


def build_messages(frag, sep, roles):
    """指示と断片の渡し方。1メッセージに連結するか、systemとuserに分けるか。

    linebatch はロールで分けており、今回踏んだ `---` を構造上踏まない。ただし gemma系は
    チャットテンプレートに system ロールを持たないので、アダプタ側で user ターンへ
    畳み込まれる可能性がある。畳み込まれるなら、効いているのはロールではなく連結子。
    """
    if roles:
        return [{"role": "system", "content": PROMPT_A},
                {"role": "user", "content": SEPS[sep].lstrip("\n") + frag}]
    return [{"role": "user", "content": PROMPT_A + SEPS[sep] + frag}]


def run_doc(rel, sampler, raw_dir, splitter, level, tag, mode="none", sep="rule",
            roles=False):
    doc = ROOT / rel
    pristine = doc.read_text(encoding="utf-8")
    pairs = pick_injections(pristine)
    if len(pairs) < 3:
        return {"doc": rel, "kind": "skipped", "note": f"アンカー{len(pairs)}件"}

    injected = pristine
    for old, new in pairs:
        injected = injected.replace(old, new, 1)

    pri_secs = splitter(pristine, level)
    inj_secs = splitter(injected, level)
    # 注入は行内の置換なので行数は動かない。ズレたら以降の突き合わせが無意味になる
    if len(pri_secs) != len(inj_secs):
        return {"doc": rel, "kind": "error", "ok": False,
                "error": f"ブロック数不一致 {len(pri_secs)}!={len(inj_secs)}"}
    assert join_blocks(inj_secs) == injected, "分割が可逆でない"

    hot = [i for i, s in enumerate(inj_secs) if LINK_RE.search("\n".join(s))]

    out_secs = [list(s) for s in inj_secs]
    per_section, total_out, total_in, total_dt = [], 0, 0, 0.0
    for i in hot:
        core, tail = split_trailing_blanks(inj_secs[i])
        frag = "\n".join(core)
        msgs = build_messages(frag, sep, roles)
        # 拘束は断片そのものから作る。リンク平坦化は文字を減らすだけなので、期待出力の
        # 文字集合は送った断片の部分集合になり、正解を弾く余地がない。
        # GBNFを渡すと投機的デコードが止まるので、実運用は ban(banned_tokens)を使う
        gram = {"none": lambda _: None,
                "alphabet": alphabet_grammar,
                "confusable": confusable_grammar,
                "ban": lambda _: None}[mode](frag)
        bans = confusable_bans(frag) if mode == "ban" else None
        t0 = time.monotonic()
        raw, finish, usage = call(msgs, max_tokens=12000, temperature=0,
                                  sampler=SAMPLERS[sampler], grammar=gram,
                                  banned_tokens=bans)
        dt = time.monotonic() - t0
        new_core, _ = split_trailing_blanks(unfence(raw, frag).split("\n"))
        out_secs[i] = new_core + tail

        result = "\n".join(new_core)
        want = "\n".join(split_trailing_blanks(pri_secs[i])[0])
        total_out += usage.get("completion_tokens") or 0
        total_in += usage.get("prompt_tokens") or 0
        total_dt += dt
        per_section.append({
            "idx": i,
            "heading": inj_secs[i][0][:60],
            "ok": result.rstrip("\n") == want.rstrip("\n"),
            "seconds": round(dt, 1),
            "in_tokens": usage.get("prompt_tokens"),
            "out_tokens": usage.get("completion_tokens"),
            "finish_reason": finish,
            "sent_bytes": len(frag.encode("utf-8")),
            "bytes": len(result.encode("utf-8")),
            "bytes_expected": len(want.rstrip("\n").encode("utf-8")),
            "grammar_bytes": len(gram) if gram else 0,
            "alphabet": len(set(frag)),
            "blocked": ("".join(confusable_banned(frag))
                        if mode in ("confusable", "ban")
                        else forbidden(frag)),
        })

    assembled = join_blocks(out_secs)
    kind, hunks = classify(assembled, pristine)
    accepted, reasons, gstats = gate(injected, assembled, LINK_RE, RESIDUE_RE)

    name = rel.rsplit("/", 1)[-1].replace(".md", "")
    (raw_dir / f"sec_{name}_{tag}.md").write_text(assembled, encoding="utf-8")

    return {
        "doc": rel, "kind": kind, "ok": kind == "exact",
        "gate_accepted": accepted, "gate_reasons": reasons[:3], "gate": gstats,
        "sections_total": len(inj_secs), "sections_sent": len(hot),
        "sent_bytes": sum(s["sent_bytes"] for s in per_section),
        "max_sent_bytes": max((s["sent_bytes"] for s in per_section), default=0),
        "doc_bytes": len(injected.encode("utf-8")),
        "seconds": round(total_dt, 1),
        "in_tokens": total_in, "out_tokens": total_out,
        "similarity": round(similarity(assembled, pristine), 5),
        "links_left": assembled.count("]("),
        "bytes": len(assembled.encode("utf-8")),
        "bytes_expected": len(pristine.rstrip("\n").encode("utf-8")),
        "sections": per_section,
        "hunks": hunks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=len(TARGETS))
    ap.add_argument("--sampler", choices=list(SAMPLERS), default="nodry")
    ap.add_argument("--split", choices=["heading", "para"], default="heading",
                    help="heading=見出しで割る / para=空行で割る(フェンス内は割らない)")
    ap.add_argument("--level", type=int, default=2,
                    help="2=##で割る / 3=###でも割る(--split heading のみ)")
    ap.add_argument("--tag", default="sec")
    ap.add_argument("--constrain",
                    choices=["none", "alphabet", "confusable", "ban"],
                    default="none",
                    help="alphabet/confusable=GBNF(投機が止まる) / "
                         "ban=banned_tokensで同じ文字を封じる(投機が生きる)")
    ap.add_argument("--sep", choices=list(SEPS), default="rule",
                    help="プロンプトと断片の区切り。rule=`---` / blank=空行のみ")
    ap.add_argument("--roles", action="store_true",
                    help="指示をsystem、断片をuserに分ける(linebatchと同じ渡し方)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    raw_dir = OUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    splitter = split_sections if args.split == "heading" else split_paragraphs
    gran = f"L{args.level}" if args.split == "heading" else "para"
    out = OUT_DIR / f"section_{args.tag}_{gran}_{ts}.json"

    records = []
    for rel in TARGETS[:args.docs]:
        try:
            rec = run_doc(rel, args.sampler, raw_dir, splitter, args.level,
                          f"{args.tag}_{gran}", args.constrain, args.sep,
                          args.roles)
        except (urllib.error.URLError, TimeoutError, KeyError) as e:
            rec = {"doc": rel, "kind": "error", "ok": False, "error": repr(e)}
        records.append(rec)
        out.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"{rec['kind']:14} gate={str(rec.get('gate_accepted')):5} "
              f"{rec['doc'].rsplit('/', 1)[-1]:32} "
              f"sec={rec.get('sections_sent')}/{rec.get('sections_total')} "
              f"sim={rec.get('similarity')} "
              f"bytes={rec.get('bytes')}/{rec.get('bytes_expected')} "
              f"links={rec.get('links_left')} "
              f"in={rec.get('in_tokens')} out={rec.get('out_tokens')} "
              f"{rec.get('seconds')}s", flush=True)
        for s in rec.get("sections") or []:
            if not s["ok"]:
                print(f"    x [{s['heading']}] {s['bytes']}/{s['bytes_expected']}B "
                      f"tok={s['out_tokens']}/{s['finish_reason']}", flush=True)
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
    sent = sum(r.get("sections_sent") or 0 for r in records)
    tot = sum(r.get("sections_total") or 0 for r in records)
    print(f"\n完全一致 {ok}/{len(records)}   ゲート素通りの破損 {len(leaked)}   "
          f"誤って弾いた正解 {len(false_rej)}")
    if args.constrain != "none":
        secs = [s for r in records for s in (r.get("sections") or [])]
        gb = [s["grammar_bytes"] for s in secs]
        comma = sum(1 for s in secs if "," in (s.get("blocked") or ""))
        print(f"拘束({args.constrain}): GBNF {min(gb)}〜{max(gb)}バイト   "
              f"ASCII `,` を塞いだセクション {comma}/{len(secs)}")
    sent_b = sum(r.get("sent_bytes") or 0 for r in records)
    doc_b = sum(r.get("doc_bytes") or 0 for r in records)
    print(f"送ったブロック {sent}/{tot}   "
          f"送信 {sent_b}/{doc_b}B ({sent_b / max(doc_b, 1):.1%})   "
          f"最大ブロック {max((r.get('max_sent_bytes') or 0 for r in records), default=0)}B")
    print(f"入力 {sum(r.get('in_tokens') or 0 for r in records)} tok   "
          f"生成 {sum(r.get('out_tokens') or 0 for r in records)} tok   "
          f"合計 {round(sum(r.get('seconds') or 0 for r in records), 1)}s   -> {out}")


if __name__ == "__main__":
    main()
