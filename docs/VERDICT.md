# Verdict lines

Godot prints these lines from `studio` op `test`. The same lines are what a Unity editor can print. This package does not ship that Unity runner. `csharp-game-editor` can use this file as the shared shape.

Parts, in this order: `fell`, `sound`, `picked`, `door`, `timeout_lose`, `restart`.

A sentence that matches none of them is not a test. The reply is:

```
This sentence cannot be a test.
```

The check scores only the parts the sentence matched. The model does not write the check. Each matched part is `name=true` or `name=false`, joined by one space, in the order above.

- Pass: `STUDIO ` plus those parts, and process exit 0, and that line was printed by this package's check after it watched the loop.
- Fail: `STUDIO_FAIL ` plus those parts. A process exit other than 0 is a fail. A process exit 0 with no check observation line is also a fail, and the ledger records exit 1.

A `STUDIO` line printed by the loop is not the pass. A loop that prints `STUDIO` and quits 0 is a fail.

`fell=true` means the body has settled on the floor. `sound=true` means a player was observed playing, including a generated beep. `picked=true` means the pickup happened. `door=true` means the door is open and its collision shape is disabled. `timeout_lose=true` means the timer ended the round. `restart=true` means the round started over.

The sentence `The key is picked and a sound plays and the door opens. After 60, timeout_lose, then restart.` matches `sound`, `picked`, `door`, `timeout_lose` and `restart`. It does not match `fell`. The part name has to be that whole word. `paint the floor`, a count of seconds, `collect` and `try again` match nothing.

A test that has failed is not a pass until a later run of that same test exits 0. After Godot exits, the bridge appends that row. Exit 0 is recorded only when that process returned 0 and the check's observation line is a pass. Any other result is recorded as exit 1. The Godot process does not write the file and is not given a ledger key. A row the bridge did not append after that wait is not a pass. The row is not a project file.

`docs/verdict.json` is the same contract in one object.
