"""原文に出てこない文字を出力できなくするGBNF文法を組み立てる。

分割後に残った失敗クラスは `、`→`, `(CJK読点のASCII化)1つだけで、これは書式ではなく
**原文に無い文字を1つ持ち込む**という形をしている。だから13KB原文を丸ごと文法に符号化する
必要はない。**原文に現れた文字の集合**を許可し、それ以外を禁じるだけで、この失敗は
確率が下がるのではなく構造的に出力し得なくなる。

実doc 8本を `##` で割った54セクションのうち53本(98%)は ASCII `,` を含まない。
つまりほぼ全セクションでこの制約は実際に拘束する。

**言語コード(`ja` など)では代用できない。**「日本語だからCJK範囲を許す」形にしても
`,` は ASCII 側なので通ってしまう。効かせているのは言語ではなく「この原文に出てこない」
という原文由来の集合であること。

**文字を増やす操作には使えない。**リンク平坦化は文字を減らすだけなので追加は空でよいが、
翻訳や加筆のように新しい文字が出るタスクでは `extra` で明示的に足す必要がある。
足し忘れると、モデルが正しく書いても文法が出させない。
"""


def _esc(ch):
    """GBNFの文字クラス内に置ける形にする。

    全部 \\uXXXX に倒す。`]` `\\` `^` `-` の個別エスケープを書き分けるより、
    一律にコードポイント表記へ落とすほうが取りこぼしが無い。
    """
    cp = ord(ch)
    if cp > 0xFFFF:
        return f"\\U{cp:08x}"
    return f"\\u{cp:04x}"


def alphabet_grammar(text, extra=""):
    """textに現れた文字だけを任意個並べられる文法を返す。

    順序も長さも拘束しない(そこはスコープゲートの担当)。
    禁じるのは「無い文字の持ち込み」だけ。
    """
    chars = sorted(set(text) | set(extra))
    if not chars:
        raise ValueError("空の原文から文法は作れない")
    body = "".join(_esc(c) for c in chars)
    return f"root ::= char*\nchar ::= [{body}]\n"


def forbidden(text, extra=""):
    """この文法が実際に塞ぐ文字のうち、代表的なものを返す(ログ用)。"""
    allowed = set(text) | set(extra)
    watch = ",.!?;:'\"()[]{}<>-_`~ 　、。「」・"
    return "".join(c for c in watch if c not in allowed)


# 全角/半角で対になる約物。観測された失敗はすべてこの対の上を滑る形だった。
CONFUSABLE = {
    "、": ",", "。": ".", "，": ",", "．": ".",
    "！": "!", "？": "?", "：": ":", "；": ";",
    "(": "(", ")": ")", "「": '"', "」": '"',
    "　": " ", "〜": "~", "－": "-",
}
PAIRS = list(CONFUSABLE.items()) + [(v, k) for k, v in CONFUSABLE.items()]


def confusable_banned(text, extra=""):
    """原文にある約物の「相方」のうち、原文には出てこないものを返す。

    295文字の許可リストは的が大きい。実際に必要なのは
    **原文に `、` があって `,` が無いなら `,` を出させない**という1対だけで、
    残り294文字分の照合は毎トークン払うだけの空振りになっている。
    """
    present = set(text) | set(extra)
    return sorted({b for a, b in PAIRS if a in present and b not in present})


def confusable_grammar(text, extra=""):
    """相方だけを禁じる否定クラスを返す。塞ぐものが無ければ None。

    許可リスト方式(alphabet_grammar)より弱い。未知の文字の持ち込みは通すが、
    そこはスコープゲートの担当で、文法が二重に見る必要はない。
    """
    ban = confusable_banned(text, extra)
    if not ban:
        return None
    body = "".join(_esc(c) for c in ban)
    return f"root ::= char*\nchar ::= [^{body}]\n"


def confusable_bans(text, extra=""):
    """同じ拘束を `banned_tokens` で渡す形。塞ぐものが無ければ空。

    **文法を使わずに済ませるための経路。**KoboldCpp は投機的デコードに入るかを
    `grammar != nullptr` で判定しており(gpttype_adapter.cpp v1.117.1 L6451)、
    GBNFを渡した瞬間にドラフトを1本も引かなくなる。`banned_tokens` はこの条件式に
    含まれないので、同じ文字を封じたままMTPが生きる。

    向こう側は「その文字を含むトークンID」を語彙走査で列挙し、サンプリング直前に
    ロジットを最下位へ落とす(L5595-5607, L6779-6783)。**文法のような構造的な
    禁止ではない** — 貪欲法で選ばれなくなるだけで、他の全候補が潰れれば出うる。
    温度0のこの仕事では実質同じだが、保証の強さは同じではない。
    """
    return confusable_banned(text, extra)


if __name__ == "__main__":
    g = alphabet_grammar("あ、い\n")
    assert g == "root ::= char*\nchar ::= [\\u000a\\u3001\\u3042\\u3044]\n", g
    assert "," in forbidden("あ、い")
    assert "," not in forbidden("a,b")
    assert confusable_banned("あ、い") == [","]
    # 原文に両方あれば塞げない(塞ぐと正解を弾く)
    assert "," not in confusable_banned("あ、い, う")
    # 逆向きも見る。半角空白があって全角空白が無いなら、全角空白は出させない
    assert confusable_banned("a b") == ["　"]
    assert confusable_grammar("あいう") is None
    print(alphabet_grammar("あ、い\n"), end="")
    print(confusable_grammar("あ、い\n"), end="")
