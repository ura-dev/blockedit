"""機械検証。モデルの出力を受理してよいかを、モデルに依存せず決める(0トークン)。

ゲートは2列でできている。**片方だけでは穴が開く。**

  仕事をしていない側 — no-op / 対象パターンの痕跡が残っている(完了)
  やりすぎている側   — 一致範囲の外側が動いた(局所性)/ 範囲外の行を触った / preserve

範囲だけを見ていた頃、E4Bが8本中4本を素通りさせた。リンクを残したまま行を弄ると、
その行はリンクを含むので範囲の検査には通ってしまう。逆に完了だけでは、対象行の上で
太字を剥がす類のやりすぎが通る。2列は対になっていて、どちらも常に要る。

SCOPE によって使えるゲートが変わる:

  scope="match"  一致範囲だけが改変を許される。局所性ゲートが使える(最も強い)
  scope="unit"   ブロック全体が改変を許される。局所性は原理的に検査できないので、
                 ハーネスが所有する構造(ブロックの外)と no-op / 痕跡 / preserve で守る
"""

import difflib
import re

# 測定コーパス(リンク平坦化)の既定。運用側はタスクの PATTERN / RESIDUE を渡す
LINK_RE = re.compile(r"\[[^\]\n]+\]\([^)\n]+\)")

# 完了チェック用。スコープ検査の LINK_RE を流用してはいけない。
# LINK_RE は完全形しか拾わないが、失敗は完全形では残らない — E4B は開き括弧だけ
# 消した `表示](パス)` を吐き、LINK_RE から見ると「リンクは残っていない」に見える。
# 痕跡を拾う側は緩く取る。
RESIDUE_RE = re.compile(r"\]\(")


def confined(original, candidate, target_re):
    """対象パターンの外側が1文字も動いていないかを見る。

    **期待出力を知らなくても検査できる。**パターンに一致した範囲だけが差し替わり、
    その外側は逐語で残っているはず、という形しか使っていない。「何に置き換えるべきか」を
    ゲートが知る必要はないので、リンク平坦化に固有の知識にはならない。

    行単位の範囲検査(条件1)は、対象行の上での巻き添えを見ない。変更された行は
    確かにリンクを含む行なので、そこで太字が剥がれても「対象行だけ触った」に見える。
    12B+分割はこれで `**開拓の進行に伴い**` → `開拓の進行に伴い` を素通りさせた。

    差し替わった側(gap)は、元の一致範囲の部分文字列であることも要求する。
    平坦化は範囲を縮める操作なので、外の語を持ち込んだ時点で棄却してよい。
    **一致するとまでは要求しない** — それをやるとゲートが答えを持つことになり、
    モデルを呼ぶ意味が無くなる。
    """
    spans = [m.span() for m in target_re.finditer(original)]
    if not spans:
        return (original == candidate), "対象パターンを含まない範囲が変更された"

    protected, pos = [], 0
    for s, e in spans:
        protected.append(original[pos:s])
        pos = e
    protected.append(original[pos:])

    cur = 0
    for i, seg in enumerate(protected):
        if not seg:
            continue
        idx = candidate.find(seg, cur)
        if i == 0 and not candidate.startswith(seg):
            return False, f"先頭が変わった — {seg[:40]!r}"
        if idx < 0:
            return False, f"パターン外が消えた/変わった — {seg[:40]!r}"
        gap = candidate[cur:idx]
        if i and gap not in original[spans[i - 1][0]:spans[i - 1][1]]:
            return False, f"一致範囲の外から語が入った — {gap[:40]!r}"
        cur = idx + len(seg)
    if protected[-1]:
        if not candidate.endswith(protected[-1]):
            return False, f"末尾が変わった — {protected[-1][-40:]!r}"
    # **原文が一致範囲で終わっているときの穴。**protected の最後は空文字列になり、
    # 空はループの先頭でスキップされ endswith も無条件で通る。つまり末尾への追記が
    # どのゲートにも当たらない。PATTERNがリンクだけで文中にあった測定条件では
    # protected[-1] が常に非空だったため一度も露出しなかった。
    # 文末までを範囲に取るタスク(参照文の削除など)で初めて出る。
    elif (tail := candidate[cur:]) not in original[spans[-1][0]:spans[-1][1]]:
        return False, f"一致範囲の後ろに語が足された — {tail[:40]!r}"
    return True, None


