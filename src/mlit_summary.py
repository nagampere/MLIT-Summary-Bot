#%%
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

#%%
def get_fetch_date(days_back: int = 1) -> dt.date:
    """指定日前の日付を JST で取得"""
    fetch_date = dt.datetime.now(JST).date() - dt.timedelta(days=days_back)
    # 土曜日の場合は金曜日に調整
    if fetch_date.weekday() == 5:
        return fetch_date - dt.timedelta(days=1)
    # 日曜日の場合は金曜日に調整
    if fetch_date.weekday() == 6:
        return fetch_date - dt.timedelta(days=2)

    return fetch_date


#%%
def fetch_soup(url: str, timeout: int = 20) -> BeautifulSoup:
    """Fetch URL and return a BeautifulSoup object with correct encoding.

    This function tries to determine the correct charset from the HTTP
    Content-Type header first. If none is found, it falls back to
    requests' apparent_encoding. It decodes the raw content and passes
    the resulting text to BeautifulSoup to avoid mojibake.
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    # Try to get charset from header
    content_type = resp.headers.get("content-type", "")
    m = re.search(r"charset=([^\s;]+)", content_type, flags=re.I)
    encoding = None
    if m:
        encoding = m.group(1).strip().strip('"')

    # Fallback to requests apparent_encoding (uses charset_normalizer/chardet)
    if not encoding:
        encoding = getattr(resp, "apparent_encoding", None)

    # As a final fallback, assume utf-8
    if not encoding:
        encoding = "utf-8"

    try:
        text = resp.content.decode(encoding, errors="replace")
    except Exception:
        # If decoding fails for any reason, fall back to requests.text
        text = resp.text

    return BeautifulSoup(text, "html.parser")

#%%
def fetch_press_releases(days_back: int = 1, limit: int = 20):
    """国交省プレスリリースRSSから直近の項目を取得"""
    feed_url = os.getenv("MLIT_PRESS_RSS", MLIT_PRESS_RSS_DEFAULT)
    d = feedparser.parse(feed_url)

    fetch_date = get_fetch_date(days_back=days_back)
    items = []

    for e in d.entries[:limit]:
        pub_date = None
        if getattr(e, "published_parsed", None):
            t = e.published_parsed
            # convert struct_time (assumed UTC) to JST-aware datetime, then back to struct_time
            dt_utc = dt.datetime(
                t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=dt.timezone.utc
            )
            pub_date = dt_utc.astimezone(JST).date()
        elif getattr(e, "updated_parsed", None):
            t = e.updated_parsed
            # convert struct_time (assumed UTC) to JST-aware datetime, then back to struct_time
            dt_utc = dt.datetime(
                t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=dt.timezone.utc
            )
            pub_date = dt_utc.astimezone(JST).date()
        elif getattr(e, "dc:date", None):
            t = e.dc_date_parsed
            # convert struct_time (assumed UTC) to JST-aware datetime, then back to struct_time
            dt_utc = dt.datetime(
                t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=dt.timezone.utc
            )
            pub_date = dt_utc.astimezone(JST).date()
            print(f"Entry dc:date date: {pub_date} for {e.title}")
        else:
            # 日付が取れない場合はとりあえず昨日扱い
            pub_date = fetch_date

        if fetch_date == pub_date:
            # ページ内テキストをまとめて取得（雑だが汎用）
            detail_url = e.link
            detail_soup = fetch_soup(detail_url)
            text = detail_soup.get_text("\n", strip=True)
            body = textwrap.shorten(text, width=8000, placeholder="...")
            items.append(
                {
                    "kind": "報道発表",
                    "title": e.title,
                    "link": e.link,
                    "date": pub_date.isoformat(),
                    "content": body,
                }
            )

    return items

#%%
def _parse_japanese_date(text: str):
    """『2025年11月18日』形式から date を抜く"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if not m:
        return None
    y, mth, d = map(int, m.groups())
    return dt.date(y, mth, d)

#%%
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
    md_tmp = re.sub(r"\*\*(.+?)\*\*", r"*\1* ", md_tmp)

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

#%%
def fetch_minister_interviews(days_back: int = 1, max_items: int = 5):
    """大臣記者会見一覧から直近の会見を取得し、本文をスクレイピング"""
    soup = fetch_soup(MLIT_DAIJIN_LIST_URL)

    fetch_date = get_fetch_date(days_back=days_back)
    fetch_date_str = fetch_date.strftime("%y%m%d")
    items = []

    # シンプルに一覧ページ内のリンクを上から順にたどる
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # 大臣会見一覧ページでは相対パスで 'daijin...' のようなリンクが来るが、
        # 絶対パス '/report/interview/daijin251118.html' のような場合もあるため
        # 特定の日付ページ（daijin251118.html）を含むリンクも許可する。
        if f"daijin{fetch_date_str}.html" not in href and not href.startswith("daijin"):
            continue
        detail_url = requests.compat.urljoin(MLIT_DAIJIN_LIST_URL, href)
        print(f"Fetching interview detail: {detail_url}")
        detail_soup = fetch_soup(detail_url)

        # ページ内テキストをまとめて取得（雑だが汎用）
        text = detail_soup.get_text("\n", strip=True)
        # 冒頭付近から日付をパース
        pub_date = _parse_japanese_date(text) or fetch_date

        if fetch_date != pub_date:
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

#%%
def build_prompt(interviews, press_releases, days_back: int = 1) -> str:
    """ChatGPT用プロンプトを組み立て"""
    lines = []
    for item in interviews:
        lines.append(
            f"[大臣会見] {item['date']} {item['title']} ({item['link']})\n"
            f"本文抜粋:\n{item['content']}\n"
        )
    for item in press_releases:
        # RSSの summary だけでもそこそこ要旨が分かる
        raw = item.get("content") or ""
        lines.append(
            f"[報道発表] {item['date']} {item['title']} ({item['link']})\n"
            f"本文抜粋:\n{raw}\n"
        )

    source_text = "\n\n".join(lines)
    fetch_date = get_fetch_date(days_back=days_back)

    prompt = f"""
あなたは日本の行政情報に詳しいアシスタントです。
以下に、国土交通省の大臣記者会見と報道発表資料のテキストがあります。

これらを読み、**日本語**で次のようなMarkdown要約を作成してください。

- 全体の冒頭に「本日の国土交通省 大臣会見・報道発表サマリー（{fetch_date}時点）」というタイトル。
- セクションごとに1行の水平線（---）で区切る。
- セクション1: ①大臣記者会見の要点
  - 箇条書きで 3〜8 行程度
  - 政策的に重要そうなポイントは太字で強調
- セクション2: ②報道発表資料の要点
  - リスト形式で「・タイトル（所管局）: 本文の要約」のように短く整理
  - タイトルは20文字以内に要約し、太字で強調
- セクション3: ③業務・投資・研究のインプリケーション
  - 交通計画・都市計画・インフラ投資などの観点から、
    気づき・考察・チェックした方が良さそうな点を2〜4行でコメント

出力は**完全なMarkdownのみ**にしてください（余計な説明文は不要）。

===== 元テキスト =====
{source_text}
"""
    return textwrap.dedent(prompt).strip()

#%%
def summarize_with_ai(interviews, press_releases, days_back: int = 1):
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    prompt = build_prompt(interviews, press_releases, days_back)

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
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
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

#%%
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

#%%
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

    markdown = summarize_with_ai(interviews, press, days_back=days_back)

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

#%%
if __name__ == "__main__":
    main()