import os
import re
import smtplib
import textwrap
import datetime as dt
import zoneinfo
from dotenv import load_dotenv
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
import feedparser
from openai import OpenAI
from anthropic import Anthropic
import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

MLIT_PRESS_RSS_DEFAULT = "https://www.mlit.go.jp/pressrelease.rdf"
MLIT_DAIJIN_LIST_URL = "https://www.mlit.go.jp/report/interview/daijin.html"


def fetch_press_releases(days_back: int = 1, limit: int = 20):
    """国交省プレスリリースRSSから直近の項目を取得"""
    feed_url = os.getenv("MLIT_PRESS_RSS", MLIT_PRESS_RSS_DEFAULT)
    d = feedparser.parse(feed_url)

    today = dt.datetime.now(JST).date()
    items = []

    for e in d.entries[:limit]:
        pub_date = None
        if getattr(e, "published_parsed", None):
            t = e.published_parsed
            pub_date = dt.date(t.tm_year, t.tm_mon, t.tm_mday)
        elif getattr(e, "updated_parsed", None):
            t = e.updated_parsed
            pub_date = dt.date(t.tm_year, t.tm_mon, t.tm_mday)
        else:
            # 日付が取れない場合はとりあえず今日扱い
            pub_date = today

        if (today - pub_date).days <= days_back:
            items.append(
                {
                    "kind": "報道発表",
                    "title": e.title,
                    "link": e.link,
                    "date": pub_date.isoformat(),
                    "raw_summary": getattr(e, "summary", ""),
                }
            )

    return items


