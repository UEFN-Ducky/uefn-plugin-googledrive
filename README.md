# Google Drive

Pull 3D models, textures, and audio straight from Google Drive into UEFN. One-click 'Sign in with Google' (read-only), point it at one Drive folder you drop assets into, and Ducky downloads them safely (allowlisted types, size caps, nothing executed) and imports them as ready-to-use assets.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`googledrive`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/googledrive-1.0.10.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `gdrive_api_key`, `gdrive_oauth_client_id`, `gdrive_oauth_client_secret`, `gdrive_oauth_token` locally (DPAPI), not in this package.

## Next release: ship compiled

This plugin still ships its Python source on the Store. Its next release has to ship compiled and signed, the way Ducky Account and Roguelike do:

1. Give `scripts/release.py` and `scripts/build_zip.py` the compiled build from `uefn-plugin-account` (`build_compiled_zip`, upload by ticket, `--plain` only as an escape hatch).
2. Bump `version` and set `min_app_version` to `1.2.357` or newer (the Store keeps older apps from seeing it).
3. Publish, then check the download with the start-up license check (signature, id and version, compiled, team access), not only the signature.
4. A compiled build can't `importlib.reload` its own modules (Python raises SystemError), so reload only when running from source. Before publishing, install the source and compiled zips into a throwaway Ducky and check they register the same panel calls, tools and workflow nodes, and that those calls still work after the plugin reloads.

For this plugin:

- `backend/__init__.py` runs `scripts/selfcheck.py`, which a compiled build doesn't ship. Only run it when the file is there.

Remove this section once a compiled version is live.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
