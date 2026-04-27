import base64
content_b64 = ""
content = base64.b64decode(content_b64).decode("utf-8")
with open(r"overlay\opt\pisowifi\backend\app.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Written", len(content), "chars")