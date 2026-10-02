# News Blog Auto Publisher — GitHub पर

## ⚠️ जरूरी (करने के बाद भूलें नहीं)
पुराने कोड में GitHub token और Telegram token सीधे लिखे हुए थे — वो मान लें **leak हो चुके हैं**:
1. GitHub token को Settings → Developer settings → Personal access tokens में जाकर **Delete/Revoke** करें, नया बनाएं।
2. Telegram token @BotFather से `/mybots` → अपना बॉट → **Revoke current token** करके नया लें।
नए दोनों tokens नीचे बताई secrets में डालें, पुराने अब काम नहीं आने चाहिए।

## रिपो में डालनी हैं ये फाइलें
```
news_blog_automation.py
requirements.txt
.github/workflows/news-blog-publisher.yml
```

## Secrets (Settings → Secrets and variables → Actions)

| Secret Name          | Value                                   |
|------------------------|--------------------------------------------|
| `GH_PAT`                | GitHub Personal Access Token (scope: `repo`) |
| `TELEGRAM_BOT_TOKEN`    | नया Telegram बॉट token                      |
| `TELEGRAM_CHAT_ID`      | आपकी chat id                               |

(अगर यह workflow `bgnewswab/sgnewswab` रिपो के अंदर ही चलेगी, तो `GH_PAT` की जगह built-in token से भी काम चल सकता है — वो बदलाव बाद में बता दीजिएगा, अभी के लिए PAT वाला तरीका हर जगह काम करेगा।)

## इस्तेमाल कैसे करें
Actions → "News Blog Auto Publisher" → Run workflow → `news_urls` में एक या कई लिंक (space/comma से अलग करके) डालें → Run।

## क्या बदला है
- Hardcoded tokens हटाकर सिर्फ secrets से आने लायक बनाया।
- URL अब `NEWS_URLS` env var से भी आ सकता है (GitHub Actions input से)।
- हर स्टेज (कोई URL ना मिलना, scrape/AI/publish fail, पूरा crash) पर Telegram पर बताएगा।
