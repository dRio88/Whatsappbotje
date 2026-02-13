# auto_update_github.py
import base64
import requests
import os
import json
from datetime import datetime

# ---------- CONFIG ----------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
REPO_OWNER = "dRio88"       # <== zet hier je GitHub username
REPO_NAME = "Whatsappbotje"                 # <== repo naam
MAIN_BRANCH = "main"
GENERATED_FILE = "app.py"                # bestand dat Codex genereert
LOCAL_PATH = f"./generated/{GENERATED_FILE}"
BRANCH_PREFIX = "bot-update-"

# ---------- Branch name ----------
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
new_branch = f"{BRANCH_PREFIX}{timestamp}"

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# ---------- Step 1: Get latest SHA of main branch ----------
url_ref = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/ref/heads/{MAIN_BRANCH}"
resp = requests.get(url_ref, headers=headers)
resp.raise_for_status()
sha_main = resp.json()["object"]["sha"]

# ---------- Step 2: Create new branch ----------
url_create_branch = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/git/refs"
payload_branch = {
    "ref": f"refs/heads/{new_branch}",
    "sha": sha_main
}
resp = requests.post(url_create_branch, headers=headers, data=json.dumps(payload_branch))
resp.raise_for_status()
print(f"✅ Branch '{new_branch}' aangemaakt")

# ---------- Step 3: Read generated file ----------
with open(LOCAL_PATH, "rb") as f:
    content = f.read()
encoded_content = base64.b64encode(content).decode("utf-8")

# ---------- Step 4: Check if file exists ----------
url_file = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{GENERATED_FILE}?ref={new_branch}"
resp = requests.get(url_file, headers=headers)
if resp.status_code == 200:
    sha_file = resp.json()["sha"]
else:
    sha_file = None

# ---------- Step 5: Push file to new branch ----------
payload_file = {
    "message": f"Bot update {GENERATED_FILE}",
    "content": encoded_content,
    "branch": new_branch
}
if sha_file:
    payload_file["sha"] = sha_file

resp = requests.put(url_file, headers=headers, data=json.dumps(payload_file))
resp.raise_for_status()
print(f"✅ Bestand '{GENERATED_FILE}' gepusht naar '{new_branch}'")

# ---------- Step 6: Create PR ----------
url_pr = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/pulls"
payload_pr = {
    "title": f"Bot update {GENERATED_FILE} - {timestamp}",
    "head": new_branch,
    "base": MAIN_BRANCH,
    "body": "Automatisch gegenereerde update door TrendGuard bot (Codex/GPT)"
}
resp = requests.post(url_pr, headers=headers, data=json.dumps(payload_pr))
resp.raise_for_status()
print(f"✅ Pull request aangemaakt: {resp.json()['html_url']}")
