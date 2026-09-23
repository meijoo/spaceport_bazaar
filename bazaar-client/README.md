# Build a Bazaar trading client

Practice with two planets: your client controls **P01** and the server controls
**P02**. You will advertise resources, exchange water for food, accept a gift,
and handle an intentional error.

The folder contains two Linux server binaries, the message definitions in
[bazaar.proto](bazaar.proto), and this guide. Running the server requires no
Rust installation or game repository.

## Recommended: develop in a container

We recommend a **Linux Docker container or devcontainer** for developing your
client. A [Docker container](https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/)
keeps your project's tools and dependencies together. A
[devcontainer](https://code.visualstudio.com/docs/devcontainers/containers)
lets an editor such as VS Code work directly in that environment. This gives
your team a consistent setup, including on Windows and macOS.

Run **both your client and the practice server inside the same container**, in
separate terminals. They can then connect using `ws://127.0.0.1:3001/ws`; the
practice server only listens for local connections. Run `uname -m` inside that
environment to select the matching binary, and choose a Linux image that meets
the runtime requirements below.

**Platforms:** Linux ARM64 and Linux x86-64. Run `uname -m` in the
environment where you will run the server, then choose its matching
executable. On Windows or macOS, use a Linux Docker container or
devcontainer; WSL is another option on Windows.

| CPU | `uname -m` | Executable | Runtime requirements |
| --- | --- | --- | --- |
| ARM64 | `aarch64` / `arm64` | `spaceport-validate-linux-arm64` | Linux with glibc 2.34 or newer and libgcc_s.so.1. No Rust installation is needed to run the server. |
| x86-64 (Intel/AMD 64-bit) | `x86_64` / `amd64` | `spaceport-validate-linux-x86_64` | Linux with glibc 2.34 or newer and libgcc_s.so.1. No Rust installation is needed to run the server. |

## Start the practice server

Run the matching executable in a directory where it can create files:

**Linux ARM64:**

```sh
./spaceport-validate-linux-arm64 --codec protobuf
```

**Linux x86-64 (Intel/AMD 64-bit):**

```sh
./spaceport-validate-linux-x86_64 --codec protobuf
```

It listens at `ws://127.0.0.1:3001/ws` and creates:

| File | What you use it for |
| --- | --- |
| `validation-credentials.json` | Find P01's access token so your client can connect. |
| `validation-report.json` | See the server's record of your progress and any unexpected command. |

The terminal prints the next action. Stop with Ctrl+C. Restarting creates a new
exercise and credentials. Connections must come from your own computer.
To change the port or output filenames:

```sh
./spaceport-validate-linux-x86_64 --codec protobuf --addr 127.0.0.1:3002 \
  --credential-file ./credentials.json --report ./report.json
```

## Connect your client

In `validation-credentials.json`, find the entry in `players` whose
`station_id` is `P01`. Use that entry's `token` in these WebSocket connection
headers:

```text
Authorization: Bearer <P01 token>
Sec-WebSocket-Protocol: bazaar.protobuf.v2
```

The second header selects the message format, or *subprotocol*. Request this
one format and check that the server confirms it. Your WebSocket library must
support custom headers; a browser's built-in API cannot set `Authorization`.
Only P01 can connect to this exercise.

Send one `bazaar.v2.ClientMessage` as **binary Protobuf bytes** in each WebSocket
message. Decode incoming messages as `bazaar.v2.ServerMessage`. Do not wrap the
bytes in JSON, Base64, or an extra length prefix. WebSocket ping, pong, and close
messages are connection controls, not Protobuf messages.

Keep reading even when you have nothing to send: the server pushes updates
automatically, without polling. Read the first `state`, then confirm readiness
as shown in step 1 before sending trading commands.

## Understand the messages

| Server message | Meaning |
| --- | --- |
| `state` | Your current inventory, offers, completed trades, and public advertisements. |
| `result` | The outcome of your command. Match `request_id` to the command and check `ok` and `code`. |
| `protocol_error` | The server could not process your message. Read `code` and follow `close_session`. |
| `readiness` | Confirms your readiness declaration. Wait for `ready: true` before sending trading commands. |

A `state` is also called a **snapshot**: a complete picture at one moment.
Replace your previous view with it. Its inventory already includes completed
trades, so do not add or subtract their amounts again.

| Field or term | Meaning |
| --- | --- |
| `run_id` | Identifies this exercise. Copy it from the first state into every message. Restarting creates a new run. |
| `request_id` | Your label for one command. Use a new ID for a new command; reuse an ID only to retry that exact command. |
| Object ID | The server's name for an advertisement, offer, or transaction. Read it from a result or state; do not invent it. |
| `snapshot_sequence` | Counts state messages on this connection: 1, 2, 3, and so on. It increases even for another copy of unchanged state and starts over at 1 on a new connection. |
| `world_version` | Counts revisions to the shared simulation. Ticks and newly processed commands can advance it, including some rejections. Sync and exact retries do not. |
| Tick | One simulation time step, when production, resource consumption (**upkeep**), and expiration happen. Trades can settle between ticks. This exercise stays at tick 0. |
| `expires_tick` | The tick at which an offer or advertisement stops being usable, not a number of seconds. |

An **advertisement** says what a planet claims to sell or seek; it does not prove
stock or move resources. An **offer** proposes an exact exchange. Acceptance
completes a **transaction**, moving both sides together.

Offer amounts use the proposer's perspective: `give` is what it pays; `receive`
is what it asks for. P02 offering a component with zero `receive` is a gift to
you, which you must still accept.

A resource **bundle** has named `water`, `food`, and `components` quantities,
including zeros. The guide abbreviates these as `(water, food, components)`.
Your `self.specialty` identifies your production resource; peer stock and
production specialties are private.

## Complete the exchange

Follow the steps in order. Each separates **Send**, **Receive**, and **Check**.
Do not send the next command until you have handled all responses listed for
the current step. Steps 5 and 6 require only reading messages.

The code blocks use readable Protobuf text notation; send the corresponding
**binary messages, not this text**. Replace `<RUN_ID>` with your run ID and the
other placeholders as directed. Use `protocol_version: "2.0"` and the sample
request IDs. Valid IDs contain 1–64 letters, digits, underscores, or hyphens.

The counter values below assume one connection with no extra syncs or retries.
Every state in this exercise has `tick: 0` and `phase: PHASE_RUNNING`.

### 1. Read your starting state and confirm readiness

**Send:** Nothing after connecting.

**Receive:** One `state` with `world_version: 2` and `snapshot_sequence: 1`.

**Check:** Your station is `P01`, inventory is `(30,30,30)`, and
`self.specialty` is `RESOURCE_WATER`. P02 has an advertisement selling food and
seeking water. Save `run_id` for every command below.


**Send:** Declare that you have read the state and are ready to continue. Use
its run ID and `snapshot_sequence` (1 on this new connection).

```textproto-ready
ready {
  type: READY_TYPE_READY
  protocol_version: "2.0" run_id: "<RUN_ID>"
  ready: true snapshot_sequence: 1
}
```

**Receive:** One `readiness` message.

**Check:** Its `run_id`, `ready: true`, and `snapshot_sequence: 1` match your
message. You can now send step 2. This exchange does not change inventory,
world version, state-message sequence, command limits, or exercise step.

Readiness is required on **every connection**, including reconnects. Sending
`ready: false` reports that you are not ready and keeps trading commands blocked;
it is not a required first message. You can send `ready: true` directly after
reading state. `sync` remains available while not ready. Trading commands sent
before readiness receive `CONTROL_CODE_BAD_MESSAGE` with `close_session: false`;
confirm readiness, then resend the command. In a classroom game, the instructor
still controls when the run starts.

### 2. Advertise water for food

**Send:**

```textproto
advertise {
  type: ADVERTISE_TYPE_ADVERTISE
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-advertise-1"
  body { selling { items: RESOURCE_WATER } seeking { items: RESOURCE_FOOD } expires_tick: 6 }
}
```

**Receive:** A successful `result`, followed by a `state` with
`world_version: 3` and `snapshot_sequence: 2`.

**Check:** The result has your `request_id`, `ok: true`, and
`code: RESULT_CODE_OK`. Your advertisement appears in `advertisements.items`.
Your inventory is still `(30,30,30)` because advertising does not trade anything.
All successful command results below should pass the same request-ID and
success checks.

### 3. Replace your advertisement with a request for components

**Send:**

```textproto
advertise {
  type: ADVERTISE_TYPE_ADVERTISE
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-advertise-seeking-1"
  body { selling {} seeking { items: RESOURCE_COMPONENTS } expires_tick: 6 }
}
```

**Receive:** A successful `result`, then a `state` with `world_version: 4`
and `snapshot_sequence: 3`.

**Check:** Your new advertisement sells nothing and seeks components. It
replaces your first advertisement; each planet has at most one active listing.
Save `result.object_id.value` as **ADVERTISEMENT_ID** for step 8.
Inventory remains `(30,30,30)`.

### 4. Offer two water for one food

**Send:**

```textproto
offer {
  type: OFFER_COMMAND_TYPE_OFFER
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-offer-1"
  body {
    recipient_id: "P02"
    give { water: 2 food: 0 components: 0 }
    receive { water: 0 food: 1 components: 0 }
    expires_tick: 6
  }
}
```

**Receive:** A successful `result`, then a `state` with `world_version: 5`
and `snapshot_sequence: 4`.

**Check:** Your offer to P02 appears in `offers.items` with
`status: OFFER_STATUS_OPEN`. Inventory is still `(30,30,30)`: proposing a trade
does not transfer or reserve resources. Save the offer's ID to track it.
**Keep reading:** the server sends the two updates in steps 5 and 6 next.

### 5. Observe P02 accept your offer

**Send:** Nothing. P02 accepts automatically.

**Receive:** One `state` with `world_version: 6` and `snapshot_sequence: 5`.

**Check:** Your offer is now `OFFER_STATUS_ACCEPTED`. You have one transaction
and inventory `(28,31,30)`: you paid two water and received one food. You do not
receive P02's command result; you learn what happened from the new state.

### 6. Observe P02 offer you a gift

**Send:** Nothing. P02 creates this offer automatically.

**Receive:** One `state` with `world_version: 7` and `snapshot_sequence: 6`.

**Check:** Find the open offer from P02 to P01. Its `give` bundle contains one
component, and its `receive` bundle is all zeros. Save its `offer_id` as
**ZERO_PRICE_OFFER_ID**. Inventory remains `(28,31,30)` until you accept.

Together, steps 4–6 deliver **one result followed by three state messages**
without another command from your client.

### 7. Accept the gift

**Send:**

```textproto
accept {
  type: ACCEPT_TYPE_ACCEPT
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-accept-1"
  body { offer_id: "<ZERO_PRICE_OFFER_ID>" }
}
```

**Receive:** A successful `result`, then a `state` with `world_version: 8`
and `snapshot_sequence: 7`.

**Check:** The result identifies the accepted offer and its transaction. You
now have two transactions and inventory `(28,31,31)`. Your advertisement seeking
components is still active; receiving a gift does not remove it.

### 8. Remove your advertisement

**Send:**

```textproto
withdraw {
  type: WITHDRAW_TYPE_WITHDRAW
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-withdraw-1"
  body { object_id: "<ADVERTISEMENT_ID>" }
}
```

**Receive:** A successful `result`, then a `state` with `world_version: 9`
and `snapshot_sequence: 8`.

**Check:** Your advertisement is absent from `advertisements.items`. Both
completed trades remain in your history. Inventory stays `(28,31,31)`.

### 9. Observe an intentional request-limit error

The server keeps each command's result so retries can recover it. This exercise
allows only **five stored command results**, and you have used all five. The
next command deliberately exceeds that limit.

**Send:** This is a new command, so it has a new request ID.

```textproto
advertise {
  type: ADVERTISE_TYPE_ADVERTISE
  protocol_version: "2.0" run_id: "<RUN_ID>" request_id: "student-advertise-2"
  body { selling { items: RESOURCE_WATER } seeking { items: RESOURCE_FOOD } expires_tick: 6 }
}
```

**Receive:** Only a `protocol_error` with
`code: CONTROL_CODE_REQUEST_CAPACITY_EXCEEDED` and `close_session: false`.
There is no result or automatic state for this command.

**Check:** The error's `request_id.value` matches `student-advertise-2`.
Keep the connection open. The rejected request has not created an advertisement
or used another result-storage slot.

### 10. Ask for the final state

**Send:** `sync` asks for a fresh state. It has no body or request ID.

```textproto
sync { type: SYNC_TYPE_SYNC protocol_version: "2.0" run_id: "<RUN_ID>" }
```

**Receive:** Only a `state` with `world_version: 9` and
`snapshot_sequence: 9`. Notice that the state-message counter advanced while
the world revision did not.

**Check:** Inventory is `(28,31,31)`, there are two transactions, and
`request_results.items` contains the five stored command results. You imported
one food and one component (`imported_total: (0,1,1)`) and exported two water
(`exported_total: (2,0,0)`). Production, consumption, and shortage counters are
zero because no simulation tick occurred.

With one readiness declaration, you have sent **8 messages** and received
**16 messages**. The report should show `status: "sample exchange completed"`,
`last_completed_step: 10`, and `final_inventory: { "water": 28, "food": 31, "components": 31 }`.
Also check your client's state and logs: the server report cannot verify that
your client decoded or displayed its responses correctly.

## If you need to retry or reconnect

- **Sync:** Requests current state without using a stored-command slot or
  advancing the exercise before its final step. Extra states change later
  `snapshot_sequence` values from those in this guide.
- **Retry a recorded command:** Send the same command and original request ID.
  You receive its stored result and current state without repeating the action.
  Changing the command while reusing its ID gives `RESULT_CODE_REQUEST_ID_CONFLICT`.
  Step 9's request-limit error has no stored result: continue to `sync` instead
  of retrying that command.
- **Reconnect:** The same running server retains progress and sends current
  state with sequence 1. The new connection replaces the old one; ignore any
  late messages from the old connection. Repeat step 1’s readiness exchange with
  this connection’s state before sending commands or retries.
- **Restart:** Creates a new exercise. Read its new credentials and state;
  discard old pending commands and object IDs.

A new command sent out of order ends this scripted exercise with a
`scenario mismatch` report. Restart to try again. Extra syncs and exact retries
are allowed.

## Message-format checks and connection errors

Follow the required fields in `bazaar.proto`, including zeros, `false`, and each
nested `type`. Select one command or response per outer message. Other details
that matter when constructing messages:

- Lists have an `items` field inside a required container. `selling {}` is an
  empty list; omitting `selling` is invalid.
- Optional values also need a container: `object_id { value: "..." }` means an
  ID exists; `object_id { null: true }` means none exists. Include exactly one.
- Unknown fields or enum values, duplicate non-list fields, and `null: false`
  are rejected. Keep commands within 16,384 bytes and preserve full 64-bit integers.

Bad credentials cause HTTP 401; wrong station or message-format selection causes
HTTP 400. Malformed messages produce `CONTROL_CODE_BAD_MESSAGE`: fix the payload
and repeat the step. Wrong protocol strings or run IDs close the connection.
For every `protocol_error`, follow `close_session`.

<details>
<summary>Package details for reporting problems</summary>

Server source: spaceport 0.1.0, based on commit `5e937f0`. Message format: `bazaar.protobuf.v2`.

Schema checksum (SHA-256 fingerprint of `bazaar.proto`): `3e5d7631db9577c84c7035d384e75d412d255d3bdcfbb37d2247ca568786f351`.

</details>
