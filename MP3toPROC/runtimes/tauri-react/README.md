# MP3toPROC web runtime (Tauri / React / three.js)

A TypeScript port of `mp3toproc/director.py` for Web Audio. It has been checked for timing parity against the Python reference.

Full integration guide: [`../../mp3toproc-tauri-react-PLAYBOOK.md`](../../mp3toproc-tauri-react-PLAYBOOK.md)

```bash
# parity check against a Python golden timeline
python -m mp3toproc preview proc/soundtrack.json examples/events.example.json -o p.wav --events-json golden.json
npx tsx runtimes/tauri-react/src/music/parity.test.ts proc/ golden.json
```
