"""KoboldCpp への呼び出しと、プロンプトの組み立て。

**区切りは空行で固定する。**指示と本文を `---` で連結していた頃、モデルがそれを
Markdownの水平線として本文に写していた。空行に替えるだけで段落分割は 2/8 → 8/8、
見出し分割は 7/8 → 8/8、全文スワップも 5/8 → 7/8 に動いた。ロール分離にしても
`---` を残せば 0/8 なので、効いているのは連結子そのもので渡し方ではない。
選ばせる理由がないので、ここに定数として持つ。
"""

import http.client
import json
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:5001/v1"

SEP = "\n\n"

# 既定は反復抑制のみ。**DRYは既定にしない。**
# 逐語コピーの正解は原文の長い反復そのものなので、DRY は正しい出力を罰する
# (dry_allowed_length 12 を必ず踏む)。行スコープでは 48件の分類で DRY 有無の
# 出力が md5 まで完全一致し、速度差も測定ノイズだった(14.8s 対 14.6s)ので、
# 揃える側は無効のほうを取る。暴走が実際に出たタスクだけ SAMPLER で上書きする。
SAMPLER = {"rep_pen": 1.03, "dry_multiplier": 0.0}

DRY_SAMPLER = {
    "rep_pen": 1.03,
    "dry_multiplier": 0.8,
    "dry_base": 1.75,
    "dry_allowed_length": 12,
    "dry_sequence_breakers": [],
}


def build_messages(task, frag):
    """FEWSHOT があれば対話形式、無ければ指示と断片を空行で連結した1メッセージ。

    連結でもロール分離でも出力はバイト一致するので、形は few-shot を渡せるか
    どうかだけで決めてよい。
    """
    header = task.INSTRUCTION.strip()
    pitfall = getattr(task, "PITFALL", None)
    if pitfall:
        src, wrong, right = pitfall
        header += f"\n\nよくある誤り:\n  入力: {src}\n  誤り: {wrong}\n  正解: {right}\n"

    fewshot = getattr(task, "FEWSHOT", None)
    if not fewshot:
        return [{"role": "user", "content": header + SEP + frag}]

    msgs = [{"role": "system", "content": header}]
    for src, dst in fewshot:
        msgs.append({"role": "user", "content": src})
        msgs.append({"role": "assistant", "content": dst})
    msgs.append({"role": "user", "content": frag})
    return msgs


def call(base_url, messages, frag, sampler=None, timeout=600):
    """温度0で1ブロック分を投げる。返り値は (本文, finish_reason)。

    出力上限を入力長から決める。逐語コピー+局所編集なので出力長は入力長を超えない。
    日本語はおおよそ1文字≒1トークンなので、文字数の2倍+256で暴走の物理的な上限になる。
    """
    payload = {
        "model": "koboldcpp",
        "messages": messages,
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max(256, len(frag) * 2 + 256),
        **(sampler if sampler is not None else SAMPLER),
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
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
    return ch["message"]["content"], ch.get("finish_reason")


def model_name(base_url):
    """疎通確認を兼ねてモデル名を取る。取れなければ None(=未起動とみなす)。

    KoboldCpp 専用の `/api/v1/model` だけを見ていた頃、OpenAI互換の他サーバを
    「未起動」と誤判定していた。**判定に使うのは互換エンドポイントのほうを先にする。**
    呼び出し本体(`/v1/chat/completions`)は元から互換仕様なので、ここだけが壁だった。
    """
    base = base_url.rstrip("/")
    for url, pick in (
        (base + "/models", lambda d: (d.get("data") or [{}])[0].get("id")),
        (base.removesuffix("/v1") + "/api/v1/model", lambda d: d.get("result")),
    ):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                got = pick(json.loads(resp.read().decode("utf-8")))
        except (urllib.error.URLError, TimeoutError, ValueError,
                KeyError, IndexError, AttributeError):
            continue
        if got:
            return got
    return None
