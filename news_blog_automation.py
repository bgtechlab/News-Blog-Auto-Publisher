import asyncio
from datetime import datetime, timezone, timedelta
import base64
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import requests
import urllib3
from bs4 import BeautifulSoup
from g4f.client import Client
from github import Auth, Github
from github.GithubException import UnknownObjectException, GithubException
from telegram import Bot

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ================= 0. IST DATE / TIME HELPERS =================
# GitHub Actions UTC me chalta hai, isliye hamesha IST (UTC+5:30) use karo.
IST = timezone(timedelta(hours=5, minutes=30))
HINDI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
                "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def ist_now():
    return datetime.now(IST)


def ist_date_hi(dt=None):
    dt = dt or ist_now()
    return f"{dt.day:02d} {HINDI_MONTHS[dt.month - 1]} {dt.year}"


# ================= 1. CONFIGURATION =================
# SURAKSHA NOTE: koi bhi secret yahan hardcode NAHI karna — sirf GitHub Secrets
# (environment variables) se aayega. Isse accidentally public commit hone par
# bhi koi token leak nahi hota.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
REPO_NAME = os.getenv("GITHUB_REPO", "bgnewswab/sgnewswab")
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://bgnewswab.github.io/sgnewswab").rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN environment variable / secret me nahi mila!")

DEFAULT_FALLBACK_IMAGE = f"{SITE_BASE_URL}/assets/images/default-news.jpg"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "hi,en-US;q=0.9,en;q=0.8",
}

client = Client()

# ================= 2. ADVANCED SCRAPER =================
def _has_keyword(text_lower, keywords):
    """English keywords ko poore shabd ki tarah match karta hai (jaise 'ai' ab 'rain' me nahi milega).
    Hindi keywords substring se match hote hain."""
    for k in keywords:
        if k.isascii():
            if re.search(rf"\b{re.escape(k)}\b", text_lower):
                return True
        elif k in text_lower:
            return True
    return False


def fallback_category_detection(text):
    text_lower = text.lower()
    if _has_keyword(text_lower, ["cricket", "match", "dpl", "score", "ipl", "football", "trophy", "sports", "खेल"]):
        return "Sports"
    elif _has_keyword(text_lower, ["election", "bjp", "congress", "parliament", "minister", "modi", "governance", "राजनीति"]):
        return "Politics"
    elif _has_keyword(text_lower, ["ai", "mobile", "app", "tech", "software", "google", "apple", "टेक"]):
        return "Tech"
    elif _has_keyword(text_lower, ["recruitment", "vacancy", "admit card", "exam", "railway", "jobs", "नौकरी"]):
        return "Jobs"
    elif _has_keyword(text_lower, ["sensex", "nifty", "tax", "budget", "bank", "economy", "बिजनेस"]):
        return "Business"
    elif _has_keyword(text_lower, ["movie", "actor", "box office", "bollywood", "cinema", "मनोरंजन"]):
        return "Entertainment"
    return "National"


def _jsonld_articles(soup):
    """JSON-LD blocks se Article/NewsArticle metadata collect karo."""
    found = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = json.loads(raw.strip())
        except Exception:
            continue
        stack = parsed if isinstance(parsed, list) else [parsed]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])
                typ = item.get("@type", [])
                if isinstance(typ, str):
                    typ = [typ]
                if any(t in ("Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting") for t in typ):
                    found.append(item)
    return found


def _meta_content(soup, *keys):
    for key in keys:
        tag = (soup.find("meta", attrs={"property": key}) or
               soup.find("meta", attrs={"name": key}))
        if tag and tag.get("content"):
            value = tag.get("content").strip()
            if value:
                return value
    return ""


def _is_cricinfo_url(url):
    try:
        host = requests.utils.urlparse(url).netloc.lower().split(":")[0]
        return host == "cricinfo.com" or host.endswith(".cricinfo.com") or host == "espncricinfo.com" or host.endswith(".espncricinfo.com")
    except Exception:
        return False


