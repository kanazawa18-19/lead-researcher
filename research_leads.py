import os
import re
import requests
from bs4 import BeautifulSoup
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]   # xapp-... (Socket Mode用)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

LEAD_PATTERN = re.compile(r"貴社名[：:]")
PROCESSED_REACTION = "white_check_mark"

# 自分自身のbot_idを起動時に取得（自己ループ防止）
OWN_BOT_ID = app.client.auth_test()["bot_id"]


def is_test(lead):
    company = lead.get("company", "").strip()
    return not company or "テスト" in company or company.lower() == "test"


def parse_lead(text):
    patterns = {
        "name":    r"お名前[：:]\s*(.+)",
        "company": r"貴社名[：:]\s*(.+)",
        "email":   r"メールアドレス[：:]\s*(.+)",
        "phone":   r"連絡先電話番号[：:]\s*(.+)",
        "doc":     r"資料名[：:]\s*(.+)",
    }
    return {k: m.group(1).strip() for k, p in patterns.items() if (m := re.search(p, text))}


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
        "instagram": ("instagram.com",   f"{company_name} site:instagram.com"),
        "rakuten":   ("travel.rakuten.co.jp", f"{company_name} site:travel.rakuten.co.jp"),
        "jalan":     ("jalan.net",        f"{company_name} site:jalan.net"),
        "booking":   ("booking.com",      f"{company_name} site:booking.com"),
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
    prompt = f"""ホテル・旅館の営業担当向けに企業調査サマリーを作成してください。

リード情報:
- 会社名: {lead.get('company', '不明')}
- 担当者: {lead.get('name', '不明')}
- メール: {lead.get('email', '')}
- 電話: {lead.get('phone', '')}
- ダウンロード資料: {lead.get('doc', '')}

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

    msg = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


@app.event("message")
def handle_lead(event, client, logger):
    print(f"EVENT: {event}", flush=True)  # デバッグ用

    # 自分自身の投稿はスキップ（無限ループ防止）
    if event.get("bot_id") == OWN_BOT_ID:
        print(f"SKIPPED own bot message", flush=True)
        return

    ts = event["ts"]
    channel = event["channel"]
    text = event.get("text", "") or ""

    if not LEAD_PATTERN.search(text):
        return

    lead = parse_lead(text)

    if is_test(lead):
        logger.info(f"Skipped (test): {lead.get('company')}")
        return

    if not lead.get("company"):
        logger.info(f"No company found, skipping. text={text[:100]}")
        return

    company = lead["company"]
    logger.info(f"Processing: {company}")

    domain = lead.get("email", "").split("@")[-1] if "@" in lead.get("email", "") else ""
    site_content = fetch_site(domain) if domain else ""
    urls = search_urls(company)

    summary = build_summary(lead, site_content, urls)

    client.chat_postMessage(
        channel=channel,
        thread_ts=ts,
        text=(
            f"*{company}* 調査\n\n"
            f"*【リード情報】*\n```{text}```\n\n"
            f"*【企業調査】*\n{summary}"
        ),
    )
    client.reactions_add(channel=channel, name=PROCESSED_REACTION, timestamp=ts)
    logger.info(f"Done: {company}")


if __name__ == "__main__":
    print("Starting Slack Lead Bot...", flush=True)
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
