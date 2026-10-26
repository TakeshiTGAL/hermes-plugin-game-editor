# Verdict lines

Godot prints these lines from `studio` op `test`. The same lines are what a Unity editor can print. This package does not ship that Unity runner. `csharp-game-editor` can use this file as the shared shape.

Parts, in this order: `fell`, `sound`, `picked`, `door`, `timeout_lose`, `restart`.

A sentence that matches none of them is not a test. The reply is:

```
This sentence cannot be a test.
```

The check scores only the parts the sentence matched. The model does not write the check. Each matched part is `name=true` or `name=false`, joined by one space, in the order above.

- Pass: `STUDIO ` plus those parts, and process exit 0.
- Fail: `STUDIO_FAIL ` plus those parts, and a process exit other than 0.

`fell=true` means the body has settled on the floor. `sound=true` means a player was observed playing, including a generated beep. `picked=true` means the pickup happened. `door=true` means the door is open and its collision shape is disabled. `timeout_lose=true` means the timer ended the round. `restart=true` means the round started over.

The sentence `Pick up the key and a sound plays and the door opens. If you do not get out in 60 seconds, start over.` matches `sound`, `picked`, `door`, `timeout_lose` and `restart`. It does not match `fell`.

A test that has failed is not a pass until a later run of that same test exits 0. The Godot process appends that row. The row is not a project file.

`docs/verdict.json` is the same contract in one object.
