# Stock Market Game connector for Claude

Lets Claude read your Stock Market Game account, look up live prices, and place or cancel trades
(with a preview step first).

## 1. Put the code on GitHub
1. Make a free account at github.com.
2. Click **+ → New repository**, name it `smg-connector`, choose **Private**, and create it.
3. Click **uploading an existing file**, drag in `server.py`, `requirements.txt`, and this README, and click **Commit changes**.

## 2. Host it on Render (free)
1. Make a free account at render.com and connect your GitHub.
2. Click **New → Web Service** and pick your `smg-connector` repo.
3. Fill in:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python server.py`
   - **Instance type:** Free
4. Under **Environment Variables**, add:
   | Key | Value |
   |---|---|
   | `SMG_USERNAME` | your game username |
   | `SMG_PASSWORD` | your game password |
   | `CONNECTOR_SECRET` | a long random string (mash the keyboard, 30+ letters and numbers, no spaces or slashes) |
5. Click **Create Web Service** and wait for "Live". Copy your address, like `https://smg-connector-abcd.onrender.com`.

## 3. Add it to Claude
1. In Claude, go to **Settings → Connectors → Add custom connector**.
2. Name: `Stock Market Game`
3. URL: `https://YOUR-RENDER-ADDRESS/YOUR_CONNECTOR_SECRET/mcp`
4. Click **Add**, then turn it on in a chat with **+ → Connectors**.

Keep this URL private: anyone who has it can trade on your account.

## Notes
- Free Render servers go to sleep after ~15 minutes idle. The first request after that can take up to a minute.
- **First test:** ask Claude to "preview buying 1 share of AAPL". Previews never place trades. If the site
  rejects the preview, ask Claude to run `find_trade_codes` and tell you which values to set. You can
  set them as environment variables on Render (`SMG_EQTYPE`, `SMG_CODE_BUY`, `SMG_CODE_SELL`,
  `SMG_CODE_SHORT`, `SMG_CODE_COVER`, `SMG_CODE_MARKET`, `SMG_CODE_LIMIT`, `SMG_CODE_STOP`) without touching code.
- To change your password later, update `SMG_PASSWORD` on Render.
