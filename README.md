# WristTalk for Home Assistant

[![Tests](https://github.com/grzegorz-krzyzak/ha-wristtalk/actions/workflows/tests.yml/badge.svg)](https://github.com/grzegorz-krzyzak/ha-wristtalk/actions/workflows/tests.yml)

Voice assistant for Home Assistant on a Garmin watch: open the WristTalk app, say a command, see it
and the reply, hear the reply from the watch speaker. This integration is the Home Assistant side:
it pairs the watches and runs their voice through an Assist pipeline you choose.

**Status: 0.4.0, tested in the Connect IQ simulator and on a fēnix 8 Solar 51 mm against a real Home Assistant.**
The watch app: [garmin-wristtalk](https://github.com/grzegorz-krzyzak/garmin-wristtalk) - not in
the Connect IQ store yet.

**A personal, non-commercial hobby project by a private individual.** Not affiliated with or endorsed by
the Open Home Foundation, Home Assistant or Garmin.

## Requirements

- Home Assistant 2026.10 or newer with an Assist pipeline.
- An **HTTPS** address with a trusted certificate (for example Home Assistant Cloud). The watch
  reaches Home Assistant through the phone, and iOS refuses plain `http://` - also at home.
- A Garmin watch with Connect IQ API 6.0.3 (microphone and speaker for apps), e.g. fēnix 8.

## Installation

With [HACS](https://hacs.xyz): HACS -> the three dots -> **Custom repositories**, add
`https://github.com/grzegorz-krzyzak/ha-wristtalk` as an **Integration**, then install WristTalk and
restart Home Assistant.

By hand: copy `custom_components/wristtalk` into your `config/custom_components/` and restart Home
Assistant.

## Setup

1. Add the integration: pick the default pipeline for new watches and the address for the watches.
2. In the integration card, **Add watch**. Pairing stays open for 3 minutes.
3. On the phone, in the Connect IQ app, set the house address in the WristTalk app's settings, then open
   WristTalk on the watch.
4. Type the code the watch shows into **Add watch**. Only the watch whose code you type is added.

Each watch becomes its own device with its own key and a **Status** sensor (Paired / Needs pairing again). To pair a watch again, use **Pair again** (the ⚙ next to the watch on the integration page), or **Fix** in Repairs when the watch asked for it - the device and its settings stay. Deleting the watch in Home Assistant revokes its
key at once. It blocks the watch's further commands; it does not undo an action its current
command has already started.

**The watch can do everything you exposed to Assist** - also over the internet, through your phone.
Turn on the watch's passcode (the watch's settings, System, Passcode) so a lost watch is not an open
door, and delete a lost watch in Home Assistant.

## Replies on the watch

WristTalk adds nothing to what your agent is told - like Home Assistant Voice PE, which sends no
extra instruction with a normal command. The reply style is your agent's: if you want shorter
replies on the watch, put it in the agent's prompt or pick a separate pipeline for the watch.

Each watch's device card has its settings: the **Assistant** (pipeline), **Finished speaking
detection** and **Reply voice quality** - *Fast* (the default: smaller, arrives sooner through
the phone), *Balanced* or *Best* (larger; worth it with headphones).

Home Assistant's `extra_system_prompt` mechanism could carry a watch-specific instruction (e.g.
"answer shorter"), but WristTalk does not use it - we do not know how it would work with your agent
or how it would affect it.

## Development

Tests run against the Home Assistant version they are pinned to:

```sh
uv venv -p 3.14 .venv
uv pip install -p .venv/bin/python pytest-homeassistant-custom-component==0.13.375  # HA 2026.10.4
uv pip install -p .venv/bin/python hassil home-assistant-intents gazetteer-matcher mutagen ha-ffmpeg
.venv/bin/python -m pytest -q --cov=custom_components.wristtalk
uv pip install -p .venv/bin/python ruff  # lint and format, checked in CI as well
.venv/bin/ruff check custom_components tests && .venv/bin/ruff format --check custom_components tests
```

Assist's audio libraries (`pymicro-vad`, `pyspeex-noise`) have no wheels for Python 3.14 on macOS;
the tests stand in for them when they are missing. GitHub Actions runs the tests on Linux with the
real ones (`.github/workflows/tests.yml`).

## Licence

Apache-2.0 - see [LICENSE](LICENSE). The name "WristTalk" and the icon are not covered by the
licence.
