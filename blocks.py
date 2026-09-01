"""ブロック分割と貼り戻し。可逆であることをこのモジュールが保証する。

安全性は「モデルが正しいこと」ではなく、**割ったものが1バイトも変わらず戻る**ことに
依存している。送らなかったブロックは原文からそのまま貼り戻すので、モデルの出力は
ブロックの位置にしか入らない。

分割の実装を測定ハーネス(bench/section_swap.py)と実運用(blockedit.py)で共有し、
可逆性のassertも1箇所に置く。片方だけ直すと、測定で出した成績が運用の成績でなくなる。

**行の終端文字はこのモジュールが所有する。**`text.split("\\n")` で割ると CRLF の
`\\r` が本文側に残り、モデルに渡って返ってこない。ゲートは `splitlines()` で判定する
ためCRに盲目で、貼り戻した瞬間にそのブロック全行の改行コードが黙ってLFに変わる。
docs 55本中22本がCRLFなので、これは実際に踏む。行は終端文字ごと保持し、モデルに
渡す本文だけを剥がす。
"""

import re

FENCE_RE = re.compile(r"^\s*(```|~~~)")

UNITS = ("line", "para", "heading", "full")


def to_lines(text):
    """終端文字を保持したまま行に割る。`"".join()` で必ず元に戻る。

    `splitlines(keepends=True)` を使わないのは、垂直タブや U+2028 でも割れてしまい
    Markdown 本文の1文字で分割位置が変わるため。改行と認めるのは `\\n` だけにして、
    CRLF の `\\r` は終端文字の一部として扱う。
    """
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def body(line):
    """行の本文(終端文字を除く)。モデルに渡すのはこちらだけ。"""
    return line.rstrip("\r\n")


def term(line):
    """行の終端文字。`""`(EOFで改行なし)/ `"\\n"` / `"\\r\\n"` のいずれか。"""
    return line[len(body(line)):]


def strip_fence(text):
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t


def _blocks(lines, bounds):
    """境界の行番号リストを連続スライスに変換する。全行がどれか1つのブロックに入る。"""
    if not bounds:
        return [lines]
    bounds = list(bounds)
    if bounds[0] != 0:
        bounds.insert(0, 0)
    bounds.append(len(lines))
    return [lines[a:b] for a, b in zip(bounds, bounds[1:])]


def split_lines(text, level=None):
    """1行を1ブロックにする。linebatch の行スコープがこれにあたる。"""
    return [[ln] for ln in to_lines(text)]


def split_sections(text, level=2):
    """見出しでブロックに割る。先頭の見出し前(前文)は独立したブロックになる。"""
    lines = to_lines(text)
    rx = re.compile(rf"^#{{2,{level}}} ")
    return _blocks(lines, [i for i, ln in enumerate(lines) if rx.match(ln)])


def split_paragraphs(text, level=None):
    """空行でブロックに割る。コードフェンスの内側の空行では割らない。

    見出し分割は最大ブロックが 7,980B まで膨らむ(1セクションで doc の50%を占める
    docがある)。生成量は送信量にほぼ比例するので、そこが速度の上限になっていた。

    境界は「空行の次に来る非空行」。見出し分割と同じく末尾の空行は前のブロックに残るので、
    貼り戻しと可逆性のassertは同じ仕組みがそのまま使える。
    """
    lines = to_lines(text)
    bounds, fence, prev_blank = [], None, True
    for i, ln in enumerate(lines):
        s = ln.strip()
        if fence is not None:
            if s.startswith(fence):
                fence = None
            prev_blank = False
            continue
        if s and prev_blank:
            bounds.append(i)
        m = FENCE_RE.match(ln)
        if m:
            fence = m.group(1)
        prev_blank = not s
    return _blocks(lines, bounds)


def split_full(text, level=None):
    """割らない。ファイル1本が1ブロック。"""
    return [to_lines(text)]


_SPLITTERS = {"line": split_lines, "para": split_paragraphs,
              "heading": split_sections, "full": split_full}


def split(text, unit="para", level=2):
    """UNIT名でディスパッチし、可逆性をその場で検査する。"""
    try:
        blocks = _SPLITTERS[unit](text, level)
    except KeyError:
        raise ValueError(f"UNIT は {UNITS} のいずれか: {unit!r}") from None
    if join_blocks(blocks) != text:
        raise AssertionError(f"分割が可逆でない(unit={unit})")
    return blocks