def gate(original, candidate, target_re, residue_re):
    """(accept, 理由, 統計) を返す。scope="match" 用の本体。

    受理条件は3つ。

    1. **範囲** — 変更・削除された元の行が、すべて対象パターンを含む行であること。
       対象行を1行も含まない位置での挿入も拒否する(捏造の追加を止める)。
    2. **完了** — 出力に対象パターンの痕跡(`residue_re`)が1つも残っていないこと。
    3. **局所性** — 一致範囲の外側が1文字も動いていないこと(`confined`)。

    2がないと、モデルが仕事をせずリンクを残したまま行を弄った場合に素通りする。
    その行は当然リンクを含むので、範囲の検査からは「対象行だけ触った」に見えるため。
    E4Bはこれで4/8を素通りさせた(26B・12Bでは一度も顕在化していない)。

    **2の判定に target_re を使わない。**完全形の `[表示](パス)` しか拾わないので、
    開き括弧だけ消えた `表示](パス)` を「残っていない」と誤判定する。実際にそう実装して
    E4Bの2件を取りこぼした。痕跡側は緩いパターンで取る。
    """
    a = original.splitlines()
    b = candidate.splitlines()
    stats = {"hunks": 0, "changed_lines": 0, "out_of_scope_lines": 0,
             "inserted": 0, "deleted": 0, "target_left": 0, "unconfined": 0}
    reasons = []

    stats["target_left"] = len(residue_re.findall(candidate))
    if stats["target_left"]:
        reasons.append(f"対象パターンの痕跡が出力に{stats['target_left']}件残っている(未完了)")

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        stats["hunks"] += 1
        orig_lines = a[i1:i2]
        stats["changed_lines"] += len(orig_lines)
        if tag == "insert":
            stats["inserted"] += (j2 - j1)
            # 対象行の直後でない挿入は、モデルが勝手に足した本文とみなす
            anchor = a[i1 - 1] if i1 else ""
            if not target_re.search(anchor):
                reasons.append(f"L{i1}: 対象外の位置に{j2 - j1}行を挿入")
            continue
        if tag == "delete":
            stats["deleted"] += len(orig_lines)
        bad = [ln for ln in orig_lines if not target_re.search(ln)]
        if bad:
            stats["out_of_scope_lines"] += len(bad)
            reasons.append(
                f"L{i1 + 1}-{i2}: 対象外の行を{len(bad)}行{'削除' if tag == 'delete' else '変更'}"
                f" — {bad[0][:60]!r}")
            continue
        # 3. 対象行の上での巻き添え。hunkごとに連結して見るので、
        #    差分ブロックの行数が n:m でも対応付けを要らなくしてある
        ok, why = confined("\n".join(orig_lines), "\n".join(b[j1:j2]), target_re)
        if not ok:
            stats["unconfined"] += 1
            reasons.append(f"L{i1 + 1}-{i2}: 対象行の上で範囲外が動いた — {why}")

    return (not reasons), reasons, stats


def check(task, frag, new, finish_reason, max_bytes, scope):
    """ランナーが1ブロックごとに通す門。通れば None、落ちれば理由の文字列を返す。

    ここが SCOPE の差を吸収する唯一の場所である。scope 以外の分岐を足さない。
    """
    # 打ち切られた出力は「変換が下手」ではなく上限不足。区別できないと原因が
    # ランナー側にあることに気付けないので、理由として明示して必ず落とす。
    if finish_reason == "length":
        return "max_tokensで打ち切り(上限不足)"
    if not new.strip():
        return "出力が空"
    if len(new.encode("utf-8")) > max_bytes:
        return f"出力が上限超過({len(new.encode('utf-8'))}B > {max_bytes}B)"
    # 仕事をしていない側。scope="match" では RESIDUE でも落ちるが、こちらのほうが
    # 理由が具体的に出る
    if new == frag:
        return "無変更(no-op)"

    if scope == "match":
        accepted, reasons, _ = gate(frag, new, task.PATTERN, task.RESIDUE)
        if not accepted:
            return " / ".join(reasons[:3])
    else:
        # ブロック全体が改変可なので局所性は検査できない。ハーネスが所有するのは
        # ブロックの外側だけなので、ブロックの形を崩す出力をここで止める
        if len(new.split("\n")) > len(frag.split("\n")):
            return "行数が増えた(ブロックの外にはみ出す)"
        if len(new) > len(frag) * 3 + 40:
            return f"出力が異常に長い({len(frag)} -> {len(new)}文字)"
        residue = getattr(task, "RESIDUE", None)
        if residue is not None and residue.search(new):
            return "対象パターンの痕跡が出力に残っている(未完了)"

    for s in getattr(task, "preserve", lambda _: [])(frag):
        if s not in new:
            return f"保存されるべき部分文字列が消失: {s!r}"
    validate = getattr(task, "validate", None)
    if validate:
        return validate(frag, new)
    return None
