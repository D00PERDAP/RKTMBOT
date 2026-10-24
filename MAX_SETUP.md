# MAX bot — development deployment

The MAX adapter is in `max_bot.py`. It uses the same schedule parsing and SQLite file as Telegram, but stores MAX user profiles separately. It does not modify Telegram handlers.

**Important:** MAX officially supports long polling for development/testing only. For a public production launch migrate to a webhook subscription. Do not run a MAX webhook subscription and long polling at the same time.

## VPS setup

After merging this PR:

```bash
cd /opt/rktm
git pull
grep '^MAX_BOT_TOKEN=' .env >/dev/null && echo 'MAX token configured'
.venv/bin/python -m py_compile max_bot.py
.venv/bin/python max_bot.py
```

In MAX, send `/start` and then `/groups`. Select an item by its number and use `/today`.

Once the interactive test works, stop with Ctrl+C and install a separate service:

```ini
# /etc/systemd/system/rktm-max.service
[Unit]
Description=RKTM MAX bot (development polling)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/rktm
ExecStart=/opt/rktm/.venv/bin/python /opt/rktm/max_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rktm-max
sudo journalctl -u rktm-max -f
```

Use a dedicated unprivileged service account when available. Do not commit `.env` or print your token in logs.

## MVP commands

`/start`, `/help`, `/groups`, `/teachers`, `/next`, `/prev`, `/today`, `/day 0` through `/day 5`, `/profile`, `/save`, `/favorites`, `/news`.

This first version is text-based. Favorite switching, inline buttons, notifications, and MAX admin functions are not implemented yet.