def _resolve_cricinfo_homepage(url, session):
    """Homepage/section URL diya ho to usme se pehla actual Hindi story URL choose karo."""
    parsed = requests.utils.urlparse(url)
    path = parsed.path.rstrip("/")
    if not _is_cricinfo_url(url) or path not in ("/hindi", ""):
        return url
    try:
        response = session.get(url, timeout=20)
        if response.status_code != 200:
            logging.warning("Cricinfo homepage HTTP status: %s", response.status_code)
            return url
        soup = BeautifulSoup(response.content, "html.parser")
        for anchor in soup.select('a[href*="/hindi/story/"]'):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            story_url = requests.compat.urljoin(response.url, href).split("#")[0]
            story_path = requests.utils.urlparse(story_url).path
            if "/hindi/story/" in story_path and story_path.rstrip("/") != "/hindi/story":
                logging.info("🏏 Cricinfo homepage se latest story mili: %s", story_url)
                return story_url
    except Exception as e:
        logging.warning("Cricinfo homepage story discovery failed: %s", e)
    return url


def scrape_news_article(url):
    logging.info(f"🔄 Processing Source URL: {url[:100]}...")

    session = requests.Session()
    session.headers.update(HEADERS)

    # User homepage URL de sakta hai; Cricinfo Hindi homepage se actual story choose karo.
    url = _resolve_cricinfo_homepage(url, session)

    now_ist = ist_now()
    data = {
        "title": "",
        "image": "",
        "content": "",
        "category": "Sports" if _is_cricinfo_url(url) else "National",
        "source_url": url,
        "published_date": ist_date_hi(now_ist),
        "published_time": now_ist.strftime("%I:%M %p") + " IST",
        "published_iso": now_ist.isoformat(),
    }

    res = None
    for attempt in range(3):
        try:
            res = session.get(url, timeout=20)
            if res.status_code == 200:
                break
            logging.warning("Scrape HTTP status %s (attempt %s)", res.status_code, attempt + 1)
        except Exception as e:
            logging.warning(f"⚠️ Scraping attempt {attempt + 1} failed: {e}")
            time.sleep(2)

    if not res or res.status_code != 200:
        logging.error("❌ Website inaccessible via HTTP request.")
        return None

    try:
        soup = BeautifulSoup(res.content, "html.parser")
        structured = _jsonld_articles(soup)
        article_meta = structured[0] if structured else {}

        # Cricinfo pages can render story text in metadata / non-standard DOM;
        # structured metadata and OpenGraph are tried before generic selectors.
        title_candidates = [
            article_meta.get("headline", ""),
            article_meta.get("name", ""),
            _meta_content(soup, "og:title", "twitter:title"),
            (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""),
            (soup.title.get_text(" ", strip=True) if soup.title else ""),
        ]
        for candidate in title_candidates:
            candidate = re.sub(r"\s+", " ", str(candidate or "")).strip()
            candidate = re.sub(r"\s*[-|]\s*(NDTV|AajTak|Jagran|News18|AmarUjala|Dainik|ESPNcricinfo|Cricinfo).*$", "", candidate, flags=re.IGNORECASE)
            if len(candidate) > 5:
                data["title"] = candidate
                break

        img = article_meta.get("image", "")
        if isinstance(img, list):
            img = next((x for x in img if isinstance(x, str)), "")
        if isinstance(img, dict):
            img = img.get("url") or img.get("contentUrl") or ""
        if not isinstance(img, str) or not img.startswith("http"):
            img = _meta_content(soup, "og:image", "twitter:image")
        data["image"] = img if isinstance(img, str) and img.startswith("http") else DEFAULT_FALLBACK_IMAGE

        # First prefer publisher article containers, then known body selectors.
        content_selectors = [
            '[data-testid*="article"]', '[class*="StoryBody"]', '[class*="story-body"]',
            '[class*="article-body"]', '[class*="ArticleBody"]', '[class*="content"]',
            "article", ".article-content", ".story-content", ".detail-content",
            ".news-content", ".main-content", ".post-content", ".entry-content",
            "#articleBody", "#storyContent"
        ]
        full_content = ""
        for selector in content_selectors:
            content_elem = soup.select_one(selector)
            if not content_elem:
                continue
            for unwanted in content_elem.select("script, style, iframe, nav, footer, .social-share, .advertisement, .related-stories"):
                unwanted.decompose()
            content_parts = [
                re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
                for p in content_elem.find_all("p")
            ]
            content_parts = [p for p in content_parts if len(p) > 35]
            candidate_text = "\n\n".join(content_parts[:30])
            if len(candidate_text) > len(full_content):
                full_content = candidate_text
            if len(full_content) >= 250:
                break

        # Some publishers expose the story body directly in NewsArticle JSON-LD.
        if len(full_content) < 120:
            body = article_meta.get("articleBody") or article_meta.get("description") or ""
            if isinstance(body, str) and len(body.strip()) > len(full_content):
                full_content = body.strip()

        # Last fallback: visible paragraphs, excluding menus and tiny fragments.
        if len(full_content) < 120:
            paragraphs = []
            for p in soup.find_all("p"):
                text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
                if len(text) > 45 and text not in paragraphs:
                    paragraphs.append(text)
            full_content = "\n\n".join(paragraphs[:20])

        if not data["title"]:
            logging.error("Article title extraction failed for %s", url)
            return None
        if not full_content.strip():
            logging.error("Article body extraction failed for %s", url)
            return None

        data["content"] = full_content.strip()
        data["category"] = "Sports" if _is_cricinfo_url(url) else fallback_category_detection(f"{data['title']} {data['content']}")
        logging.info("✅ Extracted article: title=%r, content_chars=%s", data["title"], len(data["content"]))

    except Exception as e:
        logging.exception(f"⚠️ Scraping Error: {e}")
        return None

    return data


