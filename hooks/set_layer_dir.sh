#!/usr/bin/env bash
#
# set_layer_dir.sh -- 分析層の根の絶対パスを、後続の Bash コマンドに LAYER_DIR として
#                     渡す SessionStart フック。
#
# 何のための道具か:
#   分析層の文書は、道具のパスを手書きしない。代わりにシェルの $LAYER_DIR から組み立てる
#   （例: bash "$LAYER_DIR/tools/run" phase_timer.py --help）。このフックは、その
#   LAYER_DIR をセッションの初めに一度だけ設定する。分析層だけを切り出して配った場合でも、
#   層の根が変わるだけで、文書の側は書き換えずに済む。
#
# 受け渡しの仕組み:
#   Claude Code は、フックのプロセスにだけ環境変数 CLAUDE_ENV_FILE を渡す。そこに書いた
#   内容を、Claude Code が各 Bash コマンドの前に preamble として実行する。このフックは、
#   そのファイルに `export LAYER_DIR=<層の根>` の1行を追記する。上書きはしない（同じ
#   ファイルに他のフックが書くことがある）。値は printf '%q' で引用するので、空白や $ を
#   含むパスでも1行として正しく読める。CLAUDE_ENV_FILE が無いとき（手で動かしたとき）は、
#   何も書かずに正常に終わる。
#
# なぜ Python を使わないか（「他の道具と揃える」つもりで Python に戻さないこと）:
#   仕事は「層の根を求めて1行追記する」だけで、Python は要らない。以前は tools/run 経由の
#   Python で書いていたが、そうするとセッションを開くたびに Python 環境の立ち上げに依存する。
#   tools/run は最初に `uv run` を試すので、初回は依存の導入に時間がかかり、フックの制限時間を
#   超えるか、uv が環境を作れずに失敗して、何も書かずに終わりうる。bash だけならその依存が無い。
#
# なぜ bash で起動するか（登録の形を変えないこと）:
#   src/.claude/settings.json では shell-form（command を1つの文字列で書く）に
#   "shell": "bash" を付けて登録する。こうすると Claude Code は PATH から bash を探さず、
#   Bash ツールに使っているのと同じ Git Bash でこのフックを起動する。exec-form で
#   "command": "bash" と書くと、Windows では PATH から bash を探し、Git for Windows の
#   bash.exe が PATH に無い機械では見つからず、WSL の起動口（System32\bash.exe）が先に
#   当たる機械ではそれで落ちる。"args" を付けると "shell" が無視されて exec-form に戻る
#   ので、付けない。スクリプトを `bash <パス>` で起動するのは、Windows のチェックアウトで
#   実行ビットが残らないことがあるため。改行は src/.gitattributes で LF に固定している
#   （CRLF になると bash が $'\r': command not found で落ちる）。
#
# 層の根の求め方:
#   このスクリプトの置き場（<層>/hooks/）の親。環境変数・カレントディレクトリ・git の
#   いずれにも依存しない。Git Bash の pwd は /c/Users/... を返すが、この形は Windows の
#   Read ツールや python が扱えない。Git Bash では pwd -W が C:/Users/... を返すので
#   それを使い、-W を知らない bash（macOS・Linux）では pwd に落とす。
#
# 終了番号:
#   0  LAYER_DIR を設定した。または CLAUDE_ENV_FILE が無く、何もしなかった。どちらも
#      標準出力に1行だけ出す（SessionStart の標準出力はセッションの文脈に載るため）。
#   1  層の根を求められなかったか、CLAUDE_ENV_FILE が指すファイルに追記できなかった。
#      stderr に理由と次にすべきことを出す。それに従って直してから、セッションを開き直す。
#
# 使い方:
#   bash <分析層の根>/hooks/set_layer_dir.sh
#   引数は取らない（渡されても見ない）。フックのイベント（標準入力の JSON）も読まない。

set -u

# 変数の展開は、すべて ${name} と波括弧で囲む。macOS 標準の bash 3.2 は UTF-8 ロケールの
# 下で、$name の直後に続く日本語（例: 全角の括弧）の先頭バイトを変数名の一部と読み、
# set -u の下で unbound variable で落ちる。メッセージが日本語なので、$name に戻さないこと。

# --- 層の根（このスクリプトの置き場の親）を求める ------------------------------
# cd の出力は捨てる（CDPATH が設定されていると、cd が移動先を標準出力に出すため）。
if ! layer_root="$(
    cd -- "$(dirname -- "${0}")/.." >/dev/null 2>&1 || exit 1
    pwd -W 2>/dev/null || pwd
)" || [ -z "${layer_root}" ]; then
    printf '%s\n' "ERROR: 分析層の根を求められませんでした: ${0} の置き場の親ディレクトリに移動できません。" >&2
    printf '%s\n' "このスクリプトが <分析層の根>/hooks/ に置かれ、そのディレクトリを読めることを確かめてから、セッションを開き直してください。" >&2
    exit 1
fi

# --- フックとして呼ばれていないとき（手で動かしたとき）は何もしない --------------
env_file="${CLAUDE_ENV_FILE:-}"
if [ -z "${env_file}" ]; then
    printf '%s\n' "CLAUDE_ENV_FILE が無いため LAYER_DIR は設定していません（分析層の根: ${layer_root}）"
    exit 0
fi

# --- export LAYER_DIR=<層の根> を1行追記する ------------------------------------
# 追記に失敗したときの bash のエラー（パス込み）を拾い、最後の ": " より後の理由だけを使う。
printf -v quoted_root '%q' "${layer_root}"
if ! write_error="$( { printf 'export LAYER_DIR=%s\n' "${quoted_root}" >>"${env_file}"; } 2>&1 )"; then
    reason="${write_error##*: }"
    printf '%s\n' "ERROR: LAYER_DIR を後続のコマンドに渡せませんでした: ${env_file} に追記できません（${reason:-理由不明}）。" >&2
    printf '%s\n' "CLAUDE_ENV_FILE が指す先に書き込める状態に直してから、セッションを開き直してください。" >&2
    printf '%s\n' "それまでは LAYER_DIR が空のままなので、分析層の根（${layer_root}）を絶対パスで直接指定してください。" >&2
    exit 1
fi

printf '%s\n' "LAYER_DIR を設定しました: ${layer_root}"
exit 0
