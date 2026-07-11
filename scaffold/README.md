# __TITLE__

This is a Chalk video project. Creative source is ordinary Markdown and Python;
`./chalk` handles recording, timing, builds, review memory, and exact snapshots.

Start with `brief.md` and `outline.md`, then edit the cohesive `script.md` while
visual work proceeds independently in `scenes/`.

```bash
./setup.sh
./chalk                 # state and suggested next action
./chalk check
./chalk watch intro     # render changed proxy and play it
./chalk record intro    # immutable take -> transcript -> immediate A/V review
./chalk open            # video-centered local project room
./chalk review --full
./chalk release v1
```

See `AGENTS.md` for the collaboration contract and run `./chalk --help` for the
less common context, note, take, snapshot, trace, and doctor commands.
