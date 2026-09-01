---
paths:
  - "blockedit.py"
  - "blockedit_selftest.py"
  - "blocks.py"
  - "gates.py"
  - "task.py"
  - "report.py"
  - "client.py"
---

# 本体を触ったら回帰を通す

```
uv run python blockedit_selftest.py     # ゲートと分割の回帰。22件。LLMを呼ばない
```

分割・ゲート・宣言検証はここが唯一の実装で、`bench/` も `tasks/` もこれを import する。
**LLMを呼ばないので毎回通す。**方式2の経路(`client.py` と `blockedit.py` の往復)まで
変えたときは KoboldCpp を上げて `--limit` の試走まで見る。

## ゲートを緩めない

安全性は「モデルが正しいこと」ではなく、ランナーがブロックの外側と行末文字を所有すること、
および落ちたブロックを適用しないことで成り立っている。2列(仕事をしていない側 / やりすぎて
いる側)は実測で片方ずつ穴が開いたので対にしてある — 詳細は README の「常に効いている
検証ゲート」。

**頼まれるまでゲートを緩めない。**落ちるブロックが増えたときの既定の答えは、ゲートを
外すことではなくタスク側の `PATTERN` を狭めること。

`UNIT` と `SCOPE` を実行時オプションにしない。CLIの上書きは狭める方向にだけ置く
(`--files` / `--limit` / `--select-only`)。