# ================= 3. ENHANCED SEO & AI GENERATOR =================
def get_ai_response(prompt):
    models = ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]
    for model_name in models:
        try:
            time.sleep(1)
            res = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            content = res.choices[0].message.content
            if content and len(content.strip()) > 10:
                clean_content = re.sub(r"^```[a-zA-Z]*\n?", "", content.strip())
                return re.sub(r"\n?```$", "", clean_content)
        except Exception:
            continue
    return ""


def generate_seo_content_hinglish(news_data):
    title = news_data["title"]
    content = news_data["content"]
    source_url = news_data["source_url"]

    prompt = f"""
    तुम एक Professional Hindi SEO News Editor और Google News Expert हो।
    दिए गए न्यूज आर्टिकल को बिना तथ्य बदले Google Search, Google News और Google Discover के लिए पूरी तरह SEO Optimized JSON Structure में लिखो।

    Headline: {title}
    Reference Material: {content}

    Strict JSON Schema Output Format Required:
    {{
        "seo_title": "60-65 अक्षरों का बिना Clickbait वाला Focus Keyword युक्त SEO शीर्षक",
        "meta_description": "150-160 वर्णों का Meta Description जिसमें Focus Keyword हो",
        "url_slug": "english-seo-friendly-slug",
        "focus_keyword": "2-4 शब्दों का मुख्य Keyword",
        "related_keywords": ["Keyword1", "Keyword2", "Keyword3"],
        "short_summary": "2 छोटे पैराग्राफ में खबर की भूमिका (Short Summary)",
        "quick_summary": ["बिंदु 1", "बिंदु 2", "बिंदु 3", "बिंदु 4"],
        "featured_snippet": "40-60 शब्दों में सटीक उत्तर (Featured Snippet Answer)",
        "article_body": "700-1000 शब्दों का विस्तार से लिखा गया आर्टिकल। इसमें अनिवार्य रूप से <h2> और <h3> Heading टैग्स का प्रयोग करें जैसे: <h2>क्या है मामला?</h2>, <h2>सरकार ने क्या कहा?</h2>, <h2>इससे किसे फायदा होगा?</h2>, <h2>आगे क्या होगा?</h2>। भाषा सरल, निष्पक्ष और पत्रकारिता स्तर की होनी चाहिए।",
        "faqs": [
            {{"question": "प्रश्न 1?", "answer": "उत्तर 1"}},
            {{"question": "प्रश्न 2?", "answer": "उत्तर 2"}},
            {{"question": "प्रश्न 3?", "answer": "उत्तर 3"}},
            {{"question": "प्रश्न 4?", "answer": "उत्तर 4"}},
            {{"question": "प्रश्न 5?", "answer": "उत्तर 5"}}
        ],
        "category": "One from [Politics, Sports, Tech, Jobs, Business, Entertainment, National]",
        "tags": ["Tag1", "Tag2", "Tag3", "Tag4", "Tag5"]
    }}
    """

    raw_response = get_ai_response(prompt)

    # Structured fallback (agar AI ka JSON kharab ya adhura ho)
    cat_fallback = fallback_category_detection(f"{title} {content}")
    fallback_slug = re.sub(r"[^\w\s-]", "", title.lower()).strip().replace(" ", "-")[:50]
    fallback_data = {
        "seo_title": title[:65],
        "meta_description": f"{title[:140]}... पूरा विवरण पढ़ें।",
        "url_slug": fallback_slug or f"news-{int(time.time())}",
        "focus_keyword": title.split()[0] if title.split() else "News",
        "related_keywords": ["Breaking News", "Latest Update"],
        "short_summary": f"<p>{title}</p>",
        "quick_summary": [title],
        "featured_snippet": title[:60],
        "article_body": f"<h2>मुख्य समाचार</h2><p>{content}</p>",
        "faqs": [],
        "category": cat_fallback,
        "tags": ["News", "Updates"],
    }

    try:
        parsed = json.loads(raw_response)
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception:
        parsed = {}

    # AI ke khali/missing fields fallback se bhar jaate hain (KeyError nahi aayega)
    ai_data = {**fallback_data, **{k: v for k, v in parsed.items() if v}}

    valid_categories = ["Politics", "Sports", "Tech", "Jobs", "Business", "Entertainment", "National"]
    if ai_data.get("category") not in valid_categories:
        ai_data["category"] = cat_fallback

    faqs = [
        f for f in ai_data.get("faqs", [])
        if isinstance(f, dict) and f.get("question") and f.get("answer")
    ]

    # Generate Google FAQ Schema (JSON-LD)
    faq_schema = ""
    if faqs:
        schema_dict = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": item["question"],
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": item["answer"]
                    }
                } for item in faqs
            ]
        }
        faq_schema = f'<script type="application/ld+json">\n{json.dumps(schema_dict, ensure_ascii=False, indent=2)}\n</script>'

    # Construct Clean HTML Code for Rendering
    quick_bullets = "".join([f"<li>{item}</li>" for item in ai_data.get("quick_summary", [])])

    faq_html_list = ""
    for faq in faqs:
        faq_html_list += f"<h4>Q: {faq['question']}</h4><p>A: {faq['answer']}</p>"

    final_html = f"""
    <div class="short-summary-box">
        {ai_data.get('short_summary', '')}
    </div>

    <div class="quick-summary-box">
        <h3>📌 मुख्य बिंदु (Quick Summary)</h3>
        <ul>
            {quick_bullets}
        </ul>
    </div>

    <div class="featured-snippet-box">
        <p><strong>संक्षेप में:</strong> {ai_data.get('featured_snippet', '')}</p>
    </div>

    {ai_data.get('article_body', '')}

    <div class="article-faq-section">
        <h2>अक्सर पूछे जाने वाले प्रश्न (FAQ)</h2>
        {faq_html_list}
    </div>

    <div class="source-credit">
        <p><strong>स्रोत और संदर्भ:</strong> यह रिपोर्ट प्राथमिक जानकारी एवं <a href="{source_url}" target="_blank" rel="nofollow noopener">आधिकारिक स्रोत</a> के आधार पर निष्पक्ष रूप से तैयार की गई है।</p>
    </div>

    {faq_schema}
    """

    return {
        "seo_title": ai_data["seo_title"],
        "meta_description": ai_data["meta_description"],
        "article_html": final_html,
        "category": ai_data["category"],
        "url_slug": ai_data["url_slug"],
        "focus_keyword": ai_data.get("focus_keyword", ""),
        "tags": ai_data.get("tags", [])
    }


