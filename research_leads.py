import os
import requests
from bs4 import BeautifulSoup
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

PROCESSED_REACTION = "white_check_mark"
OWN_BOT_ID = app.client.auth_test()["bot_id"]


def get_email_file(event):
    """メールファイルがあれば本文テキストを返す。なければNone。"""
    for f in event.get("files", []):
        if f.get("filetype") == "email" or f.get("mimetype", "").startswith("text"):
            raw = requests.get(
                f["url_private_download"],
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                timeout=10,
            ).text
            if f.get("mimetype") == "text/html":
                return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
            return raw
    return None


def extract_lead_with_claude(email_text):
    """Claudeでリード情報を抽出する。テスト・無効な場合はNoneを返す。"""
    msg = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下のメール本文からリード情報を抽出してください。

{email_text[:3000]}

以下のJSON形式で返してください（該当情報がない場合は空文字）:
{{
  "company": "会社名・施設名",
  "name": "担当者名",
  "email": "メールアドレス",
  "phone": "電話番号",
  "doc": "資料名またはお問い合わせ種別"
}}

会社名が「テスト」や空の場合、またはリード情報が含まれていない場合は null を返してください。"""
        }]
    )
    import json
    try:
        text = msg.content[0].text.strip()
        if text == "null":
            return None
        # コードブロックを除去
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None


def fetch_site(domain):
    for url in [f"https://{domain}", f"https://www.{domain}"]:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                texts = [t.get_text(" ", strip=True)
                         for t in soup.find_all(["title", "h1", "h2", "h3", "p", "address"])]
                return "\n".join(texts)[:5000]
        except Exception:
            continue
    return ""


def search_urls(company_name):
    targets = {
        "instagram": ("instagram.com",       f"{company_name} site:instagram.com"),
        "rakuten":   ("travel.rakuten.co.jp", f"{company_name} site:travel.rakuten.co.jp"),
        "jalan":     ("jalan.net",            f"{company_name} site:jalan.net"),
        "booking":   ("booking.com",          f"{company_name} site:booking.com"),
    }
    results = {}
    for key, (domain, query) in targets.items():
        try:
            r = requests.get(
                "https://www.google.com/search",
                params={"q": query, "num": 3},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a["href"]
                if "/url?q=" in href:
                    url = href.split("/url?q=")[1].split("&")[0]
                    if domain in url:
                        results[key] = url
                        break
        except Exception:
            pass
    return results


def build_summary(lead, site_content, urls):
    msg = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"""ホテル・旅館の営業担当向けに企業調査サマリーを作成してください。

リード情報:
- 会社名: {lead.get('company', '不明')}
- 担当者: {lead.get('name', '不明')}
- メール: {lead.get('email', '')}
- 電話: {lead.get('phone', '')}
- 問い合わせ種別: {lead.get('doc', '')}

企業サイト内容:
{site_content or '（取得できませんでした）'}

以下の形式で出力してください（情報がない場合は「なし」）:

• **所在地**:
• **業種・事業内容**:
• **規模感**:
• **最近のニュースやトピック**:
• **公式HP URL**: {urls.get('hp', 'なし')}
• **Instagram URL**: {urls.get('instagram', 'なし')}
• **公式LINE URL**: {urls.get('line', 'なし')}
• **楽天トラベルURL**: {urls.get('rakuten', 'なし')}
• **じゃらんネットURL**: {urls.get('jalan', 'なし')}
• **booking.com URL**: {urls.get('booking', 'なし')}"""
        }]
    )
    return msg.content[0].text


@app.event("message")
def handle_lead(event, client, logger):
    # 自分自身の投稿はスキップ
    if event.get("bot_id") == OWN_BOT_ID:
        return

    # メールファイルがない場合はスキップ
    email_text = get_email_file(event)
    if email_text is None:
        return

    print(f"EMAIL RECEIVED: {email_text[:200]}", flush=True)

    ts = event["ts"]
    channel = event["channel"]

    # Claudeでリード情報を抽出
    lead = extract_lead_with_claude(email_text)
    if not lead:
        print("Skipped: not a lead or test", flush=True)
        return

    company = lead.get("company", "")
    print(f"Processing: {company}", flush=True)

    domain = lead.get("email", "").split("@")[-1] if "@" in lead.get("email", "") else ""
    site_content = fetch_site(domain) if domain else ""
    urls = search_urls(company)
    summary = build_summary(lead, site_content, urls)

    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=(
            f"*{company}* 調査\n\n"
            f"*【リード情報】*\n```{email_text[:1000]}```\n\n"
            f"*【企業調査】*\n{summary}"
        ),
    )
    client.reactions_add(channel=channel, name=PROCESSED_REACTION, timestamp=ts)
    print(f"Done: {company}", flush=True)


if __name__ == "__main__":
    print("Starting Slack Lead Bot...", flush=True)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
