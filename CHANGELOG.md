# Changelog

## 1.0.0 (alpha)

Oaken Mail rebuilt as a thin layer on oaken-core 1.0 and oaken-plugins 1.0, in place of the original application (which is in git history at `cf3b187`).

- `oknpost` is the only command. Mail comes from plugins: Gmail, IMAP and SMTP (one instance per account), the `rfc822` formatter and the `mail` ingestor.
- Only mail goes through: providers must be core's own or tagged `mail`.
- A starting config on first run; the console says "Oaken Mail", with its logo, and calls the default queue `intake`.
- Not yet rebuilt: reading state, folders, Sieve, ask and retrieval, the enrichers and scanners; IMAP is pull only.