# ================= 4. SITEMAP & ROBOTS GENERATORS =================
def generate_sitemap_xml(news_list):
    urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

    today_ist = ist_now().strftime("%Y-%m-%d")

    u_node = ET.SubElement(urlset, "url")
    ET.SubElement(u_node, "loc").text = f"{SITE_BASE_URL}/"
    ET.SubElement(u_node, "lastmod").text = today_ist
    ET.SubElement(u_node, "changefreq").text = "always"
    ET.SubElement(u_node, "priority").text = "1.0"

    for item in news_list[:100]:
        node = ET.SubElement(urlset, "url")
        ET.SubElement(node, "loc").text = f"{SITE_BASE_URL}/article.html?id={item['id']}"
        # Har khabar ki apni asli tarikh (purani khabron me 'published' na ho to aaj ki)
        ET.SubElement(node, "lastmod").text = (item.get("published") or today_ist)[:10]
        ET.SubElement(node, "changefreq").text = "weekly"
        ET.SubElement(node, "priority").text = "0.8"

    return ET.tostring(urlset, encoding="utf-8", method="xml").decode("utf-8")


def generate_robots_txt():
    return f"""User-agent: *
Allow: /

Sitemap: {SITE_BASE_URL}/sitemap.xml
"""


# ================= 5. PUBLISH TO GITHUB =================
def publish_to_github_batch(seo_data, news_data):
    if not GITHUB_TOKEN or GITHUB_TOKEN == "YOUR_GITHUB_TOKEN":
        logging.error("❌ GITHUB_TOKEN सेट नहीं है!")
        return None

    try:
        auth = Auth.Token(GITHUB_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(REPO_NAME)

        json_file_path = "data/news.json"

        try:
            file_content = repo.get_contents(json_file_path, ref="main")
            # GitHub Contents API may return encoding="none" for large files.
            # In that case ContentFile.decoded_content cannot be used; read the blob directly.
            if getattr(file_content, "encoding", None) == "base64":
                raw_json = file_content.decoded_content
            else:
                logging.info("ℹ️ data/news.json returned encoding=%s; reading Git blob directly.", getattr(file_content, "encoding", None))
                blob = repo.get_git_blob(file_content.sha)
                # PyGithub GitBlob exposes base64 text in .content, not .decoded_content.
                raw_json = base64.b64decode(blob.content)
            existing_data = json.loads(raw_json.decode("utf-8"))
            file_sha = file_content.sha
        except UnknownObjectException as e:
            if getattr(e, "status", None) != 404:
                raise
            logging.warning(f"ℹ️ {json_file_path} does not exist; creating it.")
            existing_data = []
            file_sha = None
        except Exception:
            logging.exception(f"❌ Cannot read {json_file_path}; not treating it as missing.")
            raise


        for article in existing_data:
            if article.get("source_url") == news_data["source_url"] or article.get("slug") == seo_data["url_slug"]:
                logging.warning("⚠️ यह खबर पहले से मौजूद है। Skipping...")
                return f"{SITE_BASE_URL}/article.html?id={article['id']}"

        new_id = max([item.get("id", 0) for item in existing_data], default=0) + 1
        related_ids = [item["id"] for item in existing_data if item.get("category") == seo_data["category"]][:3]

        img_alt_text = f"{seo_data['focus_keyword']} - {seo_data['seo_title']}"

        new_article = {
            "id": new_id,
            "slug": seo_data["url_slug"],
            "title": seo_data["seo_title"],
            "summary": seo_data["meta_description"],
            "content": seo_data["article_html"],
            "author": "SG News Team",
            "date": news_data["published_date"],
            "time": news_data["published_time"],
            "published": news_data["published_iso"],
            "category": seo_data["category"],
            "image": news_data["image"],
            "image_alt": img_alt_text,
            "tags": seo_data.get("tags", ["News"]),
            "isHero": True,
            "isTrending": True,
            "views": f"{round(1.2 + (new_id % 5) * 0.4, 1)}k",
            "readTime": f"{max(2, len(seo_data['article_html'].split()) // 150)} मिनट",
            "source_url": news_data["source_url"],
            "related_ids": related_ids
        }

        for item in existing_data:
            item["isHero"] = False

        existing_data.insert(0, new_article)

        if len(existing_data) > 100:
            existing_data = existing_data[:100]

        updated_json_str = json.dumps(existing_data, ensure_ascii=False, indent=2)
        published_url = f"{SITE_BASE_URL}/article.html?id={new_id}"

        if file_sha:
            try:
                latest_file = repo.get_contents(json_file_path, ref="main")
                repo.update_file(json_file_path, f"Auto Publish ID {new_id}: {seo_data['seo_title']}", updated_json_str, latest_file.sha, branch="main")
            except GithubException as e:
                if getattr(e, "status", None) == 422:
                    latest_file = repo.get_contents(json_file_path, ref="main")
                    repo.update_file(json_file_path, f"Auto Publish ID {new_id}: {seo_data['seo_title']} (retry)", updated_json_str, latest_file.sha, branch="main")
                else:
                    raise
        else:
            repo.create_file(json_file_path, f"Auto Create ID {new_id}: {seo_data['seo_title']}", updated_json_str, branch="main")


        try:
            r_file = repo.get_contents("robots.txt", ref="main")
            repo.update_file("robots.txt", "Auto Update robots.txt", generate_robots_txt(), r_file.sha, branch="main")
        except UnknownObjectException as e:
            if getattr(e, "status", None) != 404:
                raise
            repo.create_file("robots.txt", "Auto Create robots.txt", generate_robots_txt(), branch="main")


        try:
            s_file = repo.get_contents("sitemap.xml", ref="main")
            repo.update_file("sitemap.xml", "Auto Update sitemap.xml", generate_sitemap_xml(existing_data), s_file.sha, branch="main")
        except UnknownObjectException as e:
            if getattr(e, "status", None) != 404:
                raise
            repo.create_file("sitemap.xml", "Auto Create sitemap.xml", generate_sitemap_xml(existing_data), branch="main")


        logging.info(f"✅ पब्लिश सफल! URL: {published_url}")
        return published_url

    except Exception as e:
        logging.exception(f"❌ GitHub Batch Commit Error: {e}")
        raise


# ================= 6. TELEGRAM POSTING =================
async def send_telegram_post(news_data, seo_data, post_url):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        return

    caption = f"""📰 <b>{seo_data['seo_title']}</b>

📂 <b>श्रेणी:</b> #{seo_data['category']}
📅 <b>प्रकाशित:</b> {news_data['published_date']}, {news_data['published_time']}
📖 <b>पूरी रिपोर्ट:</b> {post_url}"""

    temp_img_path = f"temp_tg_{int(time.time())}.jpg"
    photo_sent = False

    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            if news_data.get("image") and news_data["image"].startswith("http"):
                try:
                    img_res = requests.get(news_data["image"], headers=HEADERS, timeout=10)
                    if img_res.status_code == 200:
                        with open(temp_img_path, "wb") as f:
                            f.write(img_res.content)
                        with open(temp_img_path, "rb") as photo_file:
                            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo_file, caption=caption[:1024], parse_mode="HTML")
                        photo_sent = True
                except Exception as e:
                    logging.warning(f"⚠️ Telegram Photo Upload Error: {e}")
                finally:
                    if os.path.exists(temp_img_path):
                        os.remove(temp_img_path)

            if not photo_sent:
                try:
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"⚠️ Telegram Delivery Error: {e}")
    except Exception as e:
        logging.error(f"❌ Telegram Bot Error: {e}")