def join_blocks(blocks):
    return "".join("".join(b) for b in blocks)


def block_text(block):
    """ブロックの検索対象になる文字列。終端文字は含めない。"""
    return "\n".join(body(ln) for ln in block)


def split_trailing_blanks(lines):
    """末尾の空行を切り離す。

    ブロックは境界で割るので末尾に空行を含む。strip_fence は .strip() するため
    そのまま貼り戻すとブロック間の空行が消え、モデルの失敗に見える差分になる。
    空行の骨格はモデルに渡さず、こちらで原文どおり復元する。
    """
    n = len(lines)
    while n and not lines[n - 1].strip():
        n -= 1
    return lines[:n], lines[n:]


def terminator(core):
    """ブロック本体が使っている終端文字。混在していれば None。

    None を返したブロックは呼び出し側が棄却する。**推測して貼り戻さない** —
    改行コードの取り違えは本文に出ないので、静かに混ざる類の破損になる。
    最終行だけは終端なし(EOF)がありうるので、そこは別に扱う。
    """
    if not core:
        return "\n"
    kinds = {term(ln) for ln in core[:-1]}
    last = term(core[-1])
    if last:
        kinds.add(last)
    if not kinds:
        return "\n"
    return kinds.pop() if len(kinds) == 1 else None


def relines(text, core):
    """モデルが返した本文に、元ブロックの終端文字を戻す。

    行数が変わってもよい(参照文の削除で1行減るなど)。最終行の終端は原文の最終行に
    合わせるので、EOFに改行が無いファイルに改行を足してしまうことがない。
    """
    t = terminator(core)
    if t is None:
        return None
    parts = text.split("\n")
    last_term = term(core[-1]) if core else t
    return [p + t for p in parts[:-1]] + [parts[-1] + last_term]


def unfence(raw, frag):
    """断片自身がコードフェンスで始まるときは strip_fence を通さない。

    strip_fence は先頭の ``` を剥がす。全文スワップでは剥がす対象がモデルの飾りだけ
    だったが、段落まで割るとフェンス1個がそのままブロックになるので本物を消しうる。
    """
    if frag.lstrip().startswith(("```", "~~~")):
        return raw.strip()
    return strip_fence(raw)


_OUTER_WS = re.compile(r"\A(\s*).*?(\s*)\Z", re.S)


def outer_ws(text):
    """断片の先頭・末尾の空白。行の字下げと、行末の空白がここに入る。"""
    m = _OUTER_WS.match(text)
    return m.group(1), m.group(2)


def reown_ws(text, frag):
    """外枠の空白を原文どおりに戻す。**モデルの領分ではない。**

    ランナーが終端文字とブロックの外側を所有しているのと同じ理由で、字下げと行末の
    空白も所有する。分類も言い換えも字下げを動かさないので、動いたなら飾りの剥がし
    (`unfence` の strip)かモデル側の脱落で、どちらも復元でよい。

    実際に両方が起きている。`clean` の strip は `  - 子項目` を `- 子項目` にして
    いたし、FreeToken(OpenAI互換・port 1919)は連続スペースを n-1 個にして返す。
    行スコープでは塊全体が改変可なので局所性ゲートが効かず、字下げの消えた行がその
    まま書き込まれていた。

    **守れるのは外枠だけ。**行の内側の連続スペース(`` `a  b` `` → `` `a b` ``)は、
    ここでは区別がつかない。行スコープに持ち込むなら `preserve()` で拾う。
    """
    if not text.strip():
        return text
    lead, trail = outer_ws(frag)
    return lead + text.strip() + trail


def clean(raw, frag):
    """モデル出力の飾りを剥がし、外枠の空白を戻す。

    剥がすのはコードフェンスと、全体を囲む引用符。引用符を剥がすのは、行スコープの
    分類でラベルが `"OTHER"` の形で返るため。ただし断片自身が引用符で始まる/終わる
    ときは触らない — フェンスと同じで、本物を飾りと間違えて消さないための門。
    """
    t = unfence(raw, frag)
    if not (frag.startswith(('"', "'")) or frag.endswith(('"', "'"))):
        t = t.strip().strip('"').strip("'")
    return reown_ws(t, frag)
