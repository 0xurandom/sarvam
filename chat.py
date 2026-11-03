import os
from sarvamai import SarvamAI

api_key = os.getenv("SARVAM_API_KEY")
if not api_key:
    raise SystemExit("Set SARVAM_API_KEY in .env before running this script.")

client = SarvamAI(api_subscription_key=api_key)
response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Say hello in one short sentence."}],
)
print(response.choices[0].message.content)
