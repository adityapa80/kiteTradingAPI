# Deploying Kite Terminal to the VPS (Ubuntu 24.04)

Server: any Ubuntu 24.04 VPS with a static IPv4. Below, replace `YOUR_SERVER_IP` with its address.

**How it fits together:**
- **On the server:** the app runs as a background service, listening only on the server itself (`127.0.0.1:5000`). Nothing is exposed to the internet, and the firewall allows SSH only.
- **From your PC:** an SSH tunnel makes it appear at `http://127.0.0.1:5000`, exactly as when it ran locally. The Kite redirect URL therefore stays `http://127.0.0.1:5000/callback`, and no HTTPS certificate is needed.
- **Automatic upkeep:** the service restarts itself if it stops, and the server installs security updates automatically.

---

## 1. One-time: SSH shortcut on your PC

In PowerShell, run `notepad $env:USERPROFILE\.ssh\config` and add:

```
Host kite
  HostName YOUR_SERVER_IP
  User root
  LocalForward 5000 127.0.0.1:5000
  ServerAliveInterval 30
  ExitOnForwardFailure yes
```

Check that key login works. This should log you in **without a password**:

```powershell
ssh kite
```

Type `exit` to leave.

## 2. Upload the app (PowerShell, on your PC)

```powershell
tar -czf $env:TEMP\kite-terminal.tgz --exclude=data --exclude=.env --exclude=__pycache__ --exclude=.claude --exclude=.venv -C C:\path\to\kiteapi .
scp $env:TEMP\kite-terminal.tgz kite:/root/
```

## 3. Install (on the server)

```bash
ssh kite
rm -rf /root/kite-src && mkdir -p /root/kite-src && tar -xzf /root/kite-terminal.tgz -C /root/kite-src
bash /root/kite-src/deploy/setup.sh
```

The script does all of the following:
- installs Python, a firewall (allowing SSH only), fail2ban and automatic security updates
- creates a locked-down `kite` service user
- installs the app to `/opt/kite-terminal`
- schedules a nightly backup

## 4. Add your Kite credentials (on the server)

```bash
nano /opt/kite-terminal/.env
```

Set `KITE_API_KEY` and `KITE_API_SECRET` from developers.kite.trade, and keep `KITE_MOCK=0`. Save with **Ctrl+O, Enter, Ctrl+X**, then run:

```bash
systemctl restart kite-terminal
journalctl -u kite-terminal -n 20 --no-pager
```

You should see `Kite Terminal running at http://127.0.0.1:5000 (LIVE mode)`.

In the Kite developer console, the app's **Redirect URL** must be `http://127.0.0.1:5000/callback`.

## 5. Use it (every trading day)

1. On your PC, start the tunnel and leave the window open:
   ```powershell
   ssh -N kite
   ```
2. Open **http://127.0.0.1:5000**, then click **Login with Kite**.
3. Check that the **LIVE** badge is on.

Notes:
- **One login a day:** Kite tokens expire around 6 AM, so you log in once a day. The token is stored on the server, so closing the tunnel or restarting the app doesn't log you out.
- **Close the tunnel when you like:** the server keeps running, and keeps managing MIS TP/SL, with your browser closed or the tunnel off.
- **Port clash:** don't run the local copy of the app on your PC at the same time, because both use port 5000.

## 6. Register the IP with Kite (after the network monitor passes)

On the server, run:

```bash
kite-netreport
```

It should show about 5 clean trading days and exactly one IP. Then go to **developers.kite.trade → Profile → IP whitelist**, enter `YOUR_SERVER_IP`, and save. Changes are limited to once per week.

Before that, everything except placing, modifying and cancelling orders works.

## 7. Lock down SSH (after step 1 works)

```bash
bash /root/kite-src/deploy/setup.sh --lock-ssh
```

This turns off password login entirely, so only your SSH key works. The script refuses to run if no key is installed.

Then take a **snapshot** in your VPS provider's control panel. That gives you a known-good restore point.

## 8. Off-server backup (on your PC)

Every night at 23:30 the server writes `/root/backups/kite-DATE.tgz`, containing `.env` and the TP/SL records, and keeps 14 days.

To copy the backups to your PC (`%USERPROFILE%\kite-terminal-backups`), run this once:

```powershell
powershell -NoProfile -File C:\path\to\kiteapi\deploy\backup-to-pc.ps1 -Server YOUR_SERVER_IP
```

To schedule it daily:

```powershell
schtasks /Create /SC DAILY /ST 21:00 /TN "Kite Terminal backup" /TR "powershell -NoProfile -File C:\path\to\kiteapi\deploy\backup-to-pc.ps1 -Server YOUR_SERVER_IP"
```

The scheduled task needs your SSH key to work without a passphrase prompt. Either the key has no passphrase, or the Windows `ssh-agent` service holds it.

## Updating the app later

Repeat steps 2 and 3. `setup.sh` is safe to re-run: it never touches `.env` or `data/`, and it restarts the service at the end.

## Troubleshooting

| Symptom | Check |
|---|---|
| Page doesn't load | Is the `ssh -N kite` window still open? On the server: `systemctl status kite-terminal` |
| App crashes or restarts | `journalctl -u kite-terminal -n 50 --no-pager` |
| Badge shows POLLING | The live feed is down. It usually means the Kite login expired, so log in again |
| Orders rejected: "IP … not allowed" | The IP isn't whitelisted yet (step 6), or the server's IP changed. Check `curl -4 https://api.ipify.org` |
| `bind: address already in use` when starting the tunnel | Something on your PC already uses port 5000, probably the local copy of the app. Stop it |
