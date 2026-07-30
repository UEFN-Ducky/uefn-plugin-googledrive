# Google Drive

Pull 3D models, textures, and audio straight from Google Drive into UEFN. One-click 'Sign in with Google' (read-only), point it at one Drive folder you drop assets into, and Ducky downloads them safely (allowlisted types, size caps, nothing executed) and imports them as ready-to-use assets.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`googledrive`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/googledrive-*.ducky-plugin.zip` (scripts/ and deploy/ are not packed).
