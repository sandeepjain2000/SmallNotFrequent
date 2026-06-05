import imaplib
import smtplib
import email
from email.message import EmailMessage
import json
import os
import sqlite3
import logging
import sys
from datetime import datetime
from email.utils import parseaddr
import time

# =============================================
# CONFIG
# =============================================
CONFIG_FILE = "email_config.json"
TARGET_FORWARD_EMAIL = "sandeepjain200019@gmail.com"
DB_PATH = "email_forwarder.db"
LOG_FILE = "forward_emails.log"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

BOUNCE_SENDERS = [
    "mailer-daemon@",
    "postmaster@",
    "noreply@",
    "no-reply@",
    "mailer@",
    "system@",
    "administrator@"
]

BOUNCE_SUBJECTS = [
    "delivery status notification",
    "undeliverable",
    "mail delivery failed",
    "returned mail",
    "failure notice",
    "delivery failure",
    "non-delivery",
    "message not delivered",
    "unable to deliver",
    "bounce",
    "out of office",
    "auto-reply",
    "autoreply"
]

CAREER_KEYWORDS = [
    "job", "career", "profession", "opportunity", "interview", "application", 
    "resume", "cv", "hiring", "recruiter", "recruitment", "candidate", 
    "vacancy", "role", "position", "freelance", "contract", "full-time", 
    "part-time", "offer letter", "onboarding", "talent", "assessment", 
    "profile", "open application"
]

JUNK_KEYWORDS = [
    "newsletter", "black friday", "cyber monday", "sale", "discount", 
    "limited time offer", "buy now", "promo code", "promotion", "marketing",
    "unsubscribe", "special offer"
]

# =============================================
# LOGGING SETUP
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger("email_forwarder")

