import requests

BOT_TOKEN = "7427093027:AAGNwxAR-lonTMwkPQQaBiIx778IMKVHefg"
WEBHOOK_URL = "https://web-production-f5fc2.up.railway.app/webhook"

response = requests.get(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBHOOK_URL}"
)

print(response.status_code)
print(response.text)
