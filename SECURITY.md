# Security policy

## Reporting a vulnerability

Please use GitHub's private security-advisory workflow for suspected vulnerabilities. Do not include credentials, private media paths, subtitle content, database files, or logs in a public issue.

Include the affected version, a minimal reproduction using synthetic data, and the expected security impact. Remove usernames, hostnames, IP addresses, account identifiers, and media titles before submitting diagnostic output.

## Sensitive local data

Each managed library contains a `.media-manager/` directory. It can include an absolute library path, OpenSubtitles account configuration, a password file, run history, media paths, checkpoints, logs, downloaded subtitles, and temporary work products. Movie directories may also contain `.media-manager.json` markers.

These files are runtime state, not source code. Never commit or attach them. The repository `.gitignore` excludes the usual state, credential, media, subtitle, model, and log formats, but users should still inspect every staged file before publishing.
