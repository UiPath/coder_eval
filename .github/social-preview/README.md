# Social preview image

`social-preview.png` (1280×640) is the repository's Open Graph card. It is what
Hacker News, X, Reddit, Slack, and LinkedIn show when someone shares the repo link.

`social-preview.html` is the source. The logo is inlined from
`.github/pages-stub/index.html`, so the file is self-contained — no external
stylesheets, fonts, or images.

## Regenerate

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1280,640 \
  --screenshot=out@2x.png \
  "file://$PWD/.github/social-preview/social-preview.html"

# downsample the 2× capture to the 1280×640 GitHub expects
python3 -c "from PIL import Image; \
  Image.open('out@2x.png').resize((1280,640), Image.LANCZOS) \
       .save('.github/social-preview/social-preview.png', optimize=True)"
```

## Publish

GitHub has **no API** for the social preview image. Upload it by hand:

**Settings → General → Social preview → Upload an image**
<https://github.com/UiPath/coder_eval/settings>

## What the card says

| Slot | Copy |
| --- | --- |
| Repo | github.com/**UiPath/coder_eval** |
| Headline | Playwright for coding agents. |
| Positioning | Test that your **skills**, **MCP servers**, and **CLIs** actually work when an agent uses them. |
| Where it runs | Run it from the **command line**, in a **sandbox**, as an **A/B experiment**, or as a **CI gate**. |
| Capabilities | YAML suites · Activation checks · Weighted scoring · Cost telemetry |
| Footer | Apache-2.0 · Python 3.13+ · Claude Code · Codex · Gemini · OpenCode · Pi |

The terminal panel deliberately shows `runs/latest`, not a dated run directory —
a timestamp in the image tells every future reader when the card was made.

The positioning and capability lines mirror the repo's GitHub description. Change
one and change the other, or a shared link and the repo page will say two different
things.

## Keep it honest

The card names every supported harness. When a new `agent.type` ships, this image
is a surface that has to change with it — the same rule CE047 enforces on the
README, `docs/index.md`, `docs/comparison.md`, `docs/llms.txt`, `mkdocs.yml`, the
Pages stub, and `pyproject.toml`. It is not lint-enforced, because it is a PNG.
