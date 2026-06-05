Purpose
-------
Email deliverability and forwarding utilities: bounce checking (check_bounces.py), SQLite store (email_forwarder.db), logs, and JSON credentials/config.

How to use
----------
1. Fill email_config.json and credentials_FINAL.json with SMTP or provider settings valid on this machine only; restrict file permissions.
2. Run python check_bounces.py or python forward_emails.py as appropriate; watch Logs/ and forward_emails.log for results.
3. Back up email_forwarder.db before schema experiments; treat the database as sensitive (addresses and message metadata).
4. Never commit real credentials to a public repository.
