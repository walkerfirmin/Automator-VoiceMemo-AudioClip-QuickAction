# Automator Quick Action: Import audio into Voice Memos

This guide configures [`import_voice_memo.py`](import_voice_memo.py) as a **macOS Quick Action** (right‑click in Finder) so selected audio files are copied into the Voice Memos library and registered in `CloudRecordings.db`.

---

## Prerequisites

1. **Python 3**  
   Use the system interpreter (typically `/usr/bin/python3`) or note the full path from `which python3` if you use Homebrew’s Python.

2. **FFmpeg** (`ffprobe` required; `ffmpeg` required if you use `--re-encode`)  
   Install e.g. with Homebrew: `brew install ffmpeg`  
   Quick Action shells often have a **minimal `PATH`**, so the setup below explicitly prepends Homebrew’s `bin` directories.

3. **Script location**  
   Put `import_voice_memo.py` in a **stable path without spaces** if possible, for example:

   ```bash
   mkdir -p ~/bin
   cp /path/to/import_voice_memo.py ~/bin/import_voice_memo.py
   chmod +x ~/bin/import_voice_memo.py   # optional; you can still call python3 explicitly
   ```

   You can keep the script in iCloud or other folders, but you must **quote paths correctly** in the shell step.

4. **At least one real memo in Voice Memos**  
   The script clones an existing `ZCLOUDRECORDING` row; an empty library cannot be used as a template.

5. **Audio format**  
   Voice Memos expects **`.m4a`** (AAC). The script warns on other extensions; use **`--re-encode`** in the Quick Action for the best chance of playback.

---

## Privacy: Full Disk Access

The script reads and writes under:

`~/Library/Group Containers/group.com.apple.VoiceMemos.shared/`

(and may auto-detect other Voice Memos library paths). macOS **TCC** often blocks that unless the **app that runs the workflow** has **Full Disk Access**.

1. Open **System Settings → Privacy & Security → Full Disk Access**.
2. Enable it for the process that runs your Quick Action, for example:
   - **Automator** (if you run or test from Automator)
   - **Shortcuts** (if you use Shortcuts instead)
   - Any helper listed when the action runs (sometimes related to **Workflow**, **Services**, or **Finder** automation—if import fails with “Operation not permitted”, add the app shown in the error or try adding **Finder** after testing)

3. **Restart** the app you added (or log out/in) so the change applies.

Without this, you may see the script’s message about granting Full Disk Access to your **terminal**; for Quick Actions, the **runner** is not Terminal—you must grant the **workflow host** instead.

### Add Automator manually
System Settings → Privacy & Security → Full Disk Access → (+)
Press Cmd+Shift+G and open:

`/System/Applications → Automator.app`
or
`/Applications → Automator.app`

Enable the toggle and restart Automator (or log out/in if it still fails).

Add Finder (very common fix for Finder → Quick Actions)

Same + flow:
`/System/Library/CoreServices/Finder.app`
Enable it, then Option+right-click Finder in the Dock → Relaunch.

---

## Voice Memos must be quit

`import_voice_memo.py` exits if **Voice Memos** is running, to avoid SQLite/WAL corruption.

**Two-step habit:** Quit Voice Memos (`Cmd+Q`), then run the Quick Action.

---

## Create the Quick Action in Automator

1. Open **Automator** → **File → New** → choose **Quick Action** → **Choose**.
2. In the right-hand panel, set:
   - **Workflow receives current** → **files or folders** in **Finder**.
   - (Optional) Check **Output replaces selected text** only if you know you need it; for file import, usually leave defaults.
3. In the library, add **Utilities → Run Shell Script**.
4. Configure **Run Shell Script**:
   - **Shell:** `/bin/bash` or `/bin/zsh` (either is fine).
   - **Pass input:** **as arguments** (so Finder passes each selected file path as `"$1"`, `"$2"`, …).

5. Paste a script like the following and **adjust paths** to match your Mac.

