# DEVA Universal Video Downloader — Starter

यह एक **starter prototype** है, “हर वेबसाइट से 100% डाउनलोड” वाला तैयार सिस्टम नहीं।

## इसमें क्या है?
- Blogger Page के लिए responsive frontend
- Python FastAPI backend
- `yt-dlp` से supported public video pages की metadata/format analysis
- login cookies, passwords, DRM bypass या private-content access नहीं
- arbitrary server-side file relay बंद है; `/api/download` फिलहाल 501 देता है

## 1. Backend locally चलाएँ
Python 3.10+ इस्तेमाल करें:

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

API health check: `http://127.0.0.1:8000/health`
API docs: `http://127.0.0.1:8000/docs`

## 2. Backend deploy करें
ऐसी Python hosting चुनें जो FastAPI और `yt-dlp` dependency को अनुमति देती हो। Free tiers बदलते रहते हैं; हमेशा provider की current limits जाँचें। Build/root directory `backend` रखें और start command इस्तेमाल करें:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Deployment के बाद backend URL नोट करें, जैसे `https://your-api-host.example`। यह केवल उदाहरण है, वास्तविक URL नहीं।

## 3. Blogger पर frontend जोड़ें
1. Blogger → Pages → New Page खोलें।
2. HTML view चुनें।
3. `blogger/deva-video-downloader.html` का पूरा content paste करें।
4. इस लाइन को खोजें:

```js
const API_BASE = "https://YOUR-BACKEND-DOMAIN.example";
```

5. इसे अपने असली deployed backend URL से बदलें, trailing slash न रखें।
6. Page publish करें।

यदि Blogger theme script को sanitize करे, तो HTML/JS को theme या supported HTML/JavaScript gadget में जोड़ना पड़ सकता है।

## सुरक्षा और सीमाएँ
- यह starter बाहरी public URLs analyze करता है; हर extractor/website हमेशा काम नहीं करेगा।
- `/api/download` जानबूझकर disabled है। किसी भी URL से files को server के जरिए relay करना bandwidth abuse और SSRF जैसी सुरक्षा समस्याएँ पैदा कर सकता है।
- Public production service के लिए authentication, rate limits, strict network egress restrictions, request size/time limits, abuse reporting, quotas, logging policy और exact CORS origins जोड़ें।
- URL validation में DNS rebinding/redirect-based SSRF जोखिम को केवल application code से पूरी तरह समाप्त नहीं किया जा सकता। Production में outbound network controls आवश्यक हैं।
- केवल वही content डाउनलोड करें जिसके लिए आपके पास अधिकार/अनुमति है और स्रोत के नियमों का पालन करें।
- DRM, login walls, private content या अन्य access controls को bypass न करें।
- यह code किसी third-party host पर स्वतः deploy नहीं करता; आपको hosting account पर खुद deployment करना होगा।


## 4. Offline tests
Backend folder se run karein:

```bash
python -m pip install -r requirements.txt
python -m pip install pytest httpx
pytest -q
```

Tests public network ko call nahi karte; extractor ko test stub se replace karke API response, URL validation aur disabled download relay check karte hain. Real video metadata extraction ke liye deployed environment mein `yt-dlp` dependency aur supported source ka live access chahiye.
