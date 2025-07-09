# 🛠️ SupportPal to Jira Migration Script

This Python script automates the migration of support tickets from a **SupportPal** MySQL database to **Jira Cloud**, including messages, attachments, and ticket metadata.

---

## 📦 Features

* Export either a **single ticket** or **all tickets** from SupportPal.
* Creates corresponding Jira issues with:
  * Original ticket subject and creation date
  * Full message history as comments
  * Attachments via SFTP
  * Priority mapping
* Automatically transitions the issue to "Resolve this issue"
* Preserves timestamps from the original ticket system

---

## 🔧 Requirements

* Python 3.6+
* SSH access to the SupportPal server for attachments
* A Jira Cloud account with API access

### Python Libraries

Install the required dependencies with:

```bash
pip install mysql-connector-python paramiko beautifulsoup4 jira
```

---

## 🔐 Configuration

All configuration is hardcoded in the script. Modify the following sections accordingly:

### MySQL Database (SupportPal)

```python
mysql_conf = {
    'host':     '127.0.0.1',
    'port':     3307,
    'database': 'support_pal',
    'user':     'spal_dbuser',
    'password': 'your-db-password',
    'charset':  'utf8mb4',
}
```

### SSH for Attachments

```python
ssh_conf = {
    'hostname': 'your.server.ip',
    'port':     22,
    'username': 'root',
    'password': 'your-ssh-password'
}
```

### Jira Credentials

```python
JIRA_URL       = 'https://your-domain.atlassian.net'
JIRA_USER      = 'your-email@domain.com'
JIRA_API_TOKEN = 'your-api-token'
JIRA_PROJECT   = 'PROJECTKEY'
JIRA_ISSUETYPE = '[System] Service request'
```

> **Important:** Never commit your credentials to version control. Use environment variables or `.env` files with `python-dotenv` for better security.

---

## 🚀 Usage

Run the script in your terminal:

```bash
python3 migrate_supportpal_to_jira.py
```

You’ll be prompted to choose:

```
Export single ticket or all tickets? Enter 1 for single, 2 for all:
```

* **1**: Enter a specific SupportPal ticket number.
* **2**: Export all tickets from the database.

---

## 📂 Attachments Handling

Attachments are pulled from a remote server via **SFTP** and uploaded to Jira.

They are temporarily stored in:

```
SupportPal to Jira/attachments/
```

Make sure the SSH user has access to the SupportPal storage path:

```python
remote_attachment_path = "/var/www/html/storage/app/tickets"
```

---

## 🗂️ Priority Mapping

SupportPal → Jira Priority mapping:

| SupportPal ID | Jira Priority      |
| ------------- | ------------------ |
| 1             | Wishlist           |
| 2             | Nice To Have       |
| 3             | Must Have          |
| 4             | Must Have - Urgent |

---

## 📘 Sample Output

```
Found 4 ticket(s) to export.
Creating issue for SupportPal #1532...
✅ Created QSD-214 with priority 'Must Have'
→ Forced transition to 'Resolve this issue'
→ Set Jira created/updated to 2024-11-17T10:34:22
→ Added 3 comments.
→ Uploaded attachment: screenshot.png
...
✅ Done importing tickets.
```

---

## 🧹 Cleanup

* Closes MySQL connection
* Closes SSH and SFTP sessions

---

## ⚠️ Security Warning

This script contains **hardcoded secrets** for demonstration. In production:

* Use `.env` or secret manager
* Never commit sensitive credentials
* Limit permissions of SSH and DB users

---
