"""
GEMINI_API_KEY が有効かどうか、使えるモデルは何かを確認するだけのデバッグ用スクリプト。
"""

import os
import json

import requests

key = os.environ["GEMINI_API_KEY"]
resp = requests.get(
    "https://generativelanguage.googleapis.com/v1beta/models", params={"key": key}
)
data = resp.json()

if "models" in data:
    print("OK: このキーで使えるモデル一覧")
    for m in data["models"]:
        if "generateContent" in m.get("supportedGenerationMethods", []):
            print(" -", m["name"])
else:
    print("ERROR RESPONSE:")
    print(json.dumps(data, ensure_ascii=False, indent=2))