def _parse_japanese_date(text: str):
    """『2025年11月18日』形式から date を抜く"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if not m:
        return None
    y, mth, d = map(int, m.groups())
    return dt.date(y, mth, d)


def _convert_md_to_slack(md: str) -> str:
    """簡易的に GitHub スタイルの Markdown を Slack の mrkdwn 表現に変換する。

    - **bold** -> *bold*
    - 見出し (#, ##, ###) は行全体を太字にする
    - リストの先頭 '- ' や '* ' を '• ' に置換
    - 3バッククオートのコードブロックはそのまま残す (Slack も ``` をサポート)

    完璧な変換ではありませんが、主要なスタイルが Slack で見やすくなります。
    """
    # preserve code blocks
    code_blocks = {}
    def _code_repl(m):
        key = f"__CODEBLOCK_{len(code_blocks)}__"
        code_blocks[key] = m.group(0)
        return key

    md_tmp = re.sub(r"```[\s\S]*?```", _code_repl, md)

    # bold **text** -> *text*
    md_tmp = re.sub(r"\*\*(.+?)\*\*", r"*\1*", md_tmp)

    # headings: lines starting with # -> make bold and remove leading hashes
    def _hdr(m):
        line = m.group(0)
        text = re.sub(r"^#+\s*", "", line)
        return f"*{text}*"

    md_tmp = re.sub(r"(?m)^[ \t]*#{1,6}\s+.*$", _hdr, md_tmp)

    # lists: - or * at line start -> bullet
    md_tmp = re.sub(r"(?m)^[ \t]*[-*]\s+", "• ", md_tmp)

    # restore code blocks
    for k, v in code_blocks.items():
        md_tmp = md_tmp.replace(k, v)

    return md_tmp


def fetch_minister_interviews(days_back: int = 1, max_items: int = 5):
    """大臣記者会見一覧から直近の会見を取得し、本文をスクレイピング"""
    res = requests.get(MLIT_DAIJIN_LIST_URL, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    today = dt.datetime.now(JST).date()
    today_str = today.strftime("%y%m%d")
    items = []

    # シンプルに一覧ページ内のリンクを上から順にたどる
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # 大臣会見一覧ページでは相対パスで 'daijin...' のようなリンクが来るが、
        # 絶対パス '/report/interview/daijin251118.html' のような場合もあるため
        # 特定の日付ページ（daijin251118.html）を含むリンクも許可する。
        if f"daijin{today_str}.html" not in href and not href.startswith("daijin"):
            continue
        detail_url = requests.compat.urljoin(MLIT_DAIJIN_LIST_URL, href)
        print(f"Fetching interview detail: {detail_url}")

        detail_res = requests.get(detail_url, timeout=20)
        detail_res.raise_for_status()
        detail_soup = BeautifulSoup(detail_res.text, "html.parser")

        # ページ内テキストをまとめて取得（雑だが汎用）
        text = detail_soup.get_text("\n", strip=True)
        # 冒頭付近から日付をパース
        pub_date = _parse_japanese_date(text) or today
        
        if (today - pub_date).days > days_back:
            continue

        # 本文テキストは長くなりすぎるので適度にトリム
        body = textwrap.shorten(text, width=8000, placeholder="...")

        items.append(
            {
                "kind": "大臣会見",
                "title": a.get_text(strip=True),
                "link": detail_url,
                "date": pub_date.isoformat(),
                "content": body,
            }
        )

        if len(items) >= max_items:
            break

    return items


def build_prompt(interviews, press_releases):
    """ChatGPT用プロンプトを組み立て"""
    lines = []
    for item in interviews:
        lines.append(
            f"[大臣会見] {item['date']} {item['title']} ({item['link']})\n"
            f"本文抜粋:\n{item['content']}\n"
        )
    for item in press_releases:
        # RSSの summary だけでもそこそこ要旨が分かる
        raw = item.get("raw_summary") or ""
        lines.append(
            f"[報道発表] {item['date']} {item['title']} ({item['link']})\n"
            f"概要（RSS）:\n{raw}\n"
        )

    source_text = "\n\n".join(lines)

    prompt = f"""
あなたは日本の行政情報に詳しいアシスタントです。
以下に、国土交通省の大臣記者会見と報道発表資料のテキストがあります。

これらを読み、**日本語**で次のようなMarkdown要約を作成してください。

- 全体の冒頭に「本日の国土交通省 大臣会見・報道発表サマリー（YYYY-MM-DD）」というタイトル
- セクション1: 大臣記者会見の要点
  - 箇条書きで 3〜8 行程度
  - 政策的に重要そうなポイントは太字で強調
- セクション2: 報道発表資料の要点
  - リスト形式で「・タイトル（所管局）: 要約」のように短く整理
- セクション3: 研究・業務の仮想的なインプリケーション（任意）
  - 交通計画・都市計画・インフラ投資などの観点から、
    気づきやチェックした方が良さそうな点を2〜4行でコメント

出力は**完全なMarkdownのみ**にしてください（余計な説明文は不要）。

===== 元テキスト =====
{source_text}
"""
    return textwrap.dedent(prompt).strip()


def summarize_with_ai(interviews, press_releases):
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    prompt = build_prompt(interviews, press_releases)

    used_model = None  # 後でMarkdownに明記する用

    if provider == "claude":
        # Claude
        api_key = os.getenv("ANTHROPIC_API_KEY")
        model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        used_model = f"Claude ({model})"

        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_md = response.content[0].text

    elif provider == "gemini":
        # Gemini
        api_key = os.getenv("GEMINI_API_KEY")
        model = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
        used_model = f"Gemini ({model})"

        genai.configure(api_key=api_key)
        gmodel = genai.GenerativeModel(model)
        response = gmodel.generate_content(prompt)
        # テキストのみを取り出し
        summary_md = response.text

    else:
        # OpenAI（デフォルト）
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        used_model = f"OpenAI ({model})"

        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model,
            input=prompt,
        )
        summary_md = response.output_text

    # 末尾に「どのAIを使ったか」をMarkdownで追記
    footer = f"\n\n---\n_この要約は **{used_model}** を用いて自動生成されました。_"
    return summary_md.strip() + footer


def send_to_slack(markdown_text: str, debug: bool = False):
    """
    Slack SDK（slack_sdk.WebClient）を使ってメッセージ送信
    debug=True のときは SLACK_DEBUG_CHANNEL_ID（自分DMなど）に送る
    """
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        print("Slackのトークンが設定されていないためスキップします")
        return

    client = WebClient(token=token)

    normal_channel = os.getenv("SLACK_CHANNEL_ID")
    debug_channel = os.getenv("SLACK_DEBUG_CHANNEL_ID")

    if debug and debug_channel:
        channel = debug_channel
        print(f"Slackデバッグモード: {channel} に送信します")
    else:
        channel = normal_channel
        print(f"Slack通常モード: {channel} に送信します")

    if not channel:
        print("Slackの送信先チャンネルIDが設定されていないためスキップします")
        return

    # Markdown -> Slack mrkdwn に簡易変換
    slack_text = _convert_md_to_slack(markdown_text)

    # Slack の blocks 内のテキストは約 3000 文字が実用上の上限なので段落単位で分割する
    def _chunk_text(s: str, limit: int = 3000):
        paragraphs = s.split("\n\n")
        chunks = []
        cur = ""
        for p in paragraphs:
            if cur:
                candidate = cur + "\n\n" + p
            else:
                candidate = p
            if len(candidate) > limit:
                if cur:
                    chunks.append(cur)
                    cur = p
                    if len(cur) > limit:
                        # さらに段落自体が長い場合は強制的に分割
                        for i in range(0, len(cur), limit):
                            chunks.append(cur[i : i + limit])
                        cur = ""
                else:
                    for i in range(0, len(p), limit):
                        chunks.append(p[i : i + limit])
                    cur = ""
            else:
                cur = candidate
        if cur:
            chunks.append(cur)
        return chunks

    chunks = _chunk_text(slack_text)

    # blocks を作成して送信
    blocks = []
    for c in chunks:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": c}})

    try:
        resp = client.chat_postMessage(
            channel=channel,
            text=(chunks[0] if chunks else markdown_text)[:2000],  # fallback text
            blocks=blocks,
        )
        if not resp.get("ok"):
            raise RuntimeError(f"Slack送信に失敗しました: {resp}")
    except SlackApiError as e:
        # Slack API エラー詳細を出しておくとデバッグしやすい
        print(f"SlackApiError: {e.response.get('error')}")
        raise


def send_email(markdown_text: str):
    """SMTP（例: Gmail）でメール送信"""
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    to_addr = os.getenv("SMTP_TO")
    from_addr = os.getenv("SMTP_FROM", user)

    if not (host and user and password and to_addr):
        print("メール設定がないためスキップします")
        return

    msg = EmailMessage()
    today = dt.datetime.now(JST).date().isoformat()
    msg["Subject"] = f"国交省 大臣会見・報道発表サマリー {today}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(markdown_text)

    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(user, password)
        server.send_message(msg)


def main():
    days_back = int(os.getenv("MLIT_DAYS_BACK", "1"))
    debug_mode = os.getenv("SLACK_DEBUG_MODE", "false").lower() in ("1", "true", "yes", "on")

    interviews = fetch_minister_interviews(days_back=days_back)
    press = fetch_press_releases(days_back=days_back)

    if not interviews and not press:
        print("対象期間内のデータがありませんでした")
        return

    markdown = summarize_with_ai(interviews, press)

    # 読み込んだ HTML ファイルの URL を明示的に付与して Slack/ファイルに埋め込む
    sources_lines = ["\n\n---\n## ソース"]
    for it in interviews:
        sources_lines.append(f"- [大臣会見] {it.get('title','')} : {it.get('link','')}")
    for pr in press:
        sources_lines.append(f"- [報道発表] {pr.get('title','')} : {pr.get('link','')}")

    sources_md = "\n".join(sources_lines)

    # 末尾にソース一覧を付与して出力・送信する
    full_markdown = markdown.strip() + sources_md

    with open("latest_summary.md", "w", encoding="utf-8") as f:
        f.write(full_markdown)

    delivery = os.getenv("DELIVERY", "slack")  # slack / email / both

    if delivery in ("slack", "both"):
        send_to_slack(full_markdown, debug=debug_mode)

    if delivery in ("email", "both"):
        send_email(full_markdown)

    print("完了しました")


if __name__ == "__main__":
    main()