# =============================================
# DATABASE HELPERS
# =============================================
def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            accounts_checked INTEGER DEFAULT 0,
            emails_forwarded INTEGER DEFAULT 0,
            emails_skipped INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            account TEXT,
            original_sender TEXT,
            subject TEXT,
            status TEXT,
            processed_at TEXT
        )
    """)
    conn.commit()
    return conn

# =============================================
# PROCESSING HELPERS
# =============================================
def is_system_or_bounce(msg) -> bool:
    """Check if an email message is a bounce/NDR/system mail."""
    sender_raw = msg.get("From", "") or ""
    _, sender_email = parseaddr(sender_raw)
    sender = sender_email.lower()
    subject = (msg.get("Subject", "") or "").lower()

    for pattern in BOUNCE_SENDERS:
        if pattern in sender:
            return True
    
    if subject == "": # sometimes empty subject is a system mail or bounce
        return True

    for pattern in BOUNCE_SUBJECTS:
        if pattern in subject:
            return True
    return False

def is_career_related(subject, body):
    subject_lower = subject.lower()
    body_lower = body.lower()
    
    # Priority check: Skip other filters if 'Sandeep Jain' is in the subject
    if "sandeep jain" in subject_lower:
        return True
    
    # Strictly filter out junk first
    for word in JUNK_KEYWORDS:
        if word in subject_lower:
            return False
            
    # Then check if it has career words
    for word in CAREER_KEYWORDS:
        if word in subject_lower or word in body_lower:
            return True
            
    return False

def extract_body(msg, html=False):
    target_type = 'text/html' if html else 'text/plain'
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == target_type and not str(part.get('Content-Disposition', '')).startswith("attachment"):
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(errors='replace') + "\n"
    else:
        if msg.get_content_type() == target_type:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors='replace')
    return body

def create_forward_message(original_msg, from_addr, to_addr):
    fwd_msg = EmailMessage()
    original_subject = original_msg.get('Subject', 'No Subject').replace('\r', '').replace('\n', '')
    fwd_msg['Subject'] = f"Fwd: {original_subject}"
    fwd_msg['From'] = from_addr
    fwd_msg['To'] = to_addr
    
    comment = f"--- Automated Forwarding from {from_addr} ---\nOriginal Sender: {original_msg.get('From', 'Unknown')}\nDate: {original_msg.get('Date', 'Unknown')}\n"
    
    plain_body = extract_body(original_msg, html=False)
    html_body = extract_body(original_msg, html=True)
    
    if not plain_body and html_body:
        fwd_msg.set_content(comment + "\n[Original email was HTML format. Check below]")
        comment_html = comment.replace('\n', '<br>')
        fwd_msg.add_alternative(f"<div style='background-color:#f0f0f0; padding:10px; margin-bottom:10px;'><b>{comment_html}</b></div>{html_body}", subtype='html')
    elif plain_body and html_body:
        fwd_msg.set_content(comment + "\n\n" + plain_body)
        comment_html = comment.replace('\n', '<br>')
        fwd_msg.add_alternative(f"<div style='background-color:#f0f0f0; padding:10px; margin-bottom:10px;'><b>{comment_html}</b></div>{html_body}", subtype='html')
    else:
        fwd_msg.set_content(comment + "\n\n" + (plain_body if plain_body else "[Could not decode original email text or body is empty]"))
        
    return fwd_msg

# =============================================
# MAIN PROCESS LOGIC
# =============================================
def main():
    logger.info("=" * 60)
    logger.info("  STARTING EMAIL FORWARDER")
    logger.info("=" * 60)

    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file not found: {CONFIG_FILE}")
        return

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    
    accounts = config.get("profiles", {})
    if not accounts:
        logger.info("No accounts found in config.")
        return

    conn = setup_db()
    
    # Create execution record
    cur = conn.execute("INSERT INTO executions (started_at) VALUES (?)", (datetime.now().isoformat(),))
    conn.commit()
    execution_id = cur.lastrowid
    
    stats = {
        "accounts_checked": 0,
        "emails_forwarded": 0,
        "emails_skipped": 0,
        "errors": 0
    }

    for email_addr, app_password in accounts.items():
        if email_addr.lower() == TARGET_FORWARD_EMAIL.lower():
            logger.info(f"Skipping target forwarding email: {email_addr}")
            continue
            
        logger.info(f"\nProcessing account: {email_addr}")
        stats["accounts_checked"] += 1
        
        try:
            # 1. Connect via IMAP to read unread emails
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(email_addr, app_password)
            mail.select("INBOX")
            
            _, data = mail.search(None, 'UNSEEN')
            unread_ids = data[0].split()
            
            if not unread_ids:
                logger.info(f"  No unread emails found.")
                mail.logout()
                continue
                
            logger.info(f"  Found {len(unread_ids)} unread email(s)")
            
            # 2. Setup SMTP for forwarding
            smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
            smtp.login(email_addr, app_password)
            
            forward_count = 0
            
            for msg_id in unread_ids:
                try:
                    _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
                    raw_email = msg_data[0][1]
                    email_message = email.message_from_bytes(raw_email)
                    
                    original_subject = email_message.get("Subject", "No Subject").replace('\r', '').replace('\n', '')
                    original_sender = email_message.get("From", "Unknown")
                    
                    # Check if already processed to avoid duplicate forwarding since we keep them unread
                    cur_check = conn.execute(
                        "SELECT id FROM processed_emails WHERE account = ? AND subject = ? AND original_sender = ?",
                        (email_addr, original_subject, original_sender)
                    )
                    if cur_check.fetchone():
                        logger.info(f"  ⏭️ Already processed previously: {original_subject} from {original_sender}")
                        continue
                    
                    # Log to DB helper function
                    def log_to_db(status):
                        conn.execute("""
                            INSERT INTO processed_emails (execution_id, account, original_sender, subject, status, processed_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (execution_id, email_addr, original_sender, original_subject, status, datetime.now().isoformat()))
                        conn.commit()

                    if is_system_or_bounce(email_message):
                        logger.info(f"  ⏭️ Skipped System/Bounce: {original_subject} from {original_sender}")
                        log_to_db("skipped")
                        stats["emails_skipped"] += 1
                    else:
                        plain_text = extract_body(email_message, html=False)
                        html_text = extract_body(email_message, html=True)
                        combined_text = plain_text + " " + html_text
                        if not is_career_related(original_subject, combined_text):
                            logger.info(f"  ⏭️ Skipped Marketing/Non-Job: {original_subject} from {original_sender}")
                            log_to_db("skipped")
                            stats["emails_skipped"] += 1
                        else:
                            if forward_count >= 5:
                                logger.info("  🛑 Reached limit of 5 forwards for this account. Remaining unread emails deferred.")
                                break
                                
                            logger.info(f"  ➔ Forwarding: {original_subject} from {original_sender}")
                            fwd_msg = create_forward_message(email_message, email_addr, TARGET_FORWARD_EMAIL)
                            smtp.send_message(fwd_msg)
                            log_to_db("forwarded")
                            stats["emails_forwarded"] += 1
                            forward_count += 1

                            
                            # Wait one minute between forwards
                            if forward_count < 5:
                                logger.info("  ⏳ Waiting 60 seconds before next forward...")
                                time.sleep(60)
                    
                except Exception as e:
                    logger.error(f"  Error processing msg_id {msg_id.decode()}: {e}")
                    stats["errors"] += 1
            
            smtp.quit()
            mail.logout()
            
        except imaplib.IMAP4.error as e:
            logger.error(f"  IMAP Authentication error for {email_addr}: {e}")
            stats["errors"] += 1
        except Exception as e:
            logger.error(f"  General error for {email_addr}: {e}")
            stats["errors"] += 1

    # Finalize execution record
    conn.execute("""
        UPDATE executions
        SET finished_at = ?, accounts_checked = ?, emails_forwarded = ?, emails_skipped = ?, errors = ?
        WHERE id = ?
    """, (
        datetime.now().isoformat(),
        stats["accounts_checked"],
        stats["emails_forwarded"],
        stats["emails_skipped"],
        stats["errors"],
        execution_id
    ))
    conn.commit()
    conn.close()

    logger.info("=" * 60)
    logger.info("  EXECUTION SUMMARY")
    logger.info(f"  Accounts Checked : {stats['accounts_checked']}")
    logger.info(f"  Emails Forwarded : {stats['emails_forwarded']}")
    logger.info(f"  Emails Skipped   : {stats['emails_skipped']}")
    logger.info(f"  Errors           : {stats['errors']}")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