# ================= 7. MAIN PROCESS =================
async def process_news(url):
    news_data = scrape_news_article(url)
    if not news_data:
        raise RuntimeError("Scrape failed: article data extract nahi hua.")

    seo_data = generate_seo_content_hinglish(news_data)
    if not seo_data:
        raise RuntimeError("AI generation failed: valid SEO response nahi mila.")

    post_url = publish_to_github_batch(seo_data, news_data)
    if not post_url:
        raise RuntimeError("Publish failed: GitHub URL return nahi hua.")

    if post_url:
        await send_telegram_post(news_data, seo_data, post_url)

    return post_url


async def _notify_telegram_plain(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Telegram notify failed: {e}")


async def main():
    # GitHub Actions ke liye: URLs env var (NEWS_URLS, space/comma separated) se aate hain.
    # Agar woh nahi mila to command line args, warna (local testing ke liye) manually poochega.
    env_urls = os.getenv("NEWS_URLS", "").strip()
    if env_urls:
        input_urls = env_urls
    elif len(sys.argv) > 1:
        input_urls = " ".join(sys.argv[1:])
    else:
        input_urls = input("\n🔗 News URL(s) - Space separated: ").strip()

    urls_list = [u.strip() for u in re.split(r"[\s,]+", input_urls) if u.strip().startswith("http")]

    if not urls_list:
        logging.error("❌ कोई वैध URL नहीं मिला।")
        await _notify_telegram_plain("⚠️ Koi valid news URL provide nahi hui.")
        return

    await _notify_telegram_plain(f"🚀 <b>News automation shuru hua</b>\n{len(urls_list)} URL(s) process honge.")

    any_success = False
    for url in urls_list:
        try:
            post_url = await process_news(url)
            logging.info(f"✅ Completed: {post_url}")
            any_success = True
        except Exception as e:
            logging.exception(f"❌ Error processing {url}")
            await _notify_telegram_plain(
                f"❌ Process fail ho gaya.\n🔗 {url}\n\n"
                f"<b>Exact error:</b> <code>{str(e)[:1200]}</code>"
            )
        await asyncio.sleep(5)

    if not any_success:
        await _notify_telegram_plain("⚠️ Kisi bhi URL se news publish nahi ho payi.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Process stopped by user.")
    except Exception as e:
        logging.error(f"Execution Error: {e}")
        try:
            asyncio.run(_notify_telegram_plain(f"❌ <b>Automation CRASH ho gaya</b>\n<code>{e}</code>"))
        except Exception:
            pass
        raise