### Example shell script

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PYTHON="/usr/bin/python3"
SCRIPT="$HOME/bin/import_voice_memo.py"

# Optional: fail fast if nothing selected
if [[ $# -eq 0 ]]; then
  echo "No files selected." >&2
  exit 1
fi

exec "$PYTHON" "$SCRIPT" "$@" --label-from-filename --re-encode --recordings-dir "/Users/$USER/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
```

- **`PYTHON`:** run `which python3` and substitute if you do not want `/usr/bin/python3`.
- **`SCRIPT`:** must point to your real `import_voice_memo.py`.
- **`"$@"`:** forwards **all** selected Finder items to the script (supports multiple files).
- **`--label-from-filename`:** memo title = file name without extension.
- **`--re-encode`:** re-encodes to AAC M4A for better Voice Memos compatibility (requires `ffmpeg` on `PATH` above).

**Without re-encode** (faster, but less reliable for non‑M4A sources):

```bash
exec "$PYTHON" "$SCRIPT" "$@" --label-from-filename
```

**Fixed title for every import** (ignore file name):

```bash
exec "$PYTHON" "$SCRIPT" "$@" --label "Imported from Finder"
```

6. **File → Save** and give the workflow a short name (e.g. **Import to Voice Memos**). It appears under **Finder → Quick Actions** (and sometimes **Services** on older macOS).

---

## First run checklist

1. Quit **Voice Memos**.
2. In Finder, select one **`.m4a`** file (or other audio if you use `--re-encode`).
3. Right‑click → **Quick Actions** → your saved action.
4. Open **Voice Memos** and confirm the new memo appears and plays.

If nothing appears, run in Terminal (same machine, Full Disk Access for **Terminal**):

```bash
python3 ~/bin/import_voice_memo.py --list-libraries
```

If the library with the **largest row count** is not the one the script auto-picks, add **`--recordings-dir`** to the `exec` line with the path Automator should use (quote the path if it contains spaces).

---

## Troubleshooting

| Symptom                                                                | Things to check                                                                                                                                                                                                                                                        |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Operation not permitted**                                            | Full Disk Access for the **workflow runner**, not only Terminal; restart that app.                                                                                                                                                                                     |
| **ffprobe failed / ffmpeg not found**                                  | `export PATH=...` includes `/opt/homebrew/bin` and `/usr/local/bin`; confirm `which ffprobe` in the **same** shell environment if you test manually.                                                                                                                   |
| **Voice Memos is running**                                             | Quit Voice Memos or add **Quit Application** before the shell step.                                                                                                                                                                                                    |
| **Import succeeds but no memo in app**                                 | Run `--list-libraries` and align **`--recordings-dir`** with the library that matches your memo count in the UI.                                                                                                                                                       |
| **Spaces in script path**                                              | Use `"$HOME/bin/import_voice_memo.py"` or move the script to `~/bin`.                                                                                                                                                                                                  |
| **ffmpeg … Error opening output** in `…/Group Containers/…/Recordings` | The script re-encodes to **$TMPDIR** first, then moves the file with Python so **ffmpeg** never writes directly into the Group Container. Update to the latest `import_voice_memo.py`. If you still see this on an old copy, drop `--re-encode` for already-M4A files. |

---

## Related script options

| Flag                    | Use in Quick Action when…                                      |
| ----------------------- | -------------------------------------------------------------- |
| `--re-encode`           | Source may not be AAC M4A or memos do not play.                |
| `--label-from-filename` | You want the memo title to match the file name.                |
| `--use-mtime`           | You want the memo date to follow the file’s modification time. |
| `--dry-run`             | Debugging only; no file or DB changes.                         |

Full list: `python3 import_voice_memo.py --help`

---

## Disclaimer

This script modifies Apple’s **Voice Memos** SQLite database. A **timestamped backup** of `CloudRecordings.db` is created before each import batch. iCloud sync may still reconcile or alter data; use at your own risk and keep backups.
