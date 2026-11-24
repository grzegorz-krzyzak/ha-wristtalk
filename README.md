# WristTalk for Home Assistant

[![Tests](https://github.com/grzegorz-krzyzak/ha-wristtalk/actions/workflows/tests.yml/badge.svg)](https://github.com/grzegorz-krzyzak/ha-wristtalk/actions/workflows/tests.yml)

Voice assistant for Home Assistant on a Garmin watch: open the WristTalk app, say a command, see it
and the reply, hear the reply from the watch speaker. This integration is the Home Assistant side:
it pairs the watches and runs their voice through an Assist pipeline you choose.

**Status: tested in the Connect IQ simulator and on a fēnix 8 Solar 51 mm against a real Home Assistant.**
The version and what changed: [releases](https://github.com/grzegorz-krzyzak/ha-wristtalk/releases).
The watch app: [WristTalk in the Connect IQ store](https://apps.garmin.com/apps/515ad521-de52-4de0-b7f5-00ec036d4914), source in
[garmin-wristtalk](https://github.com/grzegorz-krzyzak/garmin-wristtalk).

**The watch app and the integration speak a numbered protocol.** The middle number of the version
is that protocol (the integration 1.7.x and the watch app 1.7.x speak protocol 7). A
newer integration still serves older watch apps (protocol 2 and up); a watch app newer than the
integration says "Update the integration in Home Assistant" - update the integration first.

**A personal, non-commercial hobby project by a private individual.** Not affiliated with or endorsed by
the Open Home Foundation, Home Assistant or Garmin.

## Requirements

- Home Assistant 2026.10 or newer with an Assist pipeline.
- An **HTTPS** address with a trusted certificate (for example Home Assistant Cloud). The watch
  reaches Home Assistant through the phone, and iOS refuses plain `http://` - also at home.
- A Garmin watch with Connect IQ API 6.0.3 (microphone and speaker for apps), e.g. fēnix 8.

## Installation

Home Assistant 2026.11 or newer (HACS is built in as the **Marketplace**): Settings -> Marketplace ->
**Add from link**, enter `https://github.com/grzegorz-krzyzak/ha-wristtalk`, install WristTalk and restart
Home Assistant.

Home Assistant 2026.10 with [HACS](https://hacs.xyz): HACS -> the three dots -> **Custom repositories**, add
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

## Removal

Settings -> Devices & services -> WristTalk -> the three dots -> **Delete**. The keys of all watches
are revoked at once, their devices and entities go, and so does the system user **WristTalk** the
integration creates: the watches' commands run as that user, so the logbook shows where a command
came from. Then remove the
integration's files (the Marketplace, HACS, or `config/custom_components/wristtalk` by hand) and
restart Home Assistant. The watch app on the watch only says it needs pairing.

The integration fires the event `wristtalk_command_run` when a watch's command starts a pipeline
run (the command's session, the watch's satellite entity and the run's start) - nothing is stored
for it.

## Replies on the watch

WristTalk adds nothing to what your agent is told - like Home Assistant Voice PE, which sends no
extra instruction with a normal command. The reply style is your agent's: if you want shorter
replies on the watch, put it in the agent's prompt or pick a separate pipeline for the watch.

Each watch's device card has its settings: the **Assistant** (pipeline), **Finished speaking
detection** and **Reply voice quality** - *Fast* (smaller, a little rougher), *Balanced* (the
default) or *Best* (the largest).

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

## Contact & support

- **Something not working?** [Report it here](https://github.com/grzegorz-krzyzak/ha-wristtalk/issues) - one place for the integration and the watch app. Do not paste your house address.
- **No GitHub account, a question or an idea?** Write to [wristtalk.app@gmail.com](mailto:wristtalk.app@gmail.com).
- **WristTalk is free.** If it is useful to you, you can support it on [GitHub Sponsors](https://github.com/sponsors/grzegorz-krzyzak).

## Licence

Apache-2.0 - see [LICENSE](LICENSE). The name "WristTalk" and the icon are not covered by the
licence.